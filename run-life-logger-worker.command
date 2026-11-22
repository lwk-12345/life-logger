#!/bin/zsh
repo="$(cd "$(dirname "$0")" && pwd)"
cd "$repo"
python_path="python3"
if [[ -f local-config.json ]]; then
  configured="$(python3 -c 'import json; print(json.load(open("local-config.json"))["python"])')"
  if [[ -x "$configured" ]]; then python_path="$configured"; fi
fi
exec "$python_path" scripts/processing_worker.py --no-projections
