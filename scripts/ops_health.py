#!/usr/bin/env python3
"""服务体检：一条命令看清「现在该不该重启」。

用法：
    python3 scripts/ops_health.py            # 人看
    python3 scripts/ops_health.py --json     # 机器看（给门禁/自动调度用）

只读：不发写请求、不重启、不改任何文件。默认读本机 127.0.0.1:8765。

判据（来自 ②2号 的实测，见 docs/RESTART-RUNBOOK.md）：
    · /api/daily 同一接口连续 2 次 >3s
    · 冷请求连续 2 次 >10s
    · RSS >800MB 连续 2 个采样
    · 线程数 >20 持续 5 分钟
本脚本单次采样，只给「当前值 + 是否越过单次阈值」，连续判定要靠定时多次调用
（nightly-verify / 门禁会反复跑它）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE_URL = os.environ.get("LIFE_LOGGER_BASE_URL", "http://127.0.0.1:8765")
WEB_LABEL = "com.life-logger.local-web"

# 单次采样阈值（连续判定由调用方做）
THRESHOLD_DAILY_SLOW = 3.0
THRESHOLD_COLD_SLOW = 10.0
THRESHOLD_RSS_MB = 800.0
THRESHOLD_THREADS = 20

# 常驻任务清单（label → 人话说明）。launchd 里存在但没加载的也会列出来标「未加载」。
KNOWN_JOBS = {
    "com.life-logger.local-web": "本地 Web 服务（8765，网页端 + API）",
    "com.life-logger.daily-synthesis": "次日 01:30 生成前一天日报",
    "com.life-logger.daily-acceptance": "每日 07:30 跑验收脚本",
    "com.life-logger.nightly-verify": "每日 09:00 夜间校验（含门禁）",
    "com.life-logger.backup": "每日 03:00 备份权威数据到 backups/",
    "com.life-logger.bonjour": "mDNS 广播，让局域网能发现这台 Mac",
    "com.life-logger.tunnel-keep": "每 300s 保活公网隧道进程",
    "com.life-logger.watch-profile-reminder": "每日 10:00 / 16:00 提醒更新 Watch 描述文件",
    "com.life-logger.airpassport-watch": "AirPassport BLE 录音落盘守护",
    "com.life-logger.decision-shadow": "每 30s 决策影子任务（plist 在，可能未加载）",
}


def _run(cmd: list[str], timeout: float = 15.0) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 127, f"{type(exc).__name__}: {exc}"


def _token() -> str:
    """访问令牌：优先环境变量，其次 output/local-web/secrets.json。"""
    env = os.environ.get("LIFE_LOGGER_TOKEN")
    if env:
        return env
    for candidate in (ROOT / "output" / "local-web" / "secrets.json",
                      ROOT / "output" / "local-web" / "secrets.json.bak"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            tok = data.get("mobile_access_token")
            if tok:
                return str(tok)
        except Exception:  # noqa: BLE001
            continue
    return ""


def _get(path: str, timeout: float = 15.0) -> tuple[dict | None, float, str]:
    """返回 (payload, 耗时秒, 错误)。"""
    url = BASE_URL.rstrip("/") + path
    headers = {"Accept": "application/json"}
    tok = _token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            elapsed = time.monotonic() - started
            try:
                return json.loads(body), elapsed, ""
            except ValueError:
                return None, elapsed, "响应不是 JSON"
    except urllib.error.HTTPError as exc:
        return None, time.monotonic() - started, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return None, time.monotonic() - started, f"{type(exc).__name__}: {exc}"


def service_process() -> dict:
    """从 launchd 拿 PID，再读 RSS / 线程 / 运行时长。"""
    code, out = _run(["launchctl", "list"])
    pid = "-"
    last_exit = "?"
    if code == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == WEB_LABEL:
                pid, last_exit = parts[0], parts[1]
                break
    info: dict[str, object] = {"pid": pid, "last_exit": last_exit}

    if pid.isdigit():
        code, out = _run(["ps", "-o", "rss=,etime=", "-p", pid])
        if code == 0 and out.strip():
            fields = out.split()
            try:
                info["rss_mb"] = round(int(fields[0]) / 1024, 1)
            except (ValueError, IndexError):
                info["rss_mb"] = None
            info["uptime"] = fields[1] if len(fields) > 1 else None
        code, out = _run(["ps", "-M", "-p", pid])
        # ps -M 第一行是表头，其余每行一个线程
        info["threads"] = max(0, len([l for l in out.splitlines() if l.strip()]) - 1) if code == 0 else None
    else:
        info.update({"rss_mb": None, "uptime": None, "threads": None})
    return info


def launchd_jobs() -> list[dict]:
    code, out = _run(["launchctl", "list"])
    rows: dict[str, dict] = {}
    if code == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                rows[parts[2]] = {"pid": parts[0], "last_exit": parts[1]}
    result = []
    for label, desc in KNOWN_JOBS.items():
        row = rows.get(label)
        result.append({
            "label": label,
            "desc": desc,
            "loaded": row is not None,
            "pid": row.get("pid") if row else None,
            "last_exit": row.get("last_exit") if row else None,
        })
    return result


def last_gate() -> dict | None:
    """读最近一次门禁结果。脚本写的是 output/gate-result.json（--json 时）。"""
    for name in ("gate-result.json", "gate-report.json"):
        path = ROOT / "output" / name
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, SHANGHAI)
            return {
                "path": str(path.relative_to(ROOT)),
                "mtime": mtime.isoformat(timespec="seconds"),
                "verdict": data.get("verdict") or data.get("conclusion") or data.get("status"),
                "raw_keys": sorted(data.keys())[:8],
            }
    return None


def ledger_snapshot(days: int = 3) -> list[dict]:
    """最近 N 天的账本基准（重启后用来对账）。"""
    today = datetime.now(SHANGHAI).date()
    out = []
    for offset in range(1, days + 1):
        day = (today - timedelta(days=offset)).isoformat()
        payload, elapsed, err = _get(f"/api/daily?date={day}", timeout=25)
        if err or payload is None:
            out.append({"date": day, "error": err, "elapsed_s": round(elapsed, 3)})
            continue
        ledger = payload.get("expense_ledger") or {}
        out.append({
            "date": day,
            "events": len(payload.get("events") or []),
            "amount_cny": ledger.get("total_amount"),
            "totals_by_currency": ledger.get("totals_by_currency"),
            "pending_count": ledger.get("pending_count"),
            "elapsed_s": round(elapsed, 3),
        })
    return out


def collect() -> dict:
    proc = service_process()
    health, health_s, health_err = _get("/api/health", timeout=10)
    # 注意：/api/health/summary 是「手表健康数据摘要」，不是服务健康；
    # 这里照 A0 要求采一次，但单独命名，避免被误读成服务状态。
    watch_summary, watch_s, watch_err = _get("/api/health/summary", timeout=15)
    yesterday = (datetime.now(SHANGHAI).date() - timedelta(days=1)).isoformat()
    daily, daily_s, daily_err = _get(f"/api/daily?date={yesterday}", timeout=30)

    checks: list[dict] = []

    def add(name: str, ok: bool, value, threshold, hint: str) -> None:
        checks.append({"check": name, "ok": ok, "value": value,
                       "threshold": threshold, "hint": hint})

    add("rss_mb", (proc.get("rss_mb") is None) or (proc["rss_mb"] <= THRESHOLD_RSS_MB),
        proc.get("rss_mb"), THRESHOLD_RSS_MB, "RSS >800MB 连续 2 个采样就该重启")
    add("threads", (proc.get("threads") is None) or (proc["threads"] <= THRESHOLD_THREADS),
        proc.get("threads"), THRESHOLD_THREADS, "线程 >20 持续 5 分钟就该重启")
    add("api_daily_s", (not daily_err) and daily_s <= THRESHOLD_DAILY_SLOW,
        round(daily_s, 3), THRESHOLD_DAILY_SLOW, "/api/daily >3s 连续 2 次就该重启")
    add("cold_request_s", (not daily_err) and daily_s <= THRESHOLD_COLD_SLOW,
        round(daily_s, 3), THRESHOLD_COLD_SLOW, "冷请求 >10s 连续 2 次就该重启")
    add("api_health_ok", bool(health and health.get("ok")), bool(health and health.get("ok")),
        True, "/api/health 必须 ok=true")

    verdict = "PASS" if all(c["ok"] for c in checks) else "ATTENTION"
    return {
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "base_url": BASE_URL,
        "service": proc,
        "api": {
            "health": {"ok": None if health is None else health.get("ok"),
                       "elapsed_s": round(health_s, 3), "error": health_err,
                       "build": (health or {}).get("server_build_id"),
                       "started_at": (health or {}).get("server_started_at")},
            "daily_yesterday": {"date": yesterday,
                                "elapsed_s": round(daily_s, 3), "error": daily_err},
            "watch_health_summary": {"elapsed_s": round(watch_s, 3), "error": watch_err,
                                     "note": "★ 这是手表健康数据（心率/步数/睡眠），不是服务健康"},
        },
        "launchd": launchd_jobs(),
        "last_gate": last_gate(),
        "ledger_baseline": ledger_snapshot(3),
        "checks": checks,
        "verdict": verdict,
        "restart_advice": (
            "不需要重启：所有单次阈值都没越线（重启是最后手段，不是常规操作）" if verdict == "PASS"
            else "先别急着重启：把越线项连测 2 次，确认是持续劣化而不是一次冷启动；"
                 "确认劣化后才考虑重启，并先读 runbook 的「绝对不能自己重启」表"
        ),
    }


def render_human(data: dict) -> str:
    svc = data["service"]
    api = data["api"]
    lines: list[str] = []
    lines.append("═" * 62)
    lines.append(f" Life Logger 服务体检   {data['generated_at']}")
    lines.append("═" * 62)
    lines.append(f" 服务 PID   : {svc.get('pid')}   运行时长: {svc.get('uptime')}   "
                 f"上次退出码: {svc.get('last_exit')}")
    lines.append(f" RSS        : {svc.get('rss_mb')} MB   (阈值 {THRESHOLD_RSS_MB})")
    lines.append(f" 线程数     : {svc.get('threads')}       (阈值 {THRESHOLD_THREADS})")
    lines.append(f" /api/health: ok={api['health']['ok']}  {api['health']['elapsed_s']}s  "
                 f"build={api['health']['build']}")
    lines.append(f" /api/daily ({api['daily_yesterday']['date']}): "
                 f"{api['daily_yesterday']['elapsed_s']}s  "
                 f"{'ERR ' + api['daily_yesterday']['error'] if api['daily_yesterday']['error'] else ''}")
    wh = api["watch_health_summary"]
    lines.append(f" /api/health/summary: {wh['elapsed_s']}s  ← 手表健康数据，不是服务健康")
    lines.append("")
    lines.append(" 常驻任务：")
    for job in data["launchd"]:
        if job["loaded"]:
            mark = "✅"
            state = f"PID {job['pid']}" if job["pid"] not in (None, "-") else "空闲"
            state += f" · 上次退出 {job['last_exit']}"
        else:
            mark, state = "⛔", "未加载"
        lines.append(f"   {mark} {job['label']:<42} {state}")
        lines.append(f"      {job['desc']}")
    gate = data["last_gate"]
    lines.append("")
    if gate:
        lines.append(f" 最近门禁：{gate['verdict']}  ({gate['path']} @ {gate['mtime']})")
    else:
        lines.append(" 最近门禁：无（output/gate-result.json 不存在，说明还没跑过 --json 门禁）")
    lines.append("")
    lines.append(" 最近三天账本基准（重启后对账用）：")
    for row in data["ledger_baseline"]:
        if row.get("error"):
            lines.append(f"   {row['date']}  读取失败: {row['error']}")
        else:
            lines.append(f"   {row['date']}  events={row.get('events'):<4} "
                         f"amount=¥{row.get('amount_cny')}  pending={row.get('pending_count')}  "
                         f"({row.get('elapsed_s')}s)")
    lines.append("")
    lines.append(" 阈值检查：")
    for c in data["checks"]:
        lines.append(f"   {'✅' if c['ok'] else '⚠️ '} {c['check']:<16} = {c['value']}  "
                     f"(阈值 {c['threshold']})")
    lines.append("")
    lines.append(f" 结论：{data['verdict']} —— {data['restart_advice']}")
    lines.append(" 重启前请先读 docs/RESTART-RUNBOOK.md（尤其「绝对不能自己重启」那张表）。")
    lines.append("═" * 62)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Life Logger 服务体检（只读）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args(argv)
    data = collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_human(data))
    return 0 if data["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
