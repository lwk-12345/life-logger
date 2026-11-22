#!/bin/zsh
# 每日体验验收：检查简报是否「干净可用」，输出报告到
# output/local-web/acceptance-<date>.md
# 由 LaunchAgent com.life-logger.daily-acceptance 每天 07:30 拉起。
set -e
repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"
export PYTHONPATH="$repo/src"
exec $HOME/.life-logger/.venv-funasr/bin/python scripts/verify_daily_experience.py --days 5

