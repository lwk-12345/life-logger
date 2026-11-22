"""Provider-aware doctor checks.

Regression background (2026-10-17 audit): doctor required the legacy Fun-ASR
``asr`` path and imported ``funasr``/``funasr_onnx`` even when production was
configured for MOSS.  A healthy MOSS install therefore reported a permanent red
``resource_asr`` failure, and the check wasted time in the wrong runtime.
"""

import json
from types import SimpleNamespace

from life_logger.doctor import run_doctor


def _base_config(tmp_path, **overrides):
    worker = tmp_path / "worker-python"
    worker.write_text("", encoding="utf-8")
    template = tmp_path / "template.wav"
    template.write_bytes(b"wav")
    payload = {
        "python": str(worker),
        "template": str(template),
        "speaker": "local_campplus",
    }
    payload.update(overrides)
    config_path = tmp_path / "local-config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path, payload


def _runner(calls, returncode=0, stderr=""):
    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=returncode,
            stdout="worker-ok" if returncode == 0 else "",
            stderr=stderr,
        )

    return run


def _report(config_path, tmp_path, runner):
    return run_doctor(
        config_path,
        job_root=tmp_path / "local-web",
        web_health={
            "ok": True,
            "server_build_id": "provider-test",
            "server_started_at": "2026-10-17T12:00:00+08:00",
        },
        runner=runner,
        finder=lambda name: f"/usr/local/bin/{name}",
    )


def test_moss_provider_skips_legacy_asr_resources_and_funasr_imports(tmp_path) -> None:
    moss_python = tmp_path / "moss-python"
    moss_python.write_text("", encoding="utf-8")
    moss_model = tmp_path / "moss-model"
    moss_model.mkdir()
    config_path, _ = _base_config(
        tmp_path,
        asr_provider="moss",
        moss_python=str(moss_python),
        moss_model=str(moss_model),
    )
    calls = []
    report = _report(config_path, tmp_path, _runner(calls))

    assert report["ok"] is True, [c for c in report["checks"] if not c["ok"]]
    assert report["asr_provider"] == "moss"
    checks = {item["key"]: item for item in report["checks"]}
    assert "resource_asr" not in checks
    assert "resource_vad" not in checks
    assert checks["resource_moss_model"]["ok"] is True
    assert calls, "MOSS runtime should be probed in its own interpreter"
    assert all("funasr" not in command[-1] for command in calls)
    assert any("mlx_audio" in command[-1] for command in calls)


def test_moss_missing_model_is_reported_against_the_selected_provider(tmp_path) -> None:
    moss_python = tmp_path / "moss-python"
    moss_python.write_text("", encoding="utf-8")
    config_path, _ = _base_config(
        tmp_path,
        asr_provider="moss",
        moss_python=str(moss_python),
        moss_model=str(tmp_path / "missing-moss-model"),
    )
    report = _report(config_path, tmp_path, _runner([]))

    assert report["ok"] is False
    failed = {item["key"] for item in report["checks"] if not item["ok"]}
    assert failed == {"resource_moss_model"}


def test_local_turbo_checks_its_own_runtime_without_funasr(tmp_path) -> None:
    config_path, _ = _base_config(tmp_path, asr_provider="local_turbo")
    calls = []
    report = _report(config_path, tmp_path, _runner(calls))

    assert report["ok"] is True, [c for c in report["checks"] if not c["ok"]]
    assert report["asr_provider"] == "local_turbo"
    checks = {item["key"] for item in report["checks"]}
    assert "resource_asr" not in checks
    assert "resource_vad" not in checks
    assert calls
    code = calls[-1][-1]
    assert "mlx_whisper" in code
    assert "faster_whisper" in code
    assert "funasr" not in code


def test_cloud_provider_does_not_require_local_asr_runtime(tmp_path) -> None:
    config_path, _ = _base_config(tmp_path, asr_provider="doubao")
    calls = []
    report = _report(config_path, tmp_path, _runner(calls))

    assert report["ok"] is True, [c for c in report["checks"] if not c["ok"]]
    assert report["asr_provider"] == "doubao"
    checks = {item["key"] for item in report["checks"]}
    assert "resource_asr" not in checks
    assert "resource_vad" not in checks
    assert all("funasr" not in command[-1] for command in calls)


def test_legacy_config_still_enforces_legacy_resources(tmp_path) -> None:
    config_path, _ = _base_config(tmp_path)
    calls = []
    report = _report(config_path, tmp_path, _runner(calls))

    assert report["ok"] is False
    failed = {item["key"] for item in report["checks"] if not item["ok"]}
    assert {"resource_asr", "resource_vad"} <= failed
    assert any("import funasr, funasr_onnx" in command[-1] for command in calls)


def test_unknown_provider_falls_back_without_legacy_false_failures(tmp_path) -> None:
    config_path, _ = _base_config(tmp_path, asr_provider="mystery-asr")
    calls = []
    report = _report(config_path, tmp_path, _runner(calls))

    assert report["ok"] is True, [c for c in report["checks"] if not c["ok"]]
    assert report["asr_provider"] == "local_turbo"
    checks = {item["key"] for item in report["checks"]}
    assert "resource_asr" not in checks
    assert "resource_vad" not in checks
