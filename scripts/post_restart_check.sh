#!/usr/bin/env bash
# post_restart_check.sh —— 重启后一条命令回答「新代码确实加载了 + 页面确实能用」
#
# 只读脚本：不重启、不 kill、不 POST、不 DELETE、不删任何生产路径。
# 退出码：0=全 PASS；1=有 FAIL；2=参数错；3=有 SKIPPED/INCOMPLETE 但无 FAIL
#
# 用法：
#   scripts/post_restart_check.sh [--expect-pid-different 85038] \
#     [--base-url http://127.0.0.1:8765] [--token-file output/local-web/secrets.json] \
#     [--json-out PATH] [--log PATH] [--audio-tolerance-min 0] [--big-static PATH] [--dates 2026-10-26,...] [--dry-run]

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BASE_URL="http://127.0.0.1:8765"
TOKEN_FILE="$REPO_ROOT/output/local-web/secrets.json"
JSON_OUT=""
LOG_PATH=""
EXPECT_PID=""
AUDIO_TOL="0"
BIG_STATIC=""
DATES_OVERRIDE=""
DATES="2026-10-26,2026-10-27,2026-10-28,2026-10-29"
# [2026-11-01 修复] R6 原来不传 --date，页面默认"今天"：当日无数据或上游提取失败时
# 会被判成确定性故障（API status=error），让整个重启门禁假 FAIL。
# 改为钉死一个有数据的基线日（默认取 DATES 的第一天），门禁可重复。
R6_DATE=""
DRY_RUN=0

usage() {
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# [2026-10-29 修复] 缺值选项必须显式判 `$#`：
# 旧写法 `VAR="${2:-}"; shift 2` 在 `$#` 只剩 1 时 shift 失败但位置参数不退，
# `while [ $# -gt 0 ]` 永不收敛（实测 --base-url 无值 → 22 万行/秒刷屏、永不退出）。
need_value() {   # $1=选项名  $2=调用点当前的 $#（必须显式传，函数内 $# 只有 2）
  if [ "${2:-0}" -lt 2 ]; then
    echo "参数错误：$1 需要一个值（用法：$1 <value>）" >&2
    exit 2
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --base-url) need_value "$1" "$#"; BASE_URL="$2"; shift 2 ;;
    --token-file) need_value "$1" "$#"; TOKEN_FILE="$2"; shift 2 ;;
    --json-out) need_value "$1" "$#"; JSON_OUT="$2"; shift 2 ;;
    --log) need_value "$1" "$#"; LOG_PATH="$2"; shift 2 ;;
    --expect-pid-different) need_value "$1" "$#"; EXPECT_PID="$2"; shift 2 ;;
    --audio-tolerance-min) need_value "$1" "$#"; AUDIO_TOL="$2"; shift 2 ;;
    --big-static) need_value "$1" "$#"; BIG_STATIC="$2"; shift 2 ;;
    --dates) need_value "$1" "$#"; DATES_OVERRIDE="$2"; shift 2 ;;
    --r6-date) need_value "$1" "$#"; R6_DATE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "参数错误：未知参数 $1" >&2; usage >&2; exit 2 ;;
  esac
done

# 参数校验（2 = 参数错）
case "$AUDIO_TOL" in
  ''|*[!0-9.]*) echo "参数错误：--audio-tolerance-min 必须是非负数字（收到 '$AUDIO_TOL'）" >&2; exit 2 ;;
esac
if [ -z "$BASE_URL" ]; then echo "参数错误：--base-url 不能为空" >&2; exit 2; fi
if [ -n "$DATES_OVERRIDE" ]; then DATES="$DATES_OVERRIDE"; fi
if [ -z "$DATES" ]; then echo "参数错误：--dates 不能为空" >&2; exit 2; fi
# R6 日期：显式 --r6-date 优先，否则取 --dates 的第一天
if [ -z "$R6_DATE" ]; then R6_DATE="${DATES%%,*}"; fi
case "$R6_DATE" in
  [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
  *) echo "参数错误：--r6-date 格式不对（$R6_DATE，应为 YYYY-MM-DD）" >&2; exit 2 ;;
esac
IFS=',' read -r -a DATE_LIST <<< "$DATES"
for _d in "${DATE_LIST[@]}"; do
  case "$_d" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
    *) echo "参数错误：--dates 里的日期格式不对（$_d，应为 YYYY-MM-DD）" >&2; exit 2 ;;
  esac
done
unset _d

FAILS=()
SKIPS=()
declare -a RESULTS   # "R1|PASS|说明"
JSON_ITEMS=()

pass() { RESULTS+=("$1|PASS|$2"); printf '  [PASS] %-3s %s\n' "$1" "$2"; }
fail() { RESULTS+=("$1|FAIL|$2"); FAILS+=("$1"); printf '  [FAIL] %-3s %s\n' "$1" "$2"; }
skip() { RESULTS+=("$1|SKIPPED|$2"); SKIPS+=("$1"); printf '  [SKIP] %-3s %s\n' "$1" "$2"; }
info() { printf '         %s\n' "$*"; }

TMPDIR_LOCAL="$(mktemp -d /tmp/post_restart_check.XXXXXX)" || { echo "无法创建临时目录" >&2; exit 1; }
trap 'rm -rf "$TMPDIR_LOCAL"' EXIT

emit_log() {
  if [ -n "$LOG_PATH" ]; then
    mkdir -p "$(dirname "$LOG_PATH")" 2>/dev/null || true
    printf '%s\n' "$@" >> "$LOG_PATH"
  fi
}

echo "=== post_restart_check.sh ==="
echo "base_url   : $BASE_URL"
echo "token_file : $TOKEN_FILE"
echo "repo_root  : $REPO_ROOT"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "模式       : DRY-RUN（不发任何请求）"
else
  echo "模式       : 实跑"
fi
echo ""

if [ "$DRY_RUN" -eq 1 ]; then
  cat <<EOF
