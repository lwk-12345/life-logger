from pathlib import Path

import pytest

from life_logger.local_pipeline import (
    build_asr_command,
    build_attribution_command,
    validate_local_pipeline_config,
)
from scripts.local_jobs import _web_stage_commands, validate_web_config


def _config(tmp_path: Path) -> dict[str, object]:
    python = tmp_path / "python"
    asr = tmp_path / "asr"
    vad = tmp_path / "vad"
    template = tmp_path / "template.wav"
    python.write_text("", encoding="utf-8")
    asr.mkdir()
    vad.mkdir()
    template.write_bytes(b"template")
    return {
        "python": str(python),
        "asr": str(asr),
        "vad": str(vad),
        "template": str(template),
        "speaker": "cam++",
        "cpu_threads": 3,
    }


def test_web_asr_stage_uses_shared_product_command(tmp_path: Path) -> None:
    config = _config(tmp_path)
    job_dir = tmp_path / "job"
    item = {
        "sha256": "abc123",
        "stored_path": "content/voice.wav",
        "effective_start_at": "2026-09-04T18:00:00+08:00",
    }

    file_root, audio, asr, _result, web_command = _web_stage_commands(
        job_dir,
        item,
        config,
        timezone_name="Asia/Shanghai",
    )

    assert file_root == job_dir / "files" / "abc123"
    assert web_command == build_asr_command(
        audio=audio,
        output_dir=asr,
        config=config,
        start_at=item["effective_start_at"],
        timezone_name="Asia/Shanghai",
    )


def test_product_asr_command_targets_local_turbo_runner(tmp_path: Path) -> None:
    config = _config(tmp_path)
    command = build_asr_command(
        audio=tmp_path / "voice.wav",
        output_dir=tmp_path / "asr",
        config=config,
        start_at="2026-09-04T18:00:00+08:00",
        timezone_name="Asia/Shanghai",
    )

    assert command[1].endswith("scripts/run_cloud_asr_pipeline.py")
    assert command[command.index("--provider") + 1] == "local_turbo"
    assert command[command.index("--start-at") + 1] == "2026-09-04T18:00:00+08:00"
    assert command[command.index("--timezone") + 1] == "Asia/Shanghai"


def test_attribution_command_includes_speech_separation_model_when_configured(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    separation_model = tmp_path / "mossformer-separation"
    separation_model.mkdir()
    config["speech_separation_model"] = str(separation_model)

    command = build_attribution_command(
        audio=tmp_path / "voice.wav",
        transcript=tmp_path / "transcript.json",
        recordings=tmp_path / "recordings.json",
        output_dir=tmp_path / "result",
        config=config,
    )

    assert command[command.index("--separation-model") + 1] == str(
        separation_model
    )


def test_attribution_command_includes_optional_local_llm_settings(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config["local_llm"] = {
        "enabled": True,
        "model": "qwen2.5:3b",
        "base_url": "http://127.0.0.1:11434",
        "review_confidence": 0.79,
    }

    command = build_attribution_command(
        audio=tmp_path / "voice.wav",
        transcript=tmp_path / "transcript.json",
        recordings=tmp_path / "recordings.json",
        output_dir=tmp_path / "result",
        config=config,
    )

    assert "--local-llm-enabled" in command
    assert command[command.index("--local-llm-model") + 1] == "qwen2.5:3b"
    assert command[command.index("--local-llm-url") + 1] == (
        "http://127.0.0.1:11434"
    )
    assert command[command.index("--local-llm-review-confidence") + 1] == "0.79"




def test_web_and_cli_share_resource_validation(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr("life_logger.local_pipeline.shutil.which", lambda _name: None)

    with pytest.raises(ValueError, match="缺少 ffmpeg 或 ffprobe") as product_error:
        validate_local_pipeline_config(config)
    with pytest.raises(ValueError, match="缺少 ffmpeg 或 ffprobe") as web_error:
        validate_web_config(config)

    assert str(web_error.value) == str(product_error.value)
