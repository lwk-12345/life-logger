#!/usr/bin/env bash
# 产物断言（artifact truth）一键检查。
#
# 为什么要有这个脚本
# ------------------
# 单测全绿 ≠ 系统里真有东西。Q740：8 条接线测试全绿，生产 `input-context/latest.json`
# 却是 window_count=0、活动上 0 条带应用字段；同一天还有「前端 ID 集合写了但没人读」。
# 这个脚本只跑「看真实产物」的那批断言，红 = 磁盘/API 里真的没有。
#
# 用法
#   scripts/check_artifacts_truth.sh              # 跑全部
#   scripts/check_artifacts_truth.sh -k input     # 只跑名字含 input 的
#   scripts/check_artifacts_truth.sh -v           # verbose
#
# 退出码：pytest 的退出码（0=全绿；1=有真实产物问题；5=没有用例）
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

PY="${PYTHON:-python3}"
TEST_FILE="work/artifact-truth-tests-20261103/test_artifact_truth.py"
ARTIFACT_ROOT="output/local-web"
TIMELINE_DIR="$ARTIFACT_ROOT/activity-timeline/jev"
INPUT_CONTEXT="$ARTIFACT_ROOT/watch-sessions/input-context/latest.json"

if [[ ! -f "$TEST_FILE" ]]; then
  echo "✗ 找不到产物断言文件：$TEST_FILE" >&2
  exit 2
fi

echo "════════════════════════════════════════════════════════════════"
echo " 产物断言检查（artifact truth）"
echo " 仓库：$ROOT"
echo " 产物：$ARTIFACT_ROOT"
echo "────────────────────────────────────────────────────────────────"
printf ' 时间线文件: %s 个\n' "$(ls -1 "$TIMELINE_DIR"/*.json 2>/dev/null | grep -vc corrections || echo 0)"
if [[ -f "$INPUT_CONTEXT" ]]; then
  printf ' 输入上下文: %s（window_count=%s）\n' \
    "$INPUT_CONTEXT" \
    "$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("window_count"))' "$INPUT_CONTEXT" 2>/dev/null || echo '?')"
else
  printf ' 输入上下文: 缺失（%s）\n' "$INPUT_CONTEXT"
fi
echo "════════════════════════════════════════════════════════════════"

"$PY" -m pytest "$TEST_FILE" -p no:cacheprovider --tb=short -q "$@"
code=$?

echo "────────────────────────────────────────────────────────────────"
if [[ $code -eq 0 ]]; then
  echo "✅ 产物断言全绿：磁盘/API 里确实有这些东西"
elif [[ $code -eq 5 ]]; then
  echo "⚠️ 没有用例被执行（检查 -k 过滤或产物目录）"
else
  echo "❌ 产物断言失败（退出码 ${code}）"
  echo "   这一类红代表：**系统里真的没有**，不是测试环境问题。"
  echo "   逐条看上面的 AssertionError —— 每条都写了「看哪个文件、期望什么、实际多少」。"
fi
exit $code
