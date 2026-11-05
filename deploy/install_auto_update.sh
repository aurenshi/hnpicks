#!/usr/bin/env bash
# 在服务器上运行一次，把自动更新加入 root 的 crontab
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
chmod +x "$DIR/deploy/auto_update.sh"
LINE="*/5 * * * * $DIR/deploy/auto_update.sh >> /var/log/hnpicks-update.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'hnpicks.*auto_update.sh' ; echo "$LINE" ) | crontab -
echo "已安装："; crontab -l | grep auto_update
