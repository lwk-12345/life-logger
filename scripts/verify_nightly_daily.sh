#!/usr/bin/env bash
# 验证日终日报是否由定时器自动产出 (非手补) 及账本一致性
# 支持: --date YYYY-MM-DD (默认昨天)
# 支持: --dry-run (仅自检输出，不推微信)
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# 账本校验要 import life_logger（依赖 numpy 等）。PATH 里第一个 python3 是 homebrew 的，
# 没有这些依赖，会让校验脚本一启动就崩，曾被连续报成“账本不一致”。
LL_PY="${LL_PYTHON:-/opt/anaconda3/bin/python3}"
[[ -x "$LL_PY" ]] || LL_PY="python3"

# 通知出口：
#   1) 优先用仓库内 scripts/notify.sh --json（唯一真源，输出机器可读状态）
#   2) 兼容旧路径：LL_NOTIFY_SH 指向外部 notify.sh（仅测试/迁移期使用）
NOTIFY_SH="${LL_NOTIFY_SH:-}"
# 通知结果（由 run_notify 填写）：state ∈ sent | skipped | failed
NOTIFY_STATE=""
NOTIFY_RC=0
NOTIFY_ERR=""

# 脱敏：手机号、token、target/account 原文
sanitize_text() {
    python3 - "$1" <<'PYSAN'
import re, sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
s = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "1**********", s)
s = re.sub(r"(?i)(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{12,})", "<redacted-token>", s)
s = re.sub(r"(?i)(target|account)\s*[:=]\s*\S+", r"\1=<redacted>", s)
print(s.strip().replace("\n", " ⏎ ")[:300])
PYSAN
}

# 【2026-10-24 P1】只信退出码是错的：
#   agent-board notify.sh 曾「不管成败都 exit 0」，真实输出 `sendMessage ret=-2 errmsg=prepare failed`，
#   上层却打印「✅ 微信告警通知已发送」。现在成功必须有**可验证证据**（JSON status=sent 或输出含 Sent via），
#   且失败证据（ret=- / errmsg= / prepare failed / error:）优先于退出码。
run_notify() {
    local head="$1" body="$2"
    NOTIFY_STATE=""; NOTIFY_RC=0; NOTIFY_ERR=""

    local -a cmd
    # LL_NOTIFY_SH 一旦显式设置就只认它（不存在=失败 127，**绝不悄悄回落到真实通道**，否则测试会真发消息）
    if [[ -n "$NOTIFY_SH" ]]; then
        if [[ -f "$NOTIFY_SH" ]]; then
            cmd=(/bin/zsh "$NOTIFY_SH")
        else
            NOTIFY_STATE="failed"; NOTIFY_RC=127
            NOTIFY_ERR="通知脚本不存在（rc=127）: $NOTIFY_SH"
            return 1
        fi
    elif [[ -x "$REPO_ROOT/scripts/notify.sh" ]]; then
        cmd=("$REPO_ROOT/scripts/notify.sh" --json)
    else
        NOTIFY_STATE="skipped"; NOTIFY_RC=3
        NOTIFY_ERR="没有可用的通知脚本（scripts/notify.sh 缺失且未设 LL_NOTIFY_SH）"
        return 0
    fi

    set +e
    local out rc
    out=$("${cmd[@]}" "$head" "$body" 2>&1)
    rc=$?
    set -e
    NOTIFY_RC=$rc

    local json_line=""
    json_line=$(printf '%s\n' "$out" | grep -E '^\{.*"status"' | tail -1 || true)
    if [[ -n "$json_line" ]]; then
        local parsed
        parsed=$(printf '%s' "$json_line" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
print(d.get("status","")); print(d.get("reason",""))' 2>/dev/null || true)
        NOTIFY_STATE=$(printf '%s' "$parsed" | sed -n '1p')
        local reason; reason=$(printf '%s' "$parsed" | sed -n '2,$p')
        NOTIFY_ERR=$(sanitize_text "${reason:-unknown}")
    elif printf '%s' "$out" | grep -qE 'ret=-|errmsg=|prepare failed|error:'; then
        local evidence
        evidence=$(printf '%s' "$out" | grep -E 'ret=-|errmsg=|prepare failed|error:' | tail -1 | cut -c1-200)
        NOTIFY_STATE="failed"
        NOTIFY_ERR=$(sanitize_text "输出自证失败：${evidence:-unknown}")
    elif [[ "$rc" -eq 0 ]] && printf '%s' "$out" | grep -q "Sent via"; then
        NOTIFY_STATE="sent"; NOTIFY_ERR=""
    elif [[ "$rc" -eq 3 ]]; then
        NOTIFY_STATE="skipped"
        NOTIFY_ERR=$(sanitize_text "$(printf '%s' "$out" | tail -1)")
    else
        NOTIFY_STATE="failed"
        NOTIFY_ERR=$(sanitize_text "rc=${rc}：$(printf '%s' "$out" | tail -1)")
    fi

    case "$NOTIFY_STATE" in
        sent|skipped) return 0 ;;
        *) NOTIFY_STATE="failed"; [[ -z "$NOTIFY_ERR" ]] && NOTIFY_ERR="未知失败（rc=${rc}）"; return 1 ;;
    esac
}

