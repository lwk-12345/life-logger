"""mic 轮询必须**连续覆盖**，不能靠固定 lookback（2026-11-06 漏记豆包语音输入）。

事故：采样步在真实负载下中位 8.7s、p90 37s（11-06 实测），而每次只
`log show --last 5s`。两步之间的空档里 1~2 秒的短语音输入（豆包输入法长按 Option）
整段丢失：11-06 21:47:04 → 22:51:02 之间 **一个 mic_event 都没有**。

本文件不碰真 `/usr/bin/log`：只覆写 `_read_range(start, end)` 这个缝，
用合成日志行喂给同一套解析/去重/闭窗逻辑。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mac_input_context as mic  # noqa: E402

PID = "79304"
SPEECH_SECONDS = 1.5


def _line(at: datetime, active: bool) -> str:
    """伪造一行 coreaudiod 日志（与 `_MIC_RE` 对齐）。"""
    stamp = at.strftime("%Y-%m-%d %H:%M:%S.%f")
    return (
        f"{stamp} Df coreaudiod[123:456] [com.apple.audio:hal] "
        f"PublishRecordingClientInfo client {PID} running: {'yes' if active else 'no'}"
    )


class _ScriptedMicSource(mic.LiveMicSource):
    """把"日志里有什么"换成可控事件表，其余逻辑（跨度/重叠/去重/mic_gap）全用真的。"""

    def __init__(self, events: list[mic.MicEvent], **kwargs):
        super().__init__(**kwargs)
        self._events = sorted(events, key=lambda e: e.at)
        self.ranges: list[tuple[datetime, datetime]] = []

    def _read_range(self, start: datetime, end: datetime) -> str:
        self.ranges.append((start, end))
        return "\n".join(
            _line(e.at, e.active) for e in self._events if start <= e.at <= end
        )


class _LegacyMicSource(_ScriptedMicSource):
    """改前的行为：每次只读 `--last lookback`，不看上次截止。"""

    def poll(self, now: datetime | None = None) -> list[mic.MicEvent]:
        end = now or mic._now()
        return self._legacy_poll(end)

    def _legacy_poll(self, end: datetime) -> list[mic.MicEvent]:
        start = end - timedelta(seconds=self.lookback)
        self.ranges.append((start, end))
        result = "\n".join(
            _line(e.at, e.active) for e in self._events if start <= e.at <= end
        )
        events = mic.parse_mic_lines(result.splitlines(), source="coreaudiod")
        fresh = []
        for event in events:
            key = (event.at.isoformat(), event.pid, event.active)
            if key not in self._seen:
                self._seen[key] = event.at
                fresh.append(event)
        return fresh


class _MockFront:
    """前台采样源；每次 `sample()` 用当前设定的时刻（测试里逐步推进）。"""

    def __init__(self, at: datetime, bundle: str = "com.example.editor"):
        self.at = at
        self.bundle = bundle

    def sample(self) -> mic.FrontSample:
        return mic.FrontSample(self.at, self.bundle, "1234", "fixture")


def _step(collector: mic.InputContextCollector, at: datetime) -> None:
    """走一个采样步。

    采集器把**前台采样的时刻**当作本步的时钟（`sample_once` 里 `sample_at` 优先于
    传入的 `at`），所以这里同步推进前台源的时刻，等价于真实运行的墙钟前进。
    """
    collector.front_source.at = at
    collector.sample_once(at=at)


def _speech(base: datetime, offset: float = 2.0, length: float = SPEECH_SECONDS):
    """一段语音：yes 在 base+offset，no 在 base+offset+length。"""
    start = base + timedelta(seconds=offset)
    return [mic.MicEvent(start, PID, True),
            mic.MicEvent(start + timedelta(seconds=length), PID, False)]


def _collector(mic_source, front_at: datetime) -> mic.InputContextCollector:
    return mic.InputContextCollector(provider_pid=PID, front_source=_MockFront(front_at),
                                     mic_source=mic_source)


# --- 核心：9 秒步间隔 + 1.5 秒语音落在旧 5 秒盲区 ---------------------------

def test_legacy_five_second_lookback_misses_speech_in_the_step_gap():
    """改前：步间隔 9s、语音落在 [T0, T0+4] 的盲区 → 整段丢失（复现事故）。"""
    base = mic._now()
    speech = _speech(base)
    source = _LegacyMicSource(speech, lookback_seconds=5.0)
    collector = _collector(source, front_at=base)

    _step(collector, base)                    # 步 1：读 [T0-5, T0]
    _step(collector, base + timedelta(seconds=9))   # 步 2：读 [T0+4, T0+9]

    out = collector.finalize(ended_at=base + timedelta(seconds=9))
    assert out["windows"] == [], "改前应当**丢失**这段语音（这正是事故现象）"
    assert source.ranges[1][0] > speech[0].at, "旧实现的第二次读取起点已经晚于语音开始"


def test_continuous_polling_captures_speech_inside_the_step_gap():
    """改后：同样的 9 秒步间隔，因为从"上次截止 − 2s"接着读，语音被完整捕获。"""
    base = mic._now()
    speech = _speech(base)
    source = _ScriptedMicSource(speech)
    collector = _collector(source, front_at=base)

    _step(collector, base)
    _step(collector, base + timedelta(seconds=9))

    out = collector.finalize(ended_at=base + timedelta(seconds=9))
    assert len(out["windows"]) == 1, "改后必须捕获到这段 1.5 秒语音"
    window = out["windows"][0]
    assert window["complete"] is True
    assert window["duration_seconds"] == SPEECH_SECONDS
    assert window["provider_pid"] == PID
    # 第二次读取的起点必须回到"上次截止 − 2s"，而不是 T0+4
    assert source.ranges[1][0] <= base - timedelta(seconds=2)


# --- 重叠不能产生重复 -------------------------------------------------------

def test_overlap_does_not_duplicate_events():
    """每次多读 2s 重叠，事件必须靠 _seen 去重，一条都不能变两条。"""
    base = mic._now()
    speech = _speech(base, offset=1.0)
    source = _ScriptedMicSource(speech)
    collector = _collector(source, front_at=base)

    for step in range(4):                       # T0, +3, +6, +9
        _step(collector, base + timedelta(seconds=3 * step))

    out = collector.finalize(ended_at=base + timedelta(seconds=9))
    assert len(collector.mic_events) == 2, "yes/no 两条事件各只能出现一次"
    assert len(out["windows"]) == 1
    # 重叠确实发生过（相邻两次读取的区间相交），但没有造成重复
    for (s1, e1), (s2, e2) in zip(source.ranges, source.ranges[1:]):
        assert s2 <= e1, "相邻读取应当重叠"
    assert not [a for a in collector.anomalies if a["code"] == "out_of_order_ignored"]


def test_first_poll_uses_bounded_lookback_only():
    """首轮没有"上次截止"，只能用有限 lookback（不能从开天辟地读起）。"""
    base = mic._now()
    source = _ScriptedMicSource([])
    collector = _collector(source, front_at=base)

    _step(collector, base)

    start, end = source.ranges[0]
    assert abs((end - start).total_seconds() - 5.0) < 0.001


# --- 长停顿：夹上限 + 记 mic_gap，不假装覆盖 -------------------------------

def test_long_pause_is_clipped_and_reported_as_mic_gap():
    """停顿 700s：只读最后 300s，前面 400s 缺口写进 anomalies 的 mic_gap。"""
    base = mic._now()
    source = _ScriptedMicSource([])
    collector = _collector(source, front_at=base)

    _step(collector, base)
    _step(collector, base + timedelta(seconds=700))

    start, end = source.ranges[-1]
    assert abs((end - start).total_seconds() - mic.MIC_MAX_SPAN_SECONDS) < 0.001

    gaps = [a for a in collector.anomalies if a["code"] == "mic_gap"]
    assert len(gaps) == 1, collector.anomalies
    gap = gaps[0]
    assert gap["gap_from"].startswith((base - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%S"))
    # 缺口 = 停顿 700s − 上限 300s + 2s 重叠（重叠那 2s 也在"没读"的范围里，如实记账）
    assert 700 - mic.MIC_MAX_SPAN_SECONDS <= float(gap["gap_seconds"]) <= 700 - mic.MIC_MAX_SPAN_SECONDS + 3
    assert gap["gap_to"] and gap["gap_from"]


def test_short_steps_never_report_a_gap():
    base = mic._now()
    source = _ScriptedMicSource(_speech(base))
    collector = _collector(source, front_at=base)
    for step in range(3):
        _step(collector, base + timedelta(seconds=2 * step))
    assert not [a for a in collector.anomalies if a["code"] == "mic_gap"]


# --- _seen 有界 -------------------------------------------------------------

def test_seen_cache_is_bounded_and_pruned_by_time():
    base = mic._now()
    source = _ScriptedMicSource([])
    assert source._seen == {}

    # 塞满超过上限的记录，其中一半是很久以前的
    old = base - timedelta(seconds=mic.MIC_SEEN_WINDOW_SECONDS + 60)
    for index in range(mic.MIC_SEEN_MAX_ENTRIES):
        source._seen[(f"k{index}", PID, True)] = base if index % 2 else old
    source._prune_seen(base)

    assert len(source._seen) <= mic.MIC_SEEN_MAX_ENTRIES
    assert all(at >= base - timedelta(seconds=mic.MIC_SEEN_WINDOW_SECONDS)
               for at in source._seen.values())


# --- 输出契约没变 -----------------------------------------------------------

def test_window_schema_is_unchanged():
    """`mac-input-context-v1` 的窗口字段一个都没多、一个都没少。"""
    base = mic._now()
    source = _ScriptedMicSource(_speech(base))
    collector = _collector(source, front_at=base)
    _step(collector, base)
    _step(collector, base + timedelta(seconds=5))
    out = collector.finalize(ended_at=base + timedelta(seconds=5))

    assert out["schema_version"] == "mac-input-context-v1"
    assert set(out["windows"][0]) == {
        "started_at", "ended_at", "duration_seconds", "provider", "provider_pid",
        "front_bundles", "front_pids", "sample_count", "overlay_bundles",
        "target_app", "target_unknown", "reason", "complete", "delivery_inferred",
        "idle_seconds",
    }
    assert out["policy"] == {
        "reads_audio": False, "reads_transcript": False, "reads_window_title": False,
        "reads_clipboard": False, "reads_keyboard": False, "infers_delivery": False,
        "changes_permissions": False, "long_running_daemon": False,
    }
