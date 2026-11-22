#!/usr/bin/env bash
# Life Logger Watch 7天描述文件续签与一键刷机脚本
# 支持 --dry-run 只做自检与到期时间探测
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCH_DIR="$REPO_ROOT/watchos/LifeLoggerWatch"
DERIVED_DATA="$WATCH_DIR/build/DeviceRenew"
APP_PATH="$DERIVED_DATA/Build/Products/Debug-watchos/LifeLoggerWatch.app"
WATCH_ID="YOUR_WATCH_DEVICE_ID"
WATCH_UDID="YOUR_WATCH_UDID"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
    echo "🔍 [DRY-RUN 模式] 仅执行前置环境自检与到期检测，不触发构建与装机。"
fi

echo "========================================================"
echo "⌚ Life Logger Watch 7天证书/描述文件自动续签向导"
echo "========================================================"

# ---------- 1. 前置自检 (Preflight Checks) ----------
echo "📋 [自检 1/3] 检查 Mac 蓝牙状态..."
BT_ON=0
if command -v blueutil >/dev/null 2>&1; then
    if [[ "$(blueutil -p)" == "1" ]]; then
        BT_ON=1
    fi
else
    BT_STATE=$(defaults read /Library/Preferences/com.apple.Bluetooth ControllerPowerState 2>/dev/null || echo 0)
    if [[ "$BT_STATE" == "1" ]]; then
        BT_ON=1
    fi
fi

if [[ "$BT_ON" -ne 1 ]]; then
    echo "❌ 失败：Mac 蓝牙当前处于关闭状态！" >&2
    echo "   【致命原因】：Apple Watch 无线调试隧道 (CoreDevice) 强依赖 Mac 蓝牙进行初始握手广播与对端定位。" >&2
    echo "   【修复动作】：请在 Mac 顶部控制中心打开蓝牙，或在终端执行：blueutil -p 1" >&2
    exit 1
fi
echo "   ✅ Mac 蓝牙已开启 (Bluetooth: ON)"

echo "📋 [自检 2/3] 检查配对 iPhone 状态..."
# 【2026-10-22 P-33】允许测试注入设备列表文本（不触发真实 devicectl）
if [[ -n "${WATCH_DEVICE_LIST_FILE:-}" && -f "${WATCH_DEVICE_LIST_FILE}" ]]; then
    DEVICE_LIST=$(cat "${WATCH_DEVICE_LIST_FILE}")
else
    DEVICE_LIST=$(xcrun devicectl list devices 2>&1 || true)
fi

# 【2026-10-22 P-33】原来的 `grep -qE "connected|available"` 是**子串匹配**，
# `unavailable` / `disconnected` 里同样含 available/connected 子串 →
# 设备掉线反而被判成"可用"。这里改成整词匹配，并显式排除否定态。
device_state_is_ready() {
    local name_pattern="$1"
    local line
    line=$(echo "$DEVICE_LIST" | grep -iE "$name_pattern" | head -1)
    [[ -n "$line" ]] || return 1
    # 先排除否定/断开态（整词）
    if echo "$line" | grep -qiE '(^|[^[:alnum:]_])(unavailable|disconnected)([^[:alnum:]_]|$)'; then
        return 1
    fi
    echo "$line" | grep -qiE '(^|[^[:alnum:]_])(connected|available)([^[:alnum:]_]|$)'
}

if device_state_is_ready "iPhone"; then
    echo "   ✅ 配对 iPhone 可见且通信正常"
else
    echo "❌ 失败：未检测到可用的配对 iPhone！" >&2
    echo "   【原因】：watchOS 调试通信依赖伴侣 iPhone 协同建立网络。" >&2
    echo "   【修复动作】：请确保 iPhone 与 Mac 处于同一 Wi-Fi 网络，并在手机上解锁屏幕。" >&2
    exit 2
fi

echo "📋 [自检 3/3] 检查 Apple Watch 无线调试状态..."
if device_state_is_ready "Apple.*Watch"; then
    echo "   ✅ Apple Watch 处于活跃就绪状态 (State: available/connected)"
else
    echo "❌ 失败：Apple Watch 当前处于 unavailable 状态！" >&2
    echo "   【原因】：手表可能息屏休眠、未解锁或未连入同一 Wi-Fi。" >&2
    echo "   【修复动作】：请戴上手表，轻触表盘亮屏并输入密码解锁；确保手表设置中 Wi-Fi 已连接。" >&2
    exit 3
fi

