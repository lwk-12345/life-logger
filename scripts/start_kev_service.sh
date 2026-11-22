#!/usr/bin/env bash
# 启动本地 Kev-4B System-1 决策服务（Life Logger 事件闸门）
#
# 两种模式：
#   ./scripts/start_kev_service.sh               手动起：nohup 后台 + pid 文件 + 健康等待
#   ./scripts/start_kev_service.sh --foreground  交给 launchd：前台 exec 常驻，崩了由 KeepAlive 重拉
#                                                （见 scripts/install-kev-service.sh）
#
# 注意：macOS 自带 bash 3.2，在 launchd 的 C locale 下把「变量名紧跟中文」误当变量名
#       （实测报 `PORT???: unbound variable`），所以本文件里所有变量引用一律写成 ${VAR}。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_KEV="${REPO_ROOT}/.kev/.venv"
PORT="${PORT:-8000}"
PID_FILE="/tmp/kev_service.pid"
LOG_FILE="/tmp/kev_service.log"
MODEL="${MODEL:-jaredpalmer/kev-0.8b}"

FOREGROUND="${FOREGROUND:-0}"
FORCE="${FORCE:-0}"
for arg in "$@"; do
    case "${arg}" in
        --foreground) FOREGROUND=1 ;;
        --force) FORCE=1 ;;
    esac
done

export HF_HUB_DISABLE_XET=1
export HF_HUB_ENABLE_HF_TRANSFER=0
# 关键：强制离线。模型权重已在本地（adapter 在 HF 缓存、底座在 ModelScope 缓存），
# 推理时任何 HF 网络探测都会走 Clash 的 fake-ip(198.18.x) 并卡在 CLOSE_WAIT，
# 表现为 /health 正常但 /v1/systemone 永久挂起。必须离线。
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export HF_DATASETS_OFFLINE="${HF_DATASETS_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
export KEV_PREFIX_CACHE="${KEV_PREFIX_CACHE:-1}"
export KEV_PREFIX_MAX_TOKENS="${KEV_PREFIX_MAX_TOKENS:-8192}"

health_body() { curl -s -m 2 "http://127.0.0.1:${PORT}/health" || true; }
port_in_use() { lsof -i :"${PORT}" >/dev/null 2>&1; }

# 检查当前端口占用
if port_in_use; then
    RUNNING_INFO="$(health_body)"
    if [[ "${RUNNING_INFO}" == *"kev"* ]]; then
        if [[ "${FOREGROUND}" == "1" ]]; then
            # launchd 场景：已有 Kev 在服务（可能是手动起的实例）。
            # 不抢端口、不杀进程 —— 留在看护状态等它退出，退出后由本进程接管。
            # 这样 launchd 作业始终存活，KeepAlive 才有意义。
            while port_in_use && [[ "$(health_body)" == *"kev"* ]]; do
                echo "⏳ [launchd] 端口 ${PORT} 已有 Kev 实例在服务，看护等待接管 $(date '+%F %T')"
                sleep 30
            done
            if port_in_use; then
                echo "⚠️ 端口 ${PORT} 仍被占用但已不是 Kev，交给 launchd 节流后重试。"
                exit 1
            fi
            echo "🔁 旧实例已退出，launchd 托管实例接管端口 ${PORT}。"
        else
            echo "✅ Kev 服务已在端口 ${PORT} 运行中:"
            echo "${RUNNING_INFO}"
            exit 0
        fi
    elif [[ "${RUNNING_INFO}" == *"status"* ]]; then
        echo "⚠️ 端口 ${PORT} 当前正在运行其他决策服务:"
        echo "${RUNNING_INFO}"
        if [[ "${FORCE}" == "1" ]]; then
            echo "🛑 检测到 --force，正在停止旧服务..."
            lsof -ti :"${PORT}" | xargs kill -9 2>/dev/null || true
            sleep 1
        else
            echo "请添加 --force 参数强制替换，或指定 PORT=8009 启动测试实例。"
            exit 1
        fi
    else
        # 端口被别的东西占着，但不响应 /health（或响应不是决策服务）
        echo "⚠️ 端口 ${PORT} 被占用且 /health 不健康（${RUNNING_INFO}）"
        if [[ "${FOREGROUND}" == "1" ]]; then
            # launchd 场景：退出码非 0 → 由 ThrottleInterval 节流后重试，不要死循环刷日志
            echo "🛑 前台模式无法启动，交给 launchd 节流重试。"
        fi
        exit 1
    fi
fi

echo "🚀 正在启动 Kev-4B System-1 决策模型 (Port: ${PORT}, Model: ${MODEL})..."
cd "${REPO_ROOT}/.kev"

if [[ "${FOREGROUND}" == "1" ]]; then
    # launchd 托管：exec 让 kev.serve 成为主进程，进程挂掉 launchd 立刻重启。
    echo "🔁 前台模式（launchd 托管）：日志交给 StandardOutPath/StandardErrorPath。"
    exec "${VENV_KEV}/bin/python" -m kev.serve \
        --run "${MODEL}" \
        --port "${PORT}"
fi

nohup "${VENV_KEV}/bin/python" -m kev.serve \
    --run "${MODEL}" \
    --port "${PORT}" > "${LOG_FILE}" 2>&1 &

NEW_PID=$!
echo "${NEW_PID}" > "${PID_FILE}"

echo "⏳ 等待 Kev 加载就绪 (首次加载可能需要初始化权重)..."
for i in {1..60}; do
    if curl -s -m 2 "http://127.0.0.1:${PORT}/health" | grep -q '"status":"ok"' 2>/dev/null; then
        echo "🎉 Kev 服务启动成功！"
        curl -s "http://127.0.0.1:${PORT}/health"
        echo ""
        exit 0
    fi
    sleep 2
done

echo "⚠️ Kev 启动未在 120s 内完成，当前仍在加载或出错，请查看日志: ${LOG_FILE}"
exit 1
