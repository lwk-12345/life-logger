import json
import sys
from pathlib import Path

from life_logger import cli
from life_logger.pipeline import PipelineResult


def test_no_arguments_use_product_pipeline(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_load_config(path: Path):
        calls["config_path"] = path
        return {
            "python": "/python",
            "asr": "/asr",
            "vad": "/vad",
            "template": "/template.wav",
            "speaker": "cam++",
        }

    def fake_product_pipeline(input_path: Path, output_dir: Path, **kwargs):
        calls["input_path"] = input_path
        calls["output_dir"] = output_dir
        calls.update(kwargs)
        return []

    monkeypatch.setattr(sys, "argv", ["life-logger"])
    monkeypatch.setattr(cli, "load_local_config", fake_load_config)
    monkeypatch.setattr(cli, "run_local_product_pipeline", fake_product_pipeline)

    assert cli.main() == 0
    assert calls["config_path"] == Path("local-config.json")
    assert calls["input_path"] == Path("input")
    assert calls["output_dir"] == Path("output")
    assert calls["timezone_name"] == "Asia/Shanghai"
    assert calls["user_start"] is None
    assert not {"use_jev", "jev_min_confidence", "decision_provider"} & calls.keys()
    assert calls["config"]["cpu_threads"] == 2




def test_legacy_process_still_uses_legacy_pipeline(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_run_process(input_path: Path, output_dir: Path, **kwargs):
        calls["input_path"] = input_path
        calls["output_dir"] = output_dir
        calls.update(kwargs)
        return PipelineResult(
            events_path=Path("output/events.json"),
            markdown_path=Path("output/daily.md"),
            database_path=Path("output/life_logger.db"),
            event_count=1,
            warnings=[],
        )

    monkeypatch.setattr(
        sys,
        "argv",
        ["life-logger", "process", "input", "--engine", "legacy"],
    )
    monkeypatch.setattr(cli, "run_process", fake_run_process)

    assert cli.main() == 0
    assert {
        key: value
        for key, value in calls.items()
        if key not in {"progress_callback", "chunk_seconds", "cpu_threads"}
    } == {
        "input_path": Path("input"),
        "output_dir": Path("output"),
        "timezone_name": "Asia/Shanghai",
        "transcriber_name": "local",
        "user_start": None,
    }
    assert calls["chunk_seconds"] == 300.0
    assert calls["cpu_threads"] == 2
    assert callable(calls["progress_callback"])


def test_local_web_startup_preflight_checks_p4_modules(monkeypatch) -> None:
    from scripts import run_local_web

    imported: list[str] = []

    def fake_import(name: str):
        imported.append(name)
        return object()

    monkeypatch.setattr(run_local_web.importlib, "import_module", fake_import)
    run_local_web._preflight_local_features()

    assert imported == list(run_local_web.LOCAL_FEATURE_MODULES)
    assert "life_logger.local_reminders" in imported


def test_doctor_command_runs_local_self_check(monkeypatch, capsys) -> None:
    calls: dict[str, object] = {}

    def fake_doctor(config_path: Path, **kwargs):
        calls["config_path"] = config_path
        calls.update(kwargs)
        return {
            "ok": True,
            "checks": [
                {"label": "Worker Python", "ok": True, "detail": "ok", "fix": None}
            ],
            "job_stats": {"recent": []},
        }

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "life-logger",
            "doctor",
            "--config",
            "custom-config.json",
            "--job-root",
            "custom-jobs",
        ],
    )
    monkeypatch.setattr(cli, "run_doctor", fake_doctor)

    assert cli.main() == 0
    assert calls["config_path"] == Path("custom-config.json")
    assert calls["job_root"] == Path("custom-jobs")
    assert "✅ Worker Python" in capsys.readouterr().out


def test_brief_command_generates_and_delivers(monkeypatch, capsys, tmp_path) -> None:
    calls: dict[str, object] = {}
    brief_payload = {
        "schema_version": "agent-brief-v1",
        "date": "2026-09-21",
        "event_count": 2,
        "high_confidence_points": [],
    }

    def fake_build(job_root: Path, *, date: str):
        calls["job_root"] = job_root
        calls["date"] = date
        return brief_payload

    def fake_deliver(brief, **kwargs):
        calls["brief"] = brief
        calls.update(kwargs)
        return {"outbox_path": "/tmp/brief.json", "webhook": None}

    monkeypatch.setattr(cli, "build_daily_brief", fake_build)
    monkeypatch.setattr(cli, "deliver_daily_brief", fake_deliver)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "life-logger",
            "brief",
            "--date",
            "2026-09-21",
            "--job-root",
            str(tmp_path / "jobs"),
            "--outbox",
            str(tmp_path / "outbox"),
        ],
    )

    assert cli.main() == 0
    assert calls["job_root"] == tmp_path / "jobs"
    assert calls["date"] == "2026-09-21"
    assert calls["outbox_dir"] == tmp_path / "outbox"
    assert calls["brief"] == brief_payload
    output = json.loads(capsys.readouterr().out)
    assert output["brief"]["event_count"] == 2
