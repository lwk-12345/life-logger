#!/usr/bin/env bash
# 一键编译、签名并安装 Life Logger Watch 到 Apple Watch Series 5
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCH_DIR="$REPO_ROOT/watchos/LifeLoggerWatch"
SIGNED_APP="$WATCH_DIR/build/SignedDerivedData/Build/Products/Debug-watchos/LifeLoggerWatch.app"

echo "========================================================"
echo "⌚ Life Logger Watch 真机一键安装向导 (Apple Watch S5)"
echo "========================================================"

# 先核验候选包，再查询或操作设备。
if [ ! -d "$SIGNED_APP" ]; then
    echo "❌ 未找到签名产物 $SIGNED_APP，正在自动触发编译与签名..."
    "$SCRIPT_DIR/build_signed_watch_app.sh"
fi
if [ ! -f "$SIGNED_APP/embedded.mobileprovision" ]; then
    echo "❌ 安装包缺少 embedded.mobileprovision，停止安装；请先在 Xcode 配置描述文件并重新签名。" >&2
    exit 1
fi
echo "🔍 验证签名完整性（不代表真机已安装）..."
codesign --verify --deep --strict "$SIGNED_APP"
codesign -dvvv "$SIGNED_APP" 2>&1 | grep -E "Authority|Identifier|Signed Time|TeamIdentifier"

# 1. 查找连接的 Apple Watch
echo "🔍 正在扫描已配对的 Apple Watch 设备..."
WATCH_JSON=$(xcrun devicectl list devices --json-output /dev/stdout 2>/dev/null || true)
WATCH_ID=$(echo "$WATCH_JSON" | grep -B 5 -A 25 '"platform" : "watchOS"' | grep '"identifier"' | head -n 1 | sed -E 's/.*"identifier" : "([^"]+)".*/\1/' || true)

if [ -z "$WATCH_ID" ]; then
    WATCH_ID="YOUR_WATCH_DEVICE_ID"
    echo "⚠️ 未在实时广播中找到，使用已配对的 Series 5 默认 UUID: $WATCH_ID"
else
    echo "✅ 成功找到 Apple Watch 设备: $WATCH_ID"
fi


echo ""
echo "💡 提示：安装前请确保："
echo "   1. Apple Watch 处于【佩戴亮屏并已解锁】状态；"
echo "   2. iPhone 与 Mac 处于同一 Wi-Fi 网络（或热点）；"
echo "   3. 若提示连接超时，请在手表上点亮屏幕或打开【设置 → 开发者】。"
echo ""
echo "🚀 正在向 Apple Watch ($WATCH_ID) 传输并安装应用..."

if xcrun devicectl device install app --device "$WATCH_ID" "$SIGNED_APP"; then
    echo ""
    echo "🎉 安装大功告成！"
    echo "   请在 Apple Watch 表盘应用列表中轻点打开【Life Logger】。"
    echo "   点击【开始录音】即开启双重后台长时保活；手腕自然下垂自动进入原生低功耗 AOD 状态；长按 2 秒平滑防误触停止。"
else
    echo ""
    echo "⚠️ 安装未成功完成。常见原因："
    echo "   - 手表处于休眠状态：请轻触手表表盘唤醒并解锁；"
    echo "   - Xcode 无线调试隧道未激活：可打开 Xcode -> Window -> Devices and Simulators，等待 Watch 旁边显示绿色小圆点后再执行本脚本。"
    exit 1
fi
