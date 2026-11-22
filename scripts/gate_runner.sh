#!/usr/bin/env bash
# scripts/gate_runner.sh —— 门禁自动化一条命令入口（包装 scripts/gate_runner.py）
#
# 用法：
#   bash scripts/gate_runner.sh --once                  # 跑一次（默认），红黄牌定性
#   bash scripts/gate_runner.sh --watch                 # 每 30 分钟跑一次
#   bash scripts/gate_runner.sh --watch --interval 900  # 每 15 分钟
#   bash scripts/gate_runner.sh --once --fast           # 透传 verify_all.sh --fast
#
# 产物：agent-board/gate-report.json（单次）+ output/gate-history.jsonl（最近 30 次）
# 退出码：0 PASS / 10 PASS_WITH_SKIP / 20 SLOW / 30 FLAKY
#         1 FAIL_CODE / 2 FAIL_DATA / 3 FAIL_ENV
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR" || exit 3

# 解释器：优先仓库 venv（唯一装了 pytest 的那个），否则退到 python3 / python
PY_EXE=""
for candidate in "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/.venv/bin/python3"; do
  if [ -x "$candidate" ]; then PY_EXE="$candidate"; break; fi
done
if [ -z "$PY_EXE" ]; then
  if command -v python3 >/dev/null 2>&1; then PY_EXE="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then PY_EXE="$(command -v python)"
  else
    echo "gate_runner: 找不到 python 解释器" >&2
    exit 3
  fi
fi

exec "$PY_EXE" "$ROOT_DIR/scripts/gate_runner.py" --repo "$ROOT_DIR" --python "$PY_EXE" "$@"
