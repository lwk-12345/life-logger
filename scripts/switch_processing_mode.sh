#!/bin/bash
# 切换网页和后台处理的运行方式，失败时自动退回 combined。
#   split     Web 只管读，转写、抽取、时间轴等由独立 Worker 跑
#   combined  回到全部在 Web 进程内的旧方式
#   restart   两个进程一起重启，让改动过的后端代码生效
#   status    查看当前状态
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
MODE_FILE="output/local-web/runtime/processing-mode"
WEB="com.life-logger.local-web"
WORKER="com.life-logger.processing-worker"
PLIST="$HOME/Library/LaunchAgents/$WORKER.plist"
DOMAIN="gui/$(id -u)"

write_plist() {
  python3 - "$PLIST" "$ROOT" "$HOME/Library/LaunchAgents/$WEB.plist" <<'PY'
import os, plistlib, sys
target, root, web = sys.argv[1:4]
env = plistlib.load(open(web, "rb")).get("EnvironmentVariables", {})
payload = {
    "Label": "com.life-logger.processing-worker",
    "ProgramArguments": [os.path.join(root, "run-life-logger-worker.command")],
    "WorkingDirectory": root,
    "EnvironmentVariables": env,
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ThrottleInterval": 5,
    "ProcessType": "Background",
    "StandardOutPath": os.path.join(root, "output/local-web/runtime/worker.log"),
    "StandardErrorPath": os.path.join(root, "output/local-web/runtime/worker.log"),
}
with open(target, "wb") as handle:
    plistlib.dump(payload, handle)
PY
}

web_ready() {
  for _ in $(seq 1 90); do
    [[ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/)" == "200" ]] && return 0
    sleep 2
  done
  return 1
}

worker_online() {
  PYTHONPATH=src python3 - <<'PY'
import sys
from life_logger.runtime_queue import TaskQueue
sys.exit(0 if TaskQueue("output/local-web").health().get("online") else 1)
PY
}

wait_worker() {
  for _ in $(seq 1 45); do
    worker_online && return 0
    sleep 2
  done
  return 1
}

worker_loaded() { launchctl print "$DOMAIN/$WORKER" >/dev/null 2>&1; }

stop_worker() {
  worker_loaded || return 0
  launchctl bootout "$DOMAIN/$WORKER" 2>/dev/null || true
  for _ in $(seq 1 30); do
    worker_loaded || return 0
    sleep 1
  done
}

to_combined() {
  stop_worker
  printf 'combined\n' > "$MODE_FILE"
  launchctl kickstart -k "$DOMAIN/$WEB"
  web_ready || { echo "❌ 回到 combined 后网页未就绪" >&2; return 1; }
  echo "✅ 已回到 combined"
}

to_split() {
  printf 'split\n' > "$MODE_FILE"
  launchctl kickstart -k "$DOMAIN/$WEB"
  if ! web_ready; then
    echo "❌ 网页未就绪，退回 combined" >&2
    to_combined
    return 1
  fi
  write_plist
  stop_worker
  launchctl bootstrap "$DOMAIN" "$PLIST"
  if ! wait_worker; then
    echo "❌ Worker 未上线，退回 combined" >&2
    to_combined
    return 1
  fi
  echo "✅ 已切到 split：Web 和 Worker 都在线"
}

case "${1:-status}" in
  split) to_split ;;
  combined) to_combined ;;
  restart)
    if [[ "$(cat "$MODE_FILE" 2>/dev/null)" == "split" ]]; then
      launchctl kickstart -k "$DOMAIN/$WEB" && web_ready && launchctl kickstart -k "$DOMAIN/$WORKER" && wait_worker && echo "✅ Web 和 Worker 已重启"
    else
      launchctl kickstart -k "$DOMAIN/$WEB" && web_ready && echo "✅ Web 已重启"
    fi
    ;;
  status)
    echo "mode=$(cat "$MODE_FILE" 2>/dev/null || echo combined)"
    launchctl list | awk -v a="$WEB" -v b="$WORKER" '$3==a||$3==b{print $3,"pid="$1,"last_exit="$2}'
    worker_online && echo "worker=online" || echo "worker=offline"
    ;;
  *) echo "用法: $0 split|combined|restart|status" >&2; exit 2 ;;
esac
