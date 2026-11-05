#!/usr/bin/env python3
"""
HNPicks Agent —— 从 Hacker News 历年评论中，筛出被「反复、长期、多人」推荐的 YouTube 视频和 X 帖子。

流程（每轮循环）:
  1. crawl    : 通过 HN Algolia 公开 API 增量抓取提到 youtube / x.com / twitter 的评论和帖子
  2. extract  : 从评论正文里抽取视频 ID / 推文 ID，记录「谁、何时」推荐了它
  3. select   : 筛选 —— 至少 MIN_AUTHORS 个不同的人、在 MIN_MONTHS 个不同的月份推荐过
  4. enrich   : 配了 YOUTUBE_API_KEY 用 YouTube Data API 补标题/频道/时长/播放量/上传时间；否则用 oEmbed（只有标题和频道）
  5. classify : 若配置了 DEEPSEEK_API_KEY，用 DeepSeek 分类 + 写中文标题和一句话推荐理由；否则用关键词规则
  6. build    : 生成静态网站 site/（index.html + data.json + status.json）

命令:
  python hnpicks.py loop      # 常驻运行（Docker 里默认就是这个）
  python hnpicks.py once      # 只跑一轮
  python hnpicks.py build     # 只重新生成网站
  python hnpicks.py stats     # 打印数据库统计
  python hnpicks.py diag      # 检查各个外部接口是否连得通
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------------- 配置
ENV = os.environ.get
DATA_DIR = Path(ENV("HNPICKS_DATA", "/data"))
SITE_DIR = Path(ENV("HNPICKS_SITE", "/site"))
TEMPLATE_DIR = Path(__file__).resolve().parent / "web"
DB_PATH = DATA_DIR / "hnpicks.db"

MIN_AUTHORS = int(ENV("MIN_AUTHORS", "3"))          # 至少几个不同的人
MIN_MONTHS = int(ENV("MIN_MONTHS", "3"))            # 至少几个不同的月份
INTERVAL_HOURS = float(ENV("INTERVAL_HOURS", "6"))  # 追平历史后，每隔多久跑一轮
BACKFILL_FROM = ENV("BACKFILL_FROM", "2007-01-01")  # 回溯起点
REQ_PER_CYCLE = int(ENV("REQ_PER_CYCLE", "600"))    # 回溯阶段每轮最多请求 HN 多少次
ENRICH_PER_CYCLE = int(ENV("ENRICH_PER_CYCLE", "3000"))  # 有 YouTube API key 时每轮最多补多少条
OEMBED_PER_CYCLE = int(ENV("OEMBED_PER_CYCLE", "600"))   # 没有 key、走 oEmbed 时每轮最多补多少条
CLASSIFY_PER_CYCLE = int(ENV("CLASSIFY_PER_CYCLE", "300"))
DEEPSEEK_MODEL = ENV("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = ENV("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
SITE_TITLE = ENV("SITE_TITLE", "HNPicks · HN 反复安利的视频与帖子")
HN_API = ENV("HN_API", "https://hn.algolia.com/api/v1/search_by_date")
UA = "Mozilla/5.0 (compatible; HNPicks/0.2; +hobby-project)"

# 每个搜索词 × 类型(comment/story) 是一条独立的抓取游标
QUERIES = [
    ("youtube", "comment"), ("youtu.be", "comment"),
    ("twitter.com", "comment"), ("x.com", "comment"),
    ("youtube", "story"), ("youtu.be", "story"),
    ("twitter.com", "story"), ("x.com", "story"),
]

CATEGORIES = ["AI与机器学习", "编程与软件工程", "科学与数学", "技术演讲与访谈", "创业与商业",
              "历史与纪录片", "工程与硬件", "设计与艺术", "音乐", "社会与思想", "其他"]

log = logging.getLogger("hnpicks")

# ----------------------------------------------------------------------------- 工具函数
def now_ts() -> int:
    return int(time.time())


def http_json(url: str, params: dict | None = None, timeout: int = 30, retries: int = 4):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404, 410):
                raise
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            err = e
        if attempt < retries:
            log.warning("请求失败 %s (%s)，%.0fs 后重试", url[:120], err, delay)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"请求多次失败: {url[:200]}: {err}")


TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    s = re.sub(r"<p>", "\n", s or "")
    return html.unescape(TAG_RE.sub("", s)).strip()


# ----------------------------------------------------------------------------- 链接识别
YT_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?(?:[^\s\"'<>]*?&)?v=|embed/|shorts/|live/|v/)"
    r"|youtube-nocookie\.com/embed/|youtu\.be/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])"
)
X_RE = re.compile(
    r"(?:https?://)?(?:www\.|mobile\.)?(?:twitter\.com|x\.com|nitter\.net|xcancel\.com|fxtwitter\.com|vxtwitter\.com|fixupx\.com)"
    r"/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d{5,25})"
)
X_RESERVED = {"i", "intent", "share", "home", "search", "hashtag", "web"}


def extract_keys(text: str) -> dict[str, dict]:
    """返回 {key: {"kind","ext_id","handle"}}；同一段文本里同一个链接只算一次。"""
    out: dict[str, dict] = {}
    if not text:
        return out
    t = html.unescape(text)  # HN 会把 / 转义为 &#x2F;
    for m in YT_RE.finditer(t):
        vid = m.group(1)
        out[f"yt:{vid}"] = {"kind": "youtube", "ext_id": vid, "handle": None}
    for m in X_RE.finditer(t):
        handle, sid = m.group(1), m.group(2)
        if handle.lower() in X_RESERVED:
            handle = None
        out.setdefault(f"x:{sid}", {"kind": "x", "ext_id": sid, "handle": handle})
    return out


def canonical_url(kind: str, ext_id: str, handle: str | None) -> str:
    if kind == "youtube":
        return f"https://www.youtube.com/watch?v={ext_id}"
    return f"https://x.com/{handle or 'i'}/status/{ext_id}"


# ----------------------------------------------------------------------------- 数据库
SCHEMA = """
CREATE TABLE IF NOT EXISTS mentions (
  hn_id       TEXT NOT NULL,
  item_key    TEXT NOT NULL,
  author      TEXT,
  created_at  INTEGER NOT NULL,
  hn_type     TEXT,              -- comment / story
  story_id    TEXT,
  story_title TEXT,
  points      INTEGER,
  snippet     TEXT,
  PRIMARY KEY (hn_id, item_key)
);
CREATE INDEX IF NOT EXISTS idx_mentions_key ON mentions(item_key);
CREATE TABLE IF NOT EXISTS items (
  item_key   TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,
  ext_id     TEXT NOT NULL,
  handle     TEXT,
  title      TEXT,
  channel    TEXT,
  thumb      TEXT,
  tweet_text TEXT,
  enrich_status TEXT,           -- ok / noembed / dead / err
  enriched_at INTEGER,
  category   TEXT,
  title_zh   TEXT,
  reason_zh  TEXT,
  classified_by TEXT,           -- llm / rules
  classified_at INTEGER
);
CREATE TABLE IF NOT EXISTS crawl_state (
  query TEXT NOT NULL, tag TEXT NOT NULL,
  cursor_ts INTEGER NOT NULL,
  window_s INTEGER NOT NULL,
  PRIMARY KEY (query, tag)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at INTEGER, finished_at INTEGER,
  requests INTEGER, new_mentions INTEGER, picks INTEGER, note TEXT
);
"""


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    for col, typ in [("published_at", "INTEGER"), ("duration_s", "INTEGER"), ("views", "INTEGER"),
                     ("topics", "TEXT"), ("descr", "TEXT"), ("enrich_src", "TEXT")]:
        if col not in cols:  # 旧数据库自动升级
            con.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
            if col == "enrich_src":
                # 旧版把「禁止嵌入」(401) 也当成了已删除，这里清掉旧结果重新检查
                con.execute("UPDATE items SET enrich_status=NULL WHERE enrich_status IN ('dead','err')")
                con.execute("UPDATE items SET enrich_src='oembed' WHERE enrich_status='ok'")
    con.commit()
    return con


# ----------------------------------------------------------------------------- 1+2. 抓取 & 抽取
def ingest_hits(con: sqlite3.Connection, hits: list[dict], tag: str) -> int:
    n = 0
    for h in hits:
        if tag == "comment":
            text = h.get("comment_text") or ""
        else:
            text = " ".join(filter(None, [h.get("url") or "", h.get("story_text") or ""]))
        keys = extract_keys(text)
        if not keys:
            continue
        snippet = strip_html(h.get("comment_text") or h.get("title") or "")[:400]
        for key, meta in keys.items():
            cur = con.execute(
                "INSERT OR IGNORE INTO mentions VALUES (?,?,?,?,?,?,?,?,?)",
                (str(h.get("objectID")), key, h.get("author"), int(h.get("created_at_i") or 0), tag,
                 str(h.get("story_id") or h.get("objectID") or ""),
                 h.get("story_title") or h.get("title"), h.get("points"), snippet),
            )
            if cur.rowcount:
                n += 1
                con.execute("INSERT OR IGNORE INTO items(item_key, kind, ext_id, handle) VALUES (?,?,?,?)",
                            (key, meta["kind"], meta["ext_id"], meta["handle"]))
                if meta["handle"]:
                    con.execute("UPDATE items SET handle=? WHERE item_key=? AND handle IS NULL",
                                (meta["handle"], key))
    return n


def crawl(con: sqlite3.Connection, budget: int) -> tuple[int, int, bool]:
    """按时间窗口推进每条游标。窗口内结果 >1000（Algolia 上限）就把窗口折半。
    返回 (请求数, 新增提及数, 是否已全部追平)。"""
    start = int(datetime.fromisoformat(BACKFILL_FROM).replace(tzinfo=timezone.utc).timestamp())
    for q, tag in QUERIES:
        con.execute("INSERT OR IGNORE INTO crawl_state VALUES (?,?,?,?)", (q, tag, start, 30 * 86400))
    con.commit()

    requests = new = 0
    caught_up = True
    for q, tag in QUERIES:
        while requests < budget:
            row = con.execute("SELECT cursor_ts, window_s FROM crawl_state WHERE query=? AND tag=?", (q, tag)).fetchone()
            cur, win = row["cursor_ts"], row["window_s"]
            horizon = now_ts() - 120
            if cur >= horizon - 60:
                break
            end = min(cur + win, horizon)
            params = {"query": q, "tags": tag, "hitsPerPage": 1000,
                      "numericFilters": f"created_at_i>={cur},created_at_i<{end}"}
            res = http_json(HN_API, params)
            requests += 1
            nb = int(res.get("nbHits") or 0)
            if nb > 1000 and win > 3600:
                con.execute("UPDATE crawl_state SET window_s=? WHERE query=? AND tag=?", (max(3600, win // 2), q, tag))
                con.commit()
                time.sleep(0.3)
                continue
            new += ingest_hits(con, res.get("hits") or [], tag)
            # 结果少就放大窗口，加快回溯
            if nb < 300:
                win = min(win * 2, 365 * 86400)
            elif nb > 800:
                win = max(3600, int(win * 0.7))
            con.execute("UPDATE crawl_state SET cursor_ts=?, window_s=? WHERE query=? AND tag=?", (end, win, q, tag))
            con.commit()
            time.sleep(0.4)  # 客气一点：Algolia 限额 10000 次/小时
        else:
            caught_up = False
        if requests >= budget:
            caught_up = False
            break
    # 预算耗尽时再检查一次是否所有游标都已到达当前时间
    if not caught_up:
        horizon = now_ts() - 180
        caught_up = all(r[0] >= horizon for r in con.execute("SELECT cursor_ts FROM crawl_state"))
    return requests, new, caught_up


# ----------------------------------------------------------------------------- 3. 筛选
def select_picks(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        "SELECT item_key, author, created_at, hn_id, hn_type, story_id, story_title, points, snippet "
        "FROM mentions WHERE author IS NOT NULL ORDER BY created_at"
    ).fetchall()
    agg: dict[str, dict] = defaultdict(lambda: {"authors": set(), "months": set(), "mentions": []})
    for r in rows:
        a = agg[r["item_key"]]
        a["authors"].add(r["author"])
        a["months"].add(datetime.fromtimestamp(r["created_at"], timezone.utc).strftime("%Y-%m"))
        a["mentions"].append(r)
    picks = []
    for key, a in agg.items():
        if len(a["authors"]) < MIN_AUTHORS or len(a["months"]) < MIN_MONTHS:
            continue
        ms = a["mentions"]
        # 每个作者只取一条，作为「推荐语」样本；优先长一点的评论
        seen, quotes = set(), []
        for m in sorted(ms, key=lambda m: -len(m["snippet"] or "")):
            if m["author"] in seen or not m["snippet"]:
                continue
            seen.add(m["author"])
            quotes.append({"author": m["author"], "hn_id": m["hn_id"], "type": m["hn_type"],
                           "ts": m["created_at"], "text": m["snippet"][:280],
                           "story_title": m["story_title"]})
            if len(quotes) >= 5:
                break
        quotes.sort(key=lambda q: q["ts"])
        years = {mo[:4] for mo in a["months"]}
        picks.append({
            "key": key,
            "n_authors": len(a["authors"]),
            "n_months": len(a["months"]),
            "n_years": len(years),
            "n_mentions": len(ms),
            "first": ms[0]["created_at"],
            "last": ms[-1]["created_at"],
            "story_points": max([m["points"] or 0 for m in ms if m["hn_type"] == "story"] or [0]),
            "quotes": quotes,
        })
    for p in picks:  # 综合分：人数为主，跨度越长越加分
        p["score"] = round(p["n_authors"] + 0.5 * p["n_months"] + 1.0 * p["n_years"], 2)
    picks.sort(key=lambda p: -p["score"])
    return picks


# ----------------------------------------------------------------------------- 4. 补充元数据
YT_API = ENV("YT_API", "https://www.googleapis.com/youtube/v3/videos")
DUR_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def iso_duration(s: str | None) -> int | None:
    m = DUR_RE.fullmatch(s or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    total = d * 86400 + h * 3600 + mi * 60 + se
    return total or None


def enrich_backlog(con, keys: list[str]) -> list[str]:
    has_api = bool(ENV("YOUTUBE_API_KEY"))
    out = []
    for k in keys:
        it = con.execute("SELECT kind, enrich_status, enrich_src, enriched_at FROM items WHERE item_key=?", (k,)).fetchone()
        if it is None:
            continue
        st, src, at = it["enrich_status"], it["enrich_src"], it["enriched_at"] or 0
        if st is None:
            out.append(k)
        elif st == "err" and at < now_ts() - 6 * 3600:
            out.append(k)
        elif has_api and it["kind"] == "youtube" and src != "api":
            out.append(k)  # 配了 key 之后，把以前只有 oEmbed 数据的视频升级一遍（拿到时长、播放量、上传时间）
        elif has_api and it["kind"] == "youtube" and st != "dead" and at < now_ts() - 30 * 86400:
            out.append(k)  # 每 30 天刷新一次播放量
    return out


def _save(con, k, **f):
    f["enriched_at"] = now_ts()
    cols = ", ".join(f"{c}=?" for c in f)
    con.execute(f"UPDATE items SET {cols} WHERE item_key=?", (*f.values(), k))


def enrich_youtube_api(con, keys: list[str]) -> int:
    """YouTube Data API v3：每次 50 个视频，只消耗 1 个配额单位（免费额度每天 10000）。"""
    done = 0
    for i in range(0, len(keys), 50):
        chunk = keys[i:i + 50]
        ids = [k.split(":", 1)[1] for k in chunk]
        try:
            d = http_json(YT_API, {"part": "snippet,contentDetails,statistics,status", "id": ",".join(ids),
                                   "key": ENV("YOUTUBE_API_KEY"), "maxResults": 50}, retries=2)
        except urllib.error.HTTPError as e:
            log.error("YouTube Data API HTTP %s: %s", e.code, e.read().decode("utf-8", "replace")[:300])
            return done
        except Exception as e:  # noqa: BLE001
            log.error("YouTube Data API 失败: %s", e)
            return done
        got = {v["id"]: v for v in d.get("items", [])}
        for k, vid in zip(chunk, ids):
            v = got.get(vid)
            if v is None:  # 已删除 / 私有
                _save(con, k, enrich_status="dead", enrich_src="api")
                continue
            sn, cd, stt = v.get("snippet", {}), v.get("contentDetails", {}), v.get("statistics", {})
            pub = sn.get("publishedAt")
            _save(con, k,
                  title=sn.get("title"), channel=sn.get("channelTitle"),
                  thumb=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                  descr=(sn.get("description") or "")[:600] or None,
                  published_at=int(datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()) if pub else None,
                  duration_s=iso_duration(cd.get("duration")),
                  views=int(stt["viewCount"]) if stt.get("viewCount") else None,
                  enrich_status="ok" if v.get("status", {}).get("embeddable", True) else "noembed",
                  enrich_src="api")
            done += 1
        con.commit()
        time.sleep(0.2)
    return done


def enrich_oembed(con, k: str, it) -> str:
    url = canonical_url(it["kind"], it["ext_id"], it["handle"])
    try:
        if it["kind"] == "youtube":
            d = http_json("https://www.youtube.com/oembed", {"url": url, "format": "json"}, retries=1)
            _save(con, k, title=d.get("title"), channel=d.get("author_name"),
                  thumb=f"https://i.ytimg.com/vi/{it['ext_id']}/hqdefault.jpg", enrich_status="ok", enrich_src="oembed")
        else:
            d = http_json("https://publish.twitter.com/oembed", {"url": url, "omit_script": "1", "dnt": "true"}, retries=1)
            m = re.search(r"<p[^>]*>(.*?)</p>", d.get("html") or "", re.S)
            text = strip_html(m.group(1)) if m else None
            handle = (d.get("author_url") or "").rstrip("/").rsplit("/", 1)[-1] or it["handle"]
            _save(con, k, title=(text or "")[:120] or None, channel=d.get("author_name"), tweet_text=text,
                  handle=handle, enrich_status="ok", enrich_src="oembed")
        return "ok"
    except urllib.error.HTTPError as e:
        # oEmbed: 404=已删除/私有；401/403=作者禁止嵌入（视频还在，只是不能站内播放）
        if e.code == 404 or (it["kind"] == "x" and e.code in (400, 403)):
            _save(con, k, enrich_status="dead", enrich_src="oembed")
            return "dead"
        if e.code in (401, 403) and it["kind"] == "youtube":
            _save(con, k, enrich_status="noembed", enrich_src="oembed")
            return "noembed"
        _save(con, k, enrich_status="err", enrich_src="oembed")
        return f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        _save(con, k, enrich_status="err", enrich_src="oembed")
        return f"{type(e).__name__}: {str(e)[:120]}"


def enrich(con: sqlite3.Connection, keys: list[str], budget: int) -> tuple[int, int]:
    """返回 (本轮处理数, 剩余积压数)。"""
    todo = enrich_backlog(con, keys)
    batch = todo[:budget]
    yt_api = [k for k in batch if k.startswith("yt:")] if ENV("YOUTUBE_API_KEY") else []
    rest = [k for k in batch if k not in set(yt_api)]
    done = enrich_youtube_api(con, yt_api) if yt_api else 0
    errors: dict[str, int] = defaultdict(int)
    for k in rest[:OEMBED_PER_CYCLE]:
        it = con.execute("SELECT * FROM items WHERE item_key=?", (k,)).fetchone()
        r = enrich_oembed(con, k, it)
        if r not in ("ok", "dead", "noembed"):
            errors[r] += 1
            if sum(errors.values()) >= 30 and errors[r] >= 30:
                log.error("oEmbed 连续失败（%s），本轮暂停。可运行 python hnpicks.py diag 检查", r)
                break
        done += 1
        con.commit()
        time.sleep(0.3)
    if errors:
        log.warning("oEmbed 失败统计: %s", dict(errors))
    return done, max(0, len(enrich_backlog(con, keys)))


# ----------------------------------------------------------------------------- 5. 分类
RULES = [
    ("AI与机器学习", r"\b(ai|gpt|llm|neural|machine learning|deep learning|transformer|karpathy|openai|anthropic|claude|agi|reinforcement)\b"),
    ("编程与软件工程", r"\b(programming|code|coding|software|compiler|rust|python|javascript|haskell|lisp|linux|git|database|sql|vim|emacs|kernel|devops|api|unix)\b"),
    ("科学与数学", r"\b(physics|math|mathematics|quantum|biology|chemistry|universe|theorem|space|nasa|evolution|neuroscience|3blue1brown|veritasium)\b"),
    ("技术演讲与访谈", r"\b(talk|lecture|keynote|conference|interview|podcast|strange loop|gdc|defcon|ted|fireside)\b"),
    ("创业与商业", r"\b(startup|founder|business|yc|y combinator|investing|economics|market|company|ceo)\b"),
    ("历史与纪录片", r"\b(history|documentary|historical|ww2|war|ancient|story of)\b"),
    ("工程与硬件", r"\b(engineering|hardware|cpu|chip|electronics|machining|fpga|robot|engine|manufactur|3d print)\b"),
    ("设计与艺术", r"\b(design|art|typography|animation|film|ui|ux|drawing|architecture)\b"),
    ("音乐", r"\b(music|song|album|concert|guitar|piano|synth|official video|live at)\b"),
]


def rule_category(text: str) -> str:
    t = (text or "").lower()
    for cat, pat in RULES:
        if re.search(pat, t):
            return cat
    return "其他"




def deepseek_chat(system: str, user: str) -> str:
    """调用 DeepSeek（OpenAI 兼容接口），要求返回 JSON 对象。"""
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 8000,
    }).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {ENV('DEEPSEEK_API_KEY')}",
                 "User-Agent": UA})
    delay = 5.0
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read().decode("utf-8"))
            choice = d["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ValueError("输出被截断（finish_reason=length）")
            return choice["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            if e.code in (400, 401, 402, 403, 422):  # 参数错 / key 无效 / 余额不足：重试无意义
                raise RuntimeError(f"DeepSeek HTTP {e.code}: {msg}") from None
            err = f"HTTP {e.code}: {msg}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = str(e)
        if attempt < 3:
            log.warning("DeepSeek 请求失败（%s），%.0fs 后重试", err, delay)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"DeepSeek 多次请求失败: {err}")


def rules_classify(con, p) -> None:
    it = con.execute("SELECT * FROM items WHERE item_key=?", (p["key"],)).fetchone()
    if it["classified_by"] == "rules" and (it["classified_at"] or 0) >= (it["enriched_at"] or 0):
        return
    ctx = " ".join([it["title"] or "", it["channel"] or "", it["tweet_text"] or "", it["descr"] or ""] +
                   [q["story_title"] or "" for q in p["quotes"]])
    con.execute("UPDATE items SET category=?, classified_by='rules', classified_at=? WHERE item_key=?",
                (rule_category(ctx), now_ts(), p["key"]))


def classify_backlog(con, picks) -> list[dict]:
    """已补完元数据、还没被大模型分类的条目（按综合分从高到低）。"""
    return [p for p in picks if con.execute(
        "SELECT 1 FROM items WHERE item_key=? AND enrich_status IN ('ok','noembed') "
        "AND (classified_by IS NULL OR classified_by!='llm' OR topics IS NULL)", (p["key"],)).fetchone()]


def classify(con: sqlite3.Connection, picks: list[dict], budget: int) -> tuple[int, str]:
    need = classify_backlog(con, picks)[:budget]
    if not ENV("DEEPSEEK_API_KEY"):
        for p in need:
            rules_classify(con, p)
        con.commit()
        return len(need), "规则分类（未配置 DEEPSEEK_API_KEY）"
    if not need:
        return 0, f"DeepSeek 分类（{DEEPSEEK_MODEL}）"

    # 把已有的高频主题告诉模型，让它尽量复用，避免主题碎片化
    freq: dict[str, int] = defaultdict(int)
    for (tp,) in con.execute("SELECT topics FROM items WHERE topics IS NOT NULL"):
        for t in json.loads(tp):
            freq[t] += 1
    known = [t for t, _ in sorted(freq.items(), key=lambda x: -x[1])[:120]]

    system = (
        "你是一个视频网站的内容编辑。用户会给你一个 JSON 数组，每一项都是在 Hacker News 上被多位用户、跨越多个月份反复推荐的 YouTube 视频或 X 帖子。"
        "请根据标题、频道、简介、以及 HN 用户的推荐原话，为每项给出：\n"
        f"- category：只能从这些大类中选一个：{'、'.join(CATEGORIES)}\n"
        "- topics：1~3 个中文细分主题标签（2~8 个字的名词短语，如「数字电路」「分布式系统」「宇宙学」「创业融资」）。"
        + (f"优先复用这些已有主题：{'、'.join(known)}。" if known else "") + "\n"
        "- title_zh：简洁自然的中文标题（不超过 30 字，人名、产品名等专有名词保留原文）\n"
        "- reason_zh：一句话中文推荐理由（不超过 60 字），说清楚它为什么值得看，依据 HN 用户的评价，不要夸张、不要编造原话里没有的事实。\n"
        "key 原样返回，每一项都要输出。只输出 JSON，格式如下：\n"
        '{"items": [{"key": "yt:xxxxxxxxxxx", "category": "科学与数学", "topics": ["宇宙学"], "title_zh": "……", "reason_zh": "……"}]}'
    )
    done = 0
    for i in range(0, len(need), 20):
        batch = need[i:i + 20]
        payload = []
        for p in batch:
            it = con.execute("SELECT * FROM items WHERE item_key=?", (p["key"],)).fetchone()
            payload.append({
                "key": p["key"], "type": it["kind"], "title": it["title"], "channel": it["channel"],
                "duration_min": round(it["duration_s"] / 60) if it["duration_s"] else None,
                "description": (it["descr"] or "")[:300] or None,
                "tweet_text": it["tweet_text"],
                "hn_story_titles": list({q["story_title"] for q in p["quotes"] if q["story_title"]})[:3],
                "hn_quotes": [q["text"][:240] for q in p["quotes"][:4]],
            })
        try:
            text = deepseek_chat(system, json.dumps(payload, ensure_ascii=False))
            result = json.loads(text)["items"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            log.error("DeepSeek 返回无法解析: %s", e)
            continue
        except RuntimeError as e:
            log.error("%s；本轮分类暂停", e)
            for p in need:  # 先用规则兜底，网站不至于全是「其他」
                rules_classify(con, p)
            con.commit()
            break
        valid = {p["key"] for p in batch}
        for r in result:
            if not isinstance(r, dict) or r.get("key") not in valid:
                continue
            cat = r.get("category") if r.get("category") in CATEGORIES else "其他"
            topics = [str(t).strip()[:12] for t in (r.get("topics") or []) if str(t).strip()][:3]
            con.execute("UPDATE items SET category=?, topics=?, title_zh=?, reason_zh=?, classified_by='llm', classified_at=? "
                        "WHERE item_key=?", (cat, json.dumps(topics, ensure_ascii=False),
                                             str(r.get("title_zh") or "")[:80] or None,
                                             str(r.get("reason_zh") or "")[:160] or None, now_ts(), r["key"]))
            done += 1
        con.commit()
        log.info("DeepSeek 已分类 %d/%d", done, len(need))
    return done, f"DeepSeek 分类（{DEEPSEEK_MODEL}）"


# ----------------------------------------------------------------------------- 6. 生成网站
SHARDS = 64


def build_site(con: sqlite3.Connection, picks: list[dict], status: dict) -> int:
    """data.json 只放列表页需要的轻量字段；推荐原话等详情按 key 分片到 d/NN.json，打开视频时才加载。"""
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "d").mkdir(exist_ok=True)
    lite, shards = [], [dict() for _ in range(SHARDS)]
    for p in picks:
        it = con.execute("SELECT * FROM items WHERE item_key=?", (p["key"],)).fetchone()
        if it is None or it["enrich_status"] == "dead":
            continue  # 视频已删除 / 帖子已不可见
        sh = zlib.crc32(p["key"].encode()) % SHARDS
        rec = {
            "k": p["key"], "kd": "y" if it["kind"] == "youtube" else "x", "id": it["ext_id"],
            "t": it["title"], "tz": it["title_zh"], "ch": it["channel"],
            "p": it["published_at"], "d": it["duration_s"], "v": it["views"],
            "c": it["category"] or "其他", "tp": json.loads(it["topics"]) if it["topics"] else [],
            "na": p["n_authors"], "nm": p["n_months"], "f": p["first"], "l": p["last"], "s": p["score"],
            "e": 0 if it["enrich_status"] == "noembed" else 1, "sh": sh,
        }
        if it["kind"] == "x":
            rec["h"] = it["handle"]
            rec["tx"] = (it["tweet_text"] or "")[:200]
        lite.append({k: v for k, v in rec.items() if v not in (None, [], "")})
        shards[sh][p["key"]] = {"q": p["quotes"], "r": it["reason_zh"], "ds": (it["descr"] or "")[:400] or None,
                                "tx": it["tweet_text"], "h": it["handle"], "nmn": p["n_mentions"]}
    data = {"generated_at": now_ts(), "min_authors": MIN_AUTHORS, "min_months": MIN_MONTHS,
            "shards": SHARDS, "categories": CATEGORIES, "items": lite}
    tmp = SITE_DIR / "data.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(SITE_DIR / "data.json")
    for i, sh in enumerate(shards):
        (SITE_DIR / "d" / f"{i:02d}.json").write_text(json.dumps(sh, ensure_ascii=False, separators=(",", ":")),
                                                    encoding="utf-8")
    status = {**status, "picks": len(lite)}
    (SITE_DIR / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    page = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8").replace("{{SITE_TITLE}}", html.escape(SITE_TITLE))
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")
    for f in TEMPLATE_DIR.iterdir():
        if f.name != "index.html" and f.is_file():
            shutil.copy(f, SITE_DIR / f.name)
    return len(lite)


# ----------------------------------------------------------------------------- 主循环
def db_stats(con) -> dict:
    q = lambda s: con.execute(s).fetchone()[0]
    oldest = q("SELECT MIN(cursor_ts) FROM crawl_state")
    return {
        "mentions": q("SELECT COUNT(*) FROM mentions"),
        "distinct_links": q("SELECT COUNT(*) FROM items"),
        "crawl_cursor": datetime.fromtimestamp(oldest, timezone.utc).strftime("%Y-%m-%d") if oldest else None,
        "youtube_api": bool(ENV("YOUTUBE_API_KEY")),
        "interval_hours": INTERVAL_HOURS,
    }


def run_cycle() -> tuple[bool, dict]:
    con = db()
    t0 = now_ts()
    reqs, new, caught_up = crawl(con, REQ_PER_CYCLE)
    picks = select_picks(con)
    keys = [p["key"] for p in picks]
    n_enriched, enrich_left = enrich(con, keys, ENRICH_PER_CYCLE)
    n_cls, cls_mode = classify(con, picks, CLASSIFY_PER_CYCLE)
    cls_left = len(classify_backlog(con, picks)) if ENV("DEEPSEEK_API_KEY") else 0
    status = {
        "updated_at": now_ts(), "caught_up": caught_up, "classifier": cls_mode,
        "rule": f"≥{MIN_AUTHORS} 位不同用户 · ≥{MIN_MONTHS} 个不同月份",
        "enrich_backlog": enrich_left, "classify_backlog": cls_left,
        **db_stats(con),
    }
    n_site = build_site(con, picks, status)
    con.execute("INSERT INTO runs(started_at, finished_at, requests, new_mentions, picks, note) VALUES (?,?,?,?,?,?)",
                (t0, now_ts(), reqs, new, n_site,
                 f"enriched={n_enriched} left={enrich_left} classified={n_cls} left={cls_left} {cls_mode}"))
    con.commit()
    log.info("本轮完成: HN请求=%d 新提及=%d 入选=%d | 补元数据=%d 剩余=%d | 分类=%d 剩余=%d (%s) | 游标=%s 追平=%s",
             reqs, new, n_site, n_enriched, enrich_left, n_cls, cls_left, cls_mode, status["crawl_cursor"], caught_up)
    con.close()
    busy = (not caught_up) or enrich_left > 0 or (cls_left > 0 and n_cls > 0)
    return busy, status


def diag():
    """逐项检查外部接口是否可用。"""
    vid = "aircAruvnKk"
    checks = [
        ("HN Algolia", lambda: http_json(HN_API, {"query": "youtube", "tags": "comment", "hitsPerPage": 1}, retries=0)["nbHits"]),
        ("YouTube oEmbed", lambda: http_json("https://www.youtube.com/oembed",
                                             {"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}, retries=0)["title"]),
    ]
    if ENV("YOUTUBE_API_KEY"):
        checks.append(("YouTube Data API", lambda: http_json(YT_API, {"part": "snippet", "id": vid, "key": ENV("YOUTUBE_API_KEY")},
                                                             retries=0)["items"][0]["snippet"]["title"]))
    else:
        checks.append(("YouTube Data API", lambda: "未配置 YOUTUBE_API_KEY（没有时长、播放量、上传时间）"))
    if ENV("DEEPSEEK_API_KEY"):
        checks.append(("DeepSeek", lambda: deepseek_chat("只输出 JSON", '返回 {"ok": true}')[:60]))
    else:
        checks.append(("DeepSeek", lambda: "未配置 DEEPSEEK_API_KEY（只能用关键词规则分类）"))
    for name, fn in checks:
        try:
            print(f"✅ {name}: {fn()}")
        except urllib.error.HTTPError as e:
            print(f"❌ {name}: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}")
        except Exception as e:  # noqa: BLE001
            print(f"❌ {name}: {type(e).__name__}: {e}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "loop"
    if cmd == "once":
        run_cycle()
    elif cmd == "diag":
        diag()
    elif cmd == "build":
        con = db()
        build_site(con, select_picks(con), {"updated_at": now_ts(), **db_stats(con)})
    elif cmd == "stats":
        con = db()
        print(json.dumps(db_stats(con), ensure_ascii=False, indent=1))
        for r in con.execute("SELECT enrich_status, enrich_src, COUNT(*) n FROM items GROUP BY 1,2"):
            print(dict(r))
        for r in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5"):
            print(dict(r))
    elif cmd == "loop":
        try:  # 先生成一版网站，避免首轮抓取期间网站是空白 404
            con = db()
            horizon = now_ts() - int((INTERVAL_HOURS + 1) * 3600)  # 追平后游标最多落后一个更新间隔
            caught = con.execute("SELECT COUNT(*) FROM crawl_state").fetchone()[0] > 0 and \
                all(r[0] >= horizon for r in con.execute("SELECT cursor_ts FROM crawl_state"))
            build_site(con, select_picks(con), {"updated_at": now_ts(), "caught_up": caught, **db_stats(con)})
            con.close()
        except Exception:  # noqa: BLE001
            log.exception("初始网站生成失败")
        while True:
            try:
                busy, _ = run_cycle()
            except Exception:  # noqa: BLE001 —— 常驻进程：记录错误后稍后重试
                log.exception("本轮出错，10 分钟后重试")
                time.sleep(600)
                continue
            # 还有积压（回溯历史 / 补元数据 / 分类）就短暂休息后继续；全部追平后按间隔增量更新
            time.sleep(20 if busy else INTERVAL_HOURS * 3600)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
