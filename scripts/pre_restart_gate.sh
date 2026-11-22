#!/usr/bin/env bash
# pre_restart_gate.sh — 重启 local-web 前的七项门禁（**只判断，不重启**）
#
# 为什么有它：每次重启前手工跑 py_compile / node --check / pytest 红线 / 台账 /
# 六窗 idle / active_jobs / deploy_state，容易漏项且没有留痕。
#
# 用法：
#   ./scripts/pre_restart_gate.sh                # 真跑七项，最后一行给结论
#   ./scripts/pre_restart_gate.sh --dry-run      # 只打印将执行什么，不跑 pytest
#   ./scripts/pre_restart_gate.sh --expect-passed 311   # 红线测试通过数基线；auto = 不校验数量
#       ⚠ 加/删测试后要同步改这里的默认值（见 EXPECT_PASSED 注释）；
#       数量变化但 failed=0/errors=0 时只 WARN 不 FAIL，不会误拦重启
#   ./scripts/pre_restart_gate.sh --since-hours 3     # “本次改动过的 py” 的 mtime 窗口（默认 3 小时）
#   ./scripts/pre_restart_gate.sh --allow-busy-self   # 忽略当前会话自身的 busy 状态
#   ./scripts/pre_restart_gate.sh --log work/restart-gate-20261029/gate-run.log
#   ./scripts/pre_restart_gate.sh --selftest   # 只验证「结论 ↔ 退出码」映射（不是门禁）
#   ./scripts/pre_restart_gate.sh --base-url http://127.0.0.1:9   # 模拟 /api/health 不可用（测试用）
#   ./scripts/pre_restart_gate.sh --require-clean-api-jobs   # 最严：/api/jobs 明文响应也必须完整（重启后复核用）
#
# 退出码：0 = PASS；1 = FAIL；2 = 参数错误。结论行与退出码始终一致。
# 本脚本**不含任何重启动作**（不会调用 launchctl / kickstart / kill）。
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="http://127.0.0.1:8765"
# [2026-11-04 Q860] 调度台目录可用 LL_DISPATCH_DIR 覆盖，默认保留原值（不动任何判断条件）。
DISPATCH_DIR="${LL_DISPATCH_DIR:-$HOME/.life-logger/agent-board}"
PY="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/work/restart-gate-20261029"
LOG_FILE=""
# 红线测试（tests/test_beijing_business_dates.py + tests/test_local_web.py）的通过数基线。
# ⚠ 维护约定：**新增或删除测试后必须同步改这里**（否则每加一个用例门禁就漂一次）。
#   - 2026-11-02：302 → 311（Q476 在 test_beijing_business_dates.py 加了 3 个用例 × 3 个宿主时区 = 9 项）
#   - 2026-11-06：311 → 305（Q918 删决策层，test_local_web.py 去掉 6 个 Jev/Kev 用例）
#   - 数量**变多**且 failed=0/errors=0 → WARN（提示更新基线），不判 FAIL
#   - 数量**变少**且 failed=0/errors=0 → WARN + 醒目提示「可能删了测试」，不判 FAIL
#   - 想彻底不校验数量：--expect-passed auto
EXPECT_PASSED=305
SINCE_HOURS=3
DRY_RUN=0
SELFTEST=0
ALLOW_BUSY_SELF=0
SELF_ID="${PI_SESSION_ID:-}"
REQUIRE_CLEAN_API_JOBS=0

usage() {
  sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --selftest) SELFTEST=1; shift ;;
    --expect-passed) EXPECT_PASSED="${2:-}"; shift 2 ;;
    --since-hours) SINCE_HOURS="${2:-}"; shift 2 ;;
    --allow-busy-self) ALLOW_BUSY_SELF=1; shift ;;
    --require-clean-api-jobs) REQUIRE_CLEAN_API_JOBS=1; shift ;;
    --self-id) SELF_ID="${2:-}"; shift 2 ;;
    --log) LOG_FILE="${2:-}"; shift 2 ;;
    --base-url) BASE_URL="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 2 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$EXPECT_PASSED" in
  auto) : ;;
  ''|*[!0-9]*) echo "--expect-passed 需要整数或 auto，收到: '$EXPECT_PASSED'" >&2; exit 2 ;;
esac

[ -n "$LOG_FILE" ] && { mkdir -p "$(dirname "$LOG_FILE")"; exec > >(tee -a "$LOG_FILE") 2>&1; }

mkdir -p "$LOG_DIR"

now_ms() { perl -MTime::HiRes=time -e 'printf "%d", time*1000'; }

# 结论 ↔ 退出码 的唯一映射（脚本末尾和 --selftest 都用它，避免两处对不上）
verdict_code() {
  case "$1" in
    PASS)    echo 0 ;;
    FAIL)    echo 1 ;;
    DRY-RUN) echo 0 ;;
    *)       echo 2 ;;
  esac
}

TMPDIR_GATE="$(mktemp -d /tmp/pre_restart_gate.XXXXXX)"
trap 'rm -rf "$TMPDIR_GATE"' EXIT

