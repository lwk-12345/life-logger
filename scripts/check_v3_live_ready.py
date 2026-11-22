#!/usr/bin/env python3
"""Refuse to start v3 live unless the confidence gate is explicitly configured.

Read-only: never changes config, never calls a model, never publishes anything.
Exit codes: 0 = selected mode is safe to start; 2 = v3 live requested but gate
missing/invalid; 1 = config unreadable.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from life_logger.activity_prompt_v3 import check_live_configuration, settings

    path = Path(argv[1]) if len(argv) > 1 else ROOT / "local-config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}, ensure_ascii=False))
        return 1
    try:
        state = check_live_configuration(config)
    except ValueError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    options = settings(config)
    print(json.dumps({"ok": True, **state, "version": options["version"], "mode": options["mode"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