将执行：
  R1  读取 \$BASE_URL/api/version → 判 deploy_state == current 且 unloaded_files 为空
  R2  pid 与 --expect-pid-different 比对（本次参数：${EXPECT_PID:-<未传，只打印>}）
  R3  curl \$BASE_URL/api/jobs 与 ${BIG_STATIC:-<未指定大静态>} → 比对 size_download 与 Content-Length，并判 curl 退出码
  R4  循环 2026-10-26..29 打 /api/dashboard/daily → 从 dashboard 子对象取 scenes/audio/updated_at，与基线比对（容差 ${AUDIO_TOL}min）
  R5  /api/stats /api/daily /api/daily-brief 各取一天 → 200 且 JSON 可解析
  R6  node playwright 访问 \$BASE_URL/?token=<token> → blocks>0 且 console/pageerror/401 全 0
  R7  音频 Range 抽查（只看音频扩展名 .m4a/.wav/.caf/.mp3/.aac/.flac/.ogg 且 >=400KB）→ 206 且 Content-Range 合法；无音频 → SKIPPED
  R8  部署双证据：/api/version 必须同时给出 pid 与 deploy_state；deploy_state 非 current / unloaded_files 非空 / 缺字段 → FAIL
  R9  多日期 dashboard 200 且响应体非空（本次：${DATES}）
  R10 静态资源完整性：/ 与它引用的 /app.js → 200 + curl rc=0 + size_download == Content-Length
  R11 期望值 0 护栏：R4 中某日期期望 scenes=0 且实测=0 → SKIPPED 并告警（防未来假 PASS）
结论：
  DRY-RUN
EOF
  emit_log "DRY-RUN base_url=$BASE_URL"
  exit 0
fi

# ── token ────────────────────────────────────────────────
# token 文件分两类（2026-10-29 修复）：
#   ① 文件本身不可用（不存在 / 不是普通文件 / 不可读）→ **参数错，exit 2**（调用者给错了路径）
#   ② 文件可读但解析不出 mobile_access_token → **环境态问题，exit 1**（部署/配置不对，不是用法错）
if [ ! -e "$TOKEN_FILE" ] || [ ! -f "$TOKEN_FILE" ] || [ ! -r "$TOKEN_FILE" ]; then
  echo "参数错误：--token-file 指向的文件不可用（$TOKEN_FILE 不存在/不是普通文件/不可读）" >&2
  echo "结论: 参数错误 (exit 2) — 未执行任何检查" >&2
  exit 2
fi
TOKEN="$(python3 -c '
import json,sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("mobile_access_token") or "")
except Exception:
    print("")
' "$TOKEN_FILE" 2>/dev/null)"
if [ -z "$TOKEN" ]; then
  echo "  [FAIL] '$TOKEN_FILE' 可读，但解析不到 mobile_access_token（字段缺失/为空/不是合法 JSON）" >&2
  echo "结论: FAIL (exit 1) — 环境态问题（令牌文件内容不对），后续只读检查均未执行" >&2
  exit 1
fi

AUTH=(-H "Authorization: Bearer $TOKEN")

# 连通性预检：连不上直接 exit 1（不当成崩溃）
if ! curl -sS --max-time 8 -o /dev/null "${AUTH[@]}" "$BASE_URL/api/version" 2>"$TMPDIR_LOCAL/conn.err"; then
  echo "  [FAIL] 服务不可达：$BASE_URL ($(tr '\n' ' ' < "$TMPDIR_LOCAL/conn.err" | head -c 200))" >&2
  echo "结论: FAIL (exit 1) — base_url 指向的服务没有响应" >&2
  exit 1
fi

# ── R1 · 部署态 ──────────────────────────────────────────
echo "── R1 · 部署态 ──"
curl -sS --max-time 20 -o "$TMPDIR_LOCAL/version.json" "${AUTH[@]}" "$BASE_URL/api/version" || true
R1_OUT="$(python3 - "$TMPDIR_LOCAL/version.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    print("PARSE_FAIL|%s" % exc); raise SystemExit(0)
state = d.get("deploy_state")
unl = d.get("unloaded_files") or []
fields = "pid=%s deployed_at=%s code_fingerprint=%s git_commit=%s app_version=%s" % (
    d.get("pid"), d.get("deployed_at"), d.get("code_fingerprint"), d.get("git_commit"), d.get("app_version"))
ok = (state == "current") and isinstance(unl, list) and len(unl) == 0
print(("OK|" if ok else "BAD|") + "deploy_state=%s unloaded_files=%s | %s" % (state, json.dumps(unl, ensure_ascii=False), fields))
print("PID=%s" % d.get("pid"))
PY
)"
R1_VERDICT="$(printf '%s' "$R1_OUT" | head -1)"
R1_PIDLINE="$(printf '%s' "$R1_OUT" | sed -n '2p')"
CURRENT_PID="${R1_PIDLINE#PID=}"
DEPLOYED_AT="$(printf '%s' "$R1_OUT" | sed -n 's/.*deployed_at=\([^ ]*\).*/\1/p' | head -1)"
case "$R1_VERDICT" in
  OK\|*)  pass R1 "${R1_VERDICT#OK|}" ;;
  BAD\|*) fail R1 "${R1_VERDICT#BAD|}" ;;
  *)      fail R1 "无法解析 /api/version：$R1_VERDICT" ;;
esac

# ── R2 · 进程新鲜度 ──────────────────────────────────────
echo "── R2 · 进程新鲜度 ──"
# [2026-10-29 修复] pid 缺失/非数字时旧判据会伪造 PASS：
# `"None" != "85038"` 天然成立 → 打印「pid 已变化：85038 → None」。这不能证明进程更换。
R2_PID_NUMERIC=0
case "$CURRENT_PID" in
  ''|None|null|undefined|*[!0-9]*) R2_PID_NUMERIC=0 ;;
  *) R2_PID_NUMERIC=1 ;;
esac
if [ "$R2_PID_NUMERIC" = "0" ]; then
  fail R2 "无法从 /api/version 读到数字 pid（拿到 '$CURRENT_PID'），不能证明进程已更换"
  info "提示：/api/version 必须返回 pid（整数）；后端未暴露 pid 时本项无法通过"
elif [ -z "$EXPECT_PID" ]; then
  info "当前 pid=${CURRENT_PID}（未指定比对基准 --expect-pid-different，只打印不判定）"
  info "deployed_at=${DEPLOYED_AT:-<缺失>}（pid 变化时用它交叉确认部署真的换了）"
  pass R2 "pid=${CURRENT_PID}（未指定比对基准，仅打印）"
