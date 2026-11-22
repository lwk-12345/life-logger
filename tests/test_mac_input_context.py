"""Bounded input-context collector tests; uses stored de-identified fixtures."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FIXTURES = ROOT / "tests" / "fixtures"


def _fixture(rel: str) -> Path:
    """优先读入库夹具（tests/fixtures/...）；没有就退回仓库内原位置。

    换机器（新克隆）时 work/、output/ 都不存在 —— 那时只有入库夹具可用。
    本机两者都在，走哪个都等价，所以不影响本地行为。
    """
    shipped = FIXTURES / rel
    return shipped if shipped.exists() else ROOT / rel


sys.path.insert(0, str(ROOT / "scripts"))

import mac_input_context as mic  # noqa: E402

FRONT_25S = _fixture("work/input-target-signal-20261025/front-25s.txt")
FRONT_SAMPLES = _fixture("work/input-target-signal-20261025/front-samples.txt")
MIC_A0 = _fixture("work/input-target-signal-20261025/mic-a0-window.txt")


def collector(*, provider_pid="79304", overlay=()):
    return mic.InputContextCollector(provider_pid=provider_pid, overlay_bundles=overlay)


def test_stored_front_and_mic_fixtures_are_readable():
    front = mic.load_front_fixture(FRONT_SAMPLES)
    events = mic.load_mic_fixture(MIC_A0)
    assert front
    assert {item.bundle for item in front} == {"com.openai.codex"}
    assert events
    assert all(event.pid == "79304" for event in events)


def test_single_front_app_is_reported_without_delivery_inference():
    front = mic.load_front_fixture(FRONT_SAMPLES)
    c = collector()
    for item in front[:3]:
        c.add_front_sample(item)
    c.add_mic_event(mic.MicEvent(front[0].at, "79304", True))
    c.add_mic_event(mic.MicEvent(front[1].at, "79304", False))
    out = c.finalize()
    window = out["windows"][0]
    assert window["started_at"] == front[0].at.isoformat(timespec="milliseconds")
    assert window["ended_at"] == front[1].at.isoformat(timespec="milliseconds")
    assert window["target_app"] == "com.openai.codex"
    assert window["target_unknown"] is False
    assert window["delivery_inferred"] is False
    assert out["policy"]["reads_audio"] is False
    assert out["policy"]["infers_delivery"] is False


def test_missing_stop_keeps_open_window_and_unknown_target():
    front = mic.load_front_fixture(FRONT_SAMPLES)
    c = collector()
    c.add_front_sample(front[0])
    c.add_mic_event(mic.MicEvent(front[0].at, "79304", True))
    out = c.finalize()
    assert out["session"]["complete"] is False
    assert out["windows"][0]["ended_at"] is None
    assert out["windows"][0]["target_app"] is None
    assert out["windows"][0]["target_unknown"] is True
    assert out["windows"][0]["reason"] in {"no_stop_event", "no_front_samples"}


def test_out_of_order_stop_is_ignored_not_paired():
    front = mic.load_front_fixture(FRONT_SAMPLES)
    c = collector()
    c.add_front_sample(front[0])
    c.add_mic_event(mic.MicEvent(front[1].at, "79304", False))
    c.add_mic_event(mic.MicEvent(front[0].at, "79304", True))
    out = c.finalize()
    assert out["windows"] == []
    assert "orphan_stop_ignored" in {a["code"] for a in out["anomalies"]}
    assert out["session"]["complete"] is False


def test_cross_app_switch_is_unknown_never_guessed():
    front = mic.load_front_fixture(FRONT_25S)
    # Real fixture contains both Codex and Finder; keep samples inside one window.
    codex = next(item for item in front if item.bundle == "com.openai.codex")
    finder = next(item for item in front if item.bundle == "com.apple.finder")
    c = collector()
    c.add_front_sample(codex)
    c.add_front_sample(finder)
    c.add_mic_event(mic.MicEvent(min(codex.at, finder.at), "79304", True))
    c.add_mic_event(mic.MicEvent(max(codex.at, finder.at) + timedelta(milliseconds=1), "79304", False))
    out = c.finalize()
    window = out["windows"][0]
    assert window["target_app"] is None
    assert window["target_unknown"] is True
    assert window["reason"] == "front_changed_during_recording"
    assert set(window["front_bundles"]) == {"com.apple.finder", "com.openai.codex"}


def test_overlay_bundle_is_not_a_receiver():
    front = mic.load_front_fixture(FRONT_SAMPLES)
    c = collector(overlay=("com.example.inputmethod",))
    c.add_front_sample(front[0])
    c.add_front_sample(mic.FrontSample(
        front[0].at + timedelta(milliseconds=50),
        "com.example.inputmethod", "999", "fixture",
    ))
    c.add_mic_event(mic.MicEvent(front[0].at, "79304", True))
    c.add_mic_event(mic.MicEvent(front[0].at + timedelta(seconds=1), "79304", False))
    out = c.finalize()
    window = out["windows"][0]
    assert window["target_app"] is None
    assert window["target_unknown"] is True
    assert window["reason"] == "overlay_not_receiver"
    assert window["overlay_bundles"] == ["com.example.inputmethod"]


def test_provider_pid_mismatch_is_ignored():
    front = mic.load_front_fixture(FRONT_SAMPLES)
    c = collector(provider_pid="79304")
    c.add_front_sample(front[0])
    c.add_mic_event(mic.MicEvent(front[0].at, "11111", True))
    out = c.finalize()
    assert out["windows"] == []
    assert "provider_pid_mismatch_ignored" in {a["code"] for a in out["anomalies"]}


def test_wechat_is_context_not_excluded():
    front = mic.load_front_fixture(FRONT_SAMPLES)
    c = collector()
    c.add_front_sample(mic.FrontSample(front[0].at, "com.tencent.xinWeChat", "79263", "fixture"))
    c.add_mic_event(mic.MicEvent(front[0].at, "79304", True))
    c.add_mic_event(mic.MicEvent(front[0].at + timedelta(seconds=1), "79304", False))
    out = c.finalize()
    assert out["windows"][0]["target_app"] == "com.tencent.xinWeChat"


def test_cli_writes_explicit_output_path():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "context.json"
        rc = mic.main([
            "--output", str(target), "--duration", "0.1", "--interval", "0.1",
            "--front-fixture", str(FRONT_SAMPLES),
            "--mic-fixture", str(MIC_A0), "--provider-pid", "79304",
        ])
        assert rc == 0
        payload = json.loads(target.read_text())
        assert payload["schema_version"] == "mac-input-context-v1"
        assert payload["policy"]["reads_audio"] is False
        assert payload["policy"]["infers_delivery"] is False


class MockFrontSource:
    def __init__(self, samples):
        self.samples = list(samples)
        self._index = 0

    def sample(self):
        if self._index >= len(self.samples):
            return mic.FrontSample(self.samples[-1].at + timedelta(seconds=1), None, None, "exhausted")
        item = self.samples[self._index]
        self._index += 1
        return item


def test_replay_clock_advances_monotonically_and_captures_window():
    """回放时钟单调推进：消除一次性全吐导致的 no_front_samples 缺陷。"""
    base_t = mic._now()
    t0 = base_t
    t1 = base_t + timedelta(seconds=1)
    t2 = base_t + timedelta(seconds=2)
    t3 = base_t + timedelta(seconds=3)

    front_samples = [
        mic.FrontSample(t0, "com.example.editor", "1234", "fixture"),
        mic.FrontSample(t1, "com.example.editor", "1234", "fixture"),
        mic.FrontSample(t2, "com.example.editor", "1234", "fixture"),
        mic.FrontSample(t3, "com.example.editor", "1234", "fixture"),
    ]
    mic_events = [
        mic.MicEvent(t1, "79304", True),   # 10:00:01 开始录音
        mic.MicEvent(t3, "79304", False),  # 10:00:03 结束录音
    ]

    front_source = MockFrontSource(front_samples)
    mic_source = mic._FixtureMicSource(mic_events)
    collector = mic.InputContextCollector(
        provider_pid="79304",
        front_source=front_source,
        mic_source=mic_source,
    )

    # 4 次 tick 推进
    for _ in range(4):
        collector.sample_once()

    out = collector.finalize(ended_at=t3)
    assert len(out["windows"]) == 1
    window = out["windows"][0]
    assert window["complete"] is True
    assert window["sample_count"] >= 2, "窗口内采样必须被正确捕获，不能为 0"
    assert window["target_app"] == "com.example.editor"
    assert window["target_unknown"] is False
    assert window["reason"] == "single_front_app"


def test_replay_cross_app_switch_reports_unknown():
    """回放时钟下跨 app 切换正确报告 front_changed_during_recording。"""
    base_t = mic._now()
    t0 = base_t
    t1 = base_t + timedelta(seconds=1)
    t2 = base_t + timedelta(seconds=2)
    t3 = base_t + timedelta(seconds=3)

    front_samples = [
        mic.FrontSample(t0, "com.example.editor", "1234", "fixture"),
        mic.FrontSample(t1, "com.example.editor", "1234", "fixture"),
        mic.FrontSample(t2, "com.apple.finder", "5678", "fixture"),  # 中途切到 Finder
        mic.FrontSample(t3, "com.apple.finder", "5678", "fixture"),
    ]
    mic_events = [
        mic.MicEvent(t1, "79304", True),
        mic.MicEvent(t3, "79304", False),
    ]

    collector = mic.InputContextCollector(
        provider_pid="79304",
        front_source=MockFrontSource(front_samples),
        mic_source=mic._FixtureMicSource(mic_events),
    )

    for _ in range(4):
        collector.sample_once()

    out = collector.finalize(ended_at=t3)
    assert len(out["windows"]) == 1
    window = out["windows"][0]
    assert window["complete"] is True
    assert window["target_app"] is None
    assert window["target_unknown"] is True
    assert window["reason"] == "front_changed_during_recording"
    assert set(window["front_bundles"]) == {"com.example.editor", "com.apple.finder"}


def test_dynamic_provider_pid_source_distinction():
    """独立记录显式 PID 来源：动态发现标 mic_event，显式传参标 argument。"""
    now = mic._now()

    # 1. 未传 provider_pid：动态发现后标记为 mic_event，绝不误标为 argument
    c_dynamic = mic.InputContextCollector(provider_pid=None)
    assert c_dynamic.explicit_provider_pid is False
    c_dynamic.add_mic_event(mic.MicEvent(now, "88888", True))
    c_dynamic.add_mic_event(mic.MicEvent(now + timedelta(seconds=1), "88888", False))
    out_dynamic = c_dynamic.finalize()
    assert out_dynamic["provider"]["pid"] == "88888"
    assert out_dynamic["provider"]["source"] == "mic_event", "动态发现必须标 mic_event"

    # 2. 显式传入 provider_pid：标记为 argument
    c_explicit = mic.InputContextCollector(provider_pid="99999")
    assert c_explicit.explicit_provider_pid is True
    c_explicit.add_mic_event(mic.MicEvent(now, "99999", True))
    out_explicit = c_explicit.finalize()
    assert out_explicit["provider"]["pid"] == "99999"
    assert out_explicit["provider"]["source"] == "argument", "显式传参必须标 argument"

    # 3. 未传且未发现：标记为 unresolved
    c_none = mic.InputContextCollector(provider_pid=None)
    out_none = c_none.finalize()
    assert out_none["provider"]["pid"] is None
    assert out_none["provider"]["source"] == "unresolved"
