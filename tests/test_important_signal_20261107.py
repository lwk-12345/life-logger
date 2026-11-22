"""生活关键词 + 声纹 unknown 的句子不得被折叠成媒体（2026-11-07）。

背景：`source_attribution.IMPORTANT_EVENT_MARKERS` / `has_important_signal()` 原本
只服务 dictation 剔除；而 `activity_lifecycle()` 在「有归属证据但 0 条 self」时
只要命中媒体结构证据就判 media —— 于是声纹判 **unknown**、但原话明显是本人生活事实
（"卧推一组" / "花了两千块"）的卡片会被折叠进媒体区，用户永远看不到。

本文件锁三件事：
1. `important_signal_survives()` 的判据：unknown → 豁免；**明确他人** → 不豁免；
2. `activity_lifecycle()` 对 unknown+生活关键词的卡片返回 `unknown`（进待确认），
   对 non_self（深夜直播那类）**行为完全不变**；
3. 不含关键词的 unknown 行为不变（仍然可以判 media）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.source_attribution import (  # noqa: E402
    AttributionConfig,
    has_important_signal,
    important_signal_survives,
    is_confirmed_other,
    should_extract_life_events,
)
from life_logger.activity_timeline_runtime import activity_lifecycle  # noqa: E402


# --------------------------------------------------------------------------- has_important_signal
def test_important_signal_covers_fitness_and_money():
    assert has_important_signal("今天卧推一组就力竭了") is True
    assert has_important_signal("卧推") is True
    assert has_important_signal("花了两千块") is True
    assert has_important_signal("吃了碗牛肉面") is True
    assert has_important_signal("今天天气不错") is False
    assert has_important_signal("") is False


# --------------------------------------------------------------------------- 豁免判据
def test_unknown_with_signal_survives():
    assert important_signal_survives("unknown", "卧推一组") is True


def test_unknown_without_signal_does_not_survive():
    assert important_signal_survives("unknown", "今天天气不错") is False


def test_confident_other_with_signal_does_not_survive():
    """non_self + 分数明确低于 unknown_threshold（0.25）+ 无需复核 → 明确是他人。"""
    assert important_signal_survives(
        "non_self", "花了两千块",
        similarity=0.12, review_required=False, decision_reason="below_non_self_threshold",
    ) is False
    assert is_confirmed_other(
        "non_self", similarity=0.12, review_required=False,
        decision_reason="below_non_self_threshold",
    ) is True


def test_review_band_other_with_signal_survives():
    """复核带（similarity 0.25–0.50）里的 non_self 不是「明确他人」，要留。"""
    assert important_signal_survives(
        "non_self", "花了两千块",
        similarity=0.35, review_required=True, decision_reason="similarity_in_review_band",
    ) is True


def test_strong_personal_negative_match_is_confirmed_other():
    assert is_confirmed_other(
        "non_self", similarity=0.40, review_required=False,
        decision_reason="strong_personal_negative_match",
    ) is True


def test_unknown_is_never_confirmed_other():
    assert is_confirmed_other("unknown", similarity=0.05, review_required=False) is False
    assert is_confirmed_other("self", similarity=0.9, review_required=False) is False


def test_threshold_defaults_to_existing_config():
    """不另造数字：默认阈值就是 AttributionConfig().unknown_threshold。"""
    threshold = AttributionConfig().unknown_threshold
    assert threshold == 0.25
    assert is_confirmed_other("non_self", similarity=threshold - 0.01, review_required=False) is True
    assert is_confirmed_other("non_self", similarity=threshold, review_required=False) is False


def test_should_extract_life_events_unchanged_for_non_self():
    """non_self 含关键词仍然进抽取（作为他人内容待确认）——本单不改这条。"""
    assert should_extract_life_events("non_self", text="花了两千块") is True
    assert should_extract_life_events("non_self", text="今天天气不错") is False
    assert should_extract_life_events("unknown", text="卧推一组") is True
    assert should_extract_life_events("unknown", text="今天天气不错") is False
    assert should_extract_life_events("self", text="随便说点什么") is True


# --------------------------------------------------------------------------- lifecycle 折叠
def _card(*, title: str, quotes: list[tuple[str, str]]) -> dict:
    """quotes = [(source_label, quote)]，全部塞进一个 item 的 evidence。"""
    return {
        "activity_id": "act_test",
        "title": title,
        "summary": "",
        "items": [
            {
                "item_id": "item_1",
                "evidence": [
                    {"segment_id": f"seg_{i}", "row_id": i, "source_label": label, "quote": quote}
                    for i, (label, quote) in enumerate(quotes)
                ],
            }
        ],
    }


def test_unknown_quote_with_fitness_marker_is_not_folded_into_media():
    card = _card(title="视频：健身房动作讲解", quotes=[("unknown", "今天卧推一组就力竭了")])
    assert activity_lifecycle(card) == "unknown"


def test_unknown_quote_with_money_marker_is_not_folded_into_media():
    card = _card(title="视频：带货直播", quotes=[("unknown", "花了两千块买鞋")])
    assert activity_lifecycle(card) == "unknown"


def test_unknown_quote_without_marker_still_folds_into_media():
    """不含关键词的 unknown 行为不变：仍按媒体结构证据折叠。"""
    card = _card(title="视频：海边日落", quotes=[("unknown", "今天天气不错")])
    assert activity_lifecycle(card) == "media"


def test_non_self_quote_with_money_marker_still_folds_into_media():
    """non_self（深夜直播那类）行为不变：含「花了」也仍然是 media。"""
    card = _card(title="视频：带货直播", quotes=[("non_self", "花了两千块买鞋")])
    assert activity_lifecycle(card) == "media"


def test_self_evidence_still_live():
    card = _card(title="视频：健身房动作讲解", quotes=[("self", "今天卧推一组就力竭了")])
    assert activity_lifecycle(card) == "live"


def test_mixed_unknown_and_non_self_with_marker_is_not_folded():
    """unknown 命中关键词即豁免，即使同一卡片还有 non_self 证据。"""
    card = _card(
        title="视频：健身教学",
        quotes=[("non_self", "花了两千块买鞋"), ("unknown", "深蹲三组")],
    )
    assert activity_lifecycle(card) == "unknown"


def test_card_without_evidence_is_unaffected():
    """无 evidence 的卡走启发式分支，本单不改变它的结论（当前为 live）。"""
    card = {"activity_id": "act_x", "title": "随便的记录", "summary": "", "items": []}
    assert activity_lifecycle(card) == "live"
