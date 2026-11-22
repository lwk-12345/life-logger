#!/usr/bin/env bash
# 仓库内唯一的微信告警出口（严格语义，可测）。
#
# 为什么需要它（2026-10-24 P1）：
#   agent-board 的 notify.sh 曾经「不管成败都 exit 0」，于是上层
#   verify_nightly_daily.sh 只要看到 rc=0 就打印「✅ 微信告警通知已发送」，
#   哪怕真实输出是 `sendMessage ret=-2 errmsg=prepare failed`。
#   只信退出码 = 假成功。本脚本把「成功」定义为**可验证的证据**：
#     rc==0 且 输出含 `Sent via` 且 不含 ret=- / error: / prepare failed
#
# 用法:
#   scripts/notify.sh "结论一句话" ["需要你判断：…"]
#   scripts/notify.sh --json "结论" "补充"      # 输出一行 JSON 状态，便于上层解析
#   scripts/notify.sh --dry-run "结论"          # 只打印将执行的命令，不发送
#
# 退出码:
#   0 = SENT（已确认送达）
#   1 = FAILED（发送失败/超时/输出无送达证据）
#   2 = 用法错误
#   3 = SKIPPED（未配置通道 / 没有 openclaw 可执行文件 / 明确禁用）
#
# 环境变量（测试与运维都靠它们，避免真的发消息）:
#   LL_OPENCLAW         openclaw 可执行文件路径
#   LL_NOTIFY_DOC       通道文档（解析 target/account）
#   LL_NOTIFY_TARGET    直接指定 target（优先于文档）
#   LL_NOTIFY_ACCOUNT   直接指定 account（优先于文档）
#   LL_NOTIFY_MODE      auto(默认) | disabled（禁用 → SKIPPED）
#   LL_NOTIFY_TIMEOUT   秒，默认 60（原先 20；网关微信通道实测常超 20s）
#   LL_NOTIFY_MAC_FALLBACK  1(默认)=微信失败时弹 Mac 通知；0=不弹
#   LL_OSASCRIPT        osascript 路径（测试用）
set -uo pipefail

JSON=0
DRY=0
ARGS=()
for a in "$@"; do
    case "$a" in
        --json) JSON=1 ;;
        --dry-run) DRY=1 ;;
        *) ARGS+=("$a") ;;
    esac
done
HEAD="${ARGS[0]:-}"
ASK="${ARGS[1]:-}"
CHANNEL="openclaw-weixin"

emit() {  # emit <status> <rc> <reason>
    local status="$1" rc="$2" reason="$3" ok="false"
    [[ "$status" == "sent" ]] && ok="true"
    if [[ "$JSON" -eq 1 ]]; then
        python3 - "$status" "$ok" "$rc" "$reason" "$CHANNEL" "$DRY" <<'PY'
import json, sys
status, ok, rc, reason, channel, dry = sys.argv[1:7]
print(json.dumps({
    "schema": "notify-status-v1",
    "status": status,                     # sent | failed | skipped
    "ok": ok == "true",
    "rc": int(rc),
    "reason": reason,                     # 已脱敏
    "channel": channel,
    "dry_run": dry == "1",
}, ensure_ascii=False))
PY
    else
        case "$status" in
            sent)    echo "Sent via $CHANNEL" ;;
            skipped) echo "⏭ 通知已跳过：$reason" ;;
            *)       echo "❌ 通知失败：$reason" ;;
        esac
    fi
}

# 脱敏：去掉可能的手机号/长数字、token、以及 target/account 原文
sanitize() {
    python3 - "$1" <<'PY'
import re, sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
s = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "1**********", s)
s = re.sub(r"(?i)(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{12,})", "<redacted-token>", s)
s = re.sub(r"(?i)(target|account)\s*[:=]\s*\S+", r"\1=<redacted>", s)
s = s.strip().replace("\n", " ⏎ ")
print(s[:300])
PY
}

# 微信发不出去时弹 Mac 通知，避免失败没人知道。微信结果仍如实报失败。
mac_fallback() {
    [[ "${LL_NOTIFY_MAC_FALLBACK:-1}" == "1" ]] || return 1
    "${LL_OSASCRIPT:-/usr/bin/osascript}" - "$HEAD" "$ASK" >/dev/null 2>&1 <<'APPLESCRIPT'
on run argv
    display notification (item 2 of argv) with title "Life Logger" subtitle (item 1 of argv)
end run
APPLESCRIPT
}

fail_with_fallback() {  # fail_with_fallback <rc> <reason>
    local reason="$2"
    if mac_fallback; then
        reason="${reason}；已改发 Mac 通知"
    fi
    emit failed "$1" "$(sanitize "$reason")"
    exit 1
}

if [[ -z "$HEAD" ]]; then
    emit failed 2 "$(sanitize "usage: notify.sh [--json] [--dry-run] \"结论\" [\"补充\"]")"
    exit 2
fi

if [[ "${LL_NOTIFY_MODE:-auto}" == "disabled" ]]; then
    emit skipped 3 "LL_NOTIFY_MODE=disabled（显式禁用）"
    exit 3
fi

