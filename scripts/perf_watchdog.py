#!/usr/bin/env python3
"""Periodic read-path watchdog for the Life Logger local web service.

Purpose: catch intermittent ``/api/daily`` / ``/api/stats`` slowness without
requiring a human to watch the service.  It is intentionally read-only: it only
sends GET requests and appends an alert line when the same endpoint is slow on
two consecutive samples.

Usage:
    python3 scripts/perf_watchdog.py --duration 600
    python3 scripts/perf_watchdog.py --duration 300 --interval 60 --alerts /tmp/perf.jsonl

Exit code is 0 even when alerts are emitted; this is a monitor, not a CI gate.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "output" / "local-web"
DEFAULT_ALERTS = WEB_ROOT / "perf-alerts.jsonl"
DEFAULT_DAILY_STATE = WEB_ROOT / "perf-watchdog-daily.json"
DEFAULT_DAILY_SUMMARY = WEB_ROOT / "perf-watchdog-summary.jsonl"
DEFAULT_SECRETS = WEB_ROOT / "secrets.json"
# 健康基线：记录「正常情况下这个端点多久」，用于「相对退化」判定（0.3s → 1.5s 也该报）
DEFAULT_BASELINE = PROJECT_ROOT / "output" / "perf-baseline.json"
# 相对退化：比基线慢 5 倍且绝对值超过 0.3s 就报（哪怕没到 3s 硬阈值）
RELATIVE_FACTOR = 5.0
RELATIVE_FLOOR_SECONDS = 0.3
SERVICE_LABEL = "com.life-logger.local-web"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _iso_now() -> str:
    return datetime.now(tz=SHANGHAI).isoformat(timespec="seconds")


def _read_token(explicit: str | None, token_file: Path) -> str:
    if explicit:
        return explicit.strip()
    env_token = str(os.environ.get("LIFE_LOGGER_TOKEN") or "").strip()
    if env_token:
        return env_token
    try:
        payload = json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    token = str(
        payload.get("mobile_access_token")
        or payload.get("access_token")
        or ""
    ).strip()
    if token:
        return token
    fallback = Path("/tmp/ll_token")
    try:
        return fallback.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _service_pid() -> int | None:
    """Best-effort PID of the launchd-managed local web process."""
    try:
        out = subprocess.check_output(
            ["launchctl", "print", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            try:
                return int(stripped.split("=", 1)[1].strip())
            except ValueError:
                pass
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "scripts/run_local_web.py"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        pids = [int(item) for item in out.split() if item.isdigit()]
        return pids[0] if pids else None
    except (OSError, subprocess.SubprocessError):
        return None


def _rss_mb(pid: int | None) -> float | None:
    if pid is None:
        return None
    try:
        out = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
        return round(int(out) / 1024, 1) if out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _ps_thread_count(pid: int | None) -> int | None:
    if pid is None:
        return None
    try:
        out = subprocess.check_output(
            ["ps", "-M", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        lines = [line for line in out.splitlines() if line.strip()]
        # First line is a header on macOS; each physical thread follows.
        return max(0, len(lines) - 1)
    except (OSError, subprocess.SubprocessError):
        return None


def _parse_etime(raw: str) -> float | None:
    """Parse macOS ``ps -o etime=`` output into seconds."""
    text = raw.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = (int(item) for item in parts)
        elif len(parts) == 2:
            hours = 0
            minutes, seconds = (int(item) for item in parts)
        else:
            return None
    except ValueError:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _service_uptime_seconds(pid: int | None) -> float | None:
    if pid is None:
        return None
    try:
        out = subprocess.check_output(
            ["ps", "-o", "etime=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_etime(out)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_daily_state(path: Path, today: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict) or payload.get("date") != today:
        return {
            "date": today,
            "slowest": None,
            "alert_count": 0,
            "sample_count": 0,
            "last_sample": None,
            "summary_written": False,
        }
    payload.setdefault("slowest", None)
    payload.setdefault("alert_count", 0)
    payload.setdefault("sample_count", 0)
    payload.setdefault("last_sample", None)
    payload.setdefault("summary_written", False)
    return payload


def _append_daily_summary(path: Path, state: dict[str, object]) -> None:
    if state.get("summary_written"):
        return
    slowest = state.get("slowest")
    record = {
        "date": state.get("date"),
        "slowest": slowest,
        "alert_count": int(state.get("alert_count") or 0),
        "sample_count": int(state.get("sample_count") or 0),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    state["summary_written"] = True


def _debug_metrics(base_url: str, token: str) -> dict[str, object]:
    """Read the app's own memory/thread count when the endpoint is available."""
    url = base_url.rstrip("/") + "/api/debug/read-cache"
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return {}
        return {
            "rss_max_mb": payload.get("rusage_maxrss_mb"),
            "thread_count": payload.get("thread_count"),
            "read_cache_entries": (payload.get("read_cache") or {}).get("entries"),
            "gc_count": payload.get("gc_count"),
        }
    except (OSError, ValueError, urllib.error.URLError):
        return {}