elif [ "$CURRENT_PID" = "$EXPECT_PID" ]; then
  fail R2 "pid 未变化：$CURRENT_PID == 期望不同的 ${EXPECT_PID}（服务可能未真正重启）"
else
  pass R2 "pid 已变化：$EXPECT_PID → ${CURRENT_PID}（deployed_at=${DEPLOYED_AT:-<缺失>}）"
fi

# ── R3 · 大响应完整性 ────────────────────────────────────
echo "── R3 · 大响应完整性 ──"
r3_check() {  # $1=name $2=url $3=outfile
  local name="$1" url="$2" out="$3"
  local safe="${name//\//_}"
  local meta size cl curl_rc
  # [2026-10-29 修复] 必须**同一次请求**同时拿 body 字节数与 Content-Length。
  # 旧写法发两次请求（先 -w size_download，再 -D - 取 CL）：/api/jobs 列表在两次
  # 请求之间可能变化，实测出现 size=3825409 vs CL=3825408 的稳定假 FAIL。
  # 现在 header 落盘（-D）与 size 由同一次 curl 产出，两者必然同源。
  local hdr="$TMPDIR_LOCAL/$safe.hdr"
  : >"$hdr"
  size="$(curl -sS --max-time 60 -o "$out" -D "$hdr" -w '%{size_download}' "${AUTH[@]}" "$url" 2>"$TMPDIR_LOCAL/$safe.err")"
  curl_rc=$?
  cl="$(tr -d '\r' <"$hdr" | awk 'tolower($1)=="content-length:"{print $2}' | tail -1)"
  size="${size:-0}"
  cl="${cl:-}"
  local errmsg=""
  [ -s "$TMPDIR_LOCAL/$safe.err" ] && errmsg="$(tr '\n' ' ' < "$TMPDIR_LOCAL/$safe.err" | head -c 160)"
  if [ "$curl_rc" -ne 0 ]; then
    fail R3 "${name}：curl 退出码 ${curl_rc}（非 0，短读/连接中断）size_download=$size ${errmsg:+| $errmsg}"
    return
  fi
  if [ -z "$cl" ]; then
    fail R3 "${name}：响应头缺 Content-Length（无法证明完整性）size_download=$size"
    return
  fi
  if [ "$size" != "$cl" ]; then
    fail R3 "${name}：size_download=$size != Content-Length=${cl}（截断 $((cl - size)) 字节）"
    return
  fi
  pass R3 "${name}：size_download=$size == Content-Length=${cl}（curl rc=0）"
}

R3_JOBS="$TMPDIR_LOCAL/jobs.json"
r3_check "/api/jobs" "$BASE_URL/api/jobs" "$R3_JOBS"
if [ -n "$BIG_STATIC" ]; then
  r3_check "static:$BIG_STATIC" "$BASE_URL${BIG_STATIC}" "$TMPDIR_LOCAL/big"
else
  info "未传 --big-static，跳过静态大文件比对（只验 /api/jobs）"
fi

# ── R4 · 四天聚合（必须从 dashboard 子对象取）────────────
echo "── R4 · 四天聚合 ──"
R4_JSON="$TMPDIR_LOCAL/r4.json"
python3 - "$BASE_URL" "$TOKEN" "$AUDIO_TOL" "$R4_JSON" <<'PY'
import json, subprocess, sys
base, token, tol, outpath = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
EXPECT = {
    "2026-10-26": (13, 614.3),
    "2026-10-27": (6, 606.4),
    "2026-10-28": (1, 259.1),
    "2026-10-29": (3, 455.5),  # 2026-11-01 16:42 手表补发79段物理演进，实测真实覆盖 455.5min
}
rows, bad = [], []
print("  日期         ok    scenes(实测/期望)  audio(实测/期望)        updated_at")
for d, (escene, eaudio) in EXPECT.items():
    url = f"{base}/api/dashboard/daily?date={d}"
    # [2026-10-29 修复] 重启后第一次访问某天会走冷缓存，服务端返回 202 read_busy
    # （body: status=read_busy + retry_after/retry_after_ms）。旧代码把它当
    # 「顶层没有 dashboard（键名不匹配）」→ 稳定假 FAIL。这里按 retry_after 退避重试，
    # 最多 10 次、总时长 ≤60s；只有耗尽仍 202 才判 FAIL（绝不把 202 当 PASS）。
    import time
    deadline = time.monotonic() + 60.0
    payload, retries, last_retry_ms = None, 0, None
    while True:
        try:
            raw = subprocess.run(["curl", "-sS", "--max-time", "60", "-w", "\n%{http_code}",
                                  "-H", f"Authorization: Bearer {token}", url],
                                 capture_output=True)
            body_text, _, code_text = raw.stdout.decode("utf-8", "replace").rpartition("\n")
            http_code = int((code_text or "0").strip() or 0)
            payload = json.loads(body_text)
        except Exception as exc:
            rows.append({"date": d, "error": f"{type(exc).__name__}: {exc}"})
            bad.append(f"{d}: 请求/解析失败 {exc}")
            print(f"  {d}  PARSE-FAIL  {exc}")
            payload = None
            break
        busy = (http_code == 202
                or (isinstance(payload, dict) and str(payload.get("status") or "") == "read_busy"))
        if not busy:
            break
        retries += 1
        if retries >= 10 or time.monotonic() >= deadline:
            bad.append(f"{d}: 冷缓存重试 {retries} 次仍未就绪（HTTP {http_code}，"
                       f"最后一次 retry_after_ms={last_retry_ms}）")
            rows.append({"date": d, "error": "read_busy_retry_exhausted", "retries": retries,
                         "http_code": http_code})
            print(f"  {d}  RETRY-EXHAUSTED  冷缓存重试 {retries} 次仍未就绪（HTTP {http_code}）")
            payload = None
            break
        retry_ms = payload.get("retry_after_ms") if isinstance(payload, dict) else None
        if not isinstance(retry_ms, (int, float)) or retry_ms <= 0:
            retry_after = payload.get("retry_after") if isinstance(payload, dict) else None
            retry_ms = (float(retry_after) * 1000) if isinstance(retry_after, (int, float)) else 1000
        retry_ms = max(200.0, min(float(retry_ms), 5000.0))
        last_retry_ms = retry_ms
        time.sleep(retry_ms / 1000.0)
    if payload is None:
        continue
    db = payload.get("dashboard")
    if not isinstance(db, dict):
        # 关键：绝不静默当 0 —— 明确报键名不匹配
        bad.append(f"{d}: 顶层没有 dashboard 子对象（键名不匹配，顶层 keys={sorted(payload)[:8]}）")
        rows.append({"date": d, "error": "no-dashboard-subobject", "top_keys": sorted(payload)[:8]})
        print(f"  {d}  KEY-MISMATCH  顶层无 dashboard（keys={sorted(payload)[:8]}）")
        continue
    scenes = db.get("timeline_scenes")
    audio = db.get("total_audio_recorded_minutes")
    upd = db.get("updated_at")
    ok = payload.get("ok") is True
    n = len(scenes) if isinstance(scenes, list) else None
    rows.append({"date": d, "ok": ok, "scenes": n, "audio": audio, "updated_at": upd})
    has_off = isinstance(upd, str) and (upd.endswith("+08:00") or "+08:00" in upd)
    scene_ok = (n == escene)
    audio_ok = isinstance(audio, (int, float)) and abs(float(audio) - eaudio) <= tol
    print(f"  {d}  {str(ok):5} {str(n):>6}/{escene:<6}  {str(audio):>10}/{eaudio:<8}  {upd}"
          f"{'' if has_off else '  <无 +08:00>'}{'' if scene_ok else '  <scenes 不一致>'}{'' if audio_ok else '  <audio 不一致>'}")
    if not ok:
        bad.append(f"{d}: ok != true")
    if n is None:
        bad.append(f"{d}: dashboard.timeline_scenes 不是 list")
    elif not scene_ok:
        bad.append(f"{d}: scenes {n} != {escene}")
    if not audio_ok:
        bad.append(f"{d}: audio {audio} 超过容差 {tol}（期望 {eaudio}）")
    if not has_off:
        bad.append(f"{d}: updated_at 缺 +08:00 偏移（{upd}）")
