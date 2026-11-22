"""check_model_tiers.py 曾写成 ``args.dry-run``（= args.dry - run），带 --probe 就崩。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_probe_with_dry_run_does_not_crash() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_model_tiers.py"), "--probe", "--dry-run"],
        capture_output=True, text=True, timeout=60, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    assert "AttributeError" not in proc.stderr
