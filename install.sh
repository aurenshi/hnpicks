#!/usr/bin/env bash
# 在服务器上执行：bash install.sh
set -euo pipefail
cd "$(dirname "$0")"
command -v docker >/dev/null || { echo "未安装 Docker"; exit 1; }
[ -f .env ] || cp .env.example .env
mkdir -p data site
docker compose up -d --build
sleep 3
docker compose ps
IP=$(curl -s -m 5 https://ifconfig.me || hostname -I | awk '{print $1}')
echo
echo "已启动。Agent 正在回溯 HN 历史数据，第一批结果几分钟后出现："
echo "  网站:  http://$IP/"
echo "  日志:  docker compose logs -f agent"
echo "  统计:  docker compose exec agent python hnpicks.py stats"
