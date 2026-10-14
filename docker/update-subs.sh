#!/bin/sh
# 容器内手动触发：拉取订阅并热重载
# 用法：docker exec sing-box-sub update-subs
# 等价于点击面板「立即更新并重载」
set -eu
PORT="${MANAGER_PORT:-8080}"
if [ -n "${MANAGER_TOKEN:-}" ]; then
  AUTH="Authorization: Bearer $MANAGER_TOKEN"
  curl -fsS -X POST -H "$AUTH" "http://127.0.0.1:${PORT}/api/reload"
else
  curl -fsS -X POST "http://127.0.0.1:${PORT}/api/reload"
fi
echo
