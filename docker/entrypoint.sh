#!/bin/sh
# sing-box + 订阅管理面板 启动入口
# 流程：准备目录 -> 可选启动时拉取一次 -> 后台起 sing-box -> 前台起 manager
set -eu

CONFIG_PATH="${CONFIG_PATH:-/etc/sing-box/config.json}"
TEMPLATE_PATH="${TEMPLATE_PATH:-/etc/sing-box/config.template.json}"
SUBS_PATH="${SUBS_PATH:-/etc/sing-box/subscriptions.json}"
CACHE_DIR="${CACHE_DIR:-/var/cache/sing-box-subs}"
SINGBOX_BIN="${SINGBOX_BIN:-sing-box}"
PID_FILE="${PID_FILE:-/run/sing-box.pid}"
MANAGER_PORT="${MANAGER_PORT:-8080}"
UPDATE_ON_START="${UPDATE_ON_START:-true}"
AUTO_UPDATE_INTERVAL="${AUTO_UPDATE_INTERVAL:-0}"

mkdir -p "$(dirname "$CONFIG_PATH")" "$(dirname "$SUBS_PATH")" "$CACHE_DIR" /var/log/sing-box /run

if [ ! -f "$SUBS_PATH" ]; then
  echo "[]" > "$SUBS_PATH"
  echo "[entrypoint] created empty $SUBS_PATH"
fi

if [ ! -f "$TEMPLATE_PATH" ]; then
  echo "[entrypoint] ERROR: missing template $TEMPLATE_PATH" >&2
  echo "[entrypoint] 请挂载 config.template.json 到 $TEMPLATE_PATH" >&2
  exit 1
fi

# 启动时先拉取一次订阅生成 config.json（失败则沿用旧 config / 模板兜底）
if [ "$UPDATE_ON_START" = "true" ]; then
  echo "[entrypoint] fetching subscriptions before start..."
  if python3 /app/manager/manager.py --update-once; then
    echo "[entrypoint] initial fetch OK"
  else
    echo "[entrypoint] WARN: initial fetch failed, fallback to existing config" >&2
    if [ ! -f "$CONFIG_PATH" ]; then
      echo "[entrypoint] no existing config, copying template as-is"
      cp "$TEMPLATE_PATH" "$CONFIG_PATH"
    fi
  fi
else
  if [ ! -f "$CONFIG_PATH" ]; then
    cp "$TEMPLATE_PATH" "$CONFIG_PATH"
  fi
fi

echo "[entrypoint] checking config: $SINGBOX_BIN check -c $CONFIG_PATH"
"$SINGBOX_BIN" check -c "$CONFIG_PATH"

cleanup() {
  echo "[entrypoint] shutting down..."
  if [ -f "$PID_FILE" ]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
  fi
  # 杀掉后台 sing-box（manager 以 exec 接管 PID1 后此 trap 仍有效直到 exec，exec 后由 manager 接管 SIGTERM 转发）
  exit 0
}
trap cleanup TERM INT

# 后台启动 sing-box，记录 pid 供 manager SIGHUP 热重载
"$SINGBOX_BIN" run -c "$CONFIG_PATH" &
SB_PID=$!
echo "$SB_PID" > "$PID_FILE"
echo "[entrypoint] sing-box started pid=$SB_PID"

# 前台运行 manager（接管 PID1，便于 docker stop 正常退出）
# manager 自带 AUTO_UPDATE_INTERVAL 定时任务
exec python3 /app/manager/manager.py
