import json
import subprocess
import sys
from pathlib import Path

from life_logger.pipeline import run_demo
from life_logger.transcribe import LocalWhisperTranscriber

ROOT = Path(__file__).resolve().parents[1]


def test_demo_does_not_touch_local_asr_loader(tmp_path: Path, monkeypatch) -> None:
    def fail_if_loaded(*args, **kwargs):
        del args, kwargs
        raise AssertionError("Demo tier must not load local ASR")

    monkeypatch.setattr(LocalWhisperTranscriber, "_load_model", fail_if_loaded)

    result = run_demo(tmp_path / "output", timezone_name="Asia/Shanghai")

    assert result.event_count == 5


def test_zero_config_demo_verifier_runs_without_pythonpath(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify_demo.py"),
            "--output",
            str(tmp_path / "demo-output"),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={},
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["mode"] == "demo"
    assert payload["event_count"] == 5
    assert payload["transcript_count"] == 7
    assert payload["transcript_only_count"] == 2
    assert payload["network_allowed"] is False
    assert payload["local_asr_required"] is False
    assert set(payload["categories"]) == {
        "routine",
        "food",
        "expense",
        "exercise",
        "todo",
    }