json.dump({"rows": rows, "problems": bad}, open(outpath, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("  R4_PROBLEMS=" + ("; ".join(bad) if bad else ""))
PY
R4_PROBLEMS="$(python3 -c 'import json,sys; print("; ".join(json.load(open(sys.argv[1]))["problems"]))' "$R4_JSON" 2>/dev/null || echo "R4 结构化结果读取失败")"
if [ -z "$R4_PROBLEMS" ]; then
  pass R4 "四天 scenes/audio/updated_at(+08:00) 全部与基线一致（容差 ${AUDIO_TOL}min）"
else
  fail R4 "$R4_PROBLEMS"
fi

# ── R5 · 关键 API 200 ────────────────────────────────────
echo "── R5 · 关键 API 200 ──"
r5_check() {  # $1=label $2=path
  local label="$1" path="$2" code rc
  code="$(curl -sS --max-time 60 -o "$TMPDIR_LOCAL/r5.out" -w '%{http_code}' "${AUTH[@]}" "$BASE_URL$path" 2>"$TMPDIR_LOCAL/r5.err")"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    fail R5 "$label ${path}：curl 退出码 ${rc}（$(tr '\n' ' ' < "$TMPDIR_LOCAL/r5.err" | head -c 120)）"
    return
  fi
  if [ "$code" != "200" ]; then
    fail R5 "$label ${path}：HTTP ${code}（期望 200）"
    return
  fi
  if python3 -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$TMPDIR_LOCAL/r5.out" 2>/dev/null; then
    pass R5 "$label ${path}：HTTP 200 且 JSON 可解析"
  else
    fail R5 "$label ${path}：HTTP 200 但 JSON 不可解析（可能被截断）"
  fi
}
r5_check "/api/stats" "/api/stats?date=2026-10-27"
r5_check "/api/daily" "/api/daily?date=2026-10-27"
r5_check "/api/daily-brief" "/api/daily-brief?date=2026-10-27"

# [2026-10-29] R6 最小语义断言：页面场景块数必须 == /api/dashboard/daily 的
# dashboard.timeline_scenes 数（注意：在 dashboard 子对象里，顶层没有这个键）。
# 只加这一条，不重写 E2E；页面拿不到可读标识时按 SKIP 处理并明确告知。
r6_scene_assert() {   # $1=R6 的基础说明；输出 R6_SCENE_VERDICT / R6_SCENE_MSG
  local base_msg="$1" out="$TMPDIR_LOCAL/r6_scene.json" log="$TMPDIR_LOCAL/r6_scene.log"
  R6_SCENE_VERDICT="SKIP"; R6_SCENE_MSG=""
  cat >"$TMPDIR_LOCAL/r6_scene.mjs" <<'MJSEOF'
import { createRequire } from 'module';
// [2026-11-04 Q860] playwright 包位置可用 LL_PLAYWRIGHT_PKG 覆盖，默认保留原值（行为不变）。
const require = createRequire(process.env.LL_PLAYWRIGHT_PKG || '$HOME/.local/playwright/package.json');
const { chromium } = require('playwright');
const args = process.argv.slice(2);
const get = (k, d = null) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const BASE = get('--base-url', 'http://127.0.0.1:8765');
const TOKEN = get('--token', '');
const OUT = get('--out', '/tmp/r6_scene.json');
const r = { date: null, ratio: 0, bar: 0, cards: 0, rendered: false, titles: [] };
let browser;
try {
  browser = await chromium.launch({ headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 1300 } })).newPage();
  await page.goto(`${BASE}/?token=${encodeURIComponent(TOKEN)}`, { waitUntil: 'domcontentloaded', timeout: 45000 });
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    const st = await page.evaluate(() => {
      const f = document.querySelector('.day-timeline-frame');
      const d = f && f.contentDocument;
      if (!d) return { ratio: 0, bar: 0, cards: 0, date: null, titles: [] };
      let date = null;
      try { date = new URL(d.location.href).searchParams.get('date'); } catch (e) {}
      const counts = d.documentElement.dataset;
      return { ratio: Number(counts.sceneCount || 0), bar: Number(counts.sceneCount || 0),
               cards: Number(counts.activityCount || 0), date, titles: [] };
    });
    r.date = st.date || r.date; r.ratio = st.ratio; r.bar = st.bar; r.cards = st.cards; r.titles = st.titles || [];
    if (st.ratio > 0) { r.rendered = true; break; }
    await page.waitForTimeout(3000);
  }
  require('fs').writeFileSync(OUT, JSON.stringify(r));
} catch (e) {
  require('fs').writeFileSync(OUT, JSON.stringify({ ...r, error: String(e).slice(0, 300) }));
} finally { if (browser) await browser.close(); }
MJSEOF
  if ! "$NODE_BIN" "$TMPDIR_LOCAL/r6_scene.mjs" --base-url "$BASE_URL" --token "$TOKEN" --out "$out" >"$log" 2>&1; then
    R6_SCENE_MSG="页面无稳定可读标识（场景块探测脚本失败：$(tail -n 2 "$log" | tr '\n' ' ' | head -c 160)）→ 语义断言未完成"
    return
  fi
  local parsed
  parsed="$(python3 - "$out" "$BASE_URL" "$TOKEN" <<'PY6'