# 表行存储（bash 3.2：用平行数组）
ROWS=""
add_row() { ROWS="${ROWS}$1|$2|$3|$4
"; }

hr() { printf '%s\n' "────────────────────────────────────────────────────────────────────────────"; }

echo "重启门禁 pre_restart_gate.sh"
echo "仓库    : $ROOT"
if [ "$EXPECT_PASSED" = "auto" ]; then
  echo "期望红  : auto（不校验数量，只要求 failed=0 / errors=0）"
else
  echo "期望红  : ${EXPECT_PASSED} passed（数量不符但全绿 → WARN，不判 FAIL）"
fi
echo "时间    : $(date '+%Y-%m-%d %H:%M:%S')"
[ "$DRY_RUN" = "1" ] && echo "模式    : DRY-RUN（只打印命令，不执行 pytest / 不改动任何东西）"
[ "$ALLOW_BUSY_SELF" = "1" ] && echo "会话    : 忽略自身 busy（self-id=${SELF_ID:-<unset>}）"
hr

# 每项检查输出记到文件，失败时打印末尾 20 行
run_capture() {  # run_capture <outfile> <cmd...>
  local out="$1"; shift
  "$@" >"$out" 2>&1
  return $?
}

tail20() { tail -n 20 "$1" 2>/dev/null | sed 's/^/        /'; }

# ═════════════════════════════════════════════════════════════════════════════
# G1 语法：以 /api/health 的 unloaded_files 为权威来源（重启后真正会被加载的文件）
#          + mtime/git 补充（未加载但确实改过的：src/life_logger 递归、scripts/*.py|.sh、web/*.js）
# 为什么改：旧实现只按「近 N 小时 mtime 且 src/life_logger 顶层」猜。实测缺口：
#   改完超过窗口、放在子目录、或放在 scripts/ 下的坏文件都会显示 PASS，然后带着坏代码重启。
# 拿不到 unloaded_files（服务挂/401/字段缺失）→ 直接 FAIL：不许静默退回 mtime 近似当 PASS。
# ═════════════════════════════════════════════════════════════════════════════
g1_syntax() {
  local out="$TMPDIR_GATE/g1.log" t0 t1 ok=1 npy=0 njs=0 nsh=0 f
  local unloaded="$TMPDIR_GATE/g1_unloaded.txt" checked="$TMPDIR_GATE/g1_checked.txt"
  local supplement="$TMPDIR_GATE/g1_supplement.txt"
  local deploy_state_file="$TMPDIR_GATE/g1_deploy_state.txt"
  local nskip=0 mode="unloaded"
  t0=$(now_ms)
  : >"$out"; : >"$checked"

  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY-RUN: GET ${BASE_URL}/api/health → version.unloaded_files" >"$out"
    echo "DRY-RUN: 对 unloaded_files 逐个按扩展名：.py→py_compile  .js/.mjs→node --check  .sh→bash -n" >>"$out"
    echo "DRY-RUN: 补充扫描：git M ∪ 近 ${SINCE_HOURS}h mtime（src/life_logger 递归 + scripts/ + web/*.js）" >>"$out"
    echo "DRY-RUN: unloaded_files 为空 → 全量语法检查（src/life_logger/**/*.py + scripts/*.py|*.sh + web/*.js），不放行" >>"$out"
    t1=$(now_ms); G1_DETAIL="dry-run（unloaded_files 驱动）"; G1_OUT="$out"; G1_MS=$((t1 - t0)); return 0
  fi

  # ① 权威来源：/api/health → version.unloaded_files（输出绝对路径；拿不到则非 0）
  if ! "$PY" - "$ROOT" "$BASE_URL" "$deploy_state_file" >"$unloaded" 2>"$TMPDIR_GATE/g1_health.err" <<'PYG1HEALTH'
import http.client, json, os, sys

root, base = sys.argv[1], sys.argv[2]
state_path = sys.argv[3] if len(sys.argv) > 3 else ""
host = base.split("//", 1)[1].rstrip("/")
try:
    token = json.load(open(f"{root}/output/local-web/secrets.json", encoding="utf-8"))["mobile_access_token"]
except Exception:
    token = ""

def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(3)

try:
    conn = http.client.HTTPConnection(host, timeout=30)
    conn.request("GET", "/api/health", headers={"Authorization": f"Bearer {token}"})
    resp = conn.getresponse()
    raw = resp.read()
    code = resp.status
    conn.close()
except Exception as exc:
    fail(f"/api/health 连接失败: {type(exc).__name__}: {exc}")

if code != 200:
    fail(f"/api/health HTTP {code}（401/5xx 都算拿不到）")
try:
    doc = json.loads(raw.decode("utf-8", errors="replace"), strict=False)
except Exception as exc:
    fail(f"/api/health 不是合法 JSON: {type(exc).__name__}: {exc}")
v = doc.get("version") if isinstance(doc.get("version"), dict) else {}
if state_path:
    try:
        with open(state_path, "w", encoding="utf-8") as fh:
            fh.write(str(v.get("deploy_state") or "unknown"))
    except OSError:
        pass
unloaded = v.get("unloaded_files")
if not isinstance(unloaded, list):
    fail("unloaded_files 字段缺失（无法确定重启后会加载哪些文件）")

search_dirs = ["src/life_logger", "src", "scripts", "web", "."]
missing = []
os.chdir(root)
for name in unloaded:
    name = str(name)
    candidates = [name] if os.path.isabs(name) else [os.path.join(d, name) for d in search_dirs]
    hit = next((p for p in candidates if os.path.isfile(p)), None)
    if hit is None:
        missing.append(name)
    else:
        print(os.path.abspath(hit))
if missing:
    fail("unloaded_files 中以下文件在仓库里找不到: " + ", ".join(missing))
PYG1HEALTH
  then
    ok=0
    {
      echo "FAIL: 无法确定待加载文件（/api/health 不可用或 unloaded_files 不可解析）"
      sed 's/^/  原因: /' "$TMPDIR_GATE/g1_health.err" 2>/dev/null
      echo "  → 按契约 G1 判 FAIL（不许退回 mtime 近似当 PASS）"
    } >>"$out"
  fi

  if [ -s "$unloaded" ]; then
    echo "unloaded_files（重启后会被加载，权威来源）:" >>"$out"
    sed "s|^$ROOT/|  · |" "$unloaded" >>"$out"
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      printf '%s\n' "$f" >>"$checked"
      case "$f" in
        *.py) npy=$((npy + 1))
              "$PY" -m py_compile "$f" >>"$out" 2>&1 || { echo "FAILED py_compile: ${f#$ROOT/}" >>"$out"; ok=0; } ;;
        *.js|*.mjs) njs=$((njs + 1))
              if command -v node >/dev/null 2>&1; then
                node --check "$f" >>"$out" 2>&1 || { echo "FAILED node --check: ${f#$ROOT/}" >>"$out"; ok=0; }
              else
                echo "FAIL: node 不存在，无法检查 ${f#$ROOT/}" >>"$out"; ok=0
              fi ;;
        *.sh) nsh=$((nsh + 1))
              bash -n "$f" >>"$out" 2>&1 || { echo "FAILED bash -n: ${f#$ROOT/}" >>"$out"; ok=0; } ;;
        *) nskip=$((nskip + 1)); echo "SKIP: 无语法检查器（扩展名不支持）: ${f#$ROOT/}" >>"$out" ;;
      esac
    done <"$unloaded"
  else
    # [2026-10-29] 空列表 = 刚重启过（deploy_state=current）是**最需要检查**的时刻，
    # 绝不能因为“没有待加载文件”就什么都不查。这里改为全量语法检查（不依赖 git/mtime）。
    mode="full"
    local ds="unknown"
    [ -f "$deploy_state_file" ] && ds="$(cat "$deploy_state_file" 2>/dev/null || echo unknown)"
    {
      echo "unloaded_files 为空（deploy_state=${ds}）→ 全量语法检查（不跳过）:"
      echo "  src/life_logger/**/*.py + scripts/*.py|*.sh + web/*.js"
    } >>"$out"
    {
      find "$ROOT/src/life_logger" -name '*.py' 2>/dev/null
      find "$ROOT/scripts" -maxdepth 1 \( -name '*.py' -o -name '*.sh' \) 2>/dev/null
    } | sort -u >"$TMPDIR_GATE/g1_full.txt"
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      grep -qxF "$f" "$checked" && continue
      printf '%s\n' "$f" >>"$checked"
      case "$f" in
        *.py) npy=$((npy + 1))
              "$PY" -m py_compile "$f" >>"$out" 2>&1 || { echo "FAILED py_compile（全量）: ${f#$ROOT/}" >>"$out"; ok=0; } ;;
        *.sh) nsh=$((nsh + 1))
              bash -n "$f" >>"$out" 2>&1 || { echo "FAILED bash -n（全量）: ${f#$ROOT/}" >>"$out"; ok=0; } ;;
      esac
    done <"$TMPDIR_GATE/g1_full.txt"
  fi

  # ② 补充扫描：未加载但确实改过的（覆盖子目录 / scripts/ / 超出 mtime 窗口的改动）
  {
    ( cd "$ROOT" && git status --porcelain -- src/life_logger scripts 2>/dev/null \
        | awk 'substr($0,1,2) ~ /M|A/ || substr($0,1,2) == "??" { print substr($0,4) }' )
    find "$ROOT/src/life_logger" -name '*.py' -newermt "-${SINCE_HOURS} hours" 2>/dev/null
    find "$ROOT/scripts" -maxdepth 1 \( -name '*.py' -o -name '*.sh' \) -newermt "-${SINCE_HOURS} hours" 2>/dev/null
  } | grep -E '\.(py|sh)$' | sort -u >"$supplement"

  local nsup=0
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in /*) ;; *) f="$ROOT/$f" ;; esac
    if [ ! -f "$f" ]; then
      nskip=$((nskip + 1)); echo "SKIP: 文件已不存在（git 记录残留）: ${f#$ROOT/}" >>"$out"
      continue
    fi
    grep -qxF "$f" "$checked" && continue
    printf '%s\n' "$f" >>"$checked"
    nsup=$((nsup + 1))
    case "$f" in
      *.py) npy=$((npy + 1))
            "$PY" -m py_compile "$f" >>"$out" 2>&1 || { echo "FAILED py_compile（补充扫描）: ${f#$ROOT/}" >>"$out"; ok=0; } ;;
      *.sh) nsh=$((nsh + 1))
            bash -n "$f" >>"$out" 2>&1 || { echo "FAILED bash -n（补充扫描）: ${f#$ROOT/}" >>"$out"; ok=0; } ;;
    esac
  done <"$supplement"
  echo "补充扫描新增 ${nsup} 个（git M ∪ 近 ${SINCE_HOURS}h mtime；src/life_logger 递归 + scripts/）" >>"$out"

  # ③ 前端：web/*.js 全量（文件少，代价很小）
  if command -v node >/dev/null 2>&1; then
    for f in "$ROOT"/web/*.js; do
      [ -e "$f" ] || continue
      grep -qxF "$f" "$checked" && continue
      njs=$((njs + 1))
      if ! node --check "$f" >>"$out" 2>&1; then
        echo "FAILED node --check: ${f#$ROOT/}" >>"$out"; ok=0
      fi
    done
  else
    echo "FAIL: node 不存在，无法检查 web/*.js" >>"$out"
    ok=0
  fi

  t1=$(now_ms)
  if [ "$mode" = "full" ]; then
    G1_DETAIL="py=${npy} js=${njs} sh=${nsh} skip=${nskip}（unloaded_files 为空 → 全量检查）"
  else
    G1_DETAIL="py=${npy} js=${njs} sh=${nsh} skip=${nskip}（unloaded_files + 近 ${SINCE_HOURS}h 补充）"
  fi
  G1_OUT="$out"
  G1_MS=$((t1 - t0))
  return $((1 - ok))
}

# ═════════════════════════════════════════════════════════════════════════════
# G2 红线测试：pytest must be exactly EXPECT_PASSED passed（0 failed / 0 error）
# ═════════════════════════════════════════════════════════════════════════════
g2_tests() {
  local out="$TMPDIR_GATE/g2.log" xml="$TMPDIR_GATE/g2.xml" t0 t1 ok=1 counts
  t0=$(now_ms)
  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY-RUN: $PY -m pytest tests/test_beijing_business_dates.py tests/test_local_web.py -p no:randomly -o addopts='' -q --junitxml=..." >"$out"
    t1=$(now_ms); G2_DETAIL="dry-run"; G2_OUT="$out"; G2_MS=$((t1 - t0)); G2_WARN=0; return 0
  fi
  rm -f "$xml"
  # 用 junitxml 取计数：文本汇总行在不同 pytest 版本/顺序下格式不一，解析容易错（曾把
  # "1 failed, 301 passed" 误读成 failed=0）。
  ( cd "$ROOT" && "$PY" -m pytest tests/test_beijing_business_dates.py tests/test_local_web.py \
      -p no:randomly -o addopts="" -q --junitxml="$xml" ) >"$out" 2>&1
  local rc=$?
  if ! counts=$("$PY" - "$xml" <<'PYEOF'
import sys, xml.etree.ElementTree as ET
try:
    root = ET.parse(sys.argv[1]).getroot()
except Exception:
    print("0 0 0 0 0"); sys.exit(1)
suite = root if root.tag == "testsuite" else root.find("testsuite")
if suite is None:
    print("0 0 0 0 0"); sys.exit(1)
tests = int(suite.get("tests", 0)); failures = int(suite.get("failures", 0))
errors = int(suite.get("errors", 0)); skipped = int(suite.get("skipped", 0))
passed = tests - failures - errors - skipped
print(passed, failures, errors, skipped, tests)
sys.exit(0)
PYEOF
  ); then
    counts="0 0 0 0 0"
  fi
  set -- $counts
  local passed="$1" failed="$2" errors="$3" skipped="$4" total="$5"
  G2_WARN=0
  local note=""

  # [2026-11-02] 「全绿但数量变了」不是回归 —— 判据拆成两层：
  #   ① 硬失败：failed>0 / errors>0 / pytest 非 0  → 一定 FAIL
  #   ② 数量：与 EXPECT_PASSED 不符但全绿 → **WARN**（提示更新基线），不判 FAIL
  #      向下再多给一句醒目提示，因为「通过数变少」有可能意味着测试被误删。
  if [ "$failed" != "0" ] || [ "$errors" != "0" ]; then
    ok=0
  fi
  [ "$rc" = "0" ] || ok=0

  if [ "$EXPECT_PASSED" = "auto" ]; then
    note=" ; 数量基线=auto（不校验）"
  elif [ "$passed" != "$EXPECT_PASSED" ]; then
    if [ "$passed" -gt "$EXPECT_PASSED" ] 2>/dev/null; then
      note=" ; ⚠ 比基线多 $((passed - EXPECT_PASSED)) 项（新增测试？请把 EXPECT_PASSED 改为 ${passed}）"
    else
      note=" ; ⚠⚠ 比基线少 $((EXPECT_PASSED - passed)) 项（可能删了测试！请核对后把 EXPECT_PASSED 改为 ${passed}）"
    fi
    # 只有「全绿」才降级成 WARN；有 failed/errors 时上面的 ok=0 已经生效
    [ "$ok" = "1" ] && G2_WARN=1
  else
    # 数量与基线一致时不要写"一切正常"——若同时有 failed/errors，下面会覆盖
    [ "$failed" = "0" ] && [ "$errors" = "0" ] && note=" ; 与基线一致"
  fi
  if [ "$failed" != "0" ] || [ "$errors" != "0" ]; then
    note=" ; ✗ failed=${failed} errors=${errors}（硬失败，与数量无关）${note}"
  fi
  [ -z "$note" ] && note=" ; 与基线一致"

  t1=$(now_ms)
  G2_DETAIL="passed=${passed}/${EXPECT_PASSED} failed=${failed} errors=${errors} skipped=${skipped} total=${total}${note}"
  G2_OUT="$out"; G2_MS=$((t1 - t0))
  return $((1 - ok))
}

# ═════════════════════════════════════════════════════════════════════════════
# G3 台账：planning_check.sh --strict 必须给出「结论：合格」
# ═════════════════════════════════════════════════════════════════════════════
g3_planning() {
  local out="$TMPDIR_GATE/g3.log" t0 t1 ok=1 verdict
  t0=$(now_ms)
  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY-RUN: bash .planning/tools/planning_check.sh --strict" >"$out"
    t1=$(now_ms); G3_DETAIL="dry-run"; G3_OUT="$out"; G3_MS=$((t1 - t0)); return 0
  fi
  if ! ( cd "$ROOT" && bash .planning/tools/planning_check.sh --strict ) >"$out" 2>&1; then
    : # 退出码不可靠，只认结论行
  fi
  verdict=$(grep -o '结论：[^ -~]*' "$out" | tail -n 1)
  [ -z "$verdict" ] && verdict=$(grep '结论' "$out" | tail -n 1)
  case "$verdict" in
    *"结论：合格"*) ok=1 ;;
    *) ok=0 ;;
  esac
  t1=$(now_ms)
  G3_DETAIL="$verdict"
  G3_OUT="$out"; G3_MS=$((t1 - t0))
  return $((1 - ok))
}

# ═════════════════════════════════════════════════════════════════════════════
# G4 六窗：dispatch.sh list 里真实会话必须全部 idle（过滤 model=pi-intercom-cli 的仪表条目）
# ═════════════════════════════════════════════════════════════════════════════
g4_windows() {
  local out="$TMPDIR_GATE/g4.log" t0 t1 ok=1
  t0=$(now_ms)
  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY-RUN: cd '$DISPATCH_DIR' && ./dispatch.sh list" >"$out"
    t1=$(now_ms); G4_DETAIL="dry-run"; G4_OUT="$out"; G4_MS=$((t1 - t0)); return 0
  fi
  if [ ! -d "$DISPATCH_DIR" ]; then
    echo "dispatch 目录不存在: $DISPATCH_DIR" >"$out"
    t1=$(now_ms); G4_DETAIL="dispatch 目录缺失"; G4_OUT="$out"; G4_MS=$((t1 - t0)); return 1
  fi
  ( cd "$DISPATCH_DIR" && ./dispatch.sh list ) >"$out.raw" 2>&1
  local rc=$?
  export GATE_DISPATCH_DIR="$DISPATCH_DIR"
  if [ $rc -ne 0 ]; then
    echo "dispatch.sh list 失败 (exit=$rc)" >"$out"; tail20 "$out.raw" >>"$out"
    t1=$(now_ms); G4_DETAIL="dispatch 调用失败"; G4_OUT="$out"; G4_MS=$((t1 - t0)); return 1
  fi
  if ! "$PY" - "$out.raw" "$ALLOW_BUSY_SELF" "$SELF_ID" >"$out" 2>&1 <<'PYEOF'
import json, sys
raw, allow_self, self_id = sys.argv[1], sys.argv[2] == "1", sys.argv[3]
data = json.load(open(raw, encoding="utf-8"))
sessions = data.get("sessions") or []
real = [s for s in sessions if s.get("model") != "pi-intercom-cli"]
busy = [s for s in real if str(s.get("status")) != "idle"]
if allow_self and self_id:
    busy = [s for s in busy if self_id not in str(s.get("id", "")) and self_id not in str(s.get("name", ""))]
# [2026-11-07 用户同意] 忙着的窗口如果手上只有“写域全在 work/ 下”的运行中任务（离线实验/调研），
# 不碰生产代码，不挡重启。没有对应运行中任务的忙窗口（可能是人在用）照旧拦；读不到队列也照旧拦。
import os
work_only = []
try:
    queue = json.load(open(os.path.join(os.environ["GATE_DISPATCH_DIR"], "queue.json"), encoding="utf-8"))
    tasks = queue.get("tasks") if isinstance(queue, dict) else queue
    running = {}
    for t in tasks or []:
        if t.get("state") == "running":
            running.setdefault(str(t.get("owner")), []).append(t)
    for s in busy:
        mine = running.get(str(s.get("name", "")).replace("号", "")) or []
        if mine and all(t.get("scope") and all(str(p).startswith("work/") for p in t["scope"]) for t in mine):
            work_only.append(s)
except Exception as error:  # noqa: BLE001
    print(f"（读取派单队列失败，按原规则处理：{error}）")
busy = [s for s in busy if s not in work_only]
print(f"真实会话 {len(real)} 个，busy {len(busy)} 个（另有 {len(work_only)} 个只在 work/ 下做实验，不挡重启）")
for s in real:
    flag = "BUSY" if s in busy else ("work" if s in work_only else "idle")
    print(f"  [{flag}] {s.get('name')}  status={s.get('status')}  cwd={s.get('cwd')}")
sys.exit(1 if busy else 0)
PYEOF
  then
    ok=0
  fi
  t1=$(now_ms)
  G4_DETAIL="$(head -n 1 "$out")"
  G4_OUT="$out"; G4_MS=$((t1 - t0))
  return $((1 - ok))
}

# ═════════════════════════════════════════════════════════════════════════════
# G5 active_jobs：/api/jobs 活动任务必须为 0
#   2026-10-29 修复「G5 死锁」：明文响应会被服务端截断（_write_body 修复尚未加载），
#   旧实现把它整项判 FAIL → 门禁永远过不去。
#   新逻辑：
#     1) 先走 gzip（Accept-Encoding: gzip）解压后判「传输完整 + JSON 合法 + active_jobs==0」；
#     2) 明文再打一次做对照并记录实际字节；
#     3) 明文截断只有在 (a) gzip 完整自洽、(b) 明文确实截断、(c) unloaded_files 含
#        local_web.py、(d) 磁盘 _write_body 有 while 发送循环 —— 四条**同时**成立时才降级 WARN；
#     4) --require-clean-api-jobs：恢复最严，明文截断即 FAIL（重启后复核用）。
# ═════════════════════════════════════════════════════════════════════════════
g5_active_jobs() {
  local out="$TMPDIR_GATE/g5.log" t0 t1 rc
  t0=$(now_ms)
  if [ "$DRY_RUN" = "1" ]; then
    {
      echo "DRY-RUN: GET $BASE_URL/api/jobs —— 先带 Accept-Encoding: gzip 解压判完整与 active_jobs=0"
      echo "DRY-RUN: 再发明文（identity）对照，记录 received / Content-Length"
      echo "DRY-RUN: 明文截断降级 WARN 需同时满足：gzip 完整 / 明文确实截断 / unloaded_files 含 local_web.py / 磁盘 _write_body 有 while 发送循环"
      echo "DRY-RUN: --require-clean-api-jobs=${REQUIRE_CLEAN_API_JOBS}（1=最严：明文截断即 FAIL）"
    } >"$out"
    t1=$(now_ms); G5_DETAIL="dry-run（gzip 优先 + 明文对照 + 四条件降级）"; G5_OUT="$out"; G5_MS=$((t1 - t0)); G5_WARN=0; return 0
  fi
  G5_WARN=0
  "$PY" - "$ROOT" "$BASE_URL" "$REQUIRE_CLEAN_API_JOBS" "${GATE_LOCAL_WEB_PY:-}" >"$out" 2>&1 <<'PYEOF'
import gzip, http.client, json, os, re, sys
from collections import Counter

root, base, require_clean, override = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
host = base.split("//", 1)[1]
LOCAL_WEB = override or os.path.join(root, "src/life_logger/local_web.py")

try:
    token = json.load(open(f"{root}/output/local-web/secrets.json", encoding="utf-8"))["mobile_access_token"]
except Exception as exc:
    print(f"FAIL: 读取 token 失败: {type(exc).__name__}: {exc}")
    sys.exit(1)


def fetch(path, want_gzip=True, timeout=90):
    conn = http.client.HTTPConnection(host, timeout=timeout)
    headers = {"Authorization": f"Bearer {token}"}
    if want_gzip:
        headers["Accept-Encoding"] = "gzip"
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        clen = resp.getheader("Content-Length")
        enc = (resp.getheader("Content-Encoding") or "").lower()
        status = resp.status
        incomplete = None
        try:
            raw = resp.read()
        except http.client.IncompleteRead as exc:
            raw = exc.partial or b""
            incomplete = len(raw)
    finally:
        conn.close()
    return {"status": status, "clen": clen, "enc": enc, "raw": raw, "incomplete": incomplete}


def jobs_of(payload):
    doc = json.loads(payload.decode("utf-8", errors="replace"), strict=False)
    items = doc.get("jobs") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise ValueError(f"任务列表格式无效: {type(items).__name__}")
    return items


# ── 1) gzip 路径 ────────────────────────────────────────
gz = fetch("/api/jobs", want_gzip=True)
gz_ok, gz_reason, gz_dec, jobs = False, "", None, None
try:
    if gz["status"] != 200:
        gz_reason = f"HTTP {gz['status']}"
    elif gz["incomplete"] is not None:
        gz_reason = f"IncompleteRead（已读 {gz['incomplete']} 字节）"
    elif gz["clen"] is not None and len(gz["raw"]) != int(gz["clen"]):
        gz_reason = f"gzip 流 {len(gz['raw'])} != Content-Length {gz['clen']}"
    else:
        payload = gzip.decompress(gz["raw"]) if "gzip" in gz["enc"] else gz["raw"]
        gz_dec = len(payload)
        jobs = jobs_of(payload)
        gz_ok = True
except Exception as exc:
    gz_reason = f"{type(exc).__name__}: {exc}"

# ── 2) 明文路径（对照/证据）────────────────────────────
plain = fetch("/api/jobs", want_gzip=False)
plain_incomplete = plain["incomplete"]
plain_bytes = plain_incomplete if plain_incomplete is not None else len(plain["raw"])
plain_truncated = plain_incomplete is not None or (
    plain["clen"] is not None and len(plain["raw"]) != int(plain["clen"]))

# ── 3) 环境证据：unloaded 是否含 local_web.py ────────────
health = fetch("/api/health", want_gzip=True)
unloaded = []
try:
    hp = gzip.decompress(health["raw"]) if "gzip" in health["enc"] else health["raw"]
    hd = json.loads(hp.decode("utf-8", errors="replace"), strict=False)
    unloaded = (hd.get("version") or {}).get("unloaded_files") or []
except Exception:
    unloaded = []
has_local_web = any(str(x) == "local_web.py" for x in unloaded)

# ── 4) 磁盘代码修复痕迹（_write_body 里是否有 while 发送循环）──
def fix_trace(path):
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except Exception as exc:
        return False, f"读不到 {path}: {type(exc).__name__}: {exc}"
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*def _write_body\(", ln):
            start = i
            break
    if start is None:
        return False, f"{path} 里没有 _write_body 函数"
    body = []
    for j in range(start + 1, len(lines)):
        if re.match(r"^\s{0,4}def ", lines[j]):
            break
        body.append((j + 1, lines[j]))
    w = next(((n, s.strip()) for n, s in body if re.match(r"^\s*while\b", s)), None)
    s = next(((n, x.strip()) for n, x in body if "connection.send(" in x), None)
    p = next(((n, x.strip()) for n, x in body if re.search(r"sent\s*(\+=|=\s*sent\s*\+)", x)), None)
    if w and s and p:
        return True, (f"{path}:{w[0]} `{w[1][:40]}` / :{s[0]} connection.send() / :{p[0]} `{p[1][:18]}`")
    miss = []
    if not w:
        miss.append("无 while 发送循环")
    if not s:
        miss.append("无 connection.send()")
    if not p:
        miss.append("无 sent 累加")
    return False, f"{path} 的 _write_body 缺修复痕迹：{'、'.join(miss)}"


fix_ok, fix_msg = fix_trace(LOCAL_WEB)

# ── 明细 ────────────────────────────────────────────────
active = []
counts = Counter()
if jobs is not None:
    active = [j for j in jobs if isinstance(j, dict) and j.get("status") in ("running", "queued")]
    counts = Counter(j.get("status") for j in jobs if isinstance(j, dict))
print(f"gzip       : {'OK' if gz_ok else 'FAIL'}"
      f" raw={len(gz['raw'])} decompressed={gz_dec if gz_dec is not None else '-'}"
      f" enc={gz['enc'] or '-'}" + ("" if gz_ok else f"（{gz_reason}）"))
print(f"plain      : {'TRUNCATED' if plain_truncated else 'COMPLETE'}"
      f" received={plain_bytes} Content-Length={plain['clen']} enc={plain['enc'] or '-'}")
if jobs is not None:
    print(f"jobs       : 总数 {len(jobs)}，状态分布 {dict(counts)}")
    print(f"active_jobs: {len(active)}（running+queued）")
    for j in active[:10]:
        print(f"  · {j.get('id') or j.get('job_id')}  {j.get('status')}  {str(j.get('kind') or '')[:40]}")
else:
    print("active_jobs: -（gzip 路径未拿到合法 JSON，无法判定）")
print(f"unloaded   : {len(unloaded)} 个未加载，local_web.py {'在' if has_local_web else '不在'}其中")
print(f"fix_trace  : {'OK' if fix_ok else 'MISSING'}（{fix_msg}）")

# ── 判定 ────────────────────────────────────────────────
if not gz_ok:
    print(f"verdict    : FAIL（gzip 路径失败：{gz_reason}，无法证明 active_jobs）")
    sys.exit(1)
if active:
    print(f"verdict    : FAIL（active_jobs={len(active)}，必须为 0）")
    sys.exit(1)
if not plain_truncated:
    print("verdict    : PASS（gzip 与明文都完整，active_jobs=0）")
    sys.exit(0)
if require_clean:
    print("verdict    : FAIL（--require-clean-api-jobs 最严模式：明文响应被截断）")
    sys.exit(1)

missing = []
if not has_local_web:
    missing.append("(c) unloaded_files 不含 local_web.py → 说明修复已加载却仍截断，不是未加载导致的")
if not fix_ok:
    missing.append(f"(d) 磁盘 {LOCAL_WEB} 未见修复痕迹 → {fix_msg}")
if missing:
    print("verdict    : FAIL（明文截断且降级条件不满足）")
    for m in missing:
        print(f"  ✗ {m}")
    sys.exit(1)

print("verdict    : WARN（明文截断已确认且修复尚未加载，gzip 完整、active_jobs=0）")
print("  降级依据：")
print(f"    (a) gzip 完整：raw={len(gz['raw'])} / decompressed={gz_dec} 字节，JSON 可解析")
print(f"    (b) 明文确实截断：received={plain_bytes} / Content-Length={plain['clen']}")
print("    (c) unloaded_files 含 local_web.py（修复未加载）")
print(f"    (d) 磁盘修复痕迹：{fix_msg}")
print("  说明：本项的真实契约是 active_jobs==0，已由 gzip 完整响应证明；明文截断属传输层缺陷，")
print("        重启加载 local_web.py 后必须用 --require-clean-api-jobs 复核为 PASS。")
sys.exit(3)
PYEOF
  rc=$?
  t1=$(now_ms)
  case "$rc" in
    0) G5_WARN=0 ;;
    3) G5_WARN=1 ;;
    *) G5_WARN=0 ;;
  esac
  G5_DETAIL="$(python3 - "$out" <<'PYD'
import re, sys
txt = open(sys.argv[1], encoding="utf-8", errors="replace").read()
def grab(pat, default="-"):
    m = re.search(pat, txt, re.M)
    return m.group(1).strip() if m else default
verdict = grab(r"^verdict\s*:\s*(.+)$")
active = grab(r"^active_jobs:\s*([^（\n]+)")
gz = grab(r"^gzip\s*:\s*(\S+).*?decompressed=([0-9-]+)", "-")
plain = grab(r"^plain\s*:\s*(\S+).*?received=([0-9]+).*?Content-Length=([0-9]+)", "-")
print("verdict=%s；active_jobs=%s；gzip=%s；plain=%s" % (verdict.split("（")[0], active, gz, plain.split("（")[0]))
PYD
)"
  G5_OUT="$out"; G5_MS=$((t1 - t0))
  [ "$rc" = "0" ] || [ "$rc" = "3" ]
}

# ═════════════════════════════════════════════════════════════════════════════
# G6 部署状态：deploy_state + unloaded_files（只打印，不判 FAIL —— 重启前它本来就该是 stale）
# ═════════════════════════════════════════════════════════════════════════════
g6_deploy_state() {
  local out="$TMPDIR_GATE/g6.log" t0 t1
  t0=$(now_ms)
  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY-RUN: GET $BASE_URL/api/health → version.deploy_state / version.unloaded_files" >"$out"
    t1=$(now_ms); G6_DETAIL="dry-run (INFO)"; G6_OUT="$out"; G6_MS=$((t1 - t0)); return 0
  fi
  "$PY" - "$ROOT" "$BASE_URL" >"$out" 2>&1 <<'PYEOF'
import http.client, json, sys
root, base = sys.argv[1], sys.argv[2]
host = base.split("//", 1)[1]
try:
    token = json.load(open(f"{root}/output/local-web/secrets.json", encoding="utf-8"))["mobile_access_token"]
except Exception as exc:
    print(f"WARN: 读取 token 失败: {exc}")
    token = ""
conn = http.client.HTTPConnection(host, timeout=30)
try:
    conn.request("GET", "/api/health", headers={"Authorization": f"Bearer {token}"})
    resp = conn.getresponse()
    raw = resp.read()
    code = resp.status
finally:
    conn.close()
if code != 200:
    print(f"WARN: /api/health HTTP {code}（无法读取部署状态，需人工确认）")
    sys.exit(0)          # INFO：不判 FAIL
try:
    doc = json.loads(raw.decode("utf-8", errors="replace"), strict=False)
except Exception as exc:
    print(f"WARN: /api/health 不是合法 JSON: {type(exc).__name__}: {exc}")
    sys.exit(0)
v = doc.get("version") if isinstance(doc.get("version"), dict) else {}
unloaded = v.get("unloaded_files") or []
print(f"app_version      : {v.get('app_version')}")
print(f"code_fingerprint : {v.get('code_fingerprint')}")
print(f"deployed_at      : {v.get('deployed_at')}")
print(f"deploy_state     : {v.get('deploy_state')}")
print(f"unloaded_files   : {len(unloaded)} 个")
for name in unloaded:
    print(f"    · {name}")
if v.get("deploy_state") == "stale" or unloaded:
    print("→ 预期之内：磁盘代码比进程新，重启后才会变成 current（G6 不判 FAIL）")
PYEOF
  t1=$(now_ms)
  G6_DETAIL="$(grep -E '^deploy_state|^unloaded_files' "$out" | tr '\n' ' ' | sed 's/  */ /g')"
  G6_OUT="$out"; G6_MS=$((t1 - t0))
  return 0
}

