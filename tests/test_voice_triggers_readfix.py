"""读侧显式失败：QuickCaptureStore.list_all() 不再把「损坏」静默吞成 []。

[2026-10-30 读侧修] 3号窗独立验证发现：文件被截断后 append() 会正确拒绝覆盖，
但 list_all() 仍返回 []，页面/看板/日报于是显示「0 条」，用户以为当天没有记录。
本测试锁定：**文件不存在 → []（正常初态）；损坏/读失败 → 显式抛错**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from life_logger.voice_triggers import (  # noqa: E402
    QuickCaptureStore,
    QuickCaptureStoreError,
    VoiceTriggerCapture,
)


def _capture(ts: str = "2026-10-29T02:00:00+00:00", text: str = "示例") -> VoiceTriggerCapture:
    return VoiceTriggerCapture(
        id=f"cap_{ts}", trigger_type="todo", raw_text=text,
        extracted_content=text, metadata={}, captured_at=ts,
    )


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    store = QuickCaptureStore(tmp_path / "quick_captures.json")
    # 构造函数会建 []；删掉模拟「正常初态但文件还没创建」
    store.storage_path.unlink()
    assert store.list_all() == []


def test_truncated_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "quick_captures.json"
    store = QuickCaptureStore(path)
    path.write_text('[{"id": "a", "trigger_type": "to', encoding="utf-8")
    with pytest.raises(QuickCaptureStoreError) as exc:
        store.list_all()
    assert "损坏" in str(exc.value)
    assert path.read_text(encoding="utf-8").startswith('[{"id": "a"')


def test_empty_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "quick_captures.json"
    store = QuickCaptureStore(path)
    path.write_text("", encoding="utf-8")
    with pytest.raises(QuickCaptureStoreError):
        store.list_all()


def test_whitespace_only_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "quick_captures.json"
    store = QuickCaptureStore(path)
    path.write_text(" \n\t ", encoding="utf-8")
    with pytest.raises(QuickCaptureStoreError):
        store.list_all()


def test_top_level_not_list_raises(tmp_path: Path) -> None:
    path = tmp_path / "quick_captures.json"
    store = QuickCaptureStore(path)
    path.write_text('{"captures": []}', encoding="utf-8")
    with pytest.raises(QuickCaptureStoreError) as exc:
        store.list_all()
    assert "不是 list" in str(exc.value)


def test_valid_list_reads(tmp_path: Path) -> None:
    path = tmp_path / "quick_captures.json"
    store = QuickCaptureStore(path)
    store.append([_capture()])
    records = store.list_all()
    assert len(records) == 1 and records[0]["id"] == "cap_2026-10-29T02:00:00+00:00"


def test_filter_by_date_propagates_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "quick_captures.json"
    store = QuickCaptureStore(path)
    path.write_text("[", encoding="utf-8")
    with pytest.raises(QuickCaptureStoreError):
        store.filter_by_date("2026-10-29")


def test_append_refuses_to_overwrite_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "quick_captures.json"
    store = QuickCaptureStore(path)
    broken = b'[{"id": "keep-me"}'
    path.write_bytes(broken)
    with pytest.raises(QuickCaptureStoreError):
        store.append([_capture()])
    assert path.read_bytes() == broken, "append 绝不能覆盖损坏文件"


def test_append_refuses_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "quick_captures.json"
    store = QuickCaptureStore(path)
    path.write_bytes(b"")
    with pytest.raises(QuickCaptureStoreError):
        store.append([_capture()])
    assert path.read_bytes() == b"", "空文件也必须显式失败，不能伪装成正常写入"


def test_append_dedupes(tmp_path: Path) -> None:
    path = tmp_path / "quick_captures.json"
    store = QuickCaptureStore(path)
    store.append([_capture()])
    store.append([_capture()])          # 同 (type, content, captured_at) → 不重复
    assert len(store.list_all()) == 1


def test_filter_by_date_still_works(tmp_path: Path) -> None:
    path = tmp_path / "quick_captures.json"
    store = QuickCaptureStore(path)
    store.append([_capture("2026-10-29T02:00:00+00:00", "a")])   # 北京 10-29 10:00
    store.append([_capture("2026-10-28T02:00:00+00:00", "b")])   # 北京 10-28 10:00
    assert len(store.filter_by_date("2026-10-29")) == 1
    assert len(store.filter_by_date("2026-10-28")) == 1
    assert store.filter_by_date("2026-10-27") == []


# ── [2026-10-30] 非 dict 元素缺口（A0 上线阻断项）────────────────────
def _write(tmp_path, payload):
    import json
    p = tmp_path / "quick_captures.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return QuickCaptureStore(p)


def test_non_dict_elements_are_skipped_not_crashing(tmp_path, capsys):
    """[1,2,3]：不得抛 AttributeError；返回 [] 且告警含跳过条数。"""
    store = _write(tmp_path, [1, 2, 3])
    assert store.list_all() == []
    assert store.filter_by_date("2026-10-27") == []
    err = capsys.readouterr().err
    assert "3 条非对象元素" in err


def test_mixed_elements_keep_valid_records(tmp_path):
    """混合元素：合法条目仍可见且计数正确（2/5）。"""
    rec = {"trigger_type": "todo", "extracted_content": "x", "captured_at": "2026-10-27T01:00:00+00:00"}
    rec2 = {"trigger_type": "idea", "extracted_content": "y", "captured_at": "2026-10-27T02:00:00+00:00"}
    store = _write(tmp_path, [rec, 1, "a", None, rec2])
    assert [r["trigger_type"] for r in store.list_all()] == ["todo", "idea"]
    assert len(store.filter_by_date("2026-10-27")) == 2


def test_empty_list_is_legal_not_error(tmp_path):
    """空 list 是合法空历史，不得抛错（区别于空文件=损坏）。"""
    store = _write(tmp_path, [])
    assert store.list_all() == []
    assert store.filter_by_date("2026-10-27") == []


def test_skipped_count_distinguishable_from_original_count(tmp_path, capsys):
    """「跳过 3 条」必须可与「本来只有 2 条」区分（告警里有原始与保留计数）。"""
    store = _write(tmp_path, [{"trigger_type": "todo", "extracted_content": "a", "captured_at": "2026-10-27T01:00:00+00:00"},
                              {"trigger_type": "todo", "extracted_content": "b", "captured_at": "2026-10-27T02:00:00+00:00"},
                              7, 8, 9])
    assert len(store.list_all()) == 2
    err = capsys.readouterr().err
    assert "3 条非对象元素" in err and "保留 2 条" in err
