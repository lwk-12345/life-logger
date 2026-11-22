#!/usr/bin/env bash
# Apple Watch 描述文件 7天到期预警脚本（在剩余不足 24 小时时发送系统通知并写日志）
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

LOG_FILE="/tmp/watch-profile-reminder.log"
PROFILE_DIR="$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles"
ACTIVE_PROFILE=$(find "$PROFILE_DIR" -name "*.mobileprovision" -type f 2>/dev/null | head -n 1 || true)

TEST_MODE=0
if [[ "${1:-}" == "--test" || "${1:-}" == "--force" ]]; then
    TEST_MODE=1
fi

NOW_ISO=$(date '+%Y-%m-%d %H:%M:%S %Z')

if [[ -z "$ACTIVE_PROFILE" || ! -f "$ACTIVE_PROFILE" ]]; then
    echo "[$NOW_ISO] ⚠️ 未找到有效的描述文件" | tee -a "$LOG_FILE"
    exit 0
fi

EXP_ISO=$(security cms -D -i "$ACTIVE_PROFILE" 2>/dev/null | grep -A 1 "<key>ExpirationDate</key>" | grep -oE "<date>.*</date>" | sed -e 's/<date>//' -e 's/<\/date>//' || echo "")
if [[ -z "$EXP_ISO" ]]; then
    echo "[$NOW_ISO] ⚠️ 无法解析描述文件到期时间" | tee -a "$LOG_FILE"
    exit 0
fi

EXP_EPOCH=$(date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$EXP_ISO" "+%s" 2>/dev/null || date -j -f "%Y-%m-%dT%H:%M:%SZ" "$EXP_ISO" "+%s" 2>/dev/null || echo 0)
NOW_EPOCH=$(date "+%s")
DIFF_SEC=$(( EXP_EPOCH - NOW_EPOCH ))
DIFF_HOURS=$(( DIFF_SEC / 3600 ))

echo "[$NOW_ISO] ℹ️  检查完成：到期时间 UTC ${EXP_ISO}，距离到期还剩 ${DIFF_HOURS} 小时" | tee -a "$LOG_FILE"

# 正常情况：剩余不足 24 小时（到期前 1 天）触发预警
if [[ "$DIFF_SEC" -le 86400 && "$DIFF_SEC" -gt 0 ]] || [[ "$TEST_MODE" -eq 1 ]]; then
    MSG="Life Logger Watch 描述文件将在 ${DIFF_HOURS} 小时后过期，请执行 scripts/watch_renew.sh 自动续签！"
    echo "[$NOW_ISO] ⚠️ 触发到期预警通知: $MSG" | tee -a "$LOG_FILE"
    osascript -e "display notification \"$MSG\" with title \"Life Logger 签名到期预警\" sound name \"Submarine\"" 2>/dev/null || true
elif [[ "$DIFF_SEC" -le 0 ]]; then
    MSG="Life Logger Watch 描述文件已过期，请立即执行 scripts/watch_renew.sh 刷新恢复使用！"
    echo "[$NOW_ISO] 🚨 触发过期告警通知: $MSG" | tee -a "$LOG_FILE"
    osascript -e "display notification \"$MSG\" with title \"Life Logger 签名已过期\" sound name \"Basso\"" 2>/dev/null || true
fi