# ═════════════════════════════════════════════════════════════════════════════
# G7 磁盘未保存改动：git status 里被修改的 src/ 与 web/ 文件（只打印）
# ═════════════════════════════════════════════════════════════════════════════
g7_dirty() {
  local out="$TMPDIR_GATE/g7.log" t0 t1
  t0=$(now_ms)
  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY-RUN: git status --porcelain | 过滤 src/ web/ 的已修改文件" >"$out"
    t1=$(now_ms); G7_DETAIL="dry-run (INFO)"; G7_OUT="$out"; G7_MS=$((t1 - t0)); return 0
  fi
  {
    echo "已修改（M/AM/MM）的 src/ 与 web/ 文件："
    ( cd "$ROOT" && git status --porcelain -- src web 2>/dev/null ) \
      | awk 'substr($0,2,1)=="M" || substr($0,1,2)=="MM" || substr($0,1,2)=="AM" { print "  · " substr($0,4) }'
    echo ""
    echo "未跟踪（??）的 src/ 与 web/ 文件（重启不影响，但请确认是要交付的）："
    ( cd "$ROOT" && git status --porcelain -- src web 2>/dev/null ) \
      | awk 'substr($0,1,2)=="??" { print "  · " substr($0,4) }'
  } >"$out" 2>&1
  t1=$(now_ms)
  G7_DETAIL="$(grep -c '  · ' "$out") 个改动条目"
  G7_OUT="$out"; G7_MS=$((t1 - t0))
  return 0
}

