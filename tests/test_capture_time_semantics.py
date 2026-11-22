"""时间语义：区分「发生时间」与「记录指令的捕获时间」（2026-10-24）。

契约
----
* `precision != "unknown"` 且 `time.absolute` 存在 → 正常发生时刻（含 exact/minue/inferred）。
* `precision == "unknown"` 且 `time.absolute ≈ evidence.reported_at`（≤2s）
  → 这个 absolute 只是**捕获/讲述时刻**，不是发生时刻：
    - 原话是「记录一下…」这类**记录指令** → 按近似发生时间展示：
      `event_time_basis="capture_statement"`、`time.precision="approximate"`、
      `display_time.label="约HH:MM（按讲述时间）"`；
    - 普通事件 → 回落到 `reported_local`，`occurred_at=None`，捕获时刻存入 `time.capture_time`。
* 绝不允许"precision=unknown 但 absolute 存在 ⇒ 已定位"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.event_view import (  # noqa: E402
    CAPTURE_TIME_TOLERANCE_SECONDS,
    _prepare_event,
    _temporal_fields,
    build_event_view,
)

WEB_ROOT = ROOT / "output" / "local-web"
CAPTURED = "2026-10-24T06:34:17.153000+00:00"  # = 14:34:17 +08:00


def _event(quote: str, *, precision: str = "unknown", absolute: str | None = CAPTURED,
           reported_at: str | None = CAPTURED, title: str = "便签") -> dict:
    return {
        "event_id": "evt_test_capture",
        "title": title,
        "categories": ["routine"],
        "time": {"precision": precision, "absolute": absolute, "relative_seconds": None},
        "evidence": {
            "quote": quote,
            "reported_at": reported_at,
            "segment_id": None,
            "recording_id": "rec_test",
        },
    }


def _fields(prepared: dict) -> dict:
    return _temporal_fields(prepared, None)


def test_tolerance_constant_is_two_seconds() -> None:
    assert CAPTURE_TIME_TOLERANCE_SECONDS == 2.0


def test_capture_instruction_is_shown_as_approximate(tmp_path: Path) -> None:
    event, info = _prepare_event(
        _event("记录一下今天的裸重八十五点八五"), None, web_root=tmp_path
    )
    assert info["basis"] == "capture_statement"
    assert event["time"]["precision"] == "approximate"
    assert event["time"]["display_basis"] == "capture_statement"
    assert event["time"]["display_label"] == "约14:34（按讲述时间）"
    fields = _fields(event)
    assert fields["event_time_basis"] == "capture_statement"
    assert fields["display_time"] == {
        "value": "2026-10-24T14:34:17.153000+08:00",
        "precision": "approximate",
        "label": "约14:34（按讲述时间）",
        "basis": "capture_statement",
    }
    # 展示可以给"约"，但 occurred_at 只是近似值，必须能被识别
    assert fields["date_basis"] == "occurrence"


def test_plain_statement_with_capture_time_stays_unknown(tmp_path: Path) -> None:
    event, info = _prepare_event(
        _event("这个系统是不是有 bug 啊", title="电脑卡顿检查"), None, web_root=tmp_path
    )
    assert info["basis"] == "capture_time_not_occurrence"
    assert event["time"]["absolute"] is None
    assert event["time"]["precision"] == "unknown"
    assert event["time"]["capture_time"] == "2026-10-24T14:34:17.153000+08:00"
    fields = _fields(event)
    assert fields["occurred_at"] is None
    assert fields["date_basis"] == "reported_local"
    assert fields["event_time_basis"] == "reported_local"
    assert fields["display_time"] is None
    assert fields["date_is_unknown"] is True


def test_exact_precision_is_never_downgraded(tmp_path: Path) -> None:
    """10-20 的 exact 便签：absolute 也等于 reported_at，但 precision=exact 必须保留。"""
    event, info = _prepare_event(
        _event("记录一下今天的裸重", precision="exact"), None, web_root=tmp_path
    )
    assert info.get("basis") != "capture_time_not_occurrence"
    assert event["time"]["precision"] == "exact"
    assert event["time"]["absolute"] == CAPTURED
    fields = _fields(event)
    assert fields["event_time_basis"] == "exact_occurrence"
    assert fields["display_time"]["precision"] == "exact"


def test_minute_or_relative_precision_is_not_downgraded(tmp_path: Path) -> None:
    for precision in ("exact", "minute", "inferred", "relative"):
        event, _info = _prepare_event(
            _event("随便说说", precision=precision), None, web_root=tmp_path
        )
        assert event["time"]["absolute"] == CAPTURED, precision
        assert event["time"]["precision"] == precision


def test_absolute_far_from_reported_at_is_not_treated_as_capture(tmp_path: Path) -> None:
    event, info = _prepare_event(
        _event("记录一下今天的裸重", reported_at="2026-10-24T06:34:30.000000+00:00"),
        None,
        web_root=tmp_path,
    )
    assert info.get("basis") != "capture_statement"
    assert event["time"]["absolute"] == CAPTURED
    assert event["time"]["precision"] == "unknown"


def test_capture_instruction_inside_2s_window_is_approximate(tmp_path: Path) -> None:
    event, info = _prepare_event(
        _event("记一下这个", reported_at="2026-10-24T06:34:19.100000+00:00"),
        None,
        web_root=tmp_path,
    )
    assert info["basis"] == "capture_statement"
    assert event["time"]["precision"] == "approximate"


def test_title_only_capture_instruction_also_counts(tmp_path: Path) -> None:
    event, info = _prepare_event(
        _event("挺好的", title="便签：记录今天裸重"), None, web_root=tmp_path
    )
    assert info["basis"] == "capture_statement"


def test_reported_local_event_has_no_display_time(tmp_path: Path) -> None:
    event, _info = _prepare_event(
        _event("随便聊聊", absolute=None), None, web_root=tmp_path
    )
    fields = _fields(event)
    assert fields["event_time_basis"] == "reported_local"
    assert fields["display_time"] is None
    assert fields["occurred_at"] is None


# --- 真实数据守卫 ---

@pytest.mark.skipif(not WEB_ROOT.is_dir(), reason="需要本地数据")
def test_real_capture_statement_event_is_marked_approximate() -> None:
    view = build_event_view(
        WEB_ROOT, date_from="2026-10-24", date_to="2026-10-24", scope="provisional", view="display"
    )
    event = next(
        (e for e in view["events"] if e["event_id"] == "evt_ctx_07c1632c45cdd829"), None
    )
    if event is None:
        pytest.skip("样本事件不在当前数据集")
    assert event["event_time_basis"] == "capture_statement"
    assert event["time"]["precision"] == "approximate"
    assert event["display_time"]["label"].startswith("约14:34")
    assert event["display_time"]["precision"] == "approximate"


@pytest.mark.skipif(not WEB_ROOT.is_dir(), reason="需要本地数据")
def test_real_exact_note_on_1020_is_untouched() -> None:
    view = build_event_view(
        WEB_ROOT, date_from="2026-10-20", date_to="2026-10-20", scope="provisional", view="display"
    )
    event = next(
        (e for e in view["events"] if e["event_id"] == "evt_ctx_d2bec65314ff4277"), None
    )
    if event is None:
        pytest.skip("样本事件不在当前数据集")
    assert event["time"]["precision"] == "exact"
    assert event["event_time_basis"] == "exact_occurrence"


@pytest.mark.skipif(not WEB_ROOT.is_dir(), reason="需要本地数据")
def test_no_unknown_precision_event_carries_a_timestamp() -> None:
    """全量守卫：不允许出现"precision=unknown 但 absolute 存在"的自我矛盾。"""
    offenders = []
    for day in ("2026-10-22", "2026-10-23", "2026-10-24"):
        view = build_event_view(
            WEB_ROOT, date_from=day, date_to=day, scope="provisional", view="display"
        )
        for event in view["events"]:
            time_info = event.get("time") or {}
            if time_info.get("precision") == "unknown" and time_info.get("absolute"):
                offenders.append((day, event["event_id"]))
    assert offenders == []
