"""prune_comparison_history.py 的安全闸门测试（合成目录树，不碰真实 output/）。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import prune_comparison_history as prune  # noqa: E402

DAY_NS = 86_400_000_000_000


def make_snapshot(ch: Path, ts: int, files: int = 1) -> Path:
    snap = ch / f"{ts:019d}"
    snap.mkdir(parents=True)
    for i in range(files):
        (snap / f"snapshot-{i}.json").write_text(json.dumps({"ts": ts, "i": i}), encoding="utf-8")
    return snap


def make_tree(root: Path, sessions: int = 2, snapshots: int = 12, files: int = 1,
              step_ns: int | None = None) -> dict[str, Path]:
    made = {}
    base = 1_700_000_000_000_000_000
    step = step_ns if step_ns is not None else DAY_NS // 4
    for s in range(sessions):
        sid = f"{s:032X}"
        ch = root / "watch-deadbeefdeadbeefdeadbeefdeadbeef" / sid / "formal-asr" / "live-context" / "comparison-history"
        ch.mkdir(parents=True)
        for k in range(snapshots):
            make_snapshot(ch, base + k * step, files)
        made[sid] = ch
    return made


def all_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_default_is_dry_run_and_deletes_nothing(tmp_path):
    make_tree(tmp_path)
    before_files, before_bytes = all_files(tmp_path), sum(p.stat().st_size for p in all_files(tmp_path))
    rc = prune.main(["--root", str(tmp_path)])
    assert rc in (0, 1)
    after_files, after_bytes = all_files(tmp_path), sum(p.stat().st_size for p in all_files(tmp_path))
    assert before_files == after_files and before_bytes == after_bytes


def test_apply_without_plan_id_exits_2_and_deletes_nothing(tmp_path):
    make_tree(tmp_path)
    before = all_files(tmp_path)
    assert prune.main(["--root", str(tmp_path), "--apply"]) == 2
    assert before == all_files(tmp_path)


def test_apply_with_wrong_plan_id_exits_2_and_deletes_nothing(tmp_path):
    make_tree(tmp_path)
    before = all_files(tmp_path)
    assert prune.main(["--root", str(tmp_path), "--apply", "--plan-id", "deadbeefdeadbeef"]) == 2
    assert before == all_files(tmp_path)


def test_apply_with_correct_plan_id_deletes_per_plan_and_keeps_first(tmp_path):
    tree = make_tree(tmp_path, sessions=2, snapshots=12, step_ns=10 * 60 * 1_000_000_000)
    plan = prune.build_plan(tmp_path, "c", 0.0)
    assert plan["deleted"] > 0
    first_by_session = {sid: min(p.name for p in ch.iterdir()) for sid, ch in tree.items()}
    rc = prune.main(["--root", str(tmp_path), "--apply", "--plan-id", plan["plan_id"]])
    assert rc == 0
    for sid, ch in tree.items():
        remaining = sorted(p.name for p in ch.iterdir() if p.is_dir())
        assert len(remaining) >= 1, "会话被删空"
        assert first_by_session[sid] in remaining, "首份被删"
        assert len(remaining) < 12
        for name in remaining:
            for f in (ch / name).glob("*.json"):
                json.loads(f.read_text(encoding="utf-8"))


def test_active_session_is_skipped_entirely(tmp_path):
    tree = make_tree(tmp_path, sessions=1, snapshots=6)
    (sid, ch) = next(iter(tree.items()))
    live = make_snapshot(ch, 1_800_000_000_000_000_000)
    os.utime(live, (time.time(), time.time()))   # 刚刚写过
    plan = prune.build_plan(tmp_path, "c", 24.0)
    assert plan["deleted"] == 0 and plan["kept"] == 0
    assert plan["skipped_active_sessions"] and plan["skipped_active_sessions"][0]["session_id"] == sid
    assert live.is_dir()


def test_non_19_digit_directories_are_ignored(tmp_path):
    ch = tmp_path / "watch-x" / ("A" * 32) / "formal-asr" / "live-context" / "comparison-history"
    ch.mkdir(parents=True)
    for k in range(4):
        make_snapshot(ch, 1_700_000_000_000_000_000 + k)
    (ch / "not-a-snapshot").mkdir()
    (ch / "123").mkdir()
    (ch / "step1").mkdir()
    plan = prune.build_plan(tmp_path, "c", 0.0)
    targets = [str(p) for p in plan["targets"]]
    assert all(Path(t).name.isdigit() and len(Path(t).name) == 19 for t in targets)
    assert not any("not-a-snapshot" in t or t.endswith("/123") or t.endswith("/step1") for t in targets)


def test_plan_id_is_stable_for_same_input(tmp_path):
    make_tree(tmp_path, sessions=2, snapshots=10)
    a = prune.build_plan(tmp_path, "c", 0.0)["plan_id"]
    b = prune.build_plan(tmp_path, "c", 0.0)["plan_id"]
    assert a == b
    assert len(a) == 16


def test_policy_variants_keep_first_and_last(tmp_path):
    make_tree(tmp_path, sessions=1, snapshots=20)
    for policy in ("c", "a", "a2", "b"):
        plan = prune.build_plan(tmp_path, policy, 0.0)
        ch = next(iter(plan["sessions"]))["path"]
        snaps = sorted(p.name for p in Path(ch).iterdir() if p.is_dir())
        keep = set(snaps) - {p.name for p in plan["targets"]}
        assert snaps[0] in keep and snaps[-1] in keep, policy


def test_cli_output_contains_required_fields(tmp_path, capsys):
    make_tree(tmp_path, sessions=1, snapshots=8)
    prune.main(["--root", str(tmp_path)])
    doc = json.loads(capsys.readouterr().out.split("\n[dry-run]")[0])
    for key in ("kept", "deleted", "bytes_kept", "bytes_deleted",
                "skipped_active_sessions", "errors", "plan_id"):
        assert key in doc
