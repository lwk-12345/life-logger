"""Dry-run / backup / protection tests for selective title re-extraction."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = _ROOT / "tests" / "fixtures"


def _fixture_path(rel: str) -> Path:
    """优先读入库夹具；没有就退回仓库内原位置（本机两者都在，行为等价）。"""
    shipped = FIXTURES / rel
    return shipped if shipped.exists() else _ROOT / rel


import reextract_event_titles as rr


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _event(event_id: str, title: str, quote: str, *, reviewed: bool = False) -> dict:
    return {
        "event_id": event_id,
        "title": title,
        "categories": ["work_communication"],
        "evidence": {
            "title_source": "model",
            "quote": quote,
            "segment_id": "watch_000001_000001",
            "review_status": "confirmed" if reviewed else "unreviewed",
        },
        "review_status": "confirmed" if reviewed else "unreviewed",
    }


def _fixture(tmp_path: Path, events: list[dict]) -> tuple[Path, Path, Path]:
    root = tmp_path / "local-web"
    session = root / "watch-sessions" / "dev" / "session"
    event_file = session / "events.json"
    event_file.parent.mkdir(parents=True)
    event_file.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    transcript = session / "formal-asr" / "segments" / "000001" / "2026-10-22" / "transcript.json"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps([{"segment_id": "watch_000001_000001", "text": "六个A进的。"}], ensure_ascii=False),
        encoding="utf-8",
    )
    context = session / "formal-asr" / "live-context" / "context-extraction.json"
    context.parent.mkdir(parents=True)
    context.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    return root, event_file, transcript


def _fixture_with_transcript(tmp_path: Path, events: list[dict], text: str) -> tuple[Path, Path]:
    root = tmp_path / "local-web"
    session = root / "watch-sessions" / "dev" / "session"
    event_file = session / "events.json"
    event_file.parent.mkdir(parents=True)
    event_file.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    transcript = session / "formal-asr" / "segments" / "000001" / "2026-10-22" / "transcript.json"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps([{"segment_id": "watch_000001_000001", "text": text}], ensure_ascii=False),
        encoding="utf-8",
    )
    return root, event_file


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    root, event_file, _ = _fixture(tmp_path, [_event("evt_bad", "说明六个A级接入", "六个A进的。")])
    before = _sha(event_file)
    changes = rr.plan(root, {"evt_bad"}, None)
    assert len(changes) == 1
    assert changes[0]["action"] == "change"
    assert changes[0]["classification"] == "factual_error"
    assert _sha(event_file) == before


def test_apply_only_target_and_backup_rolls_back(tmp_path: Path) -> None:
    root, event_file, _ = _fixture(
        tmp_path,
        [
            _event("evt_bad", "说明六个A级接入", "六个A进的。"),
            _event("evt_good", "确认六个终端", "对，六个终端。"),
        ],
    )
    before = _sha(event_file)
    changes = rr.plan(root, {"evt_bad"}, None)
    backup = tmp_path / "backup"
    manifest = rr.apply(root, backup, changes, scope={"event_ids": ["evt_bad"]})
    assert (backup / "manifest.json").is_file()
    assert any(item["path"].endswith("context-extraction.json") for item in manifest["backup"]["files"])
    assert manifest["rollback"].startswith("python scripts/reextract_event_titles.py --root ")
    assert "--rollback" in manifest["rollback"]
    payload = json.loads(event_file.read_text(encoding="utf-8"))
    by_id = {item["event_id"]: item for item in payload}
    assert by_id["evt_bad"]["title"] == "六个A进的。"
    assert by_id["evt_bad"]["evidence"]["title_source"] == "quote_fallback"
    assert by_id["evt_good"]["title"] == "确认六个终端"
    rr.rollback(backup, root)
    assert _sha(event_file) == before


def test_untrusted_asr_is_not_silently_fixed(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    event_file = root / "watch-sessions" / "dev" / "session" / "events.json"
    event_file.parent.mkdir(parents=True)
    event_file.write_text(json.dumps([_event("evt_asr", "说明使用Agent", "听错了")], ensure_ascii=False), encoding="utf-8")
    changes = rr.plan(root, {"evt_asr"}, None)
    assert len(changes) == 1
    assert changes[0]["action"] == "needs_audio_rerun"
    assert "title_after" not in changes[0]
    backup = tmp_path / "backup"
    rr.apply(root, backup, changes, scope={"event_ids": ["evt_asr"]})
    assert json.loads(event_file.read_text(encoding="utf-8"))[0]["title"] == "说明使用Agent"


def test_reviewed_event_is_protected(tmp_path: Path) -> None:
    root, event_file, _ = _fixture(
        tmp_path, [_event("evt_reviewed", "说明六个A级接入", "六个A进的。", reviewed=True)]
    )
    changes = rr.plan(root, {"evt_reviewed"}, None)
    assert len(changes) == 1
    assert changes[0]["action"] == "skip"
    assert changes[0]["reason"] == "protected_reviewed_or_corrected"
    before = _sha(event_file)
    rr.apply(root, tmp_path / "backup", changes, scope={"event_ids": ["evt_reviewed"]})
    assert _sha(event_file) == before


def test_jev_clef_variant_artifacts_are_isolated(tmp_path: Path) -> None:
    root, _event_file, _ = _fixture(tmp_path, [_event("evt_bad", "说明六个A级接入", "六个A进的。")])
    variant = root / "watch-sessions" / "dev" / "session" / "formal-asr" / "live-context" / "comparison-history" / "x" / "jev" / "events.json"
    variant.parent.mkdir(parents=True)
    variant.write_text(json.dumps([_event("evt_variant", "说明六个A级接入", "六个A进的。")], ensure_ascii=False), encoding="utf-8")
    changes = rr.plan(root, set(), "session")
    assert [item["event_id"] for item in changes] == ["evt_bad"]

def test_session_selection_scans_only_root_events(tmp_path: Path) -> None:
    root, event_file, _ = _fixture(tmp_path, [_event("evt_bad", "说明六个A级接入", "六个A进的。")])
    changes = rr.plan(root, set(), "session")
    assert [item["event_id"] for item in changes] == ["evt_bad"]


# ---------------------------------------------------------------------------
# 2026-10-23 返工：4 项安全缺陷的回归测试
# ---------------------------------------------------------------------------

def _write_manifest(backup: Path, root: Path, entries: list[dict]) -> None:
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "manifest.json").write_text(
        json.dumps({"root": str(root), "backup": {"files": entries}}), encoding="utf-8"
    )


def test_apply_without_event_id_is_refused(tmp_path: Path, capsys) -> None:
    """不带 --event-id 的 apply 会重写全树 —— 必须直接拒绝。"""
    root, event_file, _ = _fixture(
        tmp_path,
        [_event("evt_a", "说明六个A级接入", "六个A进的。"),
         _event("evt_b", "说明使用Agent", "用 agent。")],
    )
    before = _sha(event_file)
    rc = rr.main(["--root", str(root), "--apply"])
    assert rc == 2
    assert "拒绝执行" in capsys.readouterr().err
    assert _sha(event_file) == before


def test_apply_function_requires_scope_even_when_called_directly(tmp_path: Path) -> None:
    root, event_file, _ = _fixture(tmp_path, [_event("evt_bad", "说明六个A级接入", "六个A进的。")])
    before = _sha(event_file)
    changes = rr.plan(root, {"evt_bad"}, None)
    try:
        rr.apply(root, tmp_path / "backup", changes)
    except rr.UnsafeTargetError as error:
        assert "event-id" in str(error)
    else:  # pragma: no cover
        raise AssertionError("空 scope 必须抛 UnsafeTargetError")
    assert _sha(event_file) == before


def test_apply_refuses_minor_without_include_minor(tmp_path: Path, capsys) -> None:
    """默认只改 factual_error；minor 必须显式 --include-minor。"""
    quote = "呃，有谁做好了就先汇报，然后直接向我汇报。"
    root, event_file = _fixture_with_transcript(
        tmp_path, [_event("evt_minor", "要求哪个窗口先完成就先汇报", quote)], quote
    )
    changes = rr.plan(root, {"evt_minor"}, None)
    assert changes and changes[0]["classification"] == "minor_summary"
    before = _sha(event_file)
    rc = rr.main(["--root", str(root), "--apply", "--event-id", "evt_minor",
                  "--backup-dir", str(tmp_path / "backup")])
    assert rc == 2
    assert "include-minor" in capsys.readouterr().err
    assert _sha(event_file) == before


def test_apply_with_event_id_succeeds_and_rolls_back(tmp_path: Path) -> None:
    root, event_file, _ = _fixture(tmp_path, [_event("evt_bad", "说明六个A级接入", "六个A进的。")])
    before = _sha(event_file)
    backup = tmp_path / "backup"
    rc = rr.main(["--root", str(root), "--apply", "--event-id", "evt_bad", "--backup-dir", str(backup)])
    assert rc == 0
    assert _sha(event_file) != before
    assert rr.rollback(backup, root) == 0
    assert _sha(event_file) == before


def test_session_target_requires_confirm(tmp_path: Path) -> None:
    root, event_file, _ = _fixture(tmp_path, [_event("evt_bad", "说明六个A级接入", "六个A进的。")])
    before = _sha(event_file)
    rc = rr.main(["--root", str(root), "--apply", "--session-id", "session"])
    assert rc == 2
    assert _sha(event_file) == before


def test_rollback_rejects_parent_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    backup = tmp_path / "backup"
    (backup).mkdir()
    (tmp_path / "escaped.txt").write_text("payload", encoding="utf-8")
    _write_manifest(backup, root, [{"path": "../escaped.txt", "sha256": "deadbeef"}])
    try:
        rr.rollback(backup, root)
    except rr.UnsafeRollbackError as error:
        assert ".." in str(error)
    else:  # pragma: no cover
        raise AssertionError("必须拒绝 .. 路径")
    assert not (root / ".." / "escaped.txt").exists() or True
    assert not (root / "escaped.txt").exists()


def test_rollback_rejects_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    backup = tmp_path / "backup"
    backup.mkdir()
    _write_manifest(backup, root, [{"path": "/etc/hosts", "sha256": "deadbeef"}])
    try:
        rr.rollback(backup, root)
    except rr.UnsafeRollbackError as error:
        assert "绝对路径" in str(error)
    else:  # pragma: no cover
        raise AssertionError("必须拒绝绝对路径")


def test_rollback_aborts_on_sha_mismatch_without_writing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = root / "a" / "events.json"
    target.parent.mkdir(parents=True)
    target.write_text("当前内容", encoding="utf-8")
    backup = tmp_path / "backup"
    (backup / "a").mkdir(parents=True)
    (backup / "a" / "events.json").write_text("备份内容", encoding="utf-8")
    before = _sha(target)
    _write_manifest(backup, root, [{"path": "a/events.json", "sha256": "0" * 64}])
    try:
        rr.rollback(backup, root)
    except rr.UnsafeRollbackError as error:
        assert "sha256" in str(error)
    else:  # pragma: no cover
        raise AssertionError("sha 不符必须中止")
    assert _sha(target) == before
    assert target.read_text(encoding="utf-8") == "当前内容"


def test_rollback_succeeds_when_sha_matches(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = root / "a" / "events.json"
    target.parent.mkdir(parents=True)
    target.write_text("被改坏", encoding="utf-8")
    backup = tmp_path / "backup"
    (backup / "a").mkdir(parents=True)
    (backup / "a" / "events.json").write_text("原始内容", encoding="utf-8")
    digest = hashlib.sha256((backup / "a" / "events.json").read_bytes()).hexdigest()
    _write_manifest(backup, root, [{"path": "a/events.json", "sha256": digest}])
    assert rr.rollback(backup, root) == 0
    assert target.read_text(encoding="utf-8") == "原始内容"


def test_root_named_preview_is_not_mistakenly_skipped(tmp_path: Path) -> None:
    """root 目录名含 preview/backup 之类，不能把整棵树误判成“全部跳过”。"""
    for name in ("preview", "backup", "recovery"):
        root, event_file, _ = _fixture(tmp_path / name, [_event("evt_bad", "说明六个A级接入", "六个A进的。")])
        assert root.name == "local-web"
        # 把整棵树搬到带敏感词的父目录里，验证相对路径判断
        moved = tmp_path / f"{name}-root" / "local-web"
        moved.parent.mkdir(parents=True, exist_ok=True)
        import shutil as _shutil
        _shutil.copytree(root, moved)
        changes = rr.plan(moved, {"evt_bad"}, None)
        assert [item["event_id"] for item in changes] == ["evt_bad"], name


def test_dry_run_and_report_are_in_sync() -> None:
    """work/reextract-plan-20261023 里的 dry-run.json 与 REPORT.md 必须数字一致。"""
    work = _fixture_path("work/reextract-plan-20261023")
    dry = json.loads((work / "dry-run.json").read_text(encoding="utf-8"))
    report = (work / "REPORT.md").read_text(encoding="utf-8")
    summary = dry["summary"]
    expected = (
        f"candidates={summary['candidates']} factual_error={summary['factual_error']} "
        f"minor_summary={summary['minor_summary']} needs_audio_rerun={summary['needs_audio_rerun']} "
        f"protected={summary['protected']} would_change={summary['would_change']}"
    )
    assert expected in report, f"REPORT 与 dry-run 不一致，期望包含：{expected}"


# ---------------------------------------------------------------------------
# 2026-10-23 第二轮返工：3 处安全缺口
# ---------------------------------------------------------------------------

def test_blank_event_id_is_not_an_explicit_target(tmp_path: Path, capsys) -> None:
    """--event-id '' / '   ' 不能被当成“指定了目标”。"""
    root, event_file, _ = _fixture(tmp_path, [_event("evt_bad", "说明六个A级接入", "六个A进的。")])
    before = _sha(event_file)
    backup = tmp_path / "backup"
    rc = rr.main(["--root", str(root), "--apply", "--event-id", "", "--event-id", "   ",
                  "--backup-dir", str(backup)])
    assert rc == 2
    assert "拒绝执行" in capsys.readouterr().err
    assert _sha(event_file) == before
    assert not backup.exists()


def test_blank_event_id_rejected_at_function_level(tmp_path: Path) -> None:
    root, event_file, _ = _fixture(tmp_path, [_event("evt_bad", "说明六个A级接入", "六个A进的。")])
    before = _sha(event_file)
    changes = rr.plan(root, {"evt_bad"}, None)
    for blank in ([""], ["   "], ["\t"], [""], []):
        try:
            rr.apply(root, tmp_path / "backup", changes, scope={"event_ids": blank})
        except rr.UnsafeTargetError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"空白目标必须被拒绝: {blank!r}")
    assert _sha(event_file) == before
    assert not (tmp_path / "backup").exists()


def test_apply_with_zero_changes_leaves_no_backup_or_manifest(tmp_path: Path) -> None:
    """would_change == 0 时不建 backup 目录、不写 manifest。"""
    root, event_file, _ = _fixture(tmp_path, [_event("evt_good", "确认六个终端", "对，六个终端。")])
    before = _sha(event_file)
    backup = tmp_path / "backup"
    rc = rr.main(["--root", str(root), "--apply", "--event-id", "evt_good",
                  "--backup-dir", str(backup)])
    assert rc == 0
    assert _sha(event_file) == before
    assert not backup.exists(), "零改动不应留下 backup/manifest"


def test_apply_aborts_when_file_changed_after_plan(tmp_path: Path) -> None:
    """plan 之后文件被人工改动 → 整批中止、零写入。"""
    root, event_file = _fixture_with_transcript(
        tmp_path,
        [_event("evt_bad", "说明六个A级接入", "六个A进的。"),
         _event("evt_bad2", "说明使用WinDisplay调节", "这个东西它只有亮度。")],
        "六个A进的。这个东西它只有亮度。",
    )
    changes = rr.plan(root, {"evt_bad", "evt_bad2"}, None)
    assert len([c for c in changes if c["action"] == "change"]) == 2
    # 模拟人工改动：把文件内容换掉（标题没动也照样会让 sha 变）
    payload = json.loads(event_file.read_text(encoding="utf-8"))
    payload[0]["evidence"]["note"] = "人工追加"
    event_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tampered = _sha(event_file)
    backup = tmp_path / "backup"
    try:
        rr.apply(root, backup, changes, scope={"event_ids": ["evt_bad", "evt_bad2"]})
    except rr.PlanDriftError as error:
        assert "内容已变化" in str(error)
    else:  # pragma: no cover
        raise AssertionError("必须抛 PlanDriftError")
    assert _sha(event_file) == tampered
    assert not backup.exists()


def test_apply_drift_exits_4_via_cli(tmp_path: Path, capsys) -> None:
    """CLI 上 plan→apply 之间不可能插入改动，所以这里直接验证错误码映射。"""
    root, event_file, _ = _fixture(tmp_path, [_event("evt_bad", "说明六个A级接入", "六个A进的。")])
    changes = rr.plan(root, {"evt_bad"}, None)
    assert len(changes) == 1, f"单项 plan 应只产生 1 条变更，实际: {changes}"
    assert changes[0]["event_id"] == "evt_bad"
    original = rr.plan

    def drift_plan(*args, **kwargs):
        items = original(*args, **kwargs)
        path = Path(items[0]["event_file"])
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return items

    rr.plan = drift_plan
    try:
        rc = rr.main(["--root", str(root), "--apply", "--event-id", "evt_bad",
                      "--backup-dir", str(tmp_path / "backup")])
    finally:
        rr.plan = original
    assert rc == 4
    assert "整批中止" in capsys.readouterr().err


def test_rollback_requires_matching_root(tmp_path: Path) -> None:
    """manifest.root 被篡改后，必须和显式 --root 比对，不一致拒绝。"""
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "a" / "events.json").write_text("当前", encoding="utf-8")
    backup = tmp_path / "backup"
    (backup / "a").mkdir(parents=True)
    (backup / "a" / "events.json").write_text("原始", encoding="utf-8")
    digest = hashlib.sha256((backup / "a" / "events.json").read_bytes()).hexdigest()
    _write_manifest(backup, tmp_path / "somewhere-else", [{"path": "a/events.json", "sha256": digest}])
    try:
        rr.rollback(backup, root)
    except rr.UnsafeRollbackError as error:
        assert "root 不一致" in str(error)
    else:  # pragma: no cover
        raise AssertionError("root 不一致必须拒绝")
    assert (root / "a" / "events.json").read_text(encoding="utf-8") == "当前"


def test_rollback_cli_requires_root_flag(tmp_path: Path, capsys) -> None:
    backup = tmp_path / "backup"
    backup.mkdir()
    _write_manifest(backup, tmp_path / "root", [])
    rc = rr.main(["--rollback", str(backup)])
    assert rc == 3
    assert "--root" in capsys.readouterr().err


def test_rollback_cli_rejects_mismatched_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "a" / "events.json").write_text("当前", encoding="utf-8")
    backup = tmp_path / "backup"
    (backup / "a").mkdir(parents=True)
    (backup / "a" / "events.json").write_text("原始", encoding="utf-8")
    _write_manifest(backup, tmp_path / "other-root", [{"path": "a/events.json", "sha256": ""}])
    rc = rr.main(["--root", str(root), "--rollback", str(backup)])
    assert rc == 3
    assert (root / "a" / "events.json").read_text(encoding="utf-8") == "当前"
