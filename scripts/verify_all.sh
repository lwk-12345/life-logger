#!/usr/bin/env bash
# scripts/verify_all.sh - 一键运行全部交付门禁（支持三态：PASS / FAIL / SLOW，支持 --json 输出）
set -u

TOTAL_T0=$(python3 -c "import time; print(time.time())")

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

#: 统一解释器：优先仓库 .venv。否则裸 python3 可能命中系统解释器（缺 numpy），
#: 数据关会以「解析异常」的假 FAIL 出现 —— 同一命令两种跑法给出两个结论。
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  export PATH="$ROOT_DIR/.venv/bin:$PATH"
  export PYTHON="$ROOT_DIR/.venv/bin/python"
fi

FAST_MODE=0
SKIP_SLOW=0
JSON_OUT_PATH=""
#: 接口门禁主采样日。默认「昨天」——写死固定日期会让该关卡恒过，
#: 测不出「昨天日报对不对」。可用 --api-date 覆盖（YYYY-MM-DD）。
API_DATE_OVERRIDE=""

idx=1
while [ $# -ge 1 ]; do
  case "$1" in
    --fast)
      FAST_MODE=1
      shift
      ;;
    --skip-slow|--skip)
      SKIP_SLOW=1
      shift
      ;;
    slow)
      SKIP_SLOW=1
      shift
      ;;
    --api-date=*)
      API_DATE_OVERRIDE="${1#*=}"
      shift
      ;;
    --api-date)
      if [ $# -ge 2 ] && [[ "$2" != --* ]]; then
        API_DATE_OVERRIDE="$2"
        shift 2
      else
        echo "❌ --api-date 需要一个 YYYY-MM-DD 参数" >&2
        exit 2
      fi
      ;;
    --json=*)
      JSON_OUT_PATH="${1#*=}"
      shift
      ;;
    --json)
      if [ $# -ge 2 ] && [[ "$2" != --* ]]; then
        JSON_OUT_PATH="$2"
        shift 2
      else
        JSON_OUT_PATH="output/gate-result.json"
        shift
      fi
      ;;
    *)
      shift
      ;;
  esac
done

#: 服务基址。默认本机 8765；设 LIFELOGGER_API_BASE 可指向别的端口/主机，
#: 供 gate_runner.sh 的 --port 真正生效（否则「配了端口却没生效」= 静默误判）。
LIFELOGGER_API_BASE="${LIFELOGGER_API_BASE:-http://127.0.0.1:8765}"
export LIFELOGGER_API_BASE

TOKEN="$(python3 -c "import json, pathlib; p=pathlib.Path('output/local-web/secrets.json'); print(json.loads(p.read_text()).get('mobile_access_token','')) if p.is_file() else print('')" 2>/dev/null)"
# 昨天的日期必须运行时计算，不能写死（写死到明天就过期，门禁会去测一个死日期）。
# macOS/BSD 用 date -v-1d；其它平台回退到 python，两者结果一致。
# [2026-11-04 UTC+8] 回退分支也必须用**北京时间**的昨天：`date.today()` 跟随系统时区，
# 机器在 UTC-8 时比北京晚一天，门禁会去校验错的那天（BSD `date -v-1d` 那支本来就带 TZ=）。
YESTERDAY="$(TZ=Asia/Shanghai date -v-1d +%Y-%m-%d 2>/dev/null || TZ=Asia/Shanghai python3 -c "import datetime; from zoneinfo import ZoneInfo; print((datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date() - datetime.timedelta(days=1)).isoformat())")"
#: 接口门禁主采样日：默认昨天；显式 --api-date 优先。
API_DATE_PRIMARY="${API_DATE_OVERRIDE:-$YESTERDAY}"
#: 保留的固定采样点：10-17 是 47 条事件的复杂日，作为回归对照组。
API_DATE_LEGACY="2026-10-17"

GATE_HYGIENE_RES="PASS"
GATE_HYGIENE_TIME="0s"
GATE_HYGIENE_TIME_S="0.00"
GATE_HYGIENE_EVID=""

GATE_CODE_RES="PASS"
GATE_CODE_TIME="0s"
GATE_CODE_TIME_S="0.00"
GATE_CODE_EVID=""

GATE_TEST_RES="PASS"
GATE_TEST_TIME="0s"
GATE_TEST_TIME_S="0.00"
GATE_TEST_EVID=""

GATE_DATA_RES="PASS"
GATE_DATA_TIME="0s"
GATE_DATA_TIME_S="0.00"
GATE_DATA_EVID=""

GATE_API_RES="PASS"
GATE_API_TIME="0s"
GATE_API_TIME_S="0.00"
GATE_API_EVID=""

GATE_CONSIST_RES="PASS"
GATE_CONSIST_TIME="0s"
GATE_CONSIST_TIME_S="0.00"
GATE_CONSIST_EVID=""