import datetime, json, subprocess, sys
path, base, token = sys.argv[1:4]
try:
    doc = json.load(open(path, encoding="utf-8"))
except Exception as exc:
    print("ERROR|探测结果不可解析 %s" % exc); raise SystemExit(0)
if doc.get("error"):
    print("ERROR|%s" % doc["error"]); raise SystemExit(0)
# [2026-11-04 UTC+8] 兜底必须是北京今天：原来用 datetime.date.today()，跟随系统时区，
# UTC-8 的傍晚之后北京已跨日，R6 会去校验错的那一天。
date = doc.get("date") or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
# [2026-10-30 收口] 原来 curl 不判 HTTP 码：冷缓存返回 202 read_busy 时响应体里
# 没有 dashboard 键 → 误报「顶层没有 dashboard 子对象」→ SKIP → 汇总 INCOMPLETE。
# 与 R9 同款：202 视为瞬时态，重试 <=6 次×2s；仍非 200 才给明确原因（FAIL，不是 SKIP）。
import time as _time
tmp_body = path + ".api.json"
code = "000"
raw = b""
for _attempt in range(1, 7):
    cp = subprocess.run(["curl", "-sS", "--max-time", "60", "-o", tmp_body, "-w", "%{http_code}",
                         "-H", f"Authorization: Bearer {token}",
                         f"{base}/api/dashboard/daily?date={date}"], capture_output=True)
    code = (cp.stdout or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["000"]
    code = code[0] if code else "000"
    if code == "202" and _attempt < 6:
        _time.sleep(2)
        continue
    break
if code != "200":
    # 注意：这里用 FAIL 而不是 ERROR —— 202 恒不回 200 / HTTP 非 200 是**确定的服务故障**，
    # 不能归到「无法比对→SKIP」，否则会把重启后的冷缓存故障伪装成 INCOMPLETE。
    print("FAIL|dashboard %s 重试 6 次仍非 200" % code if code == "202" else "FAIL|dashboard HTTP %s（期望 200）" % code)
    raise SystemExit(0)
try:
    raw = open(tmp_body, "rb").read()
    payload = json.loads(raw.decode("utf-8"))
except Exception as exc:
    print("ERROR|API 取数失败 %s" % exc); raise SystemExit(0)
db = payload.get("dashboard")
if not isinstance(db, dict):
    print("ERROR|HTTP 200 但顶层没有 dashboard 子对象（键名不匹配）"); raise SystemExit(0)
scenes = db.get("timeline_scenes")
if not isinstance(scenes, list):
    print("ERROR|dashboard.timeline_scenes 不是 list"); raise SystemExit(0)
dom = int(doc.get("ratio") or 0)
if not doc.get("rendered"):
    # [A0 修复 2026-10-30] 合法空日：页面 0 个场景段、API 也 0 段 —— 这是**一致**，
    # 不是「无法比对」。原来一律 NORENDER→SKIP 会把刚重启当天的空日判成 INCOMPLETE。
    if dom == 0 and len(scenes) == 0:
        print("OK|%s|%d|%d" % (date, dom, len(scenes))); raise SystemExit(0)
    print("NORENDER|%s|%d|%d" % (date, dom, len(scenes))); raise SystemExit(0)
print(("OK" if dom == len(scenes) else "MISMATCH") + "|%s|%d|%d" % (date, dom, len(scenes)))
PY6
)"
  local tag rest date dom api
  tag="${parsed%%|*}"; rest="${parsed#*|}"
  date="${rest%%|*}"; rest="${rest#*|}"; dom="${rest%%|*}"; api="${rest#*|}"
  case "$tag" in
    OK)       R6_SCENE_VERDICT="PASS"; R6_SCENE_MSG="场景块数一致：页面 ${dom} == API timeline_scenes ${api}（${date}）" ;;
    MISMATCH) R6_SCENE_VERDICT="FAIL"; R6_SCENE_MSG="页面场景块数 ${dom} != API dashboard.timeline_scenes ${api}（${date}）——页面能画但内容不对" ;;
    NORENDER) R6_SCENE_VERDICT="SKIP"; R6_SCENE_MSG="页面未渲染场景块（DOM=0，API=${api}，${date}）→ 无法比对，语义断言未完成" ;;
    FAIL)     R6_SCENE_VERDICT="FAIL"; R6_SCENE_MSG="${rest}（语义断言的取数步骤失败，按确定故障判 FAIL）" ;;
    *)        R6_SCENE_VERDICT="SKIP"; R6_SCENE_MSG="无法比对：${rest:-未知原因} → 语义断言未完成" ;;
  esac
}

# ── R6 · 真实页面（playwright）────────────────────────────
echo "── R6 · 真实页面 ──"
# [2026-11-04 Q860] node 路径可用 LL_NODE_BIN 覆盖，默认保留原值（判定条件未改）。
NODE_BIN="${LL_NODE_BIN:-$HOME/.local/node/bin/node}"
R6_SCRIPT="$REPO_ROOT/work/post-restart-check-20261029/r6_page_check.mjs"
if [ ! -x "$NODE_BIN" ] || [ ! -f "$R6_SCRIPT" ]; then
  skip R6 "playwright 不可用：node=${NODE_BIN}($([ -x "$NODE_BIN" ] && echo ok || echo missing)) script=$R6_SCRIPT"
