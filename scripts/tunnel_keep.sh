#!/usr/bin/env bash
# Life Logger 公网隧道自愈监控守护脚本 (A方案: cloudflared quick tunnel)
# 功能：健康检查 -> 状态即时刷新 -> 非200自愈重启 -> 提取新URL -> 更新入口文件 -> 微信广播通知
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

LOG_DIR="$REPO_ROOT/work/remote-tunnel-20261019"
LOG_FILE="$LOG_DIR/cf.log"
# [2026-11-04 Q860] 入口文件可用 LL_TUNNEL_URL_FILE 覆盖，默认保留原值。
URL_FILE="${LL_TUNNEL_URL_FILE:-$HOME/.life-logger/TUNNEL-URL.md}"
# 告警出口：仓库内唯一实现（严格语义，支持 --json）；测试可用 LL_NOTIFY_SH 覆盖。
NOTIFY_SH="${LL_NOTIFY_SH:-$REPO_ROOT/scripts/notify.sh}"
# 通知调用必须有硬超时：通道卡死时不能把整个巡检挂住（Q181）。
# 用仓库统一的进程组超时器（超时固定 exit 124，先 TERM 再 KILL 整组）。
RUN_WITH_TIMEOUT="${LL_RUN_WITH_TIMEOUT:-$REPO_ROOT/scripts/run-with-timeout.py}"
NOTIFY_TIMEOUT="${LL_NOTIFY_TIMEOUT:-20}"
CLOUDFLARED="/opt/homebrew/bin/cloudflared"
LOCAL_TARGET="http://127.0.0.1:8765"

# 发送告警并**按真实状态**回报（2026-10-24 P1）。
# 之前这里写的是 `zsh 旧notify.sh ... || true` 紧跟一句"✅ 微信通知发送已完成"，
# 于是通知失败也打印成功、退出码永远是 0 —— 典型假成功。
# 现在的口径：
#   sent    → 打印已送达，返回 0
#   skipped → 打印已跳过（未配置/禁用），返回 0（不算故障，但**不说**成功）
#   failed  → 打印失败原因，返回 1（调用方必须据此把本次巡检判失败）
notify_send() {
    local title="$1" message="$2" out rc status reason
    if [[ ! -x "$NOTIFY_SH" ]]; then
        echo "❌ 微信通知失败：未找到可执行的 $NOTIFY_SH"
        return 1
    fi
    # 保存调用方的 errexit 状态：函数内临时关掉 -e，结束时**恢复原状**，
    # 不能无条件 `set -e`（那会把调用方的宽松上下文改成严格模式，导致脚本提前退出）。
    local prev_errexit=0
    case "$-" in *e*) prev_errexit=1 ;; esac
    set +e
    if [[ -x "$RUN_WITH_TIMEOUT" ]]; then
        # 超时器自身也会把子进程 stderr 合并进 stdout，行为与原调用一致
        out=$("$RUN_WITH_TIMEOUT" "$NOTIFY_TIMEOUT" --grace 5 -- "$NOTIFY_SH" --json "$title" "$message" 2>&1)
    else
        out=$("$NOTIFY_SH" --json "$title" "$message" 2>&1)
    fi
    rc=$?
    [[ "$prev_errexit" -eq 1 ]] && set -e || set +e
    if [[ "$rc" -eq 124 ]]; then
        echo "❌ 微信通知失败：超时（>${NOTIFY_TIMEOUT}s，已强制终止整组）"
        return 1
    fi
    status=$(printf '%s\n' "$out" | tail -n 1 | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("status", ""))
except Exception:
    print("")' 2>/dev/null || true)
    reason=$(printf '%s\n' "$out" | tail -n 1 | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("reason", ""))
