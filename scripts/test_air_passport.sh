#!/usr/bin/env bash
# AirPassport 硬件按键与 macOS 接收桥实时检测入口
set -euo pipefail

REPO_DIR="$HOME/life-logger"
cd "${REPO_DIR}"

echo "========================================================================"
echo "🔍 启动 AirPassport 实体按键专用检测接口..."
echo "========================================================================"

swift run --package-path desktop/AirPassportReceiver air-passport-bridge --key-code 0x3A --dump-wav ./recordings_test -t
