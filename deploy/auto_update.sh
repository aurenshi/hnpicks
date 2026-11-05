#!/usr/bin/env bash
# 服务器每 5 分钟运行一次：GitHub 上有新提交就拉取并重建，没有就什么都不做。
# 安装：bash deploy/install_auto_update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
BRANCH="${BRANCH:-main}"
exec 9>/tmp/hnpicks-update.lock
flock -n 9 || exit 0   # 上一次还没跑完就跳过

git fetch -q origin "$BRANCH"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")
[ "$LOCAL" = "$REMOTE" ] && exit 0

echo "$(date '+%F %T') 更新 ${LOCAL:0:7} -> ${REMOTE:0:7}"
git reset -q --hard "origin/$BRANCH"     # .env / data / site 在 .gitignore 里，不受影响
docker compose up -d --build 2>&1 | tail -3
echo "$(date '+%F %T') 完成"
