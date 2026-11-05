"""
HN 之外的一手数据源：Lobsters、Reddit。都只用各站点官方接口。

  Lobsters : 官方 JSON 接口（无需 key）
             /domains/<域名>.json 和 /domains/<域名>/page/N.json  —— 该域名下的全部投稿，可回溯到 2012 年建站
             /s/<short_id>.json                                     —— 单个帖子及全部评论
  Reddit   : 官方 OAuth API（需要在 reddit.com/prefs/apps 建一个 script 应用）
             /r/<sub>/search?q=site:youtube.com&restrict_sr=1        —— 子版块里链接到视频的帖子
             /r/<sub>/comments/<id>                                  —— 帖子下的评论

可靠性规则（宁缺毋滥）：
  - Lobsters 标记为「作者本人投稿」(user_is_author) 的帖子不算推荐，是自我推广
  - Reddit 帖子得分 < REDDIT_MIN_SCORE、评论得分 < REDDIT_MIN_COMMENT_SCORE 的不算
  - 已删除 / 被版主处理的内容、机器人账号不算
  - 同一个人在不同站点的账号按不同的人计（无法确认是同一个人）

每个函数的第一个参数 h 是 hnpicks 主模块，用它的公共工具（http_json、extract_keys、add_mention……）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

log = logging.getLogger("hnpicks")
ENV = os.environ.get

# ----------------------------------------------------------------------------- 状态存取（kv 表）
def kv_get(con, k, default=None):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def kv_set(con, k, v):
    con.execute("INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def kv_del(con, k):
    con.execute("DELETE FROM kv WHERE k=?", (k,))


def iso_ts(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


# ============================================================================= Lobsters
LOBSTERS_BASE = ENV("LOBSTERS_BASE", "https://lobste.rs").rstrip("/")
LOBSTERS_DOMAINS = ["youtube.com", "youtu.be", "m.youtube.com", "www.youtube.com",
                    "x.com", "twitter.com", "mobile.twitter.com"]
LOBSTERS_DELAY = float(ENV("LOBSTERS_DELAY", "1.0"))   # 小社区，请求间隔放宽一点


def lobsters_enabled() -> bool:
    return ENV("LOBSTERS", "1") != "0"


def _lo_story(con, h, s: dict) -> int:
    """处理一条投稿；返回新增提及数。作者本人投稿的不算推荐。"""
    sid = s["short_id"]
    new = 0
    if not s.get("user_is_author"):
        keys = h.extract_keys(" ".join(filter(None, [s.get("url"), s.get("description")])))
        desc = (s.get("description_plain") or "").strip()
        snippet = (s.get("title") or "") + (f" —— {desc}" if desc else "")
        for key, meta in keys.items():
            new += h.add_mention(con, mid=f"lo:{sid}", key=key, meta=meta, author=s.get("submitter_user"),
                                 ts=iso_ts(s["created_at"]), typ="story", story_id=sid, story_title=s.get("title"),
                                 points=s.get("score"), snippet=snippet, source="lobsters",
                                 link=s.get("comments_url") or s.get("short_id_url"))
    # 有新评论的帖子排进待抓队列（评论里可能推荐了别的视频）
    cc = int(s.get("comment_count") or 0)
    if cc and str(cc) != kv_get(con, f"lo:cc:{sid}"):
        kv_set(con, f"lo:pending:{sid}", cc)
    return new


def _lo_comments(con, h, sid: str) -> tuple[int, bool]:
    try:
        d = h.http_json(f"{LOBSTERS_BASE}/s/{sid}.json", retries=2)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # 帖子已删除
            kv_del(con, f"lo:pending:{sid}")
            return 0, True
        raise
    new = 0
    for c in d.get("comments") or []:
        if c.get("is_deleted") or c.get("is_moderated"):
            continue
        keys = h.extract_keys(c.get("comment") or "")
        for key, meta in keys.items():
            new += h.add_mention(con, mid=f"lo:c:{c['short_id']}", key=key, meta=meta,
                                 author=c.get("commenting_user"), ts=iso_ts(c["created_at"]), typ="comment",
                                 story_id=sid, story_title=d.get("title"), points=c.get("score"),
                                 snippet=h.strip_html(c.get("comment") or "")[:400], source="lobsters",
                                 link=c.get("short_id_url") or c.get("url"))
    kv_set(con, f"lo:cc:{sid}", d.get("comment_count") or len(d.get("comments") or []))
    kv_del(con, f"lo:pending:{sid}")
    return new, True


def crawl_lobsters(con, h, budget: int) -> tuple[int, int, bool]:
    """返回 (请求数, 新增提及, 是否已追平)。先按域名翻完全部历史投稿，再抓这些帖子的评论；追平后每轮只看最新几页。"""
    if not lobsters_enabled():
        return 0, 0, True
    reqs = new = 0
    for dom in LOBSTERS_DOMAINS:
        done = kv_get(con, f"lo:{dom}:done") == "1"
        page = 1 if done else int(kv_get(con, f"lo:{dom}:page", "1"))
        while reqs < budget:
            url = f"{LOBSTERS_BASE}/domains/{dom}.json" if page == 1 else f"{LOBSTERS_BASE}/domains/{dom}/page/{page}.json"
            try:
                stories = h.http_json(url, retries=2)
            except urllib.error.HTTPError as e:
                if e.code == 404:  # Lobsters 上没有这个域名
                    kv_set(con, f"lo:{dom}:done", 1)
                    break
                raise
            reqs += 1
            if not stories:
                kv_set(con, f"lo:{dom}:done", 1)
                kv_set(con, f"lo:{dom}:page", 1)
                break
            unseen = 0
            for s in stories:
                if kv_get(con, f"lo:seen:{s['short_id']}") is None:
                    unseen += 1
                    kv_set(con, f"lo:seen:{s['short_id']}", 1)
                new += _lo_story(con, h, s)
            page += 1
            if not done:
                kv_set(con, f"lo:{dom}:page", page)
            con.commit()
            time.sleep(LOBSTERS_DELAY)
            if done and (unseen == 0 or page > 5):  # 增量：最新几页都看过了就停
                break
    pending = [r[0].split(":", 2)[2] for r in con.execute("SELECT k FROM kv WHERE k LIKE 'lo:pending:%'")]
    for sid in pending:
        if reqs >= budget:
            break
        n, _ = _lo_comments(con, h, sid)
        new += n
        reqs += 1
        con.commit()
        time.sleep(LOBSTERS_DELAY)
    caught_up = all(kv_get(con, f"lo:{d}:done") == "1" for d in LOBSTERS_DOMAINS) and \
        not con.execute("SELECT 1 FROM kv WHERE k LIKE 'lo:pending:%' LIMIT 1").fetchone()
    return reqs, new, caught_up


# ============================================================================= Reddit
REDDIT_SUBS = [s.strip() for s in ENV("REDDIT_SUBS", "programming").split(",") if s.strip()]
REDDIT_MIN_SCORE = int(ENV("REDDIT_MIN_SCORE", "10"))
REDDIT_MIN_COMMENT_SCORE = int(ENV("REDDIT_MIN_COMMENT_SCORE", "2"))
REDDIT_SITES = ["youtube.com", "youtu.be", "x.com", "twitter.com"]
# 官方搜索每个查询最多返回约 1000 条，所以用多种排序组合尽量覆盖历史
REDDIT_SWEEP = [("new", "all"), ("top", "all"), ("top", "year"), ("top", "month"),
                ("relevance", "all"), ("comments", "all")]
REDDIT_BOTS = {"AutoModerator", "[deleted]", "None", None, ""}
REDDIT_AUTH_URL = ENV("REDDIT_AUTH_URL", "https://www.reddit.com/api/v1/access_token")
REDDIT_API = ENV("REDDIT_API", "https://oauth.reddit.com").rstrip("/")
_token: dict = {}


def reddit_enabled() -> bool:
    return bool(ENV("REDDIT_CLIENT_ID") and ENV("REDDIT_CLIENT_SECRET"))


def _ua() -> str:
    return f"linux:hnpicks:v0.3 (by /u/{ENV('REDDIT_USERNAME') or 'hnpicks'})"


def _reddit_token() -> str:
    if _token.get("exp", 0) > time.time() + 60:
        return _token["v"]
    auth = base64.b64encode(f"{ENV('REDDIT_CLIENT_ID')}:{ENV('REDDIT_CLIENT_SECRET')}".encode()).decode()
    req = urllib.request.Request(REDDIT_AUTH_URL,
                                 data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                                 headers={"Authorization": f"Basic {auth}", "User-Agent": _ua()}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    if "access_token" not in d:
        raise RuntimeError(f"Reddit 授权失败: {d}")
    _token.update(v=d["access_token"], exp=time.time() + int(d.get("expires_in", 3600)))
    return _token["v"]


def reddit_get(path: str, params: dict | None = None):
    q = {"raw_json": 1, **(params or {})}
    url = f"{REDDIT_API}{path}?{urllib.parse.urlencode(q)}"
    for attempt in range(4):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_reddit_token()}", "User-Agent": _ua()})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.loads(r.read().decode())
                # 官方限额：每 10 分钟 600 次；剩余不多时主动放慢
                remaining = float(r.headers.get("x-ratelimit-remaining") or 100)
                reset = float(r.headers.get("x-ratelimit-reset") or 0)
                if remaining < 10:
                    time.sleep(reset + 1)
                else:
                    time.sleep(0.7)
                return body
        except urllib.error.HTTPError as e:
            if e.code == 401:
                _token.clear()
            elif e.code == 429:
                time.sleep(int(e.headers.get("retry-after") or 60))
            elif e.code in (403, 404):
                raise
            elif attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Reddit 请求失败: {path}")


def _rd_post(con, h, sub: str, d: dict) -> int:
    new = 0
    ok = d.get("author") not in REDDIT_BOTS and int(d.get("score") or 0) >= REDDIT_MIN_SCORE \
        and not d.get("removed_by_category")
    if ok:
        keys = h.extract_keys(" ".join(filter(None, [d.get("url"), d.get("selftext")])))
        text = (d.get("selftext") or "").strip()
        snippet = (d.get("title") or "") + (f" —— {text[:300]}" if text else "")
        for key, meta in keys.items():
            new += h.add_mention(con, mid=f"rd:{d['name']}", key=key, meta=meta, author=d.get("author"),
                                 ts=int(d.get("created_utc") or 0), typ="story", story_id=d.get("id"),
                                 story_title=d.get("title"), points=d.get("score"), snippet=snippet,
                                 source="reddit", link="https://www.reddit.com" + (d.get("permalink") or ""))
    nc = int(d.get("num_comments") or 0)
    if ok and nc and str(nc) != kv_get(con, f"rd:cc:{d['id']}"):
        kv_set(con, f"rd:pending:{sub}:{d['id']}", nc)
    return new


def _rd_comments(con, h, sub: str, pid: str) -> int:
    data = reddit_get(f"/r/{sub}/comments/{pid}", {"limit": 500, "depth": 10, "sort": "top"})
    post = data[0]["data"]["children"][0]["data"] if data and data[0]["data"]["children"] else {}
    new = 0
    stack = list(data[1]["data"]["children"]) if len(data) > 1 else []
    while stack:
        c = stack.pop()
        if c.get("kind") != "t1":
            continue
        d = c["data"]
        rep = d.get("replies")
        if isinstance(rep, dict):
            stack.extend(rep["data"]["children"])
        if d.get("author") in REDDIT_BOTS or int(d.get("score") or 0) < REDDIT_MIN_COMMENT_SCORE:
            continue
        for key, meta in h.extract_keys(d.get("body") or "").items():
            new += h.add_mention(con, mid=f"rd:{d['name']}", key=key, meta=meta, author=d.get("author"),
                                 ts=int(d.get("created_utc") or 0), typ="comment", story_id=pid,
                                 story_title=post.get("title"), points=d.get("score"),
                                 snippet=(d.get("body") or "")[:400], source="reddit",
                                 link="https://www.reddit.com" + (d.get("permalink") or ""))
    kv_set(con, f"rd:cc:{pid}", post.get("num_comments") or 0)
    kv_del(con, f"rd:pending:{sub}:{pid}")
    return new


def crawl_reddit(con, h, budget: int) -> tuple[int, int, bool]:
    if not reddit_enabled():
        return 0, 0, True
    reqs = new = 0
    full = time.time() - float(kv_get(con, "rd:last_sweep", "0")) > 86400  # 每天一次完整扫描，其余只看最新
    sweep = REDDIT_SWEEP if full else [("new", "all")]
    max_pages = 10 if full else 2
    finished = True
    for sub in REDDIT_SUBS:
        for site in REDDIT_SITES:
            for sort, t in sweep:
                after = None
                for _ in range(max_pages):
                    if reqs >= budget:
                        finished = False
                        break
                    d = reddit_get(f"/r/{sub}/search", {"q": f"site:{site}", "restrict_sr": 1, "sort": sort,
                                                        "t": t, "limit": 100, "after": after or "", "type": "link"})
                    reqs += 1
                    for ch in d["data"]["children"]:
                        new += _rd_post(con, h, sub, ch["data"])
                    con.commit()
                    after = d["data"].get("after")
                    if not after:
                        break
    if full and finished:
        kv_set(con, "rd:last_sweep", int(time.time()))
    for (k,) in con.execute("SELECT k FROM kv WHERE k LIKE 'rd:pending:%'").fetchall():
        if reqs >= budget:
            break
        _, _, sub, pid = k.split(":", 3)
        try:
            new += _rd_comments(con, h, sub, pid)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                kv_del(con, k)
            else:
                raise
        reqs += 1
        con.commit()
    caught_up = finished and not con.execute("SELECT 1 FROM kv WHERE k LIKE 'rd:pending:%' LIMIT 1").fetchone()
    return reqs, new, caught_up
