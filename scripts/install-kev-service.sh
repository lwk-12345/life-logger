#!/usr/bin/env bash
# ==============================================================================
# Life Logger Kev-4B 决策服务 LaunchAgent 安装脚本
#
# 作用：把 scripts/start_kev_service.sh --foreground 注册为当前用户的 LaunchAgent，
#       让 Kev-4B（事件闸门 + 决策注解）开机自启，进程崩了由 launchd 自动重拉。
#
# 为什么用 --foreground：launchd 只会重启「作业主进程」。如果脚本用 nohup 把
# kev.serve 甩到后台、自己退出 0，launchd 会认为作业已完成，崩了也不会重拉。
# 前台模式下脚本 exec 到 kev.serve，它就是主进程，KeepAlive 才真的生效。
#
# 关键坑（脚本注释里也写了）：必须 HF_HUB_OFFLINE=1，否则 Clash 的 fake-ip
# (198.18.x) 会让推理挂死 —— /health 正常但 /v1/systemone 永久挂起。
# 这里在 plist 里再写一遍，双保险。
#
# 用法:
#   ./scripts/install-kev-service.sh              # 安装（已装的先摘除再装）
#   ./scripts/install-kev-service.sh --uninstall  # 卸载
# ==============================================================================
set -euo pipefail

LABEL="com.life-logger.kev-service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${AGENTS_DIR}/${LABEL}.plist"
LOG_DIR="${REPO_ROOT}/output/local-web"
DOMAIN="gui/$(id -u)"
SERVICE="${DOMAIN}/${LABEL}"

if [[ "${1:-}" == "--uninstall" ]]; then
    echo "🛑 卸载 Kev 服务 LaunchAgent: ${LABEL}"
    launchctl bootout "${SERVICE}" 2>/dev/null || true
    rm -f "${PLIST_PATH}"
    # 摘掉 launchd 之后停掉它托管的进程；手动起的实例（/tmp/kev_service.pid）不动。
    echo "✅ 已卸载（监听 8000 的进程若仍在，可用 lsof -ti :8000 | xargs kill 停掉）"
    exit 0
fi

mkdir -p "${AGENTS_DIR}" "${LOG_DIR}"

echo "📝 正在生成 LaunchAgent 配置: ${PLIST_PATH}"
cat > "${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${REPO_ROOT}/scripts/start_kev_service.sh</string>
        <string>--foreground</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${REPO_ROOT}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>MODEL</key>
        <string>jaredpalmer/kev-0.8b</string>
        <key>PORT</key>
        <string>8000</string>
        <key>HF_HUB_OFFLINE</key>
        <string>1</string>
        <key>HF_DATASETS_OFFLINE</key>
        <string>1</string>
        <key>TRANSFORMERS_OFFLINE</key>
        <string>1</string>
        <key>HF_HUB_DISABLE_XET</key>
        <string>1</string>
        <key>HF_HUB_ENABLE_HF_TRANSFER</key>
        <string>0</string>
        <key>KEV_PREFIX_CACHE</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/kev-service.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/kev-service.stderr.log</string>
</dict>
</plist>
EOF

plutil -lint "${PLIST_PATH}" >/dev/null

echo "🔄 正在注册并启动 launchd 服务: ${SERVICE}"
launchctl bootout "${SERVICE}" 2>/dev/null || true
sleep 0.5
launchctl bootstrap "${DOMAIN}" "${PLIST_PATH}"
launchctl enable "${SERVICE}" 2>/dev/null || true

echo "⏳ 等待模型加载（首次加载需要初始化权重，通常 30~120s）..."
for _ in $(seq 1 60); do
    if curl -s -m 2 "http://127.0.0.1:8000/health" | grep -q '"status":"ok"' 2>/dev/null; then
        echo "🎉 Kev 服务已由 launchd 托管并就绪："
        curl -s "http://127.0.0.1:8000/health"
        echo ""
        echo "   服务标识: ${LABEL}"
        echo "   配置路径: ${PLIST_PATH}"
        echo "   运行日志: ${LOG_DIR}/kev-service.stdout.log"
        exit 0
    fi
    sleep 2
done

echo "⚠️ 120s 内未就绪，检查日志: ${LOG_DIR}/kev-service.stderr.log"
exit 1
