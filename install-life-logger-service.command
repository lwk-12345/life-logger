#!/bin/zsh
set -e
repo="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$repo/scripts/install_local_web_service.py"