# 测试用判决覆盖（生产不设此变量）：LL_FORCE_VERDICT=pass|fail
# 目的：让 notify 的 success/failure/dry-run 四条路径可被 pytest 确定性覆盖，
# 不必依赖「某天数据恰好 PASS/FAIL」。
LL_FORCE_VERDICT="${LL_FORCE_VERDICT:-}"
TARGET_DATE=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --date)
            TARGET_DATE="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        *)
            echo "未知参数: $1" >&2
            exit 2
            ;;
    esac
done

if [[ -z "$TARGET_DATE" ]]; then
    # 默认检查昨天 (上海时区)
    TARGET_DATE=$(TZ=Asia/Shanghai date -v-1d '+%Y-%m-%d' 2>/dev/null || TZ=Asia/Shanghai date -d 'yesterday' '+%Y-%m-%d')
fi

echo "========================================================"
echo "🕵️ 日终日报定时器自动生成自检 (Target: $TARGET_DATE)"
echo "========================================================"

# 执行核心 Python 判据分析
# 【测试缝】LL_FORCE_VERDICT 已设时跳过真实判据分析（生产不设该变量，必走下面这段）
if [[ -n "$LL_FORCE_VERDICT" ]]; then
    PY_RESULT='{"date":"'"$TARGET_DATE"'","crit_a":true,"crit_b":true,"crit_c":true,"amount":0,"event_count":0,"b_timestamp":null,"b_msg":"(forced)","c_msg":"(forced)","failures":[]}'
else
PY_RESULT=$(python3 - "$REPO_ROOT" "$TARGET_DATE" "$LL_PY" << 'EOF'
import json, sys, subprocess
from pathlib import Path
from datetime import datetime

repo_root = Path(sys.argv[1])
target_date = sys.argv[2]

res = {
    "date": target_date,
    "crit_a": False,
    "crit_b": False,
    "crit_c": False,
    "amount": None,
    "event_count": None,
    "b_timestamp": None,
    "b_msg": "",
    "c_msg": "",
    "failures": []
}

# 1. 判据 A: synthesis-<date>.json 是否存在
synth_file = repo_root / "output/local-web/daily-synthesis" / f"synthesis-{target_date}.json"
if synth_file.exists():
    res["crit_a"] = True
    try:
        data = json.loads(synth_file.read_text(encoding="utf-8"))
        res["amount"] = data.get("total_amount") or data.get("expense_ledger", {}).get("total_amount")
        res["event_count"] = data.get("event_count")
    except Exception as e:
        pass
else:
    res["failures"].append("判据A: 产物文件缺失 (synthesis-" + target_date + ".json 不存在)")

# 2. 判据 B: daily-scheduler.log 中是否存在 result=OK 且时间在 01:20-02:40 之间
log_file = repo_root / "output/local-web/daily-scheduler.log"
found_ok = False
b_reason = "无 OK 记录"
if log_file.exists():
    for line in log_file.read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        try:
            d = json.loads(line)
            if d.get("date") == target_date and d.get("result") == "OK":
                ts_str = d.get("timestamp", "")
                dt = datetime.fromisoformat(ts_str)
                minutes = dt.hour * 60 + dt.minute
                # 01:20 (80m) 至 02:40 (160m)
                if 80 <= minutes <= 160:
                    found_ok = True
                    res["b_timestamp"] = ts_str
                    b_reason = f"在定时器窗口内生成 ({dt.strftime('%H:%M:%S')})"
                    break
                else:
                    b_reason = f"OK 记录时间不在 01:20-02:40 窗口: {ts_str} ({dt.strftime('%H:%M:%S')})"
        except Exception:
            pass

if found_ok:
    res["crit_b"] = True
    res["b_msg"] = b_reason
else:
    res["failures"].append(f"判据B: 定时器未在 01:20-02:40 窗口生成 ({b_reason})")
    res["b_msg"] = b_reason

# 3. 判据 C: verify_ledger_consistency.py 5 条规则是否全 PASS
cmd = [sys.argv[3], str(repo_root / "scripts/verify_ledger_consistency.py"), "--from", target_date, "--to", target_date]
sub = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
if sub.returncode == 0:
    res["crit_c"] = True
    # [2026-11-06] STALE（日报过期）不算失败，但要在判据 C 里说清楚，
    # 否则「全部 PASS」会掩盖「日报生成后用户又补录了事件」这件事。
    stale_lines = [ln.strip() for ln in sub.stdout.splitlines() if "STALE" in ln]
    if stale_lines:
        res["c_msg"] = "日报过期（账本正确，已不算失败）: " + " / ".join(stale_lines[:2])
        res["c_stale"] = True
    else:
        res["c_msg"] = "5 项规则全部 PASS"
elif "汇总:" not in sub.stdout:
    # 没走到汇总就退出，是校验本身没跑起来，不能说成账本不一致。
    last_line = (sub.stdout.strip().splitlines() or ["无输出"])[-1].strip()
    res["failures"].append(f"判据C: 校验脚本没跑起来 ({last_line})")
    res["c_msg"] = f"校验脚本没跑起来: {last_line}"
else:
    res["failures"].append("判据C: 账本一致性校验失败 (5条规则未全过)")
    first_fail_line = ""
    for line in sub.stdout.splitlines():
        if "FAIL" in line or "↳" in line:
            first_fail_line = line.strip()
            break
    res["c_msg"] = first_fail_line or "存在不一致"

print(json.dumps(res, ensure_ascii=False))
EOF
)
fi

