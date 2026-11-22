#!/usr/bin/env bash
# Start Tailscale in userspace networking mode (no root/kext required)
set -euo pipefail

SOCKET="/tmp/tailscaled.socket"
STATEDIR="${HOME}/.local/share/tailscale"
mkdir -p "${STATEDIR}"

# Start daemon if not running
if ! pgrep -f "tailscaled.*${SOCKET}" >/dev/null 2>&1; then
    echo "启动 Tailscale 用户态守护进程..."
    /opt/homebrew/opt/tailscale/bin/tailscaled \
        --tun=userspace-networking \
        --socket="${SOCKET}" \
        --state="${STATEDIR}/tailscaled.state" \
        --statedir="${STATEDIR}" >/tmp/tailscaled.log 2>&1 &
    sleep 2
fi

echo "正在检查 Tailscale 状态..."
if tailscale --socket="${SOCKET}" status 2>/dev/null; then
    TS_IP=$(tailscale --socket="${SOCKET}" ip -4 2>/dev/null || true)
    echo "=========================================================="
    echo "✅ Tailscale 已在线！本机 100.x.x.x 虚拟内网 IP:"
    echo "   http://${TS_IP}:8765"
    echo "   （Apple Watch S5 代码已原生放行 100.64.0.0/10 白名单！）"
    echo "=========================================================="
else
    AUTH_URL=$(grep -o "https://login.tailscale.com/a/[a-zA-Z0-9]*" /tmp/tailscaled.log | tail -n 1 || true)
    echo "=========================================================="
    echo "⚠️ Tailscale 正在等待浏览器授权登录："
    echo "   ${AUTH_URL}"
    echo "请在浏览器打开上述链接，登录你的 Tailscale 账号即可完成绑定！"
    echo "=========================================================="
fi
