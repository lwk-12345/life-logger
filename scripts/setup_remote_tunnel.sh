#!/usr/bin/env bash
# Life Logger Remote Tunnel Setup Helper
# Enables GPS-only Apple Watch S5 to sync through iPhone 5G cellular to Mac at home.

set -euo pipefail

MAC_PORT=8765

echo "======================================================="
echo "   Life Logger 外出网络借道穿透配置向导 (Apple Watch S5)"
echo "======================================================="
echo ""
echo "在马路上，GPS 版 S5 会通过蓝牙无感借用 iPhone 的 5G 流量。"
echo "我们需要让 iPhone 能连回家里的 Mac (端口 ${MAC_PORT})。"
echo ""
echo "【方案 A：Tailscale 虚拟局域网（已内置受信任白名单 100.x.x.x）】"
echo "  1. 在这台 Mac 上安装并登录 Tailscale：brew install tailscale"
echo "  2. 在 iPhone 上安装 Tailscale App 并登录同一个账号"
echo "  3. 查看 Mac 的 Tailscale 100.x.x.x IP：tailscale ip -4"
echo "  4. 在手表 App 的设置里将 Mac 地址填入：http://<Mac-100-IP>:${MAC_PORT}"
echo "  * 优势：全加密、免公网域名、S5 底层代码已原生放行 100.64.0.0/10 私有段！"
echo ""
echo "【方案 B：Cloudflare Tunnel（零公网暴露、全球高可用）】"
echo "  1. 安装 cloudflared：brew install cloudflared"
echo "  2. 启动单次快速穿透测试："
echo "     cloudflared tunnel --url http://127.0.0.1:${MAC_PORT}"
echo "  3. 终端会输出一个临时专属加密域名（例如 https://xxxx.trycloudflare.com）"
echo "  4. 将此域名填入手表或 iPhone 代理中即可随时随地秒级直通！"
echo "======================================================="

if command -v tailscale &>/dev/null; then
    TS_IP=$(tailscale ip -4 2>/dev/null || true)
    if [ -n "$TS_IP" ]; then
        echo "✅ 检测到本机 Tailscale 已就绪！本机虚拟内网 IP: http://${TS_IP}:${MAC_PORT}"
    fi
fi