CRIT_A=$(echo "$PY_RESULT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("PASS" if d["crit_a"] else "FAIL")')
CRIT_B=$(echo "$PY_RESULT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("PASS" if d["crit_b"] else "FAIL")')
CRIT_C=$(echo "$PY_RESULT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("PASS" if d["crit_c"] else "FAIL")')
AMOUNT=$(echo "$PY_RESULT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("amount") if d.get("amount") is not None else "?")')
COUNT=$(echo "$PY_RESULT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("event_count") if d.get("event_count") is not None else "?")')
B_MSG=$(echo "$PY_RESULT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("b_msg"))')
C_MSG=$(echo "$PY_RESULT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("c_msg"))')
FAILURES=$(echo "$PY_RESULT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("；".join(d.get("failures", [])))')

echo "判据 A (产物存在): $CRIT_A"
echo "判据 B (定时窗口 01:20-02:40): $CRIT_B -> $B_MSG"
echo "判据 C (账本一致性 5条全过): $CRIT_C -> $C_MSG"

ALL_PASS=0
if [[ "$CRIT_A" == "PASS" && "$CRIT_B" == "PASS" && "$CRIT_C" == "PASS" ]]; then
    ALL_PASS=1
fi
case "$LL_FORCE_VERDICT" in
    pass) ALL_PASS=1; echo "[测试] LL_FORCE_VERDICT=pass（仅测试用）" ;;
    fail) ALL_PASS=0; FAILURES="${FAILURES:-测试强制失败}"; echo "[测试] LL_FORCE_VERDICT=fail（仅测试用）" ;;
    "") : ;;
    *) echo "未知 LL_FORCE_VERDICT=${LL_FORCE_VERDICT}（只接受 pass/fail）" >&2; exit 2 ;;
esac

echo "--------------------------------------------------------"

if [[ "$ALL_PASS" -eq 1 ]]; then
    HEAD_MSG="日终日报自动生成 ✅ ${TARGET_DATE} ¥${AMOUNT} / ${COUNT} 条"
    BODY_MSG="产物在位 / 定时窗口命中 ($B_MSG) / 账本 5 项规则全 PASS"
    echo "结论: $HEAD_MSG"
    echo "明细: $BODY_MSG"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "🔍 [DRY-RUN] 跳过微信推送: $HEAD_MSG"
        exit 0
    fi
    run_notify "$HEAD_MSG" "$BODY_MSG" || true
    case "$NOTIFY_STATE" in
        sent)
            echo "✅ 微信通知已发送（Sent via 已确认）"
            exit 0
            ;;
        skipped)
            echo "⏭ 微信通知已跳过（未配置通道 / 明确禁用）：${NOTIFY_ERR}"
            echo "   验证结论是 PASS，但通知通道不可用 → 退出码 0，且**不算已发送**"
            exit 0
            ;;
        *)
            echo "❌ 微信通知失败（${NOTIFY_ERR}）"
            echo "   验证结论本身是 PASS，但通知没发出去 → 退出码 4（不再谎报成功）"
            exit 4
            ;;
    esac
else
    HEAD_MSG="日终日报自动生成 ❌ ${TARGET_DATE} 验证失败"
    BODY_MSG="未通过项: ${FAILURES}"
    echo "结论: $HEAD_MSG"
    echo "明细: $BODY_MSG"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "🔍 [DRY-RUN] 跳过微信推送: $HEAD_MSG"
        exit 1
    fi
    run_notify "$HEAD_MSG" "$BODY_MSG" || true
    case "$NOTIFY_STATE" in
        sent)    echo "✅ 微信告警通知已发送（Sent via 已确认）" ;;
        skipped) echo "⏭ 微信告警已跳过（未配置通道）：${NOTIFY_ERR} —— 验证失败这件事没通知出去" ;;
        *)       echo "❌ 微信通知失败（${NOTIFY_ERR}）—— 验证失败这件事也没通知出去" ;;
    esac
    exit 1
fi
