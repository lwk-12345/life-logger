#!/usr/bin/env bash
# ==============================================================================
# AirPassport macOS 开机自启服务卸载脚本
# 作用: 停止并注销 AirPassport 的 LaunchAgent 服务，清理配置文件
# ==============================================================================
set -euo pipefail

LABEL="com.lifelogger.airpassport"
DOMAIN="gui/$(id -u)"
SERVICE="${DOMAIN}/${LABEL}"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"

echo "========================================================================"
echo "🛑 卸载 AirPassport 开机自启动服务"
echo "========================================================================"

echo "🔄 正在停止并注销 launchd 服务: ${SERVICE} ..."
launchctl bootout "${SERVICE}" 2>/dev/null || true

# 检查是否有残存的 AirPassport 进程
if pgrep -f "AirPassport.app/Contents/MacOS/AirPassport" >/dev/null 2>&1; then
    echo "⚠️  发现正在运行的 AirPassport 实例，正在终止..."
    pkill -f "AirPassport.app/Contents/MacOS/AirPassport" 2>/dev/null || true
fi

# 移除 plist
if [[ -f "${PLIST_PATH}" ]]; then
    echo "🗑️  正在删除配置文件: ${PLIST_PATH}"
    rm -f "${PLIST_PATH}"
fi

echo "========================================================================"
echo "✅ AirPassport 开机自启服务已完全卸载并清理！"
echo "========================================================================"
