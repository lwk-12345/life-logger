"""compress_comparison_history.py：合成目录树，不碰真实 output/。"""
from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compress_comparison_history as cch  # noqa: E402


def _tree(root: Path) -> tuple[Path, Path, Path]:
    history = root / "watch-x" / "s1" / "formal-asr" / "live-context" / "comparison-history"
    old = history / "1000" / "clef" / "decisions.json"
    new = history / "2000" / "clef" / "decisions.json"
    live = root / "watch-x" / "s1" / "formal-asr" / "live-context" / "context-extraction.json"
    for path, body in ((old, {"v": "old"}), (new, {"v": "new"}), (live, {"v": "live"})):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body), encoding="utf-8")
    return old, new, live


def test_dry_run_changes_nothing(tmp_path):
    old, new, live = _tree(tmp_path)
    assert cch.main(["--root", str(tmp_path)]) == 0
    assert old.is_file() and new.is_file() and live.is_file()
    assert not old.with_name(old.name + ".gz").exists()


def test_apply_compresses_old_only_and_content_survives(tmp_path):
    old, new, live = _tree(tmp_path)
    mtime = 1_700_000_000
    os.utime(old, (mtime, mtime))
    assert cch.main(["--root", str(tmp_path), "--apply"]) == 0
    packed = old.with_name(old.name + ".gz")
    assert not old.exists()
    with gzip.open(packed, "rt", encoding="utf-8") as stream:
        assert json.load(stream) == {"v": "old"}
    assert int(packed.stat().st_mtime) == mtime
    assert new.is_file(), "最新一份不压"
    assert live.is_file(), "history 外的文件不压"
    assert list(tmp_path.rglob("*.tmp")) == []


def test_links_are_skipped(tmp_path):
    old, _, live = _tree(tmp_path)
    sibling = old.with_name("hard.json")
    os.link(old, sibling)
    symlink = old.with_name("soft.json")
    symlink.symlink_to(live)
    assert cch.discover(tmp_path) == []


def test_failed_verification_keeps_original(tmp_path, monkeypatch):
    old, _, _ = _tree(tmp_path)
    calls = iter(["a", "b"])
    monkeypatch.setattr(cch, "_sha256", lambda stream: next(calls))
    assert cch.main(["--root", str(tmp_path), "--apply"]) == 2
    assert old.is_file()
    assert not old.with_name(old.name + ".gz").exists()
    assert list(tmp_path.rglob("*.tmp")) == []


def test_low_disk_stops_before_touching_files(tmp_path):
    old, _, _ = _tree(tmp_path)
    assert cch.main(["--root", str(tmp_path), "--apply", "--min-free-bytes", str(10**18)]) == 3
    assert old.is_file()
