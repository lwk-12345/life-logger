#!/bin/zsh
set -e
repo="$(cd "$(dirname "$0")" && pwd)"
cd "$repo"
python_path="$(python3 -c 'import json; print(json.load(open("local-config.json"))["python"])')"
"$python_path" scripts/local_jobs.py
status=$?
echo
echo "Life Logger 已结束，退出码：$status"
read -k 1 '?按任意键关闭窗口。'
