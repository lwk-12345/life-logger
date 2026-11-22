import subprocess
from pathlib import Path

from scripts import install_local_web_service as service
from scripts.install_local_web_service import LABEL, build_launch_agent


def test_launch_agent_keeps_local_web_service_managed(tmp_path: Path) -> None:
    payload = build_launch_agent(tmp_path)

    assert payload["Label"] == LABEL
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["ProgramArguments"] == [
        str(tmp_path.resolve() / "run-life-logger-web.command")
    ]
    assert payload["WorkingDirectory"] == str(tmp_path.resolve())
    assert "/opt/homebrew/bin" in payload["EnvironmentVariables"]["PATH"]
    assert "/usr/local/bin" in payload["EnvironmentVariables"]["PATH"]
    assert payload["StandardOutPath"].endswith(
        "output/local-web/launchd.stdout.log"
    )
    assert payload["StandardErrorPath"].endswith(
        "output/local-web/launchd.stderr.log"
    )


def test_launch_agent_bootstrap_retries_transient_failure(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_run(command: list[str], *, check: bool = True):
        calls.append(command)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(
                5,
                command,
                stderr="Bootstrap failed: 5: Input/output error",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(service, "_run", fake_run)
    monkeypatch.setattr(service.time, "sleep", sleeps.append)

    plist = tmp_path / "service.plist"
    service._bootstrap_launch_agent(
        "gui/501",
        plist,
        attempts=3,
        delay_seconds=0.01,
    )

    assert calls == [
        ["launchctl", "bootstrap", "gui/501", str(plist)],
        ["launchctl", "bootstrap", "gui/501", str(plist)],
        ["launchctl", "bootstrap", "gui/501", str(plist)],
    ]
    assert sleeps == [0.01, 0.01]