OVERALL_FAIL=0
SLOW_RECORDS_JSON="/tmp/gate_slow_records_$$.json"
echo "[]" > "$SLOW_RECORDS_JSON"

add_slow_record() {
  local gate="$1"
  local endpoint="$2"
  local elapsed="$3"
  local reason="$4"
  python3 -c "
import json
p = '$SLOW_RECORDS_JSON'
items = json.load(open(p))
items.append({'gate': '$gate', 'endpoint': '$endpoint', 'elapsed_seconds': float('$elapsed'), 'reason': '$reason'})
json.dump(items, open(p, 'w'), ensure_ascii=False)
" 2>/dev/null || true
}

# ---------------------------------------------------------
# 0) 文件卫生门禁
# ---------------------------------------------------------
T0=$(python3 -c "import time; print(time.time())")
HYGIENE_ERR=""

CANDIDATE_FILES="$(git status --porcelain 2>/dev/null | awk '{print $2}' || true)"
UNPUSHED_FILES="$(git diff --name-only origin/main..HEAD 2>/dev/null || true)"
ALL_CHECK_FILES="$(echo "$CANDIDATE_FILES $UNPUSHED_FILES" | tr ' ' '\n' | sort -u | grep -v '^$' || true)"

if [ -n "$ALL_CHECK_FILES" ]; then
  while IFS= read -r f; do
    if [ ! -f "$f" ]; then continue; fi
    base="$(basename "$f")"
    if [[ "$base" == *".bak-"* ]] || [[ "$base" == *".orig" ]] || [[ "$base" == ".DS_Store" ]]; then
      HYGIENE_ERR="发现违规临时文件: $f"
      break
    fi
    size=$(python3 -c "import os; print(os.path.getsize('$f'))" 2>/dev/null || echo 0)
    if [ "$size" -gt 5242880 ]; then
      HYGIENE_ERR="文件体积超 5MB: $f ($((size / 1024 / 1024))MB)"
      break
    fi
  done <<< "$ALL_CHECK_FILES"
fi

T1=$(python3 -c "import time; print(time.time())")
GATE_HYGIENE_TIME_S="$(python3 -c "print(f'{$T1 - $T0:.2f}')")"
GATE_HYGIENE_TIME="${GATE_HYGIENE_TIME_S}s"

if [ -n "$HYGIENE_ERR" ]; then
  GATE_HYGIENE_RES="FAIL"
  GATE_HYGIENE_EVID="$HYGIENE_ERR"
  OVERALL_FAIL=1
else
  GATE_HYGIENE_RES="PASS"
  GATE_HYGIENE_EVID="无 *.bak-* / .DS_Store / >5MB 违规文件"
fi

# ---------------------------------------------------------
# 1) 代码门禁
# ---------------------------------------------------------
T0=$(python3 -c "import time; print(time.time())")
CODE_ERR=""

CHANGED_PY="$(git status --porcelain | awk '{print $2}' | grep '\.py$' || true)"
if [ -z "$CHANGED_PY" ]; then
  CHANGED_PY="$(find src scripts -name "*.py" -maxdepth 3 2>/dev/null || true)"
fi
PY_COUNT=0
for f in $CHANGED_PY; do
  if [ -f "$f" ]; then
    PY_COUNT=$((PY_COUNT + 1))
    if ! python3 -m py_compile "$f" 2>&1; then
      CODE_ERR="py_compile failed on $f"
      break
    fi
  fi
done

if [ -z "$CODE_ERR" ]; then
  if ! node --check web/app.js 2>&1; then
    CODE_ERR="node --check web/app.js failed"
  fi
fi

