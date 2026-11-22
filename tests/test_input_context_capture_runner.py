import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mac_input_context as mic
import run_input_context_capture as runner


def test_live_snapshot_does_not_close_real_window(tmp_path):
    c = mic.InputContextCollector(provider_pid="fake-pid")
    at = mic._now()
    c.add_mic_event(mic.MicEvent(at, "fake-pid", True))
    c.add_front_sample(mic.FrontSample(at, "com.openai.codex", "1"))
    out = tmp_path / "latest.json"
    runner.publish(c, out)
    first = json.loads(out.read_text())
    assert first["capture_state"] == "running"
    assert first["windows"][0]["complete"] is False
    assert first["windows"][0]["target_unknown"] is True
    assert c.open_window is not None and c.windows == []
    runner.publish(c, out)
    assert c.open_window is not None and c.windows == []
    c.add_mic_event(mic.MicEvent(at + timedelta(seconds=2), "fake-pid", False))
    runner.publish(c, out, reason="duration")
    final = json.loads(out.read_text())
    assert final["capture_state"] == "stopped"
    assert len(final["windows"]) == 1
    assert final["windows"][0]["complete"] is True
    assert final["windows"][0]["provider"]["pid"] == "fake-pid"


def test_no_input_is_not_a_target(tmp_path):
    c = mic.InputContextCollector(provider_pid="fake-pid")
    out = tmp_path / "latest.json"
    runner.publish(c, out)
    payload = json.loads(out.read_text())
    assert payload["windows"] == []
    assert payload["session"]["complete"] is False