# ═════════════════════════════════════════════════════════════════════════════
# G8 台账一致性：coverage.through 与真实已处理进度是否一致（Q568）
#   为什么需要：through 是"实时水位"，历史上曾在失败窗口未消化时也前推，
#   于是页面会宣称"已整理到 23:50"，而 17:34–20:07 的 331 行从没进过模型。
#   Q561 修了写路径，但存量数据里的假 through 仍在 → 页面必须显式登记缺口。
#
#   本项做两件事：
#     1) 用 scripts/check_coverage_consistency.py --json 重新生成
#        web/coverage-gaps.json（页面读它显示缺口，只读、不改 output/）；
#     2) 判定：**默认只报不拦**（WARN）。设 LIFE_LOGGER_GATE_GAPS_BLOCK=1 后，
#        「最近 24 小时内产生的缺口」会判 FAIL（历史缺口永不拦）。
# ═════════════════════════════════════════════════════════════════════════════
g8_coverage_gaps() {
  local out="$TMPDIR_GATE/g8.log" t0 t1 rc=0
  t0=$(now_ms)
  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY-RUN: check_coverage_consistency.py --json → 刷新 web/coverage-gaps.json（历史缺口只报不拦）" >"$out"
    t1=$(now_ms); G8_DETAIL="dry-run (INFO)"; G8_OUT="$out"; G8_MS=$((t1 - t0)); return 0
  fi
  if ! "$PY" "$ROOT/scripts/check_coverage_consistency.py" --json \
        >"$TMPDIR_GATE/g8.json" 2>"$TMPDIR_GATE/g8.err"; then
    {
      echo "自检脚本执行失败（不阻塞重启）："
      tail -20 "$TMPDIR_GATE/g8.err" 2>/dev/null
    } >"$out"
    t1=$(now_ms); G8_DETAIL="自检脚本失败（不阻塞）"; G8_OUT="$out"; G8_MS=$((t1 - t0)); return 0
  fi
  G8_BLOCK="${LIFE_LOGGER_GATE_GAPS_BLOCK:-0}"
  "$PY" - "$TMPDIR_GATE/g8.json" "$ROOT/web/coverage-gaps.json" "$G8_BLOCK" >"$TMPDIR_GATE/g8.summary" 2>&1 <<'PYG8'
import datetime, json, os, sys, tempfile
src, dst, block = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
BEIJING = datetime.timezone(datetime.timedelta(hours=8))
with open(src, encoding="utf-8") as fh:
    report = json.load(fh)
days = []
for d in report.get("days") or []:
    cls = d.get("classification")
    if cls in ("no_timeline", "no_data", "consistent"):
        continue
    days.append({
        "date": d.get("date"), "classification": cls,
        "gap_count": d.get("gap_count"), "gap_first": d.get("gap_first"),
        "gap_last": d.get("gap_last"), "through": d.get("through"),
        "last_success_cutoff": d.get("last_success_cutoff"), "reason": d.get("reason"),
    })
snapshot = {
    "schema": "coverage-gaps-snapshot-v1",
    "generated_at": datetime.datetime.now(tz=BEIJING).isoformat(timespec="seconds"),
    "route": report.get("route"),
    "source": "scripts/check_coverage_consistency.py --json",
    "summary": report.get("summary") or {},
    "asr_rows_scanned": report.get("asr_rows_scanned"),
    "days": days,
}
os.makedirs(os.path.dirname(dst), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".coverage-gaps.")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(snapshot, fh, ensure_ascii=False, indent=2)
os.replace(tmp, dst)

now = datetime.datetime.now(tz=BEIJING)
fresh = []
for d in days:
    if d["classification"] != "gap":
        continue
    try:
        end = datetime.datetime.fromisoformat(d["gap_last"])
    except (TypeError, ValueError):
        continue
    if end.tzinfo is None:
        end = end.replace(tzinfo=BEIJING)
    if (now - end).total_seconds() <= 24 * 3600:
        fresh.append(d)
print(f"快照已刷新: {dst}")
print(f"缺口天数={len([d for d in days if d['classification']=='gap'])} "
      f"其它异常={len([d for d in days if d['classification']!='gap'])} "
      f"最近24h新缺口={len(fresh)}")
for d in days[:6]:
    print(f"  {d['date']} {d['classification']} rows={d['gap_count']} "
          f"{str(d.get('gap_first') or '')[11:19]}→{str(d.get('gap_last') or '')[11:19]}")
print("FRESH=%d" % len(fresh))
print("BLOCK=%d" % (1 if block else 0))
PYG8
  {
    echo "--- 缺口快照 ---"
    cat "$TMPDIR_GATE/g8.summary"
    echo ""
    echo "（默认只报不拦；设 LIFE_LOGGER_GATE_GAPS_BLOCK=1 后，最近 24h 内的新缺口才 FAIL，历史缺口永不拦）"
  } >"$out"
  local fresh block
  fresh="$(sed -n 's/^FRESH=\([0-9]*\)$/\1/p' "$TMPDIR_GATE/g8.summary" | tail -1)"
  block="$(sed -n 's/^BLOCK=\([0-9]*\)$/\1/p' "$TMPDIR_GATE/g8.summary" | tail -1)"
  fresh="${fresh:-0}"; block="${block:-1}"
  if [ "$block" = "1" ] && [ "$fresh" -gt 0 ] 2>/dev/null; then
    rc=1
    G8_DETAIL="最近 24h 新缺口 ${fresh} 天（FAIL）"
  else
    rc=0
    G8_DETAIL="$(grep -m1 '^缺口天数=' "$TMPDIR_GATE/g8.summary")"
    [ -z "$G8_DETAIL" ] && G8_DETAIL="缺口快照已刷新"
    [ "$fresh" -gt 0 ] 2>/dev/null && G8_DETAIL="${G8_DETAIL}（最近24h ${fresh} 天，已按 env 降级为告警）"
  fi
  t1=$(now_ms); G8_OUT="$out"; G8_MS=$((t1 - t0))
  return "$rc"
}