else
  R6_JSON="$TMPDIR_LOCAL/r6.json"
  if "$NODE_BIN" "$R6_SCRIPT" --base-url "$BASE_URL" --token "$TOKEN" --date "$R6_DATE" --out "$R6_JSON" \
       >"$TMPDIR_LOCAL/r6.log" 2>&1; then
    # [A0 修复 2026-10-30] 旧判据写死 blocks>0，且读 consoleErr/pageErr 两个键名；
    # 三态版脚本（sha 0de465ab…）合法空日 blocks=0 也是 PASS，键名是 consoleErrors/pageErrors，
    # 并自带 verdict 字段。改为优先采用脚本自己的 verdict，键名缺失按 0 处理，避免假 FAIL。
    R6_MSG="$(python3 - "$R6_JSON" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
ce = d.get("consoleErr", len(d.get("consoleErrors") or []))
pe = d.get("pageErr", len(d.get("pageErrors") or []))
print("date=%s activities=%s blocks=%s verdict=%s consoleErr=%s pageErr=%s r401=%s blocked=%s empty=%r" % (
    d.get("date"), d.get("activities"), d.get("blocks"), d.get("verdict"),
    ce, pe, d.get("r401"), d.get("blocked"), d.get("emptyText")))
PY
)"
    # [2026-10-30 收口] shell 侧独立不变量（不再单靠脚本自报 verdict）：
    #   activities > 0  ⇒ blocks == activities
    #   activities == 0 ⇒ blocks == 0 且 emptyText 非空
    #   consoleErrors/pageErrors 非空 ⇒ FAIL
    # 三条均独立于 verdict；verdict 降为**附加**条件（PASS 才可能过，FAIL 一定不过）。
    if python3 - "$R6_JSON" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
ce = d.get("consoleErr", len(d.get("consoleErrors") or []))
pe = d.get("pageErr", len(d.get("pageErrors") or []))
verdict = d.get("verdict")
acts = d.get("activities")
blocks = d.get("blocks")
reasons = []
# 1) activities 必须是整数（取不到不能当空日）
if not isinstance(acts, int):
    reasons.append("activities 缺失/非整数：%r" % (acts,))
else:
    if acts > 0:
        if not isinstance(blocks, int) or blocks != acts:
            reasons.append("不变量：activities=%d 但 blocks=%r" % (acts, blocks))
    else:
        if not isinstance(blocks, int) or blocks != 0:
            reasons.append("不变量：activities=0 但 blocks=%r" % (blocks,))
        empty = d.get("emptyText")
        if not (isinstance(empty, str) and empty.strip()):
            reasons.append("不变量：空日没有空态文案（emptyText=%r）" % (empty,))
# 2) verdict 仅作为附加条件（老格式/缺失 → 由不变量兑底）
if verdict is not None and verdict != "PASS":
    reasons.append("脚本 verdict=%r" % (verdict,))
# 3) JS / 401 / blocked
if ce:
    reasons.append("consoleErrors=%s" % (ce,))
if pe:
    reasons.append("pageErrors=%s" % (pe,))
if d.get("r401"):
    reasons.append("r401=%s" % (d.get("r401"),))
if d.get("blocked"):
    reasons.append("blocked=True")
if reasons:
    print("R6 不变量未通过: " + "; ".join(reasons), file=sys.stderr)
raise SystemExit(0 if not reasons else 1)
PY
    then
      r6_scene_assert "$R6_MSG"
      case "$R6_SCENE_VERDICT" in
        PASS) pass R6 "${R6_MSG}；$R6_SCENE_MSG" ;;
        FAIL) fail R6 "${R6_MSG}；$R6_SCENE_MSG" ;;
        *)    skip R6 "${R6_MSG}；$R6_SCENE_MSG" ;;
      esac
    else
      fail R6 "$R6_MSG"
    fi
  else
    fail R6 "playwright 脚本执行失败：$(tail -n 3 "$TMPDIR_LOCAL/r6.log" | tr '\n' ' ' | head -c 240)"
  fi
fi

# ── R7 · 音频 Range 抽查 ─────────────────────────────────
echo "── R7 · 音频 Range 抽查 ──"
# [2026-10-29 修复] 旧探测取 result/**/*.* 里第一个 >=400KB 的文件 → 实际命中 .json：
# 「任意大文件能 206」被当成「音频能 Range」。现在必须命中音频扩展名白名单，
# 且 URL 用 files/<hash>/<name>（服务端 audio 路由要求 path 以 files/ 开头、第二段是文件指纹）。
R7_URL="$(python3 - "$REPO_ROOT" <<'PY'
import glob, json, os, sys
root = sys.argv[1]
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".caf", ".aac", ".flac", ".ogg"}
MIN_BYTES = 400 * 1024
jobs_root = os.path.join(root, "output/local-web/jobs")
found = []          # (size, job, files/<hash>/<name>)

for st in glob.glob(os.path.join(jobs_root, "job-*/status.json")):
    job = os.path.basename(os.path.dirname(st))
    try:
        doc = json.load(open(st, encoding="utf-8"))
    except Exception:
        continue
    for item in doc.get("files") or []:
        if not isinstance(item, dict):
            continue
        stored = str(item.get("stored_path") or "")
        name = str(item.get("filename") or os.path.basename(stored))
        if os.path.splitext(name)[1].lower() not in AUDIO_EXT or not stored:
            continue
        try:
            size = os.path.getsize(os.path.join(jobs_root, job, stored))
        except OSError:
            continue
        if size < MIN_BYTES:
            continue
        marker = str(item.get("sha256") or item.get("file_id") or "")
        parts = stored.split("/")
        if not marker and len(parts) >= 2 and parts[0] == "files":
            marker = parts[1]
        if marker:
            found.append((size, job, f"files/{marker}/{name}"))

if not found:   # 兜底：磁盘按扩展名扫（只读、限量）
    for path in glob.glob(os.path.join(jobs_root, "job-*/files/*/*")):
        if os.path.splitext(path)[1].lower() not in AUDIO_EXT:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size < MIN_BYTES:
            continue
        parts = path.split(os.sep)
        job = next((x for x in parts if x.startswith("job-")), "")
        try:
            marker = parts[parts.index("files") + 1]
        except (ValueError, IndexError):
            continue
        if job:
            found.append((size, job, f"files/{marker}/{parts[-1]}"))

