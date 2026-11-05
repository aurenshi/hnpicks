# HNPicks：HN 上被反复安利的视频与帖子

这是一个常驻运行的 Agent。它会自动做下面几件事：

1. **抓取**：只用各站官方接口，抓取提到 YouTube、X/Twitter 链接的帖子和评论。
   - **Hacker News**：Algolia 公开 API，2007 年至今，无需 key
   - **Lobsters**：官方 JSON 接口（`/domains/youtube.com.json`、`/s/<id>.json`），2012 年至今，无需 key
   - **Reddit**：官方 OAuth API，默认 r/programming。官方搜索每个查询最多返回约 1000 条，所以用多种排序组合尽量覆盖历史，每天完整扫描一次
2. **抽取**：从正文里识别视频 ID 和推文 ID。各种写法都能认出来，包括 youtu.be、shorts、embed、m.youtube、x.com、twitter.com、nitter 等。
3. **筛选**：同时满足下面两条才入选（各社区人数合计；不同社区的同名账号按不同的人算）。
   - 至少 N 个**不同的人**推荐过（`MIN_AUTHORS`）
   - 推荐分布在至少 3 个**不同的月份**
4. **补全**：配了 `YOUTUBE_API_KEY` 就用 YouTube Data API 补标题、频道、时长、播放量和上传时间；没配就用 oEmbed，只有标题和频道。已经删除的视频会被自动剔除。
5. **分类**：配置了 `DEEPSEEK_API_KEY` 就由 DeepSeek 来分类，同时写中文标题和一句话推荐理由；没配置就按关键词规则分类。
6. **上线**：生成仿视频站布局的静态网站，由 Caddy 提供访问。网站有这些功能：
   - 首页按「为你推荐」排序，长视频、最近被推荐、分类和主题、搜索、筛选（上传时间、时长、类型、HN 推荐人数、排序）
   - 站内播放，显示 HN 用户原话和相关推荐
   - 赞过、历史、稍后观看都存在浏览器本地，不需要登录，可以导出和导入
   - 封面用视频中段的代表帧；支持深色模式、快捷键，手机上也能正常显示

首次启动后，Agent 会先回溯约 20 年的历史数据，大约需要 30 到 60 分钟，期间网站内容会逐步变多。追平之后，默认每 6 小时增量更新一次。

### 可靠性规则

- Lobsters 标记为「作者本人投稿」的帖子不算推荐，因为那是自我推广
- Reddit 帖子 < 10 赞、评论 < 2 赞的不算（可配置）
- 已删除、被版主处理的内容和机器人账号不算
- 被多个社区独立推荐的视频，排名加分

## 部署（香港服务器）

```bash
cd /root && tar xzf hnpicks.tar.gz && cd hnpicks && bash install.sh
```

装好后访问 `http://服务器IP/`。

## 自动更新

服务器每 5 分钟检查一次 GitHub 的 main 分支，有新提交就自动拉取并重建容器。日志在 `/var/log/hnpicks-update.log`。安装方法：

```bash
bash deploy/install_auto_update.sh
```

## 常用命令（在 /root/hnpicks 目录下执行）

| 用途 | 命令 |
|---|---|
| 看实时日志 | `docker compose logs -f agent` |
| 看进度和统计 | `docker compose exec agent python hnpicks.py stats` |
| 检查各接口通不通 | `docker compose exec agent python hnpicks.py diag` |
| 改配置后重启 | `docker compose up -d` |
| 更新代码后重建 | `docker compose up -d --build` |
| 停止 | `docker compose down` |

## 配置（`.env`）

- `MIN_AUTHORS` / `MIN_MONTHS`：筛选门槛，模板里分别是 5 和 3。改完运行 `docker compose up -d` 即可，不用重新抓数据。
- `INTERVAL_HOURS`：追平历史数据后，多久更新一次。
- `YOUTUBE_API_KEY`：强烈建议配置。免费额度每天 10000 单位，补 7000 多个视频只用约 150 单位。
- `DEEPSEEK_API_KEY`：可选，填了就开启 DeepSeek 分类、细分主题、中文标题和推荐理由。
- `DEEPSEEK_MODEL`：默认 `deepseek-chat`。每批送 20 条，`CLASSIFY_WORKERS`（默认 5）个请求同时发，每轮最多处理 1000 条，只对新入选的条目调用。
- `SITE_ADDRESS`：默认 `:80`，用服务器 IP 访问。有域名后，把域名解析到这台服务器，再把这一项改成域名，Caddy 会自动申请 HTTPS 证书。

数据保存在 `data/hnpicks.db`（SQLite），网站文件在 `site/`。