# ── G9：已证伪方向回流（新增件；不改动 G1–G8 任何逻辑）────────────────────
# 为什么有它：work/do-not-retry-20261103/REPORT.md 列了 9 个已验证失败的方向，
# 那是文档，人会忘。scripts/check_refuted_approaches.py 把它们做成静态特征闸门：
#   R1 远场阈值降到 0.22 以下（真实误收 68%）/ R2 组内二次切分（收益 1%）
#   R3 换 ERes2NetV2（AUC 0.650→0.553）/ R4 会话级投票当分类器（recall 仅 0.52–0.58）
# 已知存量登记在 scripts/refuted_approaches_baseline.json，不判失败。
# 退出：0=PASS，1=命中新的已证伪方向，2=脚本自身错误（也当 FAIL，不能静默放过）。
g9_refuted_approaches() {
  local out="$TMPDIR_GATE/g9.log" t0 t1 rc=0
  t0=$(now_ms)
  {
    echo "--- 已证伪方向检查 ---"
    if [ -f "$ROOT/scripts/check_refuted_approaches.py" ]; then
      "$PY" "$ROOT/scripts/check_refuted_approaches.py" --root "$ROOT" 2>&1 || rc=$?
    else
      echo "SKIP: scripts/check_refuted_approaches.py 不存在"
      rc=0
    fi
    echo ""
    echo "（判据与失败数字：work/do-not-retry-20261103/REPORT.md）"
  } >"$out" 2>&1
  t1=$(now_ms); G9_OUT="$out"; G9_MS=$((t1 - t0))
  case "$rc" in
    0) G9_DETAIL="$(grep -m1 '^check_refuted_approaches:' "$out" 2>/dev/null || echo '未命中已证伪方向')" ;;
    1) G9_DETAIL="命中已证伪方向（见明细；新加的改动先读 work/do-not-retry-20261103）" ;;
    *) G9_DETAIL="检查脚本自身出错 rc=$rc（不静默放过）" ;;
  esac
  return "$rc"
}