if found:
    size, job, rel = sorted(found)[-1]
    print(f"/api/jobs/{job}/audio?path={rel}")
PY
)"
if [ -z "$R7_URL" ]; then
  skip R7 "未找到音频产物（白名单 .m4a/.mp3/.wav/.caf/.aac/.flac/.ogg 且 >=400KB）→ R7 未验证"
else
  ENC_URL="$BASE_URL$R7_URL"
  HDRS="$(curl -sS --max-time 30 -o /dev/null -D - -H 'Range: bytes=0-1023' "${AUTH[@]}" "$ENC_URL" 2>"$TMPDIR_LOCAL/r7.err" | tr -d '\r')"
  R7_RC=$?
  R7_CODE="$(printf '%s' "$HDRS" | awk 'NR==1{print $2}')"
  R7_CR="$(printf '%s' "$HDRS" | awk 'tolower($1)=="content-range:"{print $2" "$3}' | tail -1)"
  if [ "$R7_RC" -ne 0 ]; then
    fail R7 "range 请求 curl 退出码 ${R7_RC}（$(tr '\n' ' ' < "$TMPDIR_LOCAL/r7.err" | head -c 120)）"
  elif [ "$R7_CODE" != "206" ]; then
    fail R7 "HTTP ${R7_CODE}（期望 206），Content-Range='${R7_CR}'"
  elif ! printf '%s' "$R7_CR" | grep -Eq '^bytes [0-9]+-[0-9]+/[0-9]+$'; then
    fail R7 "Content-Range 不合法：'${R7_CR}'"
  else
    pass R7 "206 + Content-Range '$R7_CR'（${R7_URL}）"
  fi
fi

# ── R8 · 部署双证据（pid + deploy_state）──────────────────
echo "── R8 · 部署双证据 ──"
# [2026-10-29 补盲区 Q8-G] 只比 pid 不够：新 pid 也可能在跑旧字节码。
# 必须同时给出 pid 与 deploy_state；缺字段一律 FAIL（不许 SKIP）。
R8_OUT="$(python3 - "$TMPDIR_LOCAL/version.json" <<'PY8'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    print("BAD|无法解析 /api/version：%s" % exc); raise SystemExit(0)
missing = [k for k in ("pid", "deploy_state", "unloaded_files") if k not in d]
if missing:
    print("BAD|/api/version 缺字段 %s（缺字段不等于通过，按 FAIL 处理）" % ",".join(missing)); raise SystemExit(0)
state = d.get("deploy_state"); unl = d.get("unloaded_files"); pid = d.get("pid")
if not isinstance(unl, list):
    print("BAD|unloaded_files 不是 list（%r）" % (unl,)); raise SystemExit(0)
if state != "current":
    print("BAD|pid=%s deploy_state=%s（期望 current）unloaded_files=%d" % (pid, state, len(unl))); raise SystemExit(0)
if unl:
    print("BAD|pid=%s deploy_state=%s 但 unloaded_files=%d：%s" % (
        pid, state, len(unl), json.dumps(unl, ensure_ascii=False)[:180])); raise SystemExit(0)
print("OK|pid=%s deploy_state=%s unloaded_files=0（pid 与部署态双证据一致）" % (pid, state))
PY8
)"
case "$R8_OUT" in
  OK\|*)  pass R8 "${R8_OUT#OK|}" ;;
  BAD\|*) fail R8 "${R8_OUT#BAD|}" ;;
  *)      fail R8 "R8 结果不可解析：${R8_OUT:-<空>}" ;;
esac

# ── R9 · 多日期 200 + 非空 ───────────────────────────────
echo "── R9 · 多日期 200 + 非空 ──"
# [2026-10-29 补盲区 Q8-I] 旧 R5 只打一个日期；这里对 --dates（默认四天）逐日验证。
R9_BAD=()
R9_ROWS=()
for d in "${DATE_LIST[@]}"; do
  r9_out="$TMPDIR_LOCAL/r9_${d}.json"
  r9_err="$TMPDIR_LOCAL/r9_${d}.err"
  # [A0 修复 2026-10-30] 202 = read_busy（冷缓存 + producer 排队/运行超 4s），是文档化的
  # 瞬时态而非故障：刚重启后首次读必然冷缓存。这里按 202 重试（最多 6 次、间隔 2s），
  # 仍拿不到 200 才算 FAIL，避免把「刚重启的冷缓存」误判成服务坏掉。
  r9_attempts=0
  while :; do
    r9_code="$(curl -sS --max-time 60 -o "$r9_out" -w '%{http_code}' "${AUTH[@]}" "$BASE_URL/api/dashboard/daily?date=$d" 2>"$r9_err")"
    r9_rc=$?
    r9_attempts=$((r9_attempts + 1))
    if [ "$r9_code" = "202" ] && [ "$r9_attempts" -lt 6 ]; then sleep 2; continue; fi
    break
  done
  r9_size="$(wc -c < "$r9_out" 2>/dev/null | tr -d ' ')"
  r9_size="${r9_size:-0}"
  if [ "$r9_rc" -ne 0 ]; then
    r9_msg="curl 退出码 ${r9_rc}（$(tr '\n' ' ' < "$r9_err" | head -c 120)）"
  elif [ "$r9_code" = "202" ]; then
    r9_msg="HTTP 202 read_busy 重试 ${r9_attempts} 次仍未 200（冷缓存未能在 ~10s 内产出）"
  elif [ "$r9_code" != "200" ]; then
    r9_msg="HTTP ${r9_code}（期望 200）"
  elif [ "$r9_size" -lt 3 ]; then
    r9_msg="响应体为空（${r9_size} 字节）"
  else
    r9_msg=""
  fi
  if [ -n "$r9_msg" ]; then
    R9_BAD+=("${d}: ${r9_msg}")
    R9_ROWS+=("${d} FAIL ${r9_msg}")
  else
    R9_ROWS+=("${d} 200 ${r9_size}B（尝试 ${r9_attempts} 次）")
  fi
