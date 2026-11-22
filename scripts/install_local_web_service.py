#!/usr/bin/env python3
"""Install or remove the Life Logger local web server as a macOS LaunchAgent."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import time
from pathlib import Path
from typing import Any

LABEL = "com.life-logger.local-web"
DEFAULT_PORT = 8765


def build_launch_agent(repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    log_dir = repo / "output" / "local-web"
    return {
        "Label": LABEL,
        "ProgramArguments": [str(repo / "run-life-logger-web.command")],
        "WorkingDirectory": str(repo),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "ProcessType": "Interactive",
        "EnvironmentVariables": {
            "PATH": (
                "/opt/homebrew/bin:/opt/homebrew/sbin:"
                "/usr/local/bin:/usr/local/sbin:"
                "/usr/bin:/bin:/usr/sbin:/sbin"
            ),
        },
        "StandardOutPath": str(log_dir / "launchd.stdout.log"),
        "StandardErrorPath": str(log_dir / "launchd.stderr.log"),
    }


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
    )

def _bootstrap_launch_agent(
    domain: str,
    plist_path: Path,
    *,
    attempts: int = 5,
    delay_seconds: float = 0.4,
) -> None:
    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(max(1, attempts)):
        try:
            _run(["launchctl", "bootstrap", domain, str(plist_path)])
            return
        except subprocess.CalledProcessError as error:
            last_error = error
            if attempt + 1 >= max(1, attempts):
                break
            time.sleep(delay_seconds)
    if last_error is not None:
        raise last_error



def install(repo: Path) -> Path:
    if os.uname().sysname != "Darwin":
        raise RuntimeError("LaunchAgent installation is supported only on macOS")

    repo = repo.resolve()
    command = repo / "run-life-logger-web.command"
    if not command.is_file():
        raise RuntimeError(f"missing launcher: {command}")

    log_dir = repo / "output" / "local-web"
    log_dir.mkdir(parents=True, exist_ok=True)

    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plist_path = agents_dir / f"{LABEL}.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(build_launch_agent(repo), handle, sort_keys=True)

    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    _run(["launchctl", "bootout", service], check=False)
    _bootstrap_launch_agent(domain, plist_path)
    _run(["launchctl", "enable", service], check=False)
    _run(["launchctl", "kickstart", "-k", service])
    return plist_path


def uninstall() -> Path:
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = agents_dir / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    _run(["launchctl", "bootout", service], check=False)
    plist_path.unlink(missing_ok=True)
    return plist_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    try:
        if args.uninstall:
            path = uninstall()
            print(f"Life Logger LaunchAgent removed: {path}")
        else:
            path = install(repo)
            print(f"Life Logger LaunchAgent installed: {path}")
            print(f"Open http://127.0.0.1:{DEFAULT_PORT}/")
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        detail = ""
        if isinstance(error, subprocess.CalledProcessError):
            detail = (error.stderr or error.stdout or "").strip()
        raise SystemExit(f"{error}{': ' + detail if detail else ''}") from error


if __name__ == "__main__":
    main()