# ═════════════════════════════════════════════════════════════════════════════
# 执行
# ═════════════════════════════════════════════════════════════════════════════
FAILED_ITEMS=""
WARN_ITEMS=""
mark() {  # mark <id> <rc> <detail> <ms> <outfile> [warn]
  local id="$1" rc="$2" detail="$3" ms="$4" out="$5" warn="${6:-0}" status
  if [ "$DRY_RUN" = "1" ]; then
    status="DRY"
  elif [ "$rc" = "0" ] && [ "$warn" = "1" ]; then
    status="WARN"; WARN_ITEMS="$WARN_ITEMS $id"
  elif [ "$rc" = "0" ]; then
    status="PASS"
  else
    status="FAIL"; FAILED_ITEMS="$FAILED_ITEMS $id"
  fi
  add_row "$id" "$status" "${ms} ms" "$detail"
  EVAL_OS+=("$status")
  EVAL_OUT+=("$out")
  EVAL_ID+=("$id")
  printf '  %-4s %-5s %8s  %s\n' "$id" "$status" "${ms} ms" "$detail"
}

if [ "$SELFTEST" = "1" ]; then
  echo "SELFTEST：只验证「结论 ↔ 退出码」映射，不执行任何检查、不接触服务"
  bad=0
  for pair in "PASS:0" "FAIL:1" "DRY-RUN:0"; do
    v="${pair%%:*}"; want="${pair##*:}"; got="$(verdict_code "$v")"
    if [ "$got" = "$want" ]; then echo "  ok   $v → exit $got"; else echo "  BAD  $v → exit $got (want $want)"; bad=1; fi
  done
  # 真实调用一次子进程，确认 exit 与结论行一致（用 dry-run，绝不跑 pytest）
  "$ROOT/scripts/pre_restart_gate.sh" --dry-run >/tmp/gate_selftest_dryrun.out 2>&1
  code=$?
  if [ "$code" = "0" ] && grep -q '^结论: DRY-RUN' /tmp/gate_selftest_dryrun.out; then
    echo "  ok   子进程 --dry-run → exit 0 且结论行 DRY-RUN"
  else
    echo "  BAD  子进程 --dry-run → exit $code"; bad=1
  fi
  rm -f /tmp/gate_selftest_dryrun.out
  if [ "$bad" = "0" ]; then echo "SELFTEST: PASS"; exit 0; else echo "SELFTEST: FAIL"; exit 1; fi
