#!/usr/bin/env bash
# 编译并签名 Life Logger Watch App
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCH_DIR="$REPO_ROOT/watchos/LifeLoggerWatch"
SIGNED_DIR="$WATCH_DIR/build/SignedDerivedData/Build/Products/Debug-watchos"
SIGNED_APP="$SIGNED_DIR/LifeLoggerWatch.app"
# 先保存描述文件，避免替换旧 app 时删除唯一副本。
SIGNING_TMP="$(mktemp -d "${TMPDIR:-/tmp}/lifelogger-watch-sign.XXXXXX")"
trap 'rm -rf "$SIGNING_TMP"' EXIT
ENTITLEMENTS="$SIGNING_TMP/entitlements.plist"
# 优先从 Xcode 缓存获取最新有效的描述文件（个人证书有效期 7 天）
XCODE_PROFILES_DIR="$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles"
LATEST_PROFILE=""
if [ -d "$XCODE_PROFILES_DIR" ]; then
    LATEST_PROFILE="$(ls -t "$XCODE_PROFILES_DIR"/*.mobileprovision 2>/dev/null | head -n 1 || true)"
fi

PROFILE=""
if [ -n "$LATEST_PROFILE" ] && [ -f "$LATEST_PROFILE" ]; then
    PROFILE="$LATEST_PROFILE"
elif [ -f "$WATCH_DIR/build/SignedDerivedData/embedded.mobileprovision" ]; then
    PROFILE="$WATCH_DIR/build/SignedDerivedData/embedded.mobileprovision"
elif [ -f "$SIGNED_APP/embedded.mobileprovision" ]; then
    PROFILE="$SIGNED_APP/embedded.mobileprovision"
fi

if [ -z "$PROFILE" ] || [ ! -f "$PROFILE" ]; then
    echo "❌ 缺少开发描述文件；未编译或替换现有 app。请先在 Xcode 配置签名。" >&2
    exit 1
fi
cp "$PROFILE" "$SIGNING_TMP/embedded.mobileprovision"
# 独立持久副本在后续复制/签名失败时也保留，不受 EXIT 临时目录清理影响。
PERSISTED_PROFILE="$WATCH_DIR/build/SignedDerivedData/embedded.mobileprovision"
cp "$SIGNING_TMP/embedded.mobileprovision" "$PERSISTED_PROFILE"

echo "🔨 1. 生成工程与自动续期签名编译 (-allowProvisioningUpdates)..."
xcodegen generate --spec "$WATCH_DIR/project.yml" --project-root "$WATCH_DIR"
if xcodebuild -project "$WATCH_DIR/LifeLoggerWatch.xcodeproj" \
    -scheme LifeLoggerWatch \
    -sdk watchos \
    -configuration Debug \
    -destination 'generic/platform=watchOS' \
    -allowProvisioningUpdates \
    -derivedDataPath "$WATCH_DIR/build/SignedDerivedData" \
    build; then
    if [ -f "$SIGNED_APP/embedded.mobileprovision" ]; then
        cp "$SIGNED_APP/embedded.mobileprovision" "$SIGNING_TMP/embedded.mobileprovision"
        cp "$SIGNED_APP/embedded.mobileprovision" "$PERSISTED_PROFILE"
    fi
else
    echo "⚠️ 自动续期签名编译失败，回退至本地缓存描述文件..."
    xcodebuild -project "$WATCH_DIR/LifeLoggerWatch.xcodeproj" \
        -target LifeLoggerWatch \
        -sdk watchos \
        -configuration Debug \
        -destination 'generic/platform=watchOS' \
        CODE_SIGNING_ALLOWED=NO build
    mkdir -p "$SIGNED_DIR"
    rm -rf "$SIGNED_APP"
    cp -R "$WATCH_DIR/build/Debug-watchos/LifeLoggerWatch.app" "$SIGNED_APP"
    cp "$SIGNING_TMP/embedded.mobileprovision" "$SIGNED_APP/embedded.mobileprovision"
fi

echo "✍️ 2. 校验/加固 Entitlements 并执行个人开发者证书真实签名..."
APPLE_TEAM_ID="${APPLE_TEAM_ID:-}"
if [[ -z "$APPLE_TEAM_ID" && -f "$SIGNING_TMP/embedded.mobileprovision" ]]; then
    APPLE_TEAM_ID="$(security cms -D -i "$SIGNING_TMP/embedded.mobileprovision" 2>/dev/null | plutil -extract TeamIdentifier.0 raw - 2>/dev/null || true)"
fi
APPLE_TEAM_ID="${APPLE_TEAM_ID:-YOUR_APPLE_TEAM_ID}"
cat << EOF > "$ENTITLEMENTS"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>application-identifier</key>
	<string>${APPLE_TEAM_ID}.com.lifelogger.watch</string>
	<key>com.apple.developer.healthkit</key>
	<true/>
	<key>com.apple.developer.team-identifier</key>
	<string>${APPLE_TEAM_ID}</string>
	<key>get-task-allow</key>
	<true/>
</dict>
</plist>
EOF

# 注入最新描述文件。
cp "$SIGNING_TMP/embedded.mobileprovision" "$SIGNED_APP/embedded.mobileprovision"

# 签名身份：优先环境变量；没有就从本机钥匙串自动取（**不在脚本里硬编码任何账号/邮箱**）。
# 例：export APPLE_CODESIGN_IDENTITY="Apple Development: 你的名字 (TEAMID)"
if [[ -z "${APPLE_CODESIGN_IDENTITY:-}" ]]; then
    APPLE_CODESIGN_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
        | awk '/Apple Development:/ {print $2; exit}')"
fi
if [[ -z "${APPLE_CODESIGN_IDENTITY:-}" ]]; then
    echo "❌ 没找到可用的 Apple Development 签名身份。" >&2
    echo "   请先设置环境变量 APPLE_CODESIGN_IDENTITY（见本脚本上方注释），或确认钥匙串里已安装开发者证书。" >&2
    exit 1
fi
echo "🔏 使用签名身份: $APPLE_CODESIGN_IDENTITY"

codesign --force \
    --sign "$APPLE_CODESIGN_IDENTITY" \
    --entitlements "$ENTITLEMENTS" \
    --timestamp=none \
    "$SIGNED_APP"

codesign --verify --deep --strict "$SIGNED_APP"
if ! codesign -d --entitlements - "$SIGNED_APP" 2>/dev/null | grep -q "com.apple.developer.healthkit"; then
    echo "❌ 签名产物缺少 com.apple.developer.healthkit entitlement！" >&2
    exit 1
fi
echo "✅ 签名完整性与 HealthKit Entitlement 核验通过（真机安装尚未验证）: $SIGNED_APP"
codesign -dvvv "$SIGNED_APP" 2>&1 | grep -E "Authority|Identifier|Signed Time|TeamIdentifier"