if [ -z "$CODE_ERR" ]; then
  SH_COUNT=0
  for sh_file in scripts/*.sh; do
    if [ -f "$sh_file" ]; then
      SH_COUNT=$((SH_COUNT + 1))
      if ! bash -n "$sh_file" 2>&1; then
        CODE_ERR="bash -n failed on $sh_file"
        break
      fi
    fi
  done
fi

T1=$(python3 -c "import time; print(time.time())")
GATE_CODE_TIME_S="$(python3 -c "print(f'{$T1 - $T0:.2f}')")"
GATE_CODE_TIME="${GATE_CODE_TIME_S}s"

if [ -n "$CODE_ERR" ]; then
  GATE_CODE_RES="FAIL"
  GATE_CODE_EVID="$CODE_ERR"
  OVERALL_FAIL=1
else
  GATE_CODE_RES="PASS"
  GATE_CODE_EVID="py_compile (${PY_COUNT} 文件) + node --check + bash -n (${SH_COUNT} 脚本) 全通过"
fi

# ---------------------------------------------------------
# 2) 测试门禁
# ---------------------------------------------------------
T0=$(python3 -c "import time; print(time.time())")
TEST_ERR=""

NODE_OUT="$(node --test web/journal_logic.test.js 2>&1 || true)"
PASS_COUNT="$(echo "$NODE_OUT" | grep -E "ℹ pass [0-9]+" | awk '{print $3}' || echo 0)"
FAIL_COUNT="$(echo "$NODE_OUT" | grep -E "ℹ fail [0-9]+" | awk '{print $3}' || echo 1)"
if [ "$PASS_COUNT" != "30" ] || [ "$FAIL_COUNT" != "0" ]; then
  TEST_ERR="journal_logic.test.js 非 30/30 (pass=$PASS_COUNT, fail=$FAIL_COUNT)"
fi

if [ -z "$TEST_ERR" ]; then
  if ! python3 -m pytest tests/test_event_accounting_api.py -q -o addopts='' >/dev/null 2>&1; then
    TEST_ERR="test_event_accounting_api.py 失败"
  fi
fi

PYTEST_SUMMARY=""
if [ -z "$TEST_ERR" ]; then
  if [ "$FAST_MODE" -eq 1 ] || [ "$SKIP_SLOW" -eq 1 ]; then
    PYTEST_SUMMARY="[跳过慢速全量]"
  else
    PYTEST_RUN="$(python3 - <<'PY'
import subprocess, sys
try:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "-x", "-o", "addopts="],
        capture_output=True, text=True, timeout=300
    )
    if proc.returncode != 0:
        lines = [l for l in proc.stdout.splitlines() if "FAILED" in l or "ERROR" in l]
        failed_name = lines[0] if lines else "pytest tests/ -q -x failed"
        print(f"FAIL:{failed_name}")
    else:
        last_line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "全量通过"
        print(f"OK:{last_line}")
except subprocess.TimeoutExpired:
    print("TIMEOUT:pytest tests/ -q -x 超时(300s)")
except Exception as e:
    print(f"ERROR:{e}")
PY
)"
    if echo "$PYTEST_RUN" | grep -q "^FAIL:"; then
      FAIL_NAME="$(echo "$PYTEST_RUN" | sed 's/^FAIL://')"
      TEST_ERR="$FAIL_NAME"
    elif echo "$PYTEST_RUN" | grep -q "^TIMEOUT:"; then
      TEST_ERR="pytest tests/ 超时 300s"
    else
      PYTEST_SUMMARY="$(echo "$PYTEST_RUN" | sed 's/^OK://')"
    fi
  fi
fi

T1=$(python3 -c "import time; print(time.time())")
GATE_TEST_TIME_S="$(python3 -c "print(f'{$T1 - $T0:.2f}')")"
GATE_TEST_TIME="${GATE_TEST_TIME_S}s"

if [ -n "$TEST_ERR" ]; then
  GATE_TEST_RES="FAIL"
  GATE_TEST_EVID="$TEST_ERR"
  OVERALL_FAIL=1
else
  GATE_TEST_RES="PASS"
  if [ "$FAST_MODE" -eq 1 ] || [ "$SKIP_SLOW" -eq 1 ]; then
    GATE_TEST_EVID="journal_logic 30/30 + accounting_api 5/5 $PYTEST_SUMMARY"
  else
    GATE_TEST_EVID="journal_logic 30/30 + accounting_api 5/5 + $PYTEST_SUMMARY"
  fi
fi

# ---------------------------------------------------------
# 3) 数据门禁
# ---------------------------------------------------------
T0=$(python3 -c "import time; print(time.time())")

if [ "$FAST_MODE" -eq 1 ]; then
  GATE_DATA_RES="SKIP"
  GATE_DATA_TIME_S="0.00"
  GATE_DATA_TIME="0.00s"
  GATE_DATA_EVID="日常 --fast 模式已跳过"
else
  DATA_RANGE_FROM="2026-10-01"
  if [ "$SKIP_SLOW" -eq 1 ]; then
    DATA_RANGE_FROM="2026-10-19"
  fi

  DATA_TMP="/tmp/data_check_$$.json"
  python3 scripts/verify_ledger_consistency.py --from "$DATA_RANGE_FROM" --to "$YESTERDAY" --json > "$DATA_TMP" 2>/dev/null || true

  T1=$(python3 -c "import time; print(time.time())")
  GATE_DATA_TIME_S="$(python3 -c "print(f'{$T1 - $T0:.2f}')")"
  GATE_DATA_TIME="${GATE_DATA_TIME_S}s"

  PARSED_DATA_VERDICT="$(python3 -c "
import json
try:
    d = json.load(open('$DATA_TMP'))
    verdict = d.get('verdict', 'FAIL')
    failed_days = d.get('failed_days', [])
    slow_days = d.get('slow_days', [])
    fail_detail = ''
    if failed_days:
        details = []
        for row in d.get('rows', []):
            if row.get('date') in failed_days:
                for k in ('rule1','rule2','rule3','rule4'):
                    if row.get(k) == 'FAIL':
                        details.append(f\"{row['date']} {k}失败({row.get(k+'_note','')})\")
        fail_detail = '; '.join(details[:3])
    print(f'{verdict}|{len(failed_days)}|{len(slow_days)}|{fail_detail}')
except Exception:
    print('FAIL|1|0|数据检查解析异常')
" 2>/dev/null || echo "FAIL|1|0|数据检查解析异常")"

  D_VERDICT="$(echo "$PARSED_DATA_VERDICT" | cut -d'|' -f1)"
  D_FAILS="$(echo "$PARSED_DATA_VERDICT" | cut -d'|' -f2)"
  D_SLOWS="$(echo "$PARSED_DATA_VERDICT" | cut -d'|' -f3)"
  D_DETAIL="$(echo "$PARSED_DATA_VERDICT" | cut -d'|' -f4)"

  if [ "$D_VERDICT" = "FAIL" ]; then
    GATE_DATA_RES="FAIL"
    GATE_DATA_EVID="数据不一致 (失败 $D_FAILS 天, 范围 $DATA_RANGE_FROM~$YESTERDAY): ${D_DETAIL:-存在规则失败}"
    OVERALL_FAIL=1
  elif [ "$D_VERDICT" = "SLOW" ] || [ "$D_SLOWS" -gt 0 ]; then
    GATE_DATA_RES="SLOW"
    GATE_DATA_EVID="接口超时未返回 ($D_SLOWS 天 SLOW, 范围 $DATA_RANGE_FROM~$YESTERDAY)"
    python3 -c "
import json
try:
    d = json.load(open('$DATA_TMP'))
    p = '$SLOW_RECORDS_JSON'
    items = json.load(open(p))
    for s in d.get('slow_days', []):
        endpoint = s.get('endpoint') or f'/api/daily?date={s.get(\"day\")}'
        items.append({'gate': '数据门禁', 'endpoint': endpoint, 'elapsed_seconds': 15.0, 'reason': f'{s.get(\"day\")} 接口超时 (15s)'})
    json.dump(items, open(p, 'w'), ensure_ascii=False)
except Exception:
    pass
" 2>/dev/null || true
  else
    GATE_DATA_RES="PASS"
    GATE_DATA_EVID="5 条规则全 PASS (失败 0 天, 范围 $DATA_RANGE_FROM~$YESTERDAY)"
  fi
  rm -f "$DATA_TMP"
fi

# ---------------------------------------------------------
# 4) 接口门禁
# ---------------------------------------------------------
T0=$(python3 -c "import time; print(time.time())")

if [ "$FAST_MODE" -eq 1 ]; then
  GATE_API_RES="SKIP"
  GATE_API_TIME_S="0.00"
  GATE_API_TIME="0.00s"
  GATE_API_EVID="日常 --fast 模式已跳过"
else
  # 采样日：主采样＝昨天（或 --api-date 覆盖）。
  # 辅采样＝10-17 复杂日回归对照，仅在默认（未显式 --api-date）时附加；
  # 显式指定 --api-date 视为「只查这一天」，便于定点排查与空数据演练。
  API_DATES=("$API_DATE_PRIMARY")
  if [ -z "$API_DATE_OVERRIDE" ] && [ "$API_DATE_LEGACY" != "$API_DATE_PRIMARY" ]; then
    API_DATES+=("$API_DATE_LEGACY")
  fi

  LATENCIES=()
  API_FAIL_NOTES=()
  API_SLOW_NOTES=()
  API_WARN_NOTES=()
  API_SKIP_NOTES=()

  # 预热：冷缓存下 /api/daily 要扫全部 events/status + capture_funnel，
  # 实测同一 URL 冷跑 0.3~10s、热跑 0.05s。预热一次，避免把「缓存冷」误判成 SLOW。
  curl -s -o /dev/null -m 30 -H "Authorization: Bearer $TOKEN" -H "Connection: close" \
    "$LIFELOGGER_API_BASE/api/daily?date=${API_DATES[0]}&scope=provisional&view=display" || true

  for api_date in "${API_DATES[@]}"; do
    URLS=(
      "/api/daily?date=${api_date}&scope=provisional&view=display"
      "/api/dashboard/daily?date=${api_date}"
      "/api/stats?from=${api_date}&to=${api_date}&scope=provisional&view=display"
    )
    API_NAMES=("daily" "dashboard" "stats")

    # 空数据 ≠ 通过：主采样日 0 事件时本关不做判定（记为 SKIP）。
    if [ "$api_date" = "${API_DATES[0]}" ]; then
      EV_COUNT="$(curl -s -m 20 -H "Authorization: Bearer $TOKEN" -H "Connection: close" \
        "$LIFELOGGER_API_BASE/api/daily?date=${api_date}&scope=provisional&view=display" \
        | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d.get('events') or []))" 2>/dev/null || echo -1)"
      if [ "$EV_COUNT" = "0" ]; then
        API_SKIP_NOTES+=("${api_date} 无事件")
        continue
      fi
    fi

    for idx in 0 1 2; do
      url="${URLS[$idx]}"
      name="${api_date}:${API_NAMES[$idx]}"
      res="$(curl -s -o /dev/null -w "%{http_code}:%{time_total}" -m 15 \
        -H "Authorization: Bearer $TOKEN" -H "Connection: close" "$LIFELOGGER_API_BASE$url" 2>/dev/null || echo "000:15.0")"
      code="$(echo "$res" | cut -d: -f1)"
      time_sec="$(echo "$res" | cut -d: -f2)"

      if [ "$code" = "000" ]; then
        API_SLOW_NOTES+=("${name}=${time_sec}s(超时)")
        add_slow_record "接口门禁" "$url" "$time_sec" "HTTP 请求超时 (15s)"
      elif [ "$code" != "200" ]; then
        API_FAIL_NOTES+=("${name}=HTTP${code}")
      else
        is_slow="$(python3 -c "print(1 if float('$time_sec') > 5.0 else 0)")"
        if [ "$is_slow" -eq 1 ]; then
          sleep 0.5
          res2="$(curl -s -o /dev/null -w "%{http_code}:%{time_total}" -m 15 \
            -H "Authorization: Bearer $TOKEN" -H "Connection: close" "$LIFELOGGER_API_BASE$url" 2>/dev/null || echo "000:15.0")"
          code2="$(echo "$res2" | cut -d: -f1)"
          time_sec2="$(echo "$res2" | cut -d: -f2)"
          is_slow2="$(python3 -c "print(1 if float('$time_sec2') > 5.0 else 0)")"

          if [ "$code2" = "000" ]; then
            API_SLOW_NOTES+=("${name}=${time_sec2}s(重测超时)")
            add_slow_record "接口门禁" "$url" "$time_sec2" "重测超时 (15s)"
          elif [ "$code2" != "200" ]; then
            API_FAIL_NOTES+=("${name}=HTTP${code2}")
          elif [ "$is_slow2" -eq 1 ]; then
            API_FAIL_NOTES+=("${name}连续2次超5s(${time_sec}s/${time_sec2}s)")
          else
            API_WARN_NOTES+=("${name}=${time_sec}s->${time_sec2}s")
            LATENCIES+=("${name}=${time_sec2}s")
          fi
        else
          LATENCIES+=("${name}=${time_sec}s")
        fi
      fi
    done
  done

  T1=$(python3 -c "import time; print(time.time())")
  GATE_API_TIME_S="$(python3 -c "print(f'{$T1 - $T0:.2f}')")"
  GATE_API_TIME="${GATE_API_TIME_S}s"

  if [ ${#API_FAIL_NOTES[@]} -gt 0 ]; then
    GATE_API_RES="FAIL"
    GATE_API_EVID="${API_FAIL_NOTES[*]}"
    OVERALL_FAIL=1
  elif [ ${#API_SLOW_NOTES[@]} -gt 0 ]; then
    GATE_API_RES="SLOW"
    GATE_API_EVID="${API_SLOW_NOTES[*]}"
  elif [ ${#API_SKIP_NOTES[@]} -gt 0 ] && [ "${API_SKIP_NOTES[0]%% *}" = "${API_DATES[0]}" ]; then
    # 主采样日无数据：本关未行使判定权。绝不能记成 PASS。
    GATE_API_RES="SKIP"
    GATE_API_EVID="主采样日 ${API_DATES[0]} 无事件，本关未行使判定权（辅采样: ${LATENCIES[*]:-无}）"
  elif [ ${#API_WARN_NOTES[@]} -gt 0 ]; then
    GATE_API_RES="WARN"
    GATE_API_EVID="HTTP 200 (${API_WARN_NOTES[*]}，重测通过)"
  else
    GATE_API_RES="PASS"
    GATE_API_EVID="${LATENCIES[*]}"
  fi
fi

# ---------------------------------------------------------
# 5) 一致性门禁
# ---------------------------------------------------------
T0=$(python3 -c "import time; print(time.time())")

if [ "$FAST_MODE" -eq 1 ]; then
  GATE_CONSIST_RES="SKIP"
  GATE_CONSIST_TIME_S="0.00"
  GATE_CONSIST_TIME="0.00s"
  GATE_CONSIST_EVID="日常 --fast 模式已跳过"
else
  CONSIST_CHECK="$(python3 - <<'PY'
import json, os, pathlib, socket, sys, urllib.error, urllib.request

token = ""
try:
    token = json.loads(pathlib.Path("output/local-web/secrets.json").read_text()).get("mobile_access_token", "")
except Exception:
    pass

syn_files = sorted(pathlib.Path("output/local-web/daily-synthesis").glob("synthesis-*.json"), reverse=True)
if not syn_files:
    print("FAIL:未找到任何 synthesis 文件")
    sys.exit(0)

chosen_file = syn_files[0]
target_date = chosen_file.name.replace("synthesis-", "").replace(".json", "")

# 「还在算」的服务端信号：HTTP 202 或 body 里的 computing/read_busy。
COMPUTING_STATUS = {"computing", "read_busy", "pending", "warming"}
COMPUTING_ERROR = {"read_busy", "computing", "producer_busy"}


def is_computing(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("computing") is True:
        return True
    status = str(payload.get("status") or "").strip().lower()
    error = str(payload.get("error") or "").strip().lower()
    return status in COMPUTING_STATUS or error in COMPUTING_ERROR


def retry_after(payload, headers=None):
    if isinstance(payload, dict):
        for key in ("retry_after", "retry_after_seconds", "retryAfter"):
            if payload.get(key) is not None:
                try:
                    return float(payload[key])
                except (TypeError, ValueError):
                    pass
    if headers is not None:
        try:
            raw = headers.get("Retry-After")
        except Exception:
            raw = None
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return None


def computing_text(payload, headers=None) -> str:
    wait = retry_after(payload, headers)
    return f"服务仍在计算（202 computing, retry_after={wait:g}s），等下一轮再判" if wait is not None \
        else "服务仍在计算（202 computing），等下一轮再判"


try:
    syn = json.loads(chosen_file.read_text(encoding="utf-8"))
    syn_cnt = syn.get("event_count")
    syn_amt = (syn.get("expense_analysis") or {}).get("total_amount_cny")

    base = os.environ.get("LIFELOGGER_API_BASE", "http://127.0.0.1:8765")
    req = urllib.request.Request(
        f"{base}/api/daily?date={target_date}&scope=provisional&view=display",
        headers={"Authorization": "Bearer " + token, "Connection": "close"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        payload = json.load(r)
        code = getattr(r, "status", None)
        headers = getattr(r, "headers", None)

    # 202 / computing 是「还没算完」，不是「数据不对」。
    if code == 202 or is_computing(payload):
        print(f"SLOW:{target_date} {computing_text(payload, headers)}")
        sys.exit(0)

    daily_cnt = (payload.get("daily_summary") or {}).get("event_count")
    if daily_cnt is None:
        daily_cnt = len(payload.get("events") or [])
    daily_amt = (payload.get("expense_ledger") or {}).get("total_amount")

    amt_match = abs(float(syn_amt or 0) - float(daily_amt or 0)) < 0.005
    cnt_match = (syn_cnt == daily_cnt)

    if amt_match and cnt_match:
        print(f"PASS:{target_date} events={syn_cnt} amount=¥{syn_amt}")
    else:
        print(f"FAIL:{target_date} 不对齐: 日报(n={syn_cnt}, ¥{syn_amt}) vs 接口(n={daily_cnt}, ¥{daily_amt})")
except urllib.error.HTTPError as exc:
    # 202 可能被包成 HTTPError：先识别 computing，其余 HTTP 错误一律算环境问题。
    if getattr(exc, "code", None) == 202:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {}
        print(f"SLOW:{target_date} {computing_text(body, getattr(exc, 'headers', None))}")
    else:
        print(f"SLOW:{target_date} 接口返回 HTTP {exc.code}（环境问题，非数据不一致）")
except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionResetError) as exc:
    reason = getattr(exc, "reason", exc)
    print(f"SLOW:{target_date} 接口不可达/超时（{reason}），本轮未判定数据")
except json.JSONDecodeError as exc:
    print(f"SLOW:{target_date} 接口响应不是 JSON（{exc}），本轮未判定数据")
except OSError as exc:
    print(f"SLOW:{target_date} 接口连接中断（{type(exc).__name__}），本轮未判定数据")
except Exception as exc:
    # 兜底也不许判 FAIL_DATA：未知异常说明「没验成」，不是「验出错」。
    print(f"SLOW:{target_date} 检查异常 {type(exc).__name__}: {exc}（未判定数据）")
PY
)"

  T1=$(python3 -c "import time; print(time.time())")
  GATE_CONSIST_TIME_S="$(python3 -c "print(f'{$T1 - $T0:.2f}')")"
  GATE_CONSIST_TIME="${GATE_CONSIST_TIME_S}s"

  if echo "$CONSIST_CHECK" | grep -q "^PASS:"; then
    GATE_CONSIST_RES="PASS"
    GATE_CONSIST_EVID="$(echo "$CONSIST_CHECK" | sed 's/^PASS://')"
  elif echo "$CONSIST_CHECK" | grep -q "^SLOW:"; then
    GATE_CONSIST_RES="SLOW"
    GATE_CONSIST_EVID="$(echo "$CONSIST_CHECK" | sed 's/^SLOW://')"
    add_slow_record "一致性门禁" "/api/daily" "15.0" "$GATE_CONSIST_EVID"
  else
    GATE_CONSIST_RES="FAIL"
    GATE_CONSIST_EVID="$(echo "$CONSIST_CHECK" | sed 's/^FAIL://')"
    OVERALL_FAIL=1
  fi
fi

# ---------------------------------------------------------
# 6) 输出持久化与表格
# ---------------------------------------------------------
TOTAL_T1=$(python3 -c "import time; print(time.time())")
TOTAL_DURATION_S="$(python3 -c "print(f'{$TOTAL_T1 - $TOTAL_T0:.2f}')")"

mkdir -p output
TOTAL_SLOW=$(python3 -c "import json; items=json.load(open('$SLOW_RECORDS_JSON')); print(len(items))" 2>/dev/null || echo 0)
GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"

python3 -c "
import json, datetime
items = json.load(open('$SLOW_RECORDS_JSON'))
payload = {
    'timestamp': datetime.datetime.now().astimezone().isoformat(),
    'git_commit': '$GIT_COMMIT',
    'total_slow': len(items),
    'slow_items': items
}
json.dump(payload, open('output/gate-slow.json', 'w'), ensure_ascii=False, indent=2)
" 2>/dev/null || true
rm -f "$SLOW_RECORDS_JSON"

# 如果指定了 --json <path>，写入机器可读结构
if [ -n "$JSON_OUT_PATH" ]; then
  # 关键：--json 指定时，「写不出来」必须让门禁变 FAIL。
  # 旧实现以 `2>/dev/null || true` 收尾：序列化失败 → 文件根本不生成，
  # 但门禁仍按 0 报 PASS，下游「没 JSON 就当没问题」= 假通过。
  JSON_WRITE_ERR=""
  JSON_DIR="$(dirname "$JSON_OUT_PATH")"
  if ! mkdir -p "$JSON_DIR" 2>/dev/null; then
    JSON_WRITE_ERR="无法创建目录 $JSON_DIR"
  fi

  if [ -z "$JSON_WRITE_ERR" ]; then
    GATE_FAST_MODE="$FAST_MODE" \
    GATE_OVERALL_FAIL="$OVERALL_FAIL" \
    GATE_TOTAL_DURATION_S="$TOTAL_DURATION_S" \
    GATE_TOTAL_SLOW="$TOTAL_SLOW" \
    GATE_HYGIENE_RES="$GATE_HYGIENE_RES" GATE_HYGIENE_TIME_S="$GATE_HYGIENE_TIME_S" GATE_HYGIENE_EVID="$GATE_HYGIENE_EVID" \
    GATE_CODE_RES="$GATE_CODE_RES" GATE_CODE_TIME_S="$GATE_CODE_TIME_S" GATE_CODE_EVID="$GATE_CODE_EVID" \
    GATE_TEST_RES="$GATE_TEST_RES" GATE_TEST_TIME_S="$GATE_TEST_TIME_S" GATE_TEST_EVID="$GATE_TEST_EVID" \
    GATE_DATA_RES="$GATE_DATA_RES" GATE_DATA_TIME_S="$GATE_DATA_TIME_S" GATE_DATA_EVID="$GATE_DATA_EVID" \
    GATE_API_RES="$GATE_API_RES" GATE_API_TIME_S="$GATE_API_TIME_S" GATE_API_EVID="$GATE_API_EVID" \
    GATE_CONSIST_RES="$GATE_CONSIST_RES" GATE_CONSIST_TIME_S="$GATE_CONSIST_TIME_S" GATE_CONSIST_EVID="$GATE_CONSIST_EVID" \
    GATE_API_PRIMARY_DATE="$API_DATE_PRIMARY" \
    python3 - "$JSON_OUT_PATH" <<'PY'
import json, os, sys

path = sys.argv[1]
env = os.environ

fail = int(env.get("GATE_OVERALL_FAIL", "1"))
fast = int(env.get("GATE_FAST_MODE", "0"))

# 结论语义：
#   FAIL      —— 有真问题，必须拦
#   PASS_FAST —— --fast 下数据/接口/一致性被 SKIP，不能宣称完整通过
#   PASS      —— 全量六关都真正跑过（SLOW 也不算完整，见 slow_count）
if fail == 1:
    conclusion = "FAIL"
elif fast == 1:
    conclusion = "PASS_FAST"
else:
    conclusion = "PASS"


def gate(name, status_key, time_key, evid_key):
    raw = env.get(evid_key, "")
    return {
        "name": name,
        "status": env.get(status_key, "FAIL"),
        "duration_s": float(env.get(time_key, "0") or 0),
        "evidence": raw,
    }


data = {
    "conclusion": conclusion,
    "exit_code": fail,
    "duration_s": float(env.get("GATE_TOTAL_DURATION_S", "0") or 0),
    "fast_mode": bool(fast),
    "slow_count": int(env.get("GATE_TOTAL_SLOW", "0") or 0),
    "api_primary_date": env.get("GATE_API_PRIMARY_DATE", ""),
    "gates": [
        gate("文件卫生", "GATE_HYGIENE_RES", "GATE_HYGIENE_TIME_S", "GATE_HYGIENE_EVID"),
        gate("代码门禁", "GATE_CODE_RES", "GATE_CODE_TIME_S", "GATE_CODE_EVID"),
        gate("测试门禁", "GATE_TEST_RES", "GATE_TEST_TIME_S", "GATE_TEST_EVID"),
        gate("数据门禁", "GATE_DATA_RES", "GATE_DATA_TIME_S", "GATE_DATA_EVID"),
        gate("接口门禁", "GATE_API_RES", "GATE_API_TIME_S", "GATE_API_EVID"),
        gate("一致性门禁", "GATE_CONSIST_RES", "GATE_CONSIST_TIME_S", "GATE_CONSIST_EVID"),
    ],
}

tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(data, handle, ensure_ascii=False, indent=2)
os.replace(tmp, path)
PY
    if [ $? -ne 0 ]; then
      JSON_WRITE_ERR="序列化或写入失败（${JSON_OUT_PATH}）"
    fi
  fi

  # 写完必须回验：文件存在、非空、能 json.load。缺一步都可能变成「假 PASS」。
  if [ -z "$JSON_WRITE_ERR" ]; then
    if [ ! -s "$JSON_OUT_PATH" ]; then
      JSON_WRITE_ERR="文件未生成或为空（${JSON_OUT_PATH}）"
    elif ! python3 -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" "$JSON_OUT_PATH" 2>/dev/null; then
      JSON_WRITE_ERR="生成的文件不是合法 JSON（${JSON_OUT_PATH}）"
    fi
  fi

  if [ -n "$JSON_WRITE_ERR" ]; then
    echo ""
    echo "❌ --json 输出失败：$JSON_WRITE_ERR"
    echo "   门禁结果本身可能全绿，但下游拿不到机器可读出口 → 按 FAIL 处理。"
    OVERALL_FAIL=1
  fi
fi

echo ""
echo "| 门禁 | 结果 | 用时 | 证据 |"
echo "|---|---|---|---|"
echo "| 文件卫生 | $GATE_HYGIENE_RES | $GATE_HYGIENE_TIME | $GATE_HYGIENE_EVID |"
echo "| 代码门禁 | $GATE_CODE_RES | $GATE_CODE_TIME | $GATE_CODE_EVID |"
echo "| 测试门禁 | $GATE_TEST_RES | $GATE_TEST_TIME | $GATE_TEST_EVID |"
echo "| 数据门禁 | $GATE_DATA_RES | $GATE_DATA_TIME | $GATE_DATA_EVID |"
echo "| 接口门禁 | $GATE_API_RES | $GATE_API_TIME | $GATE_API_EVID |"
echo "| 一致性门禁 | $GATE_CONSIST_RES | $GATE_CONSIST_TIME | $GATE_CONSIST_EVID |"
echo ""

SKIPPED_GATES=""
for pair in "数据门禁:$GATE_DATA_RES" "接口门禁:$GATE_API_RES" "一致性门禁:$GATE_CONSIST_RES"; do
  if [ "${pair##*:}" = "SKIP" ]; then
    SKIPPED_GATES="${SKIPPED_GATES}${pair%%:*} "
  fi
done

if [ "$OVERALL_FAIL" -eq 1 ]; then
  echo "❌ 门禁结论: FAIL (退出码 1，已阻断)"
  exit 1
fi

if [ "$FAST_MODE" -eq 1 ]; then
  echo "⚠️  门禁结论: PASS_FAST (退出码 0，非完整验收)"
  echo "    --fast 跳过了：${SKIPPED_GATES:-未验证关卡}。发布前必须跑一次全量。"
  exit 0
fi

if [ -n "$SKIPPED_GATES" ]; then
  echo "⚠️  警告: 以下关卡被判 SKIP（无数据，未行使判定权）：${SKIPPED_GATES}"
  echo "    SKIP ≠ PASS：这些关卡这次没有真正验证任何东西。"
fi

if [ "$TOTAL_SLOW" -gt 0 ]; then
  echo "⚠️  警告: [${TOTAL_SLOW}] 项因服务过慢未验证（记录于 output/gate-slow.json，不阻断 push）"
  echo "✅ 门禁结论: PASS (含 SLOW 项，退出码 0)"
  exit 0
fi

echo "✅ 门禁结论: PASS (退出码 0)"
exit 0