# ---------- 2. 到期时间检测 ----------
PROFILE_PATH="$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles"
ACTIVE_PROFILE=$(find "$PROFILE_PATH" -name "*.mobileprovision" -type f 2>/dev/null | head -n 1 || true)
if [[ -n "$ACTIVE_PROFILE" && -f "$ACTIVE_PROFILE" ]]; then
    EXP_DATE=$(security cms -D -i "$ACTIVE_PROFILE" 2>/dev/null | grep -A 1 "<key>ExpirationDate</key>" | grep -oE "<date>.*</date>" | sed -e 's/<date>//' -e 's/<\/date>//' || echo "未知")
    echo "ℹ️  当前系统描述文件到期时间 (UTC): $EXP_DATE"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "========================================================"
    echo "🎉 [DRY-RUN 结果] 所有三项前置环境自检全部通过！"
    echo "   正式执行时将自动调用 xcodebuild 刷新签发、编译产物并直刷手表。"
    exit 0
fi

# ---------- 3. 触发刷新描述文件并重新构建 ----------
echo ""
echo "🚀 [构建] 正在连接 Apple 开发者门户刷新 7 天描述文件并构建 V66+ 产物..."
mkdir -p "$DERIVED_DATA"
set +e
BUILD_OUT=$(xcodebuild -project "$WATCH_DIR/LifeLoggerWatch.xcodeproj" \
                       -scheme LifeLoggerWatch \
                       -configuration Debug \
                       -destination 'generic/platform=watchOS' \
                       -derivedDataPath "$DERIVED_DATA" \
                       -allowProvisioningUpdates \
                       build 2>&1)
BUILD_STATUS=$?
set -e

# 【2026-10-22 P-33】失败时清理本次构建目录，避免半成品留在 build/DeviceRenew
cleanup_build_dir() {
    if [[ -n "${DERIVED_DATA:-}" && -d "$DERIVED_DATA" ]]; then
        rm -rf "$DERIVED_DATA"
        echo "   🧹 已清理本次构建目录：$DERIVED_DATA"
    fi
}

if [[ $BUILD_STATUS -ne 0 ]]; then
    echo "❌ 编译/签名刷新失败 (退出码: $BUILD_STATUS)！" >&2
    echo "$BUILD_OUT" | tail -n 25 >&2
    echo "   【修复动作】：如提示证书未授权或需要登录，请在 Xcode -> Settings -> Accounts 检查 Apple ID 登录状态。" >&2
    cleanup_build_dir
    exit 4
fi

if [[ ! -d "$APP_PATH" ]]; then
    echo "❌ 未找到构建产物目录: $APP_PATH" >&2
    cleanup_build_dir
    exit 5
fi

NEW_EXP=$(security cms -D -i "$APP_PATH/embedded.mobileprovision" 2>/dev/null | grep -A 1 "<key>ExpirationDate</key>" | grep -oE "<date>.*</date>" | sed -e 's/<date>//' -e 's/<\/date>//' || echo "未知")
echo "   ✅ 构建与签名成功！新描述文件有效期顺延至 (UTC): $NEW_EXP"

# ---------- 4. 安装至 Apple Watch ----------
echo ""
echo "📲 [安装] 正在将应用传输并安装至 Apple Watch ($WATCH_ID)..."
set +e
INSTALL_OUT=$(xcrun devicectl device install app --device "$WATCH_ID" "$APP_PATH" 2>&1)
INSTALL_STATUS=$?
set -e

if [[ $INSTALL_STATUS -eq 0 ]]; then
    echo "   ✅ 安装成功 (退出码: 0)"
    echo "🎉 Apple Watch 端 Life Logger 已完成 7 天续签并恢复可用！"
    exit 0
fi

# 若无线失败且备有 USB ios-deploy 尝试备选中转
echo "⚠️  无线通道返回码非 0: $INSTALL_STATUS"
if command -v ios-deploy >/dev/null 2>&1; then
    echo "🔄 尝试通过配对 iPhone USB 隧道 (ios-deploy Companion Proxy) 进行中转安装..."
    set +e
    IOS_DEPLOY_OUT=$(ios-deploy --id "$WATCH_UDID" --bundle "$APP_PATH" --no-wifi --nostart 2>&1)
    DEPLOY_STATUS=$?
    set -e
    if [[ $DEPLOY_STATUS -eq 0 ]]; then
        echo "   ✅ 经由 iPhone USB 隧道安装成功 (退出码: 0)"
        echo "🎉 Apple Watch 端 Life Logger 已完成续签并恢复可用！"
        exit 0
    fi
fi

echo "❌ 安装未能完成！" >&2
echo "   输出信息: $INSTALL_OUT" >&2
echo "   【修复动作】：请确认手表保持点亮解锁，或在手表打开【设置 → 开发者】后重试。" >&2
exit 6
