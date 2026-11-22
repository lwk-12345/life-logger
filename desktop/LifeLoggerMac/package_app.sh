#!/usr/bin/env bash
# ==============================================================================
# LifeLogger.app 独立 macOS 应用程序打包脚本
# 将 life-logger-mac 原生窗口与菜单栏服务打包为标准的免终端独立 .app
# ==============================================================================
set -euo pipefail

PACKAGE_DIR="$(python3 -c "import os, sys; print(os.path.abspath(os.path.dirname(sys.argv[1])))" "$0")"
TARGET_APP="${1:-${PACKAGE_DIR}/LifeLogger.app}"

echo "========================================================================"
echo "📦 开始打包 LifeLogger.app ..."
echo "   源码目录: ${PACKAGE_DIR}"
echo "   输出目标: ${TARGET_APP}"
echo "========================================================================"

# 1. 编译 release 产物
echo "🔨 [1/5] 编译 Release 产物 (life-logger-mac)..."
swift build --package-path "${PACKAGE_DIR}" -c release --product life-logger-mac

BIN_DIR="$(swift build --package-path "${PACKAGE_DIR}" -c release --product life-logger-mac --show-bin-path)"
SRC_BIN="${BIN_DIR}/life-logger-mac"

if [[ ! -f "${SRC_BIN}" ]]; then
    echo "❌ 错误: 未找到编译产物: ${SRC_BIN}" >&2
    exit 1
fi
echo "   编译完成: ${SRC_BIN}"

# 2. 构建标准 .app 目录结构
echo "📁 [2/5] 构建标准 macOS Bundle 目录结构..."
rm -rf "${TARGET_APP}"
mkdir -p "${TARGET_APP}/Contents/MacOS"
mkdir -p "${TARGET_APP}/Contents/Resources"

# 3. 拷贝二进制文件并重命名为 LifeLogger
echo "🚚 [3/5] 部署可执行文件并设置权限..."
cp "${SRC_BIN}" "${TARGET_APP}/Contents/MacOS/LifeLogger"
chmod +x "${TARGET_APP}/Contents/MacOS/LifeLogger"
printf "APPL????" > "${TARGET_APP}/Contents/PkgInfo"

# 4. 注入 Info.plist
echo "📝 [4/5] 注入配置清单 Info.plist..."
PLIST_SRC="${PACKAGE_DIR}/Info.plist"
if [[ ! -f "${PLIST_SRC}" ]]; then
    echo "❌ 错误: 未找到 Info.plist: ${PLIST_SRC}" >&2
    exit 1
fi
cp "${PLIST_SRC}" "${TARGET_APP}/Contents/Info.plist"

# 5. 执行代码签名（优先使用有效 Apple Development 开发者证书 SHA-1 指纹避免同名歧义；若无则回退 ad-hoc）
SIGN_LINE="$(security find-identity -v -p codesigning 2>/dev/null | grep -m1 "Apple Development" || true)"
SIGN_HASH="$(echo "${SIGN_LINE}" | awk '{print $2}')"
SIGN_NAME="$(echo "${SIGN_LINE}" | awk -F'"' '{print $2}')"
if [[ -n "${SIGN_HASH}" ]]; then
    echo "🔏 [5/5] 检测到有效开发者证书，执行证书签名: ${SIGN_NAME} (${SIGN_HASH})..."
    if ! codesign --force --deep --sign "${SIGN_HASH}" "${TARGET_APP}"; then
        echo "⚠️ 证书签名异常，回退 ad-hoc 代码签名..."
        codesign --force --deep --sign - "${TARGET_APP}"
    fi
else
    echo "🔏 [5/5] 未检测到开发者证书，执行 ad-hoc 代码签名..."
    codesign --force --deep --sign - "${TARGET_APP}"
fi

echo "🔍 验证签名完整性..."
codesign -v "${TARGET_APP}"

echo "========================================================================"
echo "✅ LifeLogger.app 打包与签名成功！"
echo "   产物路径: ${TARGET_APP}"
echo "   二进制大小: $(ls -lh "${TARGET_APP}/Contents/MacOS/LifeLogger" | awk '{print $5}')"
echo "========================================================================"
