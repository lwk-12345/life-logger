"""Tests for data flywheel persistence and prompt-safe retrieval."""

import json
from pathlib import Path

from life_logger.flywheel import (
    feed_batch_review_decisions,
    get_active_negative_hints,
    record_dismissed_event,
    record_user_correction,
)


def test_flywheel_records_dismissals_and_corrections_durably(tmp_path: Path):
    event = {
        "event_id": "evt_test_1",
        "title": "练枪",
        "categories": ["exercise"],
        "evidence": {"quote": "我练枪"},
    }
    # 1. Record dismissal
    path_neg = record_dismissed_event(event, source_text="我练枪去看叉队一直播", root=tmp_path)
    assert path_neg is not None
    assert path_neg.is_file()
    data_neg = json.loads(path_neg.read_text(encoding="utf-8"))
    assert data_neg["title"] == "练枪"
    assert data_neg["quote"] == "我练枪"

    # 2. Record correction
    corrected = dict(event, title="看直播", categories=["entertainment"])
    path_corr = record_user_correction(event, corrected, "这不是健身", root=tmp_path)
    assert path_corr is not None
    assert path_corr.is_file()


def test_get_active_negative_hints_caps_count_and_character_budget(tmp_path: Path):
    # Create 5 negative examples
    for i in range(5):
        event = {
            "title": f"游戏词条{i}",
            "categories": ["exercise"],
            "evidence": {"quote": f"游戏词条{i}"},
        }
        record_dismissed_event(event, root=tmp_path)

    # Batch matches game terms
    batch = [{"text": "今天玩了游戏词条0和游戏词条1还有游戏词条2"}]
    hints = get_active_negative_hints(batch, root=tmp_path, max_hints=2, max_total_chars=80)

    # Strictly at most 2 hints and <= 80 characters
    assert len(hints) <= 2
    assert sum(len(h) for h in hints) <= 80
    assert any("游戏词条" in h for h in hints)


def test_unrelated_text_retrieves_zero_hints(tmp_path: Path):
    event = {
        "title": "练枪",
        "categories": ["exercise"],
        "evidence": {"quote": "我练枪"},
    }
    record_dismissed_event(event, root=tmp_path)

    # User talking about food, no overlap with "练枪"
    batch = [{"text": "中午吃了牛肉面花了二十元"}]
    hints = get_active_negative_hints(batch, root=tmp_path)
    assert hints == []


def test_feed_batch_review_decisions_records_media_negatives_and_self_voice(tmp_path: Path):
    source_records = [
        {"segment_id": "seg_1", "text": "小乖乖，你可和儿子贴心多了", "voice_similarity": 0.31, "audio_content_class": "stream"},
        {"segment_id": "seg_2", "text": "畜生，给我站住！", "voice_similarity": 0.32, "audio_content_class": "stream"},
        {"segment_id": "seg_3", "text": "我说了一句今天买了咖啡", "voice_similarity": 0.52, "acoustic_proximity": "near"},
    ]
    decisions = [
        {"segment_id": "seg_1", "source_decision": "non_speech"},
        {"segment_id": "seg_2", "source_decision": "media"},
        {"segment_id": "seg_3", "source_decision": "self"},
    ]
    counts = feed_batch_review_decisions("job_test", "result/source-attribution.json", decisions, source_records, root=tmp_path)
    assert counts["media_noise"] == 2
    assert counts["self_confirmed"] == 1

    # Check negative records created
    neg_files = list((tmp_path / "negatives").glob("*.json"))
    assert len(neg_files) == 2
    texts = [json.loads(p.read_text())["quote"] for p in neg_files]
    assert "小乖乖，你可和儿子贴心多了" in texts
    assert "畜生，给我站住！" in texts

    # Check self voice created
    voice_files = list((tmp_path / "voiceprints").glob("*.json"))
    assert len(voice_files) == 1
    v_data = json.loads(voice_files[0].read_text())
    assert v_data["text"] == "我说了一句今天买了咖啡"
    assert v_data["voice_similarity"] == 0.52