done
for _row in "${R9_ROWS[@]}"; do info "$_row"; done
unset _row
if [ "${#R9_BAD[@]}" -gt 0 ]; then
  fail R9 "$(printf '%s; ' "${R9_BAD[@]}" | sed 's/; $//')"
else
  pass R9 "${#DATE_LIST[@]} 天均 200 且响应体非空"
fi

# ── R10 · 静态资源完整性 ─────────────────────────────────
echo "── R10 · 静态资源完整性 ──"
# [2026-10-29 补盲区 Q8-H] 直接检测 327KB 截断：页面 html 与它引用的 /app.js 都要
# 200 + curl rc=0 + size_download == Content-Length。
r10_check() {   # $1=label $2=path $3=outfile
  local label="$1" path="$2" out="$3"
  local code rc size cl errmsg
  code="$(curl -sS --max-time 60 -o "$out" -w '%{http_code}' "${AUTH[@]}" "$BASE_URL$path" 2>"$TMPDIR_LOCAL/r10.err")"
  rc=$?
  size="$(wc -c < "$out" 2>/dev/null | tr -d ' ')"; size="${size:-0}"
  cl="$(curl -sS --max-time 60 -o /dev/null -D - "${AUTH[@]}" "$BASE_URL$path" 2>/dev/null | tr -d '\r' | awk 'tolower($1)=="content-length:"{print $2}' | tail -1)"
  errmsg="$(tr '\n' ' ' < "$TMPDIR_LOCAL/r10.err" 2>/dev/null | head -c 120)"
  if [ "$rc" -ne 0 ]; then
    fail R10 "${label} ${path}：curl 退出码 ${rc}（短读/截断）size_download=$size ${errmsg:+| $errmsg}"
    return
  fi
  if [ "$code" != "200" ]; then
    fail R10 "${label} ${path}：HTTP ${code}（期望 200）"
    return
  fi
  if [ -z "$cl" ]; then
    fail R10 "${label} ${path}：缺 Content-Length，无法证明完整性（size=$size）"
    return
  fi
  if [ "$size" != "$cl" ]; then
    fail R10 "${label} ${path}：size_download=$size != Content-Length=${cl}（差 $((cl - size)) 字节）"
    return
  fi
  pass R10 "${label} ${path}：HTTP 200、curl rc=0、size_download=$size == Content-Length=$cl"
}
r10_check "首页" "/" "$TMPDIR_LOCAL/r10_page.html"
APP_JS_PATH="$(grep -oE '/app\.js[^"'"'"' <>]*' "$TMPDIR_LOCAL/r10_page.html" 2>/dev/null | head -1)"
if [ -z "$APP_JS_PATH" ]; then
  fail R10 "首页 html 里找不到 /app.js 引用（无法验证主脚本完整性）"
else
  r10_check "主脚本" "$APP_JS_PATH" "$TMPDIR_LOCAL/r10_app.js"
fi

# ── R11 · 期望值 0 护栏 ──────────────────────────────────
echo "── R11 · 期望值 0 护栏 ──"
# [2026-10-29 补盲区 Q8-F] R4 的期望是写死的；若未来某天期望被写成 0，
# 而实测也是 0，会得到“看起来全对”的假 PASS。这里显式告警并要求人工确认。
R11_OUT="$(python3 - "$R4_JSON" <<'PY11'
import json, sys
EXPECT = {"2026-10-26": 13, "2026-10-27": 6, "2026-10-28": 1, "2026-10-29": 3}
try:
    rows = json.load(open(sys.argv[1], encoding="utf-8")).get("rows") or []
except Exception as exc:
    print("BAD|R4 结果不可解析：%s" % exc); raise SystemExit(0)
zero = []
for r in rows:
    d = r.get("date")
    if d in EXPECT and EXPECT[d] == 0 and r.get("scenes") == 0:
        zero.append(d)
if zero:
    print("ZERO|期望 scenes=0 且实测=0 的日期：%s（0 对 0 不能算通过，需人工确认）" % ",".join(zero))
else:
    print("OK|无 0 期望的日期（本次期望：%s）" % json.dumps(EXPECT, ensure_ascii=False))
PY11
)"
case "$R11_OUT" in
  OK\|*)   pass R11 "${R11_OUT#OK|}" ;;
  ZERO\|*) skip R11 "${R11_OUT#ZERO|}" ;;
  *)       skip R11 "R11 结果不可解析：${R11_OUT:-<空>}（防未来护栏未完成）" ;;
esac

# ── 结论 ─────────────────────────────────────────────────
echo ""
TOTAL=${#RESULTS[@]}
NFAIL=${#FAILS[@]}
NSKIP=${#SKIPS[@]}
NPASS=$((TOTAL - NFAIL - NSKIP))
echo "汇总: ${NPASS} PASS / ${NFAIL} FAIL / ${NSKIP} SKIPPED"
if [ "$NFAIL" -gt 0 ]; then
  echo "FAIL 项: ${FAILS[*]}"
  VERDICT="FAIL"; CODE=1
elif [ "$NSKIP" -gt 0 ]; then
  echo "SKIPPED 项: ${SKIPS[*]}"
  VERDICT="INCOMPLETE"; CODE=3
else
  VERDICT="PASS"; CODE=0
fi
echo "结论: $VERDICT (exit $CODE)"

if [ -n "$JSON_OUT" ]; then
  mkdir -p "$(dirname "$JSON_OUT")" 2>/dev/null || true
  python3 - "$JSON_OUT" "$VERDICT" "$CODE" "$BASE_URL" "$CURRENT_PID" "${RESULTS[@]}" <<'PY'
import json, sys, datetime
out, verdict, code, base, pid = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
items = []
for raw in sys.argv[6:]:
    parts = raw.split("|", 2)
    if len(parts) == 3:
        items.append({"id": parts[0], "status": parts[1], "detail": parts[2]})
json.dump({"schema": "post-restart-check-v1", "verdict": verdict, "exit_code": code,
           "base_url": base, "pid": pid,
           "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "items": items}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
  echo "JSON: $JSON_OUT"
fi
emit_log "$(date '+%F %T') verdict=$VERDICT code=$CODE pass=$NPASS fail=$NFAIL skip=$NSKIP"
exit "$CODE"
