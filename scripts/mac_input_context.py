#!/usr/bin/env python3
"""Bounded macOS input-context collector (recording window + frontmost app).

Manually started/stopped, bounded by --duration and optional --stop-file.
It reads ONLY:
  * coreaudiod ``PublishRecordingClientInfo`` log lines (boolean + client pid),
  * LaunchServices front app (bundle id + pid),
  * ``ps`` for the provider process name.
It never reads audio, transcript text, window titles, clipboard or keyboard.

Output is JSON. A front app is context evidence only; this tool never infers
that text was delivered/sent and never excludes WeChat/work/life events.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA = "mac-input-context-v1"
MAX_DURATION_SECONDS = 120.0
DEFAULT_DURATION_SECONDS = 10.0
_FRONT_RE = re.compile(r'"CFBundleIdentifier"="([^"]*)"\s+"pid"=(\d+)')
_MIC_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+).*?"
    r"client\s+(?P<pid>\d+)\s+running:\s*(?P<active>yes|no)",
    re.IGNORECASE,
)
_FRONT_FIXTURE_RE = re.compile(r'^\s*([0-9.]+)\s*\|\s*(.*)$')


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _now() -> datetime:
    return datetime.now().astimezone()


def _dt_from_mic_stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=_local_tz())


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds") if value else None


@dataclass(frozen=True)
class FrontSample:
    at: datetime
    bundle: str | None
    pid: str | None
    source: str = "lsappinfo"
    #: 采样时刻用户已空闲多少秒（HIDIdleTime，只读系统计数，不碰键鼠内容）
    idle_seconds: float | None = None

    def as_dict(self) -> dict:
        out = {"at": _iso(self.at), "bundle": self.bundle, "pid": self.pid, "source": self.source}
        if self.idle_seconds is not None:
            out["idle_seconds"] = self.idle_seconds
        return out


@dataclass(frozen=True)
class MicEvent:
    at: datetime
    pid: str
    active: bool
    source: str = "coreaudiod"

    def as_dict(self) -> dict:
        return {"at": _iso(self.at), "client_pid": self.pid, "active": self.active, "source": self.source}


_IDLE_CACHE: dict[str, float] = {"at": 0.0, "value": -1.0}


def read_hid_idle_seconds(*, cache_seconds: float = 5.0) -> float | None:
    """只读 HIDIdleTime（纳秒转秒）。取不到返回 None —— 调用方按「未知」处理，不猜。"""
    now = time.monotonic()
    if _IDLE_CACHE["value"] >= 0 and now - _IDLE_CACHE["at"] < cache_seconds:
        return _IDLE_CACHE["value"]
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "1"],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    if not match:
        return None
    value = int(match.group(1)) / 1_000_000_000.0
    _IDLE_CACHE.update({"at": now, "value": value})
    return value


class LiveFrontSource:
    """LaunchServices only: no AX, no System Events, no window title."""

    def sample(self) -> FrontSample:
        try:
            front = subprocess.run(
                ["lsappinfo", "front"], capture_output=True, text=True, timeout=2
            ).stdout.strip()
            if not front:
                return FrontSample(_now(), None, None, "lsappinfo_unavailable")
            info = subprocess.run(
                ["lsappinfo", "info", "-only", "bundleid,pid", front],
                capture_output=True, text=True, timeout=2,
            ).stdout
            match = _FRONT_RE.search(info)
            if not match:
                return FrontSample(_now(), None, None, "lsappinfo_unparsed")
            return FrontSample(_now(), match.group(1) or None, match.group(2) or None,
                               idle_seconds=read_hid_idle_seconds())
        except (OSError, subprocess.SubprocessError):
            return FrontSample(_now(), None, None, "lsappinfo_unavailable")


class FixtureFrontSource:
    """Replay the already-stored de-identified front sample format."""

    def __init__(self, path: str | Path):
        self.samples = load_front_fixture(path)
        self._index = 0

    def sample(self) -> FrontSample:
        if self._index >= len(self.samples):
            return FrontSample(_now(), None, None, "fixture_exhausted")
        item = self.samples[self._index]
        self._index += 1
        return item


def load_front_fixture(path: str | Path) -> list[FrontSample]:
    out: list[FrontSample] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = _FRONT_FIXTURE_RE.match(line)
        if not match:
            continue
        try:
            at = datetime.fromtimestamp(float(match.group(1)), tz=_local_tz())
        except (ValueError, OSError):
            continue
        parsed = re.search(r'"CFBundleIdentifier"="([^"]*)"\s+"pid"=(\d+)', match.group(2))
        out.append(FrontSample(
            at, parsed.group(1) if parsed else None, parsed.group(2) if parsed else None,
            "fixture",
        ))
    return out


def parse_mic_lines(lines: Iterable[str], *, source: str = "fixture") -> list[MicEvent]:
    out: list[MicEvent] = []
    for line in lines:
        match = _MIC_RE.search(line)
        if not match:
            continue
        out.append(MicEvent(
            _dt_from_mic_stamp(match.group("ts")), match.group("pid"),
            match.group("active").lower() == "yes", source,
        ))
    return out


def load_mic_fixture(path: str | Path) -> list[MicEvent]:
    return parse_mic_lines(Path(path).read_text(encoding="utf-8").splitlines())


#: mic 轮询的单次最大跨度。超过就把起点夹到 `now - 上限`，并**记一条 mic_gap**
#: 说明哪一段没读 —— 绝不假装覆盖（长时间停顿后一次读几百秒会让 `log show` 变慢，
#: 而且丢掉的本来就丢了，掩盖它只会让下游以为"那段时间没有语音输入"）。
MIC_MAX_SPAN_SECONDS = 300.0
#: 相邻两次轮询的重叠秒数（去重靠 _seen，重叠只用来吸收时钟抖动/日志延迟落盘）。
MIC_OVERLAP_SECONDS = 2.0
#: `_seen` 的时间窗与条数上限：去重只需要覆盖「重叠 + 一次跨度」，绝不能无限增长。
MIC_SEEN_WINDOW_SECONDS = 3600.0
MIC_SEEN_MAX_ENTRIES = 20000
#: 首轮（还没有"上次截止"）只回看这么多秒。
MIC_FIRST_LOOKBACK_SECONDS = 5.0


class LiveMicSource:
    """Poll read-only coreaudiod log; no audio stream is opened.

    【2026-11-06 修漏记】旧实现每次 `log show --last 5s`（lookback 上限 10s），
    而采样步在真实负载下中位 8.7s、p90 37s（11-06 实测），两步之间的空档里
    1~2 秒的短语音输入（豆包输入法长按 Option）整段丢失 —— 21:47→22:51 之间
    一个 mic_event 都没有。

    现在改成**连续覆盖**：记住上次轮询的截止时刻，下次从「上次截止 − 2s 重叠」
    读到现在，不再依赖步长；重叠部分由 `_seen` 去重。首轮用有限 lookback；
    单次跨度超过 `MIC_MAX_SPAN_SECONDS` 时夹起并记 `mic_gap`。
    """

    def __init__(
        self,
        *,
        lookback_seconds: float = MIC_FIRST_LOOKBACK_SECONDS,
        max_span_seconds: float = MIC_MAX_SPAN_SECONDS,
        overlap_seconds: float = MIC_OVERLAP_SECONDS,
    ):
        self.lookback = max(1.0, min(10.0, float(lookback_seconds)))
        self.max_span = max(10.0, float(max_span_seconds))
        self.overlap = max(0.0, min(10.0, float(overlap_seconds)))
        self._seen: dict[tuple[str, str, bool], datetime] = {}
        self._last_covered: datetime | None = None
        #: 没被读到的区间（供 collector 转成 `mic_gap` 异常，绝不静默）
        self.gaps: list[dict[str, str | float]] = []
        self.last_span_seconds: float | None = None

    def _prune_seen(self, now: datetime) -> None:
        if len(self._seen) <= MIC_SEEN_MAX_ENTRIES // 2:
            return
        cutoff = now - timedelta(seconds=MIC_SEEN_WINDOW_SECONDS)
        self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        if len(self._seen) > MIC_SEEN_MAX_ENTRIES:
            # 仍超上限（极端日志洪水）：按时间淘汰最旧的一半，绝不无限增长
            ordered = sorted(self._seen.items(), key=lambda kv: kv[1])
            self._seen = dict(ordered[len(ordered) // 2:])

    def _read_range(self, start: datetime, end: datetime) -> str:
        predicate = (
            'eventMessage CONTAINS "PublishRecordingClientInfo" '
            'AND eventMessage CONTAINS "running:"'
        )
        argv = [
            "/usr/bin/log", "show",
            "--start", start.strftime("%Y-%m-%d %H:%M:%S"),
            "--end", end.strftime("%Y-%m-%d %H:%M:%S"),
            "--info", "--debug", "--style", "compact", "--predicate", predicate,
        ]
        timeout = max(5.0, min(120.0, (end - start).total_seconds() + 5.0))
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def poll(self, now: datetime | None = None) -> list[MicEvent]:
        end = now or _now()
        if end.tzinfo is None:
            end = end.replace(tzinfo=_local_tz())
        if self._last_covered is None:
            start = end - timedelta(seconds=self.lookback)
        else:
            start = self._last_covered - timedelta(seconds=self.overlap)
        if start >= end:
            return []                     # 时钟没前进 / 回拨：不重复读
        span = (end - start).total_seconds()
        if span > self.max_span:
            # 长停顿：只读最后 max_span，前面的缺口如实记录（不假装覆盖）
            clipped_from = start
            start = end - timedelta(seconds=self.max_span)
            self.gaps.append({
                "from": _iso(clipped_from),
                "to": _iso(start),
                "seconds": round((start - clipped_from).total_seconds(), 3),
            })
        self.last_span_seconds = round((end - start).total_seconds(), 3)

        result = self._read_range(start, end)
        events = parse_mic_lines(result.splitlines(), source="coreaudiod")
        fresh: list[MicEvent] = []
        for event in events:
            key = (event.at.isoformat(), event.pid, event.active)
            if key not in self._seen:
                self._seen[key] = event.at
                fresh.append(event)
        self._last_covered = end
        self._prune_seen(end)
        return fresh


def _nearest_sample(samples, start, end, grace_seconds: float = 10.0):
    """窗口两侧 grace 秒内、离窗口最近的一次前台采样（没有就返回 None）。"""
    best = None
    best_gap = grace_seconds
    for s in samples or []:
        at = getattr(s, "at", None)
        if at is None:
            continue
        if end is not None and start <= at <= end:
            return s
        gap = min(abs((at - start).total_seconds()),
                  abs((at - end).total_seconds()) if end is not None else 1e9)
        if gap <= best_gap:
            best_gap = gap
            best = s
    return best


def _process_name(pid: str | None) -> str | None:
    if not pid:
        return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class InputContextCollector:
    """Pure state machine; sources can be live or de-identified fixtures."""

    def __init__(
        self,
        *,
        provider_pid: str | None = None,
        overlay_bundles: Sequence[str] = (),
        front_source=None,
        mic_source=None,
    ):
        self.explicit_provider_pid = bool(provider_pid and str(provider_pid).strip())
        self.provider_pid = str(provider_pid).strip() if provider_pid else None
        self.overlay_bundles = {str(x).strip() for x in overlay_bundles if str(x).strip()}
        self.front_source = front_source or LiveFrontSource()
        self.mic_source = mic_source or LiveMicSource()
        self.started_at = _now()
        self.front_samples: list[FrontSample] = []
        self.mic_events: list[MicEvent] = []
        self.open_window: dict | None = None
        self.windows: list[dict] = []
        self.anomalies: list[dict] = []
        self._last_event_at: datetime | None = None
        self._provider_seen: str | None = None

    def _anomaly(self, code: str, at: datetime | None = None, **detail) -> None:
        self.anomalies.append({"code": code, "at": _iso(at), **detail})

    def add_front_sample(self, sample: FrontSample) -> None:
        self.front_samples.append(sample)

    def add_mic_event(self, event: MicEvent) -> None:
        if self.provider_pid and event.pid != self.provider_pid:
            self._anomaly("provider_pid_mismatch_ignored", event.at, client_pid=event.pid)
            return
        if self.provider_pid is None:
            self.provider_pid = event.pid
        if self._provider_seen is None:
            self._provider_seen = event.pid
        if self._last_event_at is not None and event.at < self._last_event_at:
            self._anomaly("out_of_order_ignored", event.at, client_pid=event.pid)
            return
        self._last_event_at = event.at
        self.mic_events.append(event)
        if event.active:
            if self.open_window is not None:
                self._anomaly("duplicate_start_ignored", event.at, client_pid=event.pid)
                return
            self.open_window = {"start": event.at, "pid": event.pid, "samples": []}
            return
        if self.open_window is None:
            self._anomaly("orphan_stop_ignored", event.at, client_pid=event.pid)
            return
        self._close_window(event.at, complete=True, reason="stop_event")

    def _close_window(self, end: datetime | None, *, complete: bool, reason: str) -> None:
        window = self.open_window
        if window is None:
            return
        start = window["start"]
        samples = [
            s for s in self.front_samples
            if s.at >= start and (end is None or s.at <= end)
        ]
        bundles = sorted({s.bundle for s in samples if s.bundle})
        pids = sorted({s.pid for s in samples if s.pid})
        overlay = sorted(self.overlay_bundles.intersection(bundles))
        target: str | None = None
        unknown = True
        if not samples:
            # [2026-11-05] 豆包输入法的 mic 窗口只有 0.003–1.2s，0.5s 采样几乎不可能落在里面，
            # 于是今天 306 条里 201 条 target_unknown。这里退一步：用窗口两侧 ±10s 内最近的一次
            # 前台采样作为「当时的前台」，并如实标注来源，不冒充是窗口内的采样。
            nearest = _nearest_sample(self.front_samples, start, end)
            if nearest is not None and nearest.bundle:
                samples = [nearest]
                reason_out = "nearest_front_sample"
                bundles = [nearest.bundle]
                pids = [nearest.pid] if nearest.pid else []
            else:
                reason_out = "no_front_samples"
        elif overlay:
            reason_out = "overlay_not_receiver"
        elif len(bundles) != 1:
            reason_out = "front_changed_during_recording"
        elif not complete:
            reason_out = "no_stop_event"
        else:
            target = bundles[0]
            unknown = False
            reason_out = "single_front_app"
        provider_source = "argument" if self.explicit_provider_pid else ("mic_event" if window["pid"] else "unresolved")
        provider_data = {
            "pid": window["pid"],
            "process": _process_name(window["pid"]),
            "source": provider_source,
        }
        self.windows.append({
            "started_at": _iso(start),
            "ended_at": _iso(end),
            "duration_seconds": round((end - start).total_seconds(), 3) if end else None,
            "provider": provider_data,
            "provider_pid": window["pid"],
            "front_bundles": bundles,
            "front_pids": pids,
            "sample_count": len(samples),
            "overlay_bundles": overlay,
            "target_app": target,
            "target_unknown": unknown,
            "reason": reason_out,
            "complete": bool(complete),
            "delivery_inferred": False,
            "idle_seconds": (samples[-1].idle_seconds if samples and getattr(samples[-1], "idle_seconds", None) is not None else None),
        })
        self.open_window = None

    def sample_once(self, *, include_front: bool = True, at: datetime | None = None) -> None:
        sample_at: datetime | None = at
        if include_front:
            sample = self.front_source.sample()
            self.add_front_sample(sample)
            if sample and getattr(sample, "at", None):
                sample_at = sample.at
        clock = sample_at or self._last_event_at or _now()
        if hasattr(self.mic_source, "advance_time"):
            self.mic_source.advance_time(clock)
        try:
            events = self.mic_source.poll(clock)
        except TypeError:
            events = self.mic_source.poll()
        for event in events:
            self.add_mic_event(event)
        self._drain_mic_gaps()

    def _drain_mic_gaps(self) -> None:
        """把采集源没覆盖到的区间记成 `mic_gap` 异常 —— 不许假装覆盖。"""
        gaps = getattr(self.mic_source, "gaps", None)
        if not isinstance(gaps, list) or not gaps:
            return
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            self._anomaly(
                "mic_gap", None,
                gap_from=gap.get("from"),
                gap_to=gap.get("to"),
                gap_seconds=gap.get("seconds"),
            )
        gaps.clear()

    def finalize(self, *, stop_reason: str = "duration", ended_at: datetime | None = None) -> dict:
        end = ended_at or _now()
        if self.open_window is not None:
            self._close_window(None, complete=False, reason="no_stop_event")
        provider_pid = self.provider_pid
        return {
            "schema_version": SCHEMA,
            "generated_at": _iso(_now()),
            "session": {
                "started_at": _iso(self.started_at),
                "ended_at": _iso(end),
                "stop_reason": stop_reason,
                "duration_seconds": round((end - self.started_at).total_seconds(), 3),
                "complete": bool(self.windows) and all(w.get("complete") for w in self.windows),
            },
            "provider": {
                "pid": provider_pid,
                "process": _process_name(provider_pid),
                "source": "argument" if self.explicit_provider_pid else ("mic_event" if self.provider_pid else "unresolved"),
            },
            "policy": {
                "reads_audio": False,
                "reads_transcript": False,
                "reads_window_title": False,
                "reads_clipboard": False,
                "reads_keyboard": False,
                "infers_delivery": False,
                "changes_permissions": False,
                "long_running_daemon": False,
            },
            "windows": self.windows,
            "anomalies": self.anomalies,
            "samples": [s.as_dict() for s in self.front_samples],
            "boundary": "front app is context evidence only; it does not prove text was delivered or sent",
        }


def write_atomic(path: str | Path, payload: dict) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return target


def _bounded_duration(value: float) -> float:
    value = float(value)
    if value <= 0 or value > MAX_DURATION_SECONDS:
        raise argparse.ArgumentTypeError(f"duration must be >0 and <= {MAX_DURATION_SECONDS:g}s")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None, help="JSON output path (default: new temp file)")
    parser.add_argument("--duration", type=_bounded_duration, default=DEFAULT_DURATION_SECONDS,
                        help=f"bounded run seconds (default {DEFAULT_DURATION_SECONDS:g}, max {MAX_DURATION_SECONDS:g})")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="front sample interval seconds (0.1..5, default 0.5)")
    parser.add_argument("--provider-pid", default=None, help="only accept this dictation client PID")
    parser.add_argument("--overlay-bundle", action="append", default=[],
                        help="known input-method/overlay bundle; never used as target")
    parser.add_argument("--front-fixture", type=Path, default=None,
                        help="replay existing front sample fixture instead of LaunchServices")
    parser.add_argument("--mic-fixture", type=Path, default=None,
                        help="replay existing de-identified coreaudiod fixture")
    parser.add_argument("--stop-file", type=Path, default=None,
                        help="manual stop: stop when this file exists")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    interval = max(0.1, min(5.0, float(args.interval)))
    output = args.output or Path(tempfile.mkdtemp(prefix="life-logger-input-context-")) / "input-context.json"
    front_source = FixtureFrontSource(args.front_fixture) if args.front_fixture else LiveFrontSource()
    mic_source = None
    if args.mic_fixture:
        events = load_mic_fixture(args.mic_fixture)
        mic_source = _FixtureMicSource(events)
    collector = InputContextCollector(
        provider_pid=args.provider_pid,
        overlay_bundles=args.overlay_bundle,
        front_source=front_source,
        mic_source=mic_source or LiveMicSource(),
    )
    stop_requested = {"value": False}

    def _stop(_signum, _frame):
        stop_requested["value"] = True

    old_int = signal.signal(signal.SIGINT, _stop)
    old_term = signal.signal(signal.SIGTERM, _stop)
    deadline = time.monotonic() + float(args.duration)
    reason = "duration"
    try:
        while time.monotonic() < deadline and not stop_requested["value"]:
            if args.stop_file and args.stop_file.exists():
                reason = "stop_file"
                break
            collector.sample_once()
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    payload = collector.finalize(stop_reason=reason)
    path = write_atomic(output, payload)
    print(f"input_context_output={path}")
    print(f"windows={len(payload['windows'])} complete={payload['session']['complete']}")
    return 0


class _FixtureMicSource:
    """Replay pre-recorded mic events according to a monotonic replay clock."""

    def __init__(self, events: list[MicEvent]):
        self.events = sorted(events, key=lambda item: item.at)
        self._index = 0
        self._current_time: datetime | None = None

    def advance_time(self, at: datetime) -> None:
        if self._current_time is None or at > self._current_time:
            self._current_time = at

    def poll(self, upto: datetime | None = None) -> list[MicEvent]:
        target = upto or self._current_time
        if target is None:
            # 未提供时钟时，逐个推进，每次最多吐出 1 个事件，绝不一次性提前闭窗
            if self._index >= len(self.events):
                return []
            ev = self.events[self._index]
            self._index += 1
            return [ev]
        out: list[MicEvent] = []
        while self._index < len(self.events):
            ev = self.events[self._index]
            if ev.at <= target:
                out.append(ev)
                self._index += 1
            else:
                break
        return out


if __name__ == "__main__":
    raise SystemExit(main())
