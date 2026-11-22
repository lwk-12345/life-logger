#!/usr/bin/env bash
# ==============================================================================
# AirPassport macOS 开机自启服务安装脚本
# 作用: 将 AirPassport.app 注册为当前用户的 LaunchAgent (~/Library/LaunchAgents)
#       实现登录系统后免终端静默后台启动，常驻 macOS 菜单栏。
# ==============================================================================
set -euo pipefail

LABEL="com.lifelogger.airpassport"
REPO_DIR="$(python3 -c "import os, sys; print(os.path.abspath(os.path.join(os.path.dirname(sys.argv[1]), '..')))" "$0")"

# 1. 查找或指定 AirPassport.app 路径 (优先系统 Applications 目录，其次仓库内部)
if [[ -d "/Applications/AirPassport.app" ]]; then
    DEFAULT_APP="/Applications/AirPassport.app"
else
    DEFAULT_APP="${REPO_DIR}/desktop/AirPassportReceiver/AirPassport.app"
fi
APP_BUNDLE="${1:-${DEFAULT_APP}}"

echo "========================================================================"
echo "🚀 安装 AirPassport 开机自启动服务 (LaunchAgent)"
echo "========================================================================"
echo "📁 仓库根目录: ${REPO_DIR}"
echo "📦 目标应用:   ${APP_BUNDLE}"

if [[ ! -d "${APP_BUNDLE}" ]]; then
    echo "⚠️  未检测到已打包的 AirPassport.app，正在自动执行打包..."
    "${REPO_DIR}/desktop/AirPassportReceiver/package_app.sh" "${APP_BUNDLE}"
fi

EXECUTABLE="${APP_BUNDLE}/Contents/MacOS/AirPassport"
if [[ ! -x "${EXECUTABLE}" ]]; then
    echo "❌ 错误: 应用程序主二进制不存在或不可执行: ${EXECUTABLE}" >&2
    exit 1
fi

# 2. 准备 LaunchAgents 与 日志目录
AGENTS_DIR="${HOME}/Library/LaunchAgents"
LOG_DIR="${HOME}/Library/Logs/AirPassport"
PLIST_PATH="${AGENTS_DIR}/${LABEL}.plist"

mkdir -p "${AGENTS_DIR}"
mkdir -p "${LOG_DIR}"

# 3. 生成 LaunchAgent plist 配置文件
echo "📝 正在生成 LaunchAgent 配置: ${PLIST_PATH}"
cat <<EOF > "${PLIST_PATH}"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${EXECUTABLE}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${APP_BUNDLE}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchd.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchd.stderr.log</string>
</dict>
</plist>
EOF

# 4. 校验 plist 语法
plutil -lint "${PLIST_PATH}" >/dev/null

# 5. 加载与激活 LaunchAgent 服务
DOMAIN="gui/$(id -u)"
SERVICE="${DOMAIN}/${LABEL}"

echo "🔄 正在注册并启动 launchd 服务: ${SERVICE}"
# 若已有旧实例先安全退出
launchctl bootout "${SERVICE}" 2>/dev/null || true
# 等待半秒释放端口与句柄
sleep 0.5

# 引导加载
launchctl bootstrap "${DOMAIN}" "${PLIST_PATH}"
launchctl enable "${SERVICE}" 2>/dev/null || true
launchctl kickstart -k "${SERVICE}"

echo "========================================================================"
echo "✅ AirPassport 开机自启服务安装并启动成功！"
echo "   服务标识:   ${LABEL}"
echo "   配置路径:   ${PLIST_PATH}"
echo "   标准输出日志: ${LOG_DIR}/launchd.stdout.log"
echo "   标准错误日志: ${LOG_DIR}/launchd.stderr.log"
echo "   提示: 系统顶部菜单栏已出现 🎙️ 🔴 图标，以后每次开机登录将静默自启。"
echo "========================================================================"
