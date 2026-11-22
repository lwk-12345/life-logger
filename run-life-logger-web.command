#!/bin/zsh
# 启动 Life Logger 本地网页服务（由 LaunchAgent com.life-logger.local-web 拉起）。
# --no-browser：服务开机自启时不要再自动打开浏览器窗口，那会额外吃一份内存。
# 需要看界面时手动访问 http://172.20.10.3:8765/ 即可。
set -e
repo="$(cd "$(dirname "$0")" && pwd)"
cd "$repo"
python_path="python3"
if [[ -f local-config.json ]]; then
  configured="$(python3 -c 'import json; print(json.load(open("local-config.json"))["python"])')"
  if [[ -x "$configured" ]]; then python_path="$configured"; fi
fi
# 模式由 scripts/switch_processing_mode.sh 写入；缺文件时保持 combined。
if [[ "$(cat output/local-web/runtime/processing-mode 2>/dev/null)" == "split" ]]; then
  exec "$python_path" scripts/run_local_web.py --no-browser --processing-mode split --no-worker-start
fi
exec "$python_path" scripts/run_local_web.py --no-browser
