"""Watch 正式链路：可选焦点窗口产物 → 按行绝对时间附 input_context（旧路等价）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.watch_formal_asr import (  # noqa: E402
    INPUT_CONTEXT_ARTIFACT,
    attach_watch_input_context,
    load_input_context_windows,
)


def _windows_file(root: Path, windows: list[dict]) -> Path:
    path = root / INPUT_CONTEXT_ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "mac-input-context-v1", "windows": windows}), encoding="utf-8")
    return path


def _row(start="2026-10-25T18:05:00+08:00", end="2026-10-25T18:05:10+08:00"):
    return {"source_id": "src_1", "text": "第一组四十公斤", "source_label": "self",
            "recording_id": "watch_X", "segment_id": "000036",
            "absolute_started_at": start, "absolute_ended_at": end}


GOOD = {"started_at": "2026-10-25T18:00:00+08:00", "ended_at": "2026-10-25T18:10:00+08:00",
        "target_app": "com.openai.codex", "target_unknown": False, "complete": True,
        "front_bundles": ["com.openai.codex"], "provider": "mock"}


def test_artifact_path_is_bounded_and_explicit():
    assert INPUT_CONTEXT_ARTIFACT == "input-context/latest.json"


def test_missing_artifact_is_old_path_equivalent(tmp_path):
    assert load_input_context_windows(tmp_path) == []
    row = _row()
    stats = attach_watch_input_context([row], load_input_context_windows(tmp_path))
    assert stats == {"resolved": 0, "unknown": 0, "windows": 0}
    assert "input_context" not in row, "缺文件时行为必须与旧版完全一致"


def test_row_gets_context_by_absolute_time(tmp_path):
    _windows_file(tmp_path, [GOOD])
    row = _row()
    stats = attach_watch_input_context([row], load_input_context_windows(tmp_path))
    assert stats["resolved"] == 1
    ctx = row["input_context"]
    assert ctx["target_bundle"] == "com.openai.codex"
    assert ctx["start"] == "2026-10-25T18:00:00+08:00"      # 保留原始时区
    assert ctx["meaning"] == "input_target_only_not_delivery"
    assert row["source_label"] == "self"                     # 不改身份


def test_non_overlapping_row_is_not_attached(tmp_path):
    _windows_file(tmp_path, [GOOD])
    row = _row(start="2026-10-25T20:00:00+08:00", end="2026-10-25T20:00:10+08:00")
    stats = attach_watch_input_context([row], load_input_context_windows(tmp_path))
    assert stats["resolved"] == 0 and "input_context" not in row


def test_anomaly_window_marks_unknown_reason_but_keeps_row(tmp_path):
    _windows_file(tmp_path, [dict(GOOD, target_app=None, target_unknown=True)])
    row = _row()
    stats = attach_watch_input_context([row], load_input_context_windows(tmp_path))
    assert stats["unknown"] == 1 and stats["resolved"] == 0
    assert row["input_context_unknown_reason"] == "target_unknown"
    assert "input_context" not in row
    assert row["text"] == "第一组四十公斤" and row["source_label"] == "self"


def test_other_files_in_root_are_ignored(tmp_path):
    """bounded path：别的 json 不得被读取。"""
    (tmp_path / "windows.json").write_text(json.dumps({"windows": [GOOD]}), encoding="utf-8")
    assert load_input_context_windows(tmp_path) == []


def test_corrupt_or_oversized_artifact_returns_empty(tmp_path):
    path = tmp_path / INPUT_CONTEXT_ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_input_context_windows(tmp_path) == []


def test_watch_rows_are_passed_to_formal_extraction_with_context(tmp_path, monkeypatch):
    """接线点检查：rows 进入 run_contextual_extraction 前已带 input_context。"""
    import life_logger.contextual_extraction as ce
    import life_logger.watch_formal_asr as mod

    captured = {}

    def fake_extract(rows, **kwargs):
        captured["rows"] = rows
        return {"status": "completed", "formal_events": [], "pending_events": []}

    monkeypatch.setattr(ce, "run_contextual_extraction", fake_extract)
    _windows_file(tmp_path, [GOOD])
    rows = [_row()]
    mod.attach_watch_input_context(rows, mod.load_input_context_windows(tmp_path))
    ce.run_contextual_extraction(rows, deepseek_api_key="offline")
    assert captured["rows"][0]["input_context"]["target_bundle"] == "com.openai.codex"
