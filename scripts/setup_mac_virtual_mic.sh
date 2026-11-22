#!/usr/bin/env bash
set -e

# ==============================================================================
# AirPassport 虚拟麦克风通道配置助手 (macOS)
# 作用: 使得没有实体声卡/麦克风的工作室电脑（如 Mac mini, Mac Studio, 台式机）
#       能够通过 BlackHole 2ch 虚拟通道接收 AirPassport 的 16kHz 无线音频，
#       直接用于系统全局语音输入打字、微信语音、网页录音等。
# ==============================================================================

echo "========================================================================"
echo "🎙️  AirPassport macOS 虚拟麦克风通道检查与配置"
echo "========================================================================"

# 1. 检查系统中是否已存在 BlackHole
if system_profiler SPAudioDataType 2>/dev/null | grep -i "BlackHole" >/dev/null 2>&1; then
    echo "✅ [检测成功] 系统已安装 BlackHole 虚拟音频驱动！"
    echo ""
    echo "👉 下一步配置步骤："
    echo "   1. 打开 macOS【系统设置】 -> 【声音】 -> 【输入】(Sound Input)；"
    echo "   2. 将输入设备选中为【BlackHole 2ch】；"
    echo "   3. 启动 AirPassport 接收桥（命令行或顶部菜单栏应用）；"
    echo "   4. 按住 AirPassport 顶键说话，Mac 上的所有语音应用即可直接采集到 AirPassport 的声音！"
    echo "========================================================================"
    exit 0
fi

echo "⚠️  未检测到 BlackHole 虚拟音频驱动。"
echo "    工作室电脑（如 Mac mini）在没有外接麦克风的情况下，需要安装此轻量驱动以打通系统语音输入链路。"
echo ""
echo "💡 安装方式（二选一）："
echo ""
echo "【方式 1】通过 Homebrew 一键安装（推荐，需终端管理员权限）："
echo "   brew install --cask blackhole-2ch"
echo ""
echo "【方式 2】直接下载官方安装包（图形化双击安装）："
echo "   下载地址: https://existential.audio/downloads/BlackHole2ch-0.7.1.pkg"
echo ""

read -p "是否需要现在下载并打开官方安装包？[y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    TEMP_PKG="/tmp/BlackHole2ch-0.7.1.pkg"
    echo "正在下载官方驱动安装包至 ${TEMP_PKG} ..."
    curl -L -o "${TEMP_PKG}" "https://existential.audio/downloads/BlackHole2ch-0.7.1.pkg"
    echo "✅ 下载完成，正在打开 macOS 安装器..."
    open "${TEMP_PKG}"
    echo "请在弹出的系统安装向导中完成安装。安装后无需重启，音频服务将自动加载。"
fi

echo "========================================================================"