# 通道配置：env 优先，其次从文档解析
TARGET="${LL_NOTIFY_TARGET:-}"
ACCOUNT="${LL_NOTIFY_ACCOUNT:-}"
DOC="${LL_NOTIFY_DOC:-$HOME/.life-logger/NOTIFY-CHANNEL.md}"
if [[ -z "$TARGET" || -z "$ACCOUNT" ]]; then
    if [[ -r "$DOC" ]]; then
        [[ -z "$TARGET" ]] && TARGET=$(grep -o "target '[^']*'" "$DOC" 2>/dev/null | head -1 | sed "s/target '//;s/'$//")
        [[ -z "$ACCOUNT" ]] && ACCOUNT=$(grep -o "account '[^']*'" "$DOC" 2>/dev/null | head -1 | sed "s/account '//;s/'$//")
    fi
fi
if [[ -z "$TARGET" || -z "$ACCOUNT" ]]; then
    emit skipped 3 "未配置通道（缺 target/account，且 $DOC 不可读）"
    exit 3
fi

OC="${LL_OPENCLAW:-$HOME/.local/node/bin/openclaw}"
# launchd 的 PATH 里没有 node，openclaw 的 `env node` 会报 127。node 和 openclaw 装在同一目录。
export PATH="$(dirname "$OC"):${PATH:-/usr/bin:/bin}"
if [[ ! -x "$OC" ]]; then
    emit skipped 3 "openclaw 不存在或不可执行"
    exit 3
fi

BODY="🤖 DSH｜$HEAD"
[[ -n "$ASK" ]] && BODY="$BODY
$ASK"

if [[ "$DRY" -eq 1 ]]; then
    "$OC" message send --channel "$CHANNEL" --target "$TARGET" --account "$ACCOUNT" -m "$BODY" --dry-run
    rc=$?
    # 【Q174】dry-run 没有真的发出去，所以永远不是 sent：
    # 状态 skipped、退出码 3、理由写明"未真实发送"。命令本身失败则报 failed。
    if [[ "$rc" -ne 0 ]]; then
        emit failed "$rc" "$(sanitize "dry-run 命令失败（退出码 ${rc}）")"
        exit 1
    fi
    emit skipped 3 "dry-run：未真实发送"
    exit 3
fi

# [2026-11-06] 默认 20s 太短：openclaw 网关微信通道实测可超过 20s（daily-report-deliver.js
# 对同一条通道给的是 90s）。这里提到 60s，并对超时重试一次。
TIMEOUT="${LL_NOTIFY_TIMEOUT:-60}"
RETRY_ON_TIMEOUT="${LL_NOTIFY_RETRY_ON_TIMEOUT:-1}"
OUT_FILE=$(mktemp -t notify-out.XXXXXX)
trap 'rm -f "$OUT_FILE"' EXIT

# 硬超时：用 python3 包一层，避免 perl/SIGALRM 把 "Alarm clock" 打到 stderr 污染日志
python3 - "$TIMEOUT" "$OUT_FILE" "$OC" "$CHANNEL" "$TARGET" "$ACCOUNT" "$BODY" <<'PYCHILD'
import subprocess, sys
timeout, out_path, oc, channel, target, account, body = sys.argv[1:8]
try:
    r = subprocess.run([oc, "message", "send", "--channel", channel, "--target", target,
                        "--account", account, "-m", body],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=float(timeout))
    open(out_path, "wb").write(r.stdout)
    sys.exit(r.returncode if r.returncode >= 0 else 1)
except subprocess.TimeoutExpired as exc:
    open(out_path, "wb").write(exc.output or b"")
    sys.exit(124)
PYCHILD
rc=$?
if [[ "$rc" -eq 124 && "$RETRY_ON_TIMEOUT" == "1" ]]; then
    # 网关偶发抖动：同样的 20s 失败可能只是排队，重试一次（总上限 2 倍超时）
    : > "$OUT_FILE"
    python3 - "$TIMEOUT" "$OUT_FILE" "$OC" "$CHANNEL" "$TARGET" "$ACCOUNT" "$BODY" <<'PYCHILD2'
import subprocess, sys
timeout, out_path, oc, channel, target, account, body = sys.argv[1:8]
try:
    r = subprocess.run([oc, "message", "send", "--channel", channel, "--target", target,
                        "--account", account, "-m", body],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=float(timeout))
    open(out_path, "wb").write(r.stdout)
    sys.exit(r.returncode if r.returncode >= 0 else 1)
except subprocess.TimeoutExpired as exc:
    open(out_path, "wb").write(exc.output or b"")
    sys.exit(124)
PYCHILD2
    rc=$?
fi
if [[ "$rc" -eq 124 ]]; then
    fail_with_fallback 124 "发送超时（>${TIMEOUT}s，已重试一次）"
fi

OUT=$(cat "$OUT_FILE" 2>/dev/null || true)
# 失败证据优先于 rc：即使 rc=0，只要出现这些字样就不算送达
if printf '%s' "$OUT" | grep -qE 'ret=-|errmsg=|prepare failed|error:'; then
    evidence=$(printf '%s' "$OUT" | grep -E 'ret=-|errmsg=|prepare failed|error:' | tail -1 | cut -c1-200)
    fail_with_fallback "${rc}" "输出自证失败：${evidence:-unknown}"
fi
if [[ "$rc" -ne 0 ]]; then
    fail_with_fallback "$rc" "openclaw 退出码 ${rc}：$(printf '%s' "$OUT" | tail -1)"
fi
if ! printf '%s' "$OUT" | grep -q "Sent via"; then
    fail_with_fallback 1 "rc=0 但输出无送达证据（缺 Sent via）：$(printf '%s' "$OUT" | tail -1)"
fi

emit sent 0 "已确认送达（Sent via ${CHANNEL}）"
exit 0