fi

EVAL_OS=(); EVAL_OUT=(); EVAL_ID=()

printf '  %-4s %-5s %8s  %s\n' "项" "结果" "耗时" "关键信息"

t0=$(now_ms); g1_syntax;     mark G1 $? "$G1_DETAIL" "$G1_MS" "$G1_OUT"
t0=$(now_ms); g2_tests; G2_RC=$?; mark G2 "$G2_RC" "$G2_DETAIL" "$G2_MS" "$G2_OUT" "${G2_WARN:-0}"
t0=$(now_ms); g3_planning;   mark G3 $? "$G3_DETAIL" "$G3_MS" "$G3_OUT"
t0=$(now_ms); g4_windows;    mark G4 $? "$G4_DETAIL" "$G4_MS" "$G4_OUT"
t0=$(now_ms); g5_active_jobs; G5_RC=$?; mark G5 "$G5_RC" "$G5_DETAIL" "$G5_MS" "$G5_OUT" "${G5_WARN:-0}"
t0=$(now_ms); g6_deploy_state; mark G6 0 "$G6_DETAIL" "$G6_MS" "$G6_OUT"
t0=$(now_ms); g7_dirty;      mark G7 0 "$G7_DETAIL" "$G7_MS" "$G7_OUT"
t0=$(now_ms); g8_coverage_gaps; mark G8 $? "$G8_DETAIL" "$G8_MS" "$G8_OUT"
t0=$(now_ms); g9_refuted_approaches; G9_RC=$?; mark G9 "$G9_RC" "$G9_DETAIL" "$G9_MS" "$G9_OUT"

