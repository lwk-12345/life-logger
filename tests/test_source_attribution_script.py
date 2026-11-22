import pytest

from scripts.attribute_funasr_transcript import (
    _attach_acoustic_evidence,
    _build_events,
    _rms_dbfs,
    embedding_rows,
    embeddings_from_results,
)


def test_embedding_rows_normalizes_one_vector_and_a_batch() -> None:
    assert embedding_rows([1.0, 2.0]) == [[1.0, 2.0]]
    assert embedding_rows([[1.0, 2.0], [3.0, 4.0]]) == [
        [1.0, 2.0],
        [3.0, 4.0],
    ]


def test_embeddings_from_results_preserves_batch_order() -> None:
    results = [
        {"spk_embedding": [[1.0, 2.0], [3.0, 4.0]]},
        {"spk_embedding": [[5.0, 6.0]]},
    ]

    assert embeddings_from_results(results) == [
        [1.0, 2.0],
        [3.0, 4.0],
        [5.0, 6.0],
    ]


def test_rms_dbfs_supports_plain_python_samples() -> None:
    assert _rms_dbfs([1.0, -1.0]) == 0.0
    assert _rms_dbfs([0.1, -0.1]) == pytest.approx(-20.0)


def test_acoustic_evidence_uses_nearby_speech_as_relative_reference() -> None:
    records = [
        {
            "segment_id": "quiet",
            "start_offset_seconds": 0.0,
            "end_offset_seconds": 2.0,
        },
        {
            "segment_id": "loud",
            "start_offset_seconds": 5.0,
            "end_offset_seconds": 7.0,
        },
    ]
    measurements = {
        "quiet": {"speech_rms_dbfs": -34.0, "noise_floor_dbfs": -42.0},
        "loud": {"speech_rms_dbfs": -20.0, "noise_floor_dbfs": -42.0},
    }

    result = _attach_acoustic_evidence(records, measurements)

    assert result[0]["relative_loudness_db"] < 0
    assert result[1]["relative_loudness_db"] > 0
    assert result[1]["proximity_score"] > result[0]["proximity_score"]


def test_confirmed_short_purchase_keeps_amount_and_evidence() -> None:
    records = [
        {
            "segment_id": "seg_purchase",
            "recording_id": "rec_purchase",
            "start_offset_seconds": 3247.79,
            "end_offset_seconds": 3250.37,
            "absolute_started_at": "2026-09-16T17:00:34.790000",
            "absolute_ended_at": "2026-09-16T17:00:37.370000",
            "text": "老板，来一包那个十块的红塔山。",
            "source_label": "self",
        }
    ]

    events = _build_events(records)

    assert len(events) == 1
    assert events[0].categories == ["expense"]
    assert events[0].data["expense"]["amount"] == 10.0
    assert events[0].evidence["quote"] == records[0]["text"]


def test_low_priority_audio_does_not_publish_a_low_confidence_event() -> None:
    base_record = {
        "segment_id": "seg_purchase",
        "recording_id": "rec_purchase",
        "start_offset_seconds": 10.0,
        "end_offset_seconds": 13.0,
        "absolute_started_at": "2026-09-16T17:00:10+08:00",
        "absolute_ended_at": "2026-09-16T17:00:13+08:00",
        "text": "我刚买了一杯咖啡，花了十块钱。",
        "source_label": "self",
    }
    speech_events = _build_events(
        [
            {
                **base_record,
                "audio_content_class": "speech",
                "audio_content_scores": {
                    "speech": 0.9,
                    "music": 0.03,
                    "noise": 0.03,
                    "stream": 0.04,
                },
                "audio_processing_priority": "high",
                "audio_priority_weight": 1.0,
            }
        ]
    )
    music_events = _build_events(
        [
            {
                **base_record,
                "audio_content_class": "music",
                "audio_content_scores": {
                    "speech": 0.2,
                    "music": 0.8,
                    "noise": 0.0,
                    "stream": 0.0,
                },
                "audio_processing_priority": "low",
                "audio_priority_weight": 0.3,
            }
        ]
    )

    assert len(speech_events) == 1
    assert speech_events[0].confidence >= 0.9
    assert music_events == []
