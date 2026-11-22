import json
from types import SimpleNamespace

from life_logger.doctor import render_doctor_report, run_doctor


def _valid_config(tmp_path):
    worker = tmp_path / "worker-python"
    worker.write_text("", encoding="utf-8")
    asr = tmp_path / "asr"
    vad = tmp_path / "vad"
    asr.mkdir()
    vad.mkdir()
    template = tmp_path / "template.wav"
    template.write_bytes(b"wav")
    config_path = tmp_path / "local-config.json"
    config_path.write_text(
        json.dumps(
            {
                "python": str(worker),
                "asr": str(asr),
                "vad": str(vad),
                "template": str(template),
                "speaker": "cam++",
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_doctor_reports_environment_and_recent_jobs(tmp_path) -> None:
    config_path = _valid_config(tmp_path)
    job_root = tmp_path / "local-web"
    jobs = job_root / "jobs"
    completed = jobs / "job-20260922-completed"
    interrupted = jobs / "job-20260922-interrupted"
    completed.mkdir(parents=True)
    interrupted.mkdir(parents=True)
    (completed / "status.json").write_text(
        json.dumps(
            {
                "job_id": completed.name,
                "status": "completed",
                "current_stage": "completed",
                "updated_at": "2026-09-22T10:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (interrupted / "status.json").write_text(
        json.dumps(
            {
                "job_id": interrupted.name,
                "status": "interrupted",
                "current_stage": "interrupted",
                "updated_at": "2026-09-22T11:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    def runner(command, **_kwargs):
        assert "import funasr, funasr_onnx" in command[-1]
        return SimpleNamespace(returncode=0, stdout="worker-ok", stderr="")

    report = run_doctor(
        config_path,
        job_root=job_root,
        web_health={
            "ok": True,
            "server_build_id": "abc123",
            "server_started_at": "2026-09-22T09:00:00+00:00",
        },
        runner=runner,
        finder=lambda name: f"/usr/local/bin/{name}",
    )

    assert report["ok"] is True
    assert all(item["ok"] for item in report["checks"])
    assert any(
        item["key"] == "local_feature_modules" and item["ok"]
        for item in report["checks"]
    )
    assert report["job_stats"]["total"] == 2
    assert report["job_stats"]["status_counts"] == {
        "completed": 1,
        "interrupted": 1,
    }
    rendered = render_doctor_report(report)
    assert "✅ Worker Python" in rendered
    assert "✅ ffmpeg" in rendered
    assert interrupted.name in rendered


def test_doctor_uses_local_access_token_for_authenticated_health(tmp_path) -> None:
    config_path = _valid_config(tmp_path)
    job_root = tmp_path / "local-web"
    job_root.mkdir()
    (job_root / "secrets.json").write_text(
        json.dumps({"mobile_access_token": "doctor-token"}),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "server_build_id": "build-doctor",
                    "server_started_at": "2026-09-22T12:00:00+00:00",
                }
            ).encode("utf-8")

    def opener(request, **kwargs):
        seen["authorization"] = request.get_header("Authorization")
        seen["timeout"] = kwargs.get("timeout")
        return Response()

    def runner(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="worker-ok", stderr="")

    report = run_doctor(
        config_path,
        job_root=job_root,
        runner=runner,
        finder=lambda name: f"/usr/local/bin/{name}",
        opener=opener,
    )

    assert report["ok"] is True
    assert seen["authorization"] == "Bearer doctor-token"
    assert seen["timeout"] == 2.0


def test_doctor_returns_actionable_failures(tmp_path) -> None:
    config_path = tmp_path / "local-config.json"
    config_path.write_text(
        json.dumps(
            {
                "python": str(tmp_path / "missing-python"),
                "asr": str(tmp_path / "missing-asr"),
                "vad": str(tmp_path / "missing-vad"),
                "template": str(tmp_path / "missing-template.wav"),
                "speaker": "",
            }
        ),
        encoding="utf-8",
    )

    def runner(_command, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="missing funasr_onnx")

    report = run_doctor(
        config_path,
        job_root=tmp_path / "local-web",
        web_health={"ok": False},
        runner=runner,
        finder=lambda _name: None,
    )

    assert report["ok"] is False
    failed = [item for item in report["checks"] if not item["ok"]]
    assert failed
    assert all(item["fix"] for item in failed)
    rendered = render_doctor_report(report)
    assert "❌ Worker Python" in rendered
    assert "修复:" in rendered