except Exception:
    print("")' 2>/dev/null || true)
    ok=$(printf '%s\n' "$out" | tail -n 1 | python3 -c 'import json,sys
try:
    v = json.load(sys.stdin).get("ok")
    print("true" if v is True else ("false" if v is False else ""))
except Exception:
    print("")' 2>/dev/null || true)
    case "$status" in
        sent)
            # 【Q174】status=sent 只是"自称"。必须同时满足 rc==0 且 ok==true，
            # 否则（sent+rc1 / sent+ok:false）一律判失败 —— 绝不忽略退出码。
            if [[ "$rc" -ne 0 ]]; then
                echo "❌ 微信通知失败：notify 自称 sent 但退出码 ${rc}（不采信）"
                return 1
            fi
            if [[ "$ok" == "false" ]]; then
                echo "❌ 微信通知失败：notify 自称 sent 但 ok=false（不采信）"
                return 1
            fi
            echo "✅ 微信通知已送达（openclaw-weixin）"
            return 0 ;;
        skipped)
            echo "⏭ 微信通知已跳过：${reason:-未配置通道}"
            return 0 ;;
        "")
            # legacy 脚本：没有 JSON，只能靠 rc + 送达证据（Sent via）
            if [[ "$rc" -eq 0 ]] && printf '%s' "$out" | grep -q "Sent via"; then
                echo "✅ 微信通知已送达（legacy 脚本，Sent via 已确认）"
                return 0
            fi
            if [[ "$rc" -eq 3 ]]; then
                echo "⏭ 微信通知已跳过：${out##*$'\n'}"
                return 0
            fi
            echo "❌ 微信通知失败：notify.sh 输出无法解析（exit ${rc}）"
            return 1 ;;
        *)
            echo "❌ 微信通知失败（${status}）：${reason:-exit ${rc}}"
            return 1 ;;
    esac
}

# 测试用：只加载函数，不执行巡检（source 时生效；执行时也安全退出）。
if [[ "${LL_TUNNEL_KEEP_LIB_ONLY:-0}" == "1" ]]; then
    return 0 2>/dev/null || exit 0
fi

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
    echo "🔍 [DRY-RUN 模式] 仅执行健康检查与状态探测，不重启进程、不改文件、不发通知。"
fi

