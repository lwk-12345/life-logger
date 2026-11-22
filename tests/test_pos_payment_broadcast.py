"""Tests for offline store POS payment speaker broadcast detection."""

from life_logger.contextual_extraction import (
    parse_pos_broadcast_amount,
    synthesize_pos_payment_events,
)


def test_parse_broadcast_amounts():
    assert parse_pos_broadcast_amount("支付到账2元") == 2.0
    assert parse_pos_broadcast_amount("支付到账六点九元") == 6.9
    assert parse_pos_broadcast_amount("支付到账十四点八元") == 14.8
    assert parse_pos_broadcast_amount("微信支付收款15元") == 15.0
    assert parse_pos_broadcast_amount("支付宝到账27.6元") == 27.6
    assert parse_pos_broadcast_amount("今天天气真好") is None


def test_synthesizes_payment_event_linked_with_preceding_item():
    rows = [
        {
            "source_id": "s1",
            "segment_id": "seg_1",
            "recording_id": "rec_1",
            "source_label": "unknown",
            "text": "呃，拿两个冰杯",
            "start_offset_seconds": 10.0,
            "end_offset_seconds": 12.0,
            "timestamp": "2026-10-16T14:15:40+08:00",
        },
        {
            "source_id": "s2",
            "segment_id": "seg_1",
            "recording_id": "rec_1",
            "source_label": "non_self",
            "text": "请点击手机弹窗",
            "start_offset_seconds": 13.0,
            "end_offset_seconds": 14.0,
            "timestamp": "2026-10-16T14:15:43+08:00",
        },
        {
            "source_id": "s3",
            "segment_id": "seg_1",
            "recording_id": "rec_1",
            "source_label": "non_self",
            "text": "支付到账2元",
            "start_offset_seconds": 15.0,
            "end_offset_seconds": 16.5,
            "timestamp": "2026-10-16T14:15:48+08:00",
        },
    ]

    events = synthesize_pos_payment_events(rows)
    assert len(events) == 1
    evt = events[0]
    assert evt["categories"] == ["expense"]
    assert evt["data"]["expense"]["amount"] == 2.0
    assert "冰杯" in evt["title"] or "冰杯" in evt["data"]["expense"]["item"]
    assert evt["confidence"] >= 0.90


def test_parse_mixed_arabic_and_chinese_decimal_amounts():
    assert parse_pos_broadcast_amount("支付到账1点8元") == 1.8
    assert parse_pos_broadcast_amount("支付到账一点八元") == 1.8
    assert parse_pos_broadcast_amount("支付到账1点八元") == 1.8
    assert parse_pos_broadcast_amount("支付到账一点8元") == 1.8
    assert parse_pos_broadcast_amount("支付到账1.8元") == 1.8
    assert parse_pos_broadcast_amount("支付到账六点九元") == 6.9
    assert parse_pos_broadcast_amount("支付到账十四点八元") == 14.8
    assert parse_pos_broadcast_amount("支付到账1点85元") == 1.85


def test_parse_rejects_malformed_or_nonpositive_amounts():
    assert parse_pos_broadcast_amount("支付到账0元") is None
    assert parse_pos_broadcast_amount("支付到账-1元") is None
    assert parse_pos_broadcast_amount("支付到账1.2.3元") is None
    assert parse_pos_broadcast_amount("支付到账一点元") is None
    assert parse_pos_broadcast_amount("支付到账1e3元") is None
    assert parse_pos_broadcast_amount("支付到账1,000元") is None
    assert parse_pos_broadcast_amount("支付到账1元，谢谢") == 1.0
    assert parse_pos_broadcast_amount("支付到账十八点九九九元") == 18.999


def _expense_event(event_id, quote, amount, item, when):
    return {
        "event_id": event_id,
        "title": f"线下支付：{item} (¥{amount:.2f})",
        "categories": ["expense"],
        "data": {"expense": {"amount": amount, "item": item}},
        "evidence": {"quote": quote, "reported_at": when},
        "time": {"absolute": when},
    }


def test_collapse_recap_keeps_unrelated_equal_priced_purchase():
    from life_logger.contextual_extraction import collapse_recap_expenses

    events = [
        _expense_event(
            "evt-real-cola",
            "支付到账1点8元",
            1.8,
            "可乐",
            "2026-10-17T14:49:00+08:00",
        ),
        _expense_event(
            "evt-real-bread",
            "买了一个面包",
            1.8,
            "面包",
            "2026-10-17T15:10:00+08:00",
        ),
        _expense_event(
            "evt-recap-cola",
            "今天出门一共是买了一块八的一个可乐",
            1.8,
            "可乐",
            "2026-10-17T16:34:00+08:00",
        ),
        _expense_event(
            "evt-recap-bread",
            "今天出门一共是买了一块八的一个面包",
            1.8,
            "面包",
            "2026-10-17T16:35:00+08:00",
        ),
    ]

    kept = collapse_recap_expenses(events)
    assert [event["event_id"] for event in kept] == [
        "evt-real-cola",
        "evt-real-bread",
    ]


def test_collapse_recap_keeps_equal_item_on_a_different_local_day():
    from life_logger.contextual_extraction import collapse_recap_expenses

    events = [
        _expense_event(
            "evt-yesterday",
            "买了一个可乐",
            1.8,
            "可乐",
            "2026-10-16T14:49:00+08:00",
        ),
        _expense_event(
            "evt-recap-today",
            "今天出门一共是买了一块八的一个可乐",
            1.8,
            "可乐",
            "2026-10-17T16:34:00+08:00",
        ),
    ]

    kept = collapse_recap_expenses(events)
    assert [event["event_id"] for event in kept] == [
        "evt-yesterday",
        "evt-recap-today",
    ]


def test_transcript_serialization_keeps_utc_and_adds_shanghai_local_fields():
    from life_logger.domain import TranscriptSegment
    from scripts.run_cloud_asr_pipeline import _transcript_local_time_fields

    transcript = TranscriptSegment(
        segment_id="seg-1",
        recording_id="rec-1",
        start_offset_seconds=0.0,
        end_offset_seconds=2.0,
        absolute_started_at="2026-10-17T06:56:32+00:00",
        absolute_ended_at="2026-10-17T06:56:34+00:00",
        text="测试",
        source="moss",
        language="zh",
        transcription_confidence=None,
    )

    fields = _transcript_local_time_fields(transcript)
    assert fields["started_at_local"] == "2026-10-17T14:56:32+08:00"
    assert fields["clock_local"] == "14:56"
    # Absolute UTC values must remain untouched for downstream sorting.
    assert transcript.absolute_started_at == "2026-10-17T06:56:32+00:00"


def test_transcript_file_annotation_preserves_utc_absolute(tmp_path):
    import json

    from scripts.run_cloud_asr_pipeline import _annotate_transcript_file_local_times

    path = tmp_path / "transcript.json"
    path.write_text(
        json.dumps(
            [
                {
                    "segment_id": "seg-1",
                    "absolute_started_at": "2026-10-17T06:56:32+00:00",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert _annotate_transcript_file_local_times(path) == 1
    row = json.loads(path.read_text(encoding="utf-8"))[0]
    assert row["absolute_started_at"] == "2026-10-17T06:56:32+00:00"
    assert row["started_at_local"] == "2026-10-17T14:56:32+08:00"
    assert row["clock_local"] == "14:56"