hr
echo "G6 / G7 明细（仅打印，不判定）"
hr
sed 's/^/  /' "$G6_OUT" 2>/dev/null
echo
sed 's/^/  /' "$G7_OUT" 2>/dev/null

# dry-run：把每项“将执行什么”完整打出来
if [ "$DRY_RUN" = "1" ]; then
  hr
  echo "DRY-RUN：下列命令**没有**被执行，仅列出"
  hr
  i=0
  while [ $i -lt ${#EVAL_ID[@]} ]; do
    echo "[${EVAL_ID[$i]}]"
    sed 's/^/  /' "${EVAL_OUT[$i]}" 2>/dev/null
    i=$((i + 1))
  done
fi

# WARN 项：打印完整输出（不判 FAIL，但必须让读者看到「为什么允许降级」）
if [ -n "$WARN_ITEMS" ]; then
  hr
  echo "WARN 项明细（未判 FAIL；真实契约已由替代证据满足，需人工确认）"
  hr
  for id in $WARN_ITEMS; do
    i=0
    out=""
    while [ $i -lt ${#EVAL_ID[@]} ]; do
      if [ "${EVAL_ID[$i]}" = "$id" ]; then out="${EVAL_OUT[$i]}"; fi
      i=$((i + 1))
    done
    echo "[$id]"
    sed 's/^/  /' "$out" 2>/dev/null
    echo
  done
fi

# 失败项：打印其输出末尾 20 行
if [ -n "$FAILED_ITEMS" ]; then
  hr
  echo "失败项输出（每项末尾 20 行）"
  hr
  for id in $FAILED_ITEMS; do
    i=0
    out=""
    while [ $i -lt ${#EVAL_ID[@]} ]; do
      if [ "${EVAL_ID[$i]}" = "$id" ]; then out="${EVAL_OUT[$i]}"; fi
      i=$((i + 1))
    done
    echo "[$id]"
    tail20 "$out"
    echo
  done
fi

hr
VERDICT="PASS"
if [ "$DRY_RUN" = "1" ]; then
  VERDICT="DRY-RUN"
elif [ -n "$FAILED_ITEMS" ]; then
  VERDICT="FAIL"
fi

case "$VERDICT" in
  FAIL)    [ -n "$WARN_ITEMS" ] && echo "（另有 WARN 项:${WARN_ITEMS}，见上方明细）"
           echo "结论: FAIL（不可重启；失败项:${FAILED_ITEMS}）" ;;
  DRY-RUN) echo "结论: DRY-RUN（未执行任何检查，非门禁结论）" ;;
  PASS)    [ -n "$WARN_ITEMS" ] && echo "（含 WARN 项:${WARN_ITEMS}，真实契约已由替代证据满足；见上方明细）"
           echo "结论: PASS（七项门禁就绪；本脚本没有重启任何东西，重启是人的动作）" ;;
esac
exit "$(verdict_code "$VERDICT")"