# 写入或刷新稳定入口文件
write_url_file() {
    local target_url="$1"
    local http_code="$2"
    local pid="$3"
    local edge_ok="no"
    if [[ "$http_code" == "200" ]]; then
        edge_ok="yes"
    fi
    local local_code
    local_code=$(curl -m 3 -s -o /dev/null -w "%{http_code}" "${LOCAL_TARGET}" 2>/dev/null || echo "000")

    mkdir -p "$(dirname "${URL_FILE}")"
    cat << EOF > "${URL_FILE}"
# Life Logger 公网隧道稳定入口 (Cloudflare Tunnel)

> 自动更新时间：$(date '+%Y-%m-%d %H:%M:%S %Z')

- **公网访问地址**：[${target_url}](${target_url})
- **边缘连通性**：${edge_ok} (HTTP ${http_code})
- **本地服务状态**：HTTP ${local_code}
- **本地映射目标**：\`${LOCAL_TARGET}\`
- **进程状态**：PID ${pid} (cloudflared quick tunnel)
- **管理说明**：由 \`scripts/tunnel_keep.sh\` 自愈巡检自动维护更新。
EOF
    echo "✅ 已刷新稳定入口文件: ${URL_FILE}"
}

# 1. 获取当前已知 URL
CURRENT_URL=""
if [[ -f "${URL_FILE}" ]]; then
    CURRENT_URL=$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "${URL_FILE}" | head -n 1 || true)
fi

if [[ -z "${CURRENT_URL}" && -f "${LOG_FILE}" ]]; then
    CURRENT_URL=$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "${LOG_FILE}" | tail -n 1 || true)
fi

echo "========================================================"
echo "🛰️ Life Logger 隧道自愈监控 (Cloudflare Quick Tunnel)"
echo "========================================================"
echo "当前隧道 URL: ${CURRENT_URL:-（尚未记录）}"

# 2. 对当前 URL 执行健康检查
HTTP_CODE="000"
if [[ -n "${CURRENT_URL}" ]]; then
    echo "🔍 正在进行健康检查 (curl -m 8)..."
    HTTP_CODE=$(curl -m 8 -s -o /dev/null -w "%{http_code}" "${CURRENT_URL}" 2>/dev/null || echo "000")
fi

echo "健康检查结果: HTTP ${HTTP_CODE}"

CURRENT_PID=$(pgrep -f "cloudflared.*tunnel.*8765" | head -n 1 || echo "unknown")

# 3. 如果健康码为 200，说明隧道正常
if [[ "${HTTP_CODE}" == "200" ]]; then
    echo "✅ 隧道运行正常且边缘连通良好 (HTTP 200)。"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "   [DRY-RUN] 跳过入口文件状态刷新。"
    else
        write_url_file "${CURRENT_URL}" "${HTTP_CODE}" "${CURRENT_PID}"
    fi
    exit 0
fi

# 4. 如果健康码非 200，需要自愈重启
echo "⚠️ 隧道异常 (HTTP 状态码: ${HTTP_CODE} != 200)！"

if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "--------------------------------------------------------"
    echo "🔍 [DRY-RUN 结论] 当前隧道非 200，正式运行时将："
    echo "   1) 重启 cloudflared 进程以自愈"
    echo "   2) 从 cf.log 捕获新 URL"
    echo "   3) 更新至 ${URL_FILE}"
    echo "   4) 若 URL 发生变化，调用 notify.sh 推送微信通知"
    exit 0
fi

echo "🔄 正在重启 cloudflared 触发隧道自愈..."
pkill -f "cloudflared.*tunnel.*8765" 2>/dev/null || true
sleep 1.5

mkdir -p "${LOG_DIR}"
if [[ -f "${LOG_FILE}" ]]; then
    mv "${LOG_FILE}" "${LOG_FILE}.$(date +%s).bak" 2>/dev/null || true
fi

nohup "${CLOUDFLARED}" tunnel \
    --url "${LOCAL_TARGET}" \
    --no-autoupdate \
    --logfile "${LOG_FILE}" >/dev/null 2>&1 &

NEW_PID=$!
echo "   已拉起新 cloudflared 进程 (PID: ${NEW_PID})"

echo "⏳ 正在等待分配新的 trycloudflare 边缘 URL..."
NEW_URL=""
for i in {1..20}; do
    sleep 1
    if [[ -f "${LOG_FILE}" ]]; then
        NEW_URL=$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "${LOG_FILE}" | tail -n 1 || true)
        if [[ -n "${NEW_URL}" ]]; then
            break
        fi
    fi
done

if [[ -z "${NEW_URL}" ]]; then
    echo "❌ 获取新隧道 URL 超时，请检查本地网络或 Clash 代理规则！" >&2
    exit 1
fi

echo "🎉 成功捕获新隧道 URL: ${NEW_URL}"

sleep 2
NEW_HTTP_CODE=$(curl -m 8 -s -o /dev/null -w "%{http_code}" "${NEW_URL}" 2>/dev/null || echo "000")
echo "新 URL 首次健康探测: HTTP ${NEW_HTTP_CODE}"

# 5. 更新稳定入口文件
write_url_file "${NEW_URL}" "${NEW_HTTP_CODE}" "${NEW_PID}"

# 6. 幂等检测：若 URL 发生变化，推送微信通知
if [[ "${NEW_URL}" != "${CURRENT_URL}" ]]; then
    echo "📢 检测到 URL 发生变更 (${CURRENT_URL} -> ${NEW_URL})，正在发送微信通知..."
    if ! notify_send "隧道地址已更新：${NEW_URL}" "当前健康探测：HTTP ${NEW_HTTP_CODE}（若访问超时，请在Clash中给cloudflared加直连规则）"; then
        NOTIFY_FAILED=1
    fi
else
    echo "ℹ️  URL 未发生变化 (${NEW_URL})，根据幂等原则跳过微信推送。"
fi

echo "========================================================"
echo "🎯 自愈巡检完成：当前最新 URL 为 ${NEW_URL}"
if [[ "${NOTIFY_FAILED:-0}" -ne 0 ]]; then
    echo "❌ 本次巡检的微信告警未送达（退出码 1，交由 launchd 记录失败）" >&2
    exit 1
fi
exit 0
