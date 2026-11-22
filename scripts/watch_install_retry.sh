#!/bin/bash
# 手表装机看门狗：v15 签名产物一出来就死磕到底（remote-pairing 会随机超时，必须靠重试撞手表的唤醒窗口）
#
# 用法：
#   scripts/watch_install_retry.sh <LifeLoggerWatch.app 路径> [设备 UDID]
#   ATTEMPTS=30 INTERVAL=15 scripts/watch_install_retry.sh ...   # 调参
#   DRY_RUN=1 scripts/watch_install_retry.sh ...                 # 只打印不装
#
# 成功判据：devicectl 退出码 0；日志尾部出现 "installed" / "Install complete"。
# 不做的事：不推断"装上了"，不因为没有回执就当成功（本机口径：设备事实只能靠安装回执）。
set -u

APP="${1:-}"
DEVICE="${2:-YOUR_WATCH_DEVICE_ID}"   # My Apple Watch (Series 5)
INTERVAL="${INTERVAL:-15}"
ATTEMPTS="${ATTEMPTS:-240}"                            # 240×15s ≈ 1 小时
DRY_RUN="${DRY_RUN:-0}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO/.planning/logs"
LOG="$LOG_DIR/watch-install-retry.log"
mkdir -p "$LOG_DIR"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

if [[ -z "$APP" ]]; then
  echo "用法: $0 <LifeLoggerWatch.app 路径> [设备 UDID]" >&2
  exit 2
fi
if [[ ! -d "$APP" ]]; then
  log "❌ 找不到产物目录: ${APP}（还没构建/签名就别起看门狗）"
  exit 2
fi

VER="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Info.plist" 2>/dev/null || echo '?')"
log "=== 装机看门狗启动：CFBundleVersion=${VER} device=${DEVICE} interval=${INTERVAL}s attempts=${ATTEMPTS} dry_run=${DRY_RUN} ==="
log "=== 日志：${LOG}"

for ((i = 1; i <= ATTEMPTS; i++)); do
  if [[ "$DRY_RUN" == "1" ]]; then
    log "[$i/$ATTEMPTS] DRY_RUN: 将要执行 xcrun devicectl device install app --device $DEVICE $APP"
    exit 0
  fi
  OUT="$(xcrun devicectl device install app --device "$DEVICE" "$APP" 2>&1)"
  if [[ $? -eq 0 ]]; then
    log "[$i/$ATTEMPTS] ✅ 装机成功 → $VER"
    printf '%s\n' "$OUT" | tail -12 | tee -a "$LOG"
    exit 0
  fi
  ERR="$(printf '%s\n' "$OUT" | grep -E 'error|Error|timed out|RemotePairing|ControlChannel' | tail -2 | tr '\n' ' ')"
  log "[$i/$ATTEMPTS] ❌ 失败：${ERR:-（无错误行，退出码非 0）}"
  sleep "$INTERVAL"
done

log "❌ $ATTEMPTS 次均失败，停手。这不是脚本坏了：手表需要在唤醒/近场状态，把它戴在手腕上解锁一次再重跑。"
exit 1