def _request_once(url: str, token: str, timeout: float) -> tuple[int | None, float, str | None]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            return response.status, time.monotonic() - started, None
    except urllib.error.HTTPError as error:
        try:
            error.read()
        except OSError:
            pass
        return error.code, time.monotonic() - started, f"HTTP {error.code}"
    except Exception as error:  # timeout / connection reset / DNS
        return None, time.monotonic() - started, f"{type(error).__name__}: {error}"


def _append_alert(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _load_baseline(path: Path) -> dict[str, dict[str, object]]:
    """读健康基线：{endpoint: {"baseline_s": float, "samples": int, "updated_at": str, "values": [...]}}"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  文件不存在/损坏都从空基线开始
        return {}
    endpoints = payload.get("endpoints")
    return endpoints if isinstance(endpoints, dict) else {}


def _update_baseline(
    path: Path, baseline: dict[str, dict[str, object]], name: str, elapsed: float, healthy: bool
) -> None:
    """把本次「健康」样本并入基线（保留最近 30 个值，取最小值作基线）。

    只收健康样本：慢样本一旦并入，会把基线抬高，让退化永远检测不到。
    """
    if not healthy:
        return
    row = baseline.setdefault(name, {})
    values = [float(v) for v in (row.get("values") or []) if isinstance(v, (int, float))]
    values.append(round(elapsed, 3))
    values = values[-30:]
    row["values"] = values
    row["baseline_s"] = round(min(values), 3)
    row["samples"] = len(values)
    row["updated_at"] = _iso_now()
    _write_json_atomic(path, {"generated_at": _iso_now(), "endpoints": baseline})


def _inflight_metrics(base_url: str, token: str, timeout: float = 10.0) -> dict[str, object] | None:
    """读 /api/debug/read-inflight（②2号 提供的端点）。

    返回 None 表示「这个进程还没有该端点」（404/连接失败）——调用方必须优雅跳过，
    因为旧进程/灰度期间都可能没有它，watchdog 不能因此崩掉。
    """
    url = base_url.rstrip("/") + "/api/debug/read-inflight"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        return {"error": f"HTTP {exc.code}"}
    except Exception as error:  # noqa: BLE001
        return {"error": f"{type(error).__name__}: {error}"}


def _endpoints(yesterday: str) -> list[tuple[str, str]]:
    return [
        (
            "daily",
            f"/api/daily?date={yesterday}&scope=provisional&view=display",
        ),
        ("stats", "/api/stats"),
        ("health", "/api/health/summary"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Life Logger read-path watchdog")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between samples")
    parser.add_argument(
        "--duration", type=float, default=600.0,
        help="total run time in seconds; 0 means run forever (launchd mode)",
    )
    parser.add_argument("--threshold", type=float, default=3.0, help="slow threshold in seconds")
    parser.add_argument("--consecutive", type=int, default=2, help="slow samples before alert")
    parser.add_argument("--alerts", type=Path, default=DEFAULT_ALERTS)
    parser.add_argument("--daily-state", type=Path, default=DEFAULT_DAILY_STATE)
    parser.add_argument("--daily-summary", type=Path, default=DEFAULT_DAILY_SUMMARY)
    parser.add_argument(
        "--cold-start-grace", type=float, default=300.0,
        help="suppress alerts for this many seconds after the web service starts（默认 5 分钟："
             "重启后前 5 分钟本来就慢，不算退化）",
    )
    parser.add_argument(
        "--baseline", type=Path, default=DEFAULT_BASELINE,
        help="健康基线文件（用于相对退化判定）",
    )
    parser.add_argument(
        "--no-relative", action="store_true",
        help="关闭相对退化判定，只按绝对阈值",
    )
    parser.add_argument(
        "--summary-on-exit", action="store_true",
        help="append today's summary line when this watchdog exits",
    )
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    parser.add_argument("--token", default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--once", action="store_true", help="take one sample and exit")
    parser.add_argument(
        "--queue-depth-samples", type=int, default=3,
        help="排队深度要连续多少个采样点「都 >0 且单调涨」才报 queue_backlog（默认 3）",
    )
    parser.add_argument(
        "--queue-sustain-seconds", type=float, default=60.0,
        help="waiting_for_slot 持续 >0 超过这么多秒也报 queue_backlog（默认 60s；"
             "「一直在排队」和「排队在变长」同样是坏事）",
    )
    parser.add_argument("--no-queue", action="store_true", help="关闭排队深度判定")
    args = parser.parse_args()

    token = _read_token(args.token, args.secrets)
    base_url = args.base_url.rstrip("/")
    yesterday = (datetime.now(tz=SHANGHAI).date() - timedelta(days=1)).isoformat()
    endpoints = _endpoints(yesterday)
    streaks: dict[str, int] = {name: 0 for name, _ in endpoints}
    alerted: set[str] = set()
    started_at = time.monotonic()
    sample_index = 0
    today = datetime.now(tz=SHANGHAI).date().isoformat()
    daily_state = _load_daily_state(args.daily_state, today)
    baseline = _load_baseline(args.baseline)
    alert_total = 0
    queue_hist: list[int] = []
    queue_alerted = False
    queue_first_seen: float | None = None
    inflight_missing_logged = False

    print(
        f"[watchdog] start base={base_url} yesterday={yesterday} "
        f"interval={args.interval:g}s duration={args.duration:g}s "
        f"threshold={args.threshold:g}s consecutive={args.consecutive} alerts={args.alerts}",
        flush=True,
    )

    while True:
        sample_index += 1
        pid = _service_pid()
        uptime = _service_uptime_seconds(pid)
        in_grace = uptime is not None and uptime < args.cold_start_grace
        current_day = datetime.now(tz=SHANGHAI).date().isoformat()
        if current_day != daily_state.get("date"):
            _append_daily_summary(args.daily_summary, daily_state)
            daily_state = _load_daily_state(args.daily_state, current_day)
        debug = _debug_metrics(base_url, token)
        rss_max = debug.get("rss_max_mb")
        rss_current = _rss_mb(pid)
        rss = rss_current if rss_current is not None else rss_max
        threads = debug.get("thread_count")
        if threads is None:
            threads = _ps_thread_count(pid)

        print(
            f"[watchdog] sample={sample_index} at={_iso_now()} pid={pid} "
            f"rss_mb={rss} rss_max_mb={rss_max} threads={threads} "
            f"uptime_s={None if uptime is None else round(uptime, 1)}"
            + (" COLD_GRACE" if in_grace else "")
        )
        daily_state["sample_count"] = int(daily_state.get("sample_count") or 0) + 1
        daily_state["last_sample"] = _iso_now()
        for name, path in endpoints:
            status, elapsed, error = _request_once(base_url + path, token, args.timeout)
            # 绝对阈值
            absolute_slow = elapsed > args.threshold and status is not None
            # 相对退化：比健康基线慢 N 倍（默认 5×）且绝对值超过地板（0.3s）
            base_row = baseline.get(name) or {}
            base_s = base_row.get("baseline_s")
            relative_slow = False
            if (
                not args.no_relative
                and isinstance(base_s, (int, float))
                and float(base_s) > 0
                and status is not None
            ):
                relative_slow = (
                    elapsed > float(base_s) * RELATIVE_FACTOR
                    and elapsed > RELATIVE_FLOOR_SECONDS
                )
            slow = absolute_slow or relative_slow
            slow_reason = "absolute" if absolute_slow else ("relative" if relative_slow else "")
            streaks[name] = streaks[name] + 1 if slow else 0
            # 只有「没变慢」的样本才喂基线；否则基线会被抬高，退化就再也测不出来
            _update_baseline(args.baseline, baseline, name, elapsed, healthy=not slow)
            previous_slowest = daily_state.get("slowest")
            if not isinstance(previous_slowest, dict) or elapsed > float(
                previous_slowest.get("duration_seconds") or 0
            ):
                daily_state["slowest"] = {
                    "time": _iso_now(),
                    "endpoint": name,
                    "duration_seconds": round(elapsed, 3),
                }
            marker = f" SLOW({slow_reason})" if slow else ""
            base_note = f" base={base_s}s" if isinstance(base_s, (int, float)) else ''
            print(
                f"  {name:6s} {status if status is not None else '---':>3} "
                f"{elapsed:8.3f}s{marker}{base_note}"
                + (f" error={error}" if error else ""),
                flush=True,
            )
            if streaks[name] >= max(1, args.consecutive):
                if name not in alerted and not in_grace:
                    record = {
                        "time": _iso_now(),
                        "endpoint": name,
                        "path": path,
                        "duration_seconds": round(elapsed, 3),
                        "threshold_seconds": args.threshold,
                        "slow_reason": slow_reason,
                        "baseline_seconds": base_s,
                        "relative_factor": RELATIVE_FACTOR,
                        "consecutive": streaks[name],
                        "status": status,
                        "error": error,
                        "pid": pid,
                        "rss_mb": rss,
                        "rss_max_mb": rss_max,
                        "thread_count": threads,
                        "uptime_seconds": None if uptime is None else round(uptime, 1),
                        "cold_start_grace_seconds": args.cold_start_grace,
                        "read_cache_entries": debug.get("read_cache_entries"),
                        "gc_count": debug.get("gc_count"),
                    }
                    _append_alert(args.alerts, record)
                    alert_total += 1
                    daily_state["alert_count"] = int(daily_state.get("alert_count") or 0) + 1
                    alerted.add(name)
                    print(f"  ALERT {name} consecutive={streaks[name]} -> {args.alerts}", flush=True)
                elif in_grace and name not in alerted:
                    print(
                        f"  ALERT-SUPPRESSED {name} cold_start_grace={args.cold_start_grace:g}s",
                        flush=True,
                    )
            else:
                alerted.discard(name)

        # ── 排队深度判定（P-55）────────────────────────────────────────────
        # 「排队深度持续上涨」比「某个请求慢」出现得早得多，是真正的先兆信号。
        # 判据：waiting_for_slot > 0 且连续 N 个采样点单调上涨。
        if not args.no_queue:
            inflight = _inflight_metrics(base_url, token, args.timeout)
            if inflight is None:
                if not inflight_missing_logged:
                    print(
                        "  [queue] /api/debug/read-inflight 不可用（404，旧进程或未上线），"
                        "本轮跳过排队深度判定",
                        flush=True,
                    )
                    inflight_missing_logged = True
            elif inflight.get("error"):
                print(f"  [queue] 采集失败: {inflight['error']}（跳过）", flush=True)
            else:
                waiting = int(inflight.get("waiting_for_slot") or 0)
                active = int(inflight.get("active_producers") or 0)
                limit = inflight.get("producer_limit")
                print(
                    f"  [queue] waiting={waiting} active={active} limit={limit} "
                    f"inflight_entries={inflight.get('inflight_entries')}",
                    flush=True,
                )
                queue_hist.append(waiting)
                queue_hist = queue_hist[-max(3, args.queue_depth_samples):]
                rising = (
                    len(queue_hist) >= max(3, args.queue_depth_samples)
                    and all(v > 0 for v in queue_hist)
                    and all(b > a for a, b in zip(queue_hist, queue_hist[1:]))
                )
                # 持续排队：waiting>0 一直不散，哪怕深度不涨也报（这一样会把接口拖到分钟级）
                if waiting > 0:
                    if queue_first_seen is None:
                        queue_first_seen = time.monotonic()
                    sustained = (time.monotonic() - queue_first_seen) >= args.queue_sustain_seconds
                else:
                    queue_first_seen = None
                    sustained = False
                if (rising or sustained) and not queue_alerted and not in_grace:
                    record = {
                        "event": "queue_backlog",
                        "queue_backlog_kind": "rising" if rising else "sustained",
                        "time": _iso_now(),
                        "waiting_for_slot": waiting,
                        "sustained_seconds": (
                            None if queue_first_seen is None
                            else round(time.monotonic() - queue_first_seen, 1)
                        ),
                        "history": list(queue_hist),
                        "active_producers": active,
                        "producer_limit": limit,
                        "inflight_entries": inflight.get("inflight_entries"),
                        "inflight": inflight.get("inflight"),
                        "pid": pid,
                        "rss_mb": rss,
                        "rss_max_mb": rss_max,
                        "thread_count": threads,
                        "uptime_seconds": None if uptime is None else round(uptime, 1),
                    }
                    _append_alert(args.alerts, record)
                    alert_total += 1
                    queue_alerted = True
                    daily_state["alert_count"] = int(daily_state.get("alert_count") or 0) + 1
                    print(
                        f"  ALERT queue_backlog kind={'rising' if rising else 'sustained'} "
                        f"history={queue_hist} waiting={waiting} -> {args.alerts}",
                        flush=True,
                    )
                elif not (rising or sustained):
                    queue_alerted = False

        _write_json_atomic(args.daily_state, daily_state)
        if args.once or (
            args.duration > 0
            and (time.monotonic() - started_at) + args.interval > args.duration
        ):
            break
        time.sleep(max(0.0, args.interval))

    if args.summary_on_exit:
        _append_daily_summary(args.daily_summary, daily_state)
        _write_json_atomic(args.daily_state, daily_state)
    print(
        f"[watchdog] done alerts={alert_total} alerts_file={args.alerts} "
        f"exists={args.alerts.is_file()}",
        flush=True,
    )
    # 【2026-10-23 P-52】原来恒 return 0，让 launchd / 门禁无法感知「已经报过警」。
    # 现在：本轮出现过告警 → 2；否则 0。区别于「脚本自己崩了」（非 0 且非 2）。
    return 2 if alert_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
