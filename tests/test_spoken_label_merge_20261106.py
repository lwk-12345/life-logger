"""多段念表合并 + 查营养库口令优先级 + 写库闭环（2026-11-06）。"""
import pytest

from life_logger.food_lookup import (
    BrandFoodLibrary,
    merge_spoken_labels,
    spoken_label_entry,
)
from life_logger.voice_triggers import _scan_voice_triggers as scan

BASE = "2026-10-08T22:32:30+08:00"
S1 = ("首先是面的营养成分，每一百克636千焦，蛋白质3.6克，脂肪0.6克，碳水化合物32.5克。")
# 同一张表的补充段（只有糖和钠，不冲突）
S2_SUPP = "糖是5.6克，钠是125毫克。"
# 另一张表（数值冲突，10-08 22:33 真实原话）
S2_CONFLICT = "1879千焦，蛋白质11.5，脂肪30克，然后糖是5.6克，钠是5450毫克。"


def seg(text, at, sid="s", label="self"):
    return {"text": text, "spoken_at": at, "segment_id": sid, "source_label": label}


def test_two_segments_merge_and_second_adds_sugar_sodium():
    merged = merge_spoken_labels([seg(S1, BASE, "s1"), seg(S2_SUPP, "2026-10-08T22:33:02+08:00", "s2")])
    assert len(merged) == 1
    m = merged[0]
    assert m["status"] == "ok" and m["item"] == "面"          # 名字以第一段为准
    assert m["sugar_g"] if False else m["per_100g"]["sugar_g"] == 5.6   # 第二段补齐
    assert m["per_100g"]["sodium_mg"] == 125.0
    assert m["segment_ids"] == ["s1", "s2"] and m["merged_segments"] == 2


def test_conflicting_second_segment_merges_but_goes_to_review():
    merged = merge_spoken_labels([seg(S1, BASE, "s1"), seg(S2_CONFLICT, "2026-10-08T22:33:02+08:00", "s2")])
    m = merged[0]
    assert m["merged_segments"] == 2                 # 仍合并（派单要求）
    assert m["status"] == "needs_review"
    assert "value_conflict" in m["reasons"]
    assert m["per_100g"]["sugar_g"] == 5.6           # 糖仍然补上
    assert m["item"] == "面"


def test_gap_over_90s_splits_into_two_groups():
    merged = merge_spoken_labels([seg(S1, BASE, "s1"), seg(S2_SUPP, "2026-10-08T22:40:00+08:00", "s2")])
    assert len(merged) == 2
    assert merged[1]["status"] == "needs_review" and "missing_item_name" in merged[1]["reasons"]


def test_non_self_segment_is_not_merged():
    merged = merge_spoken_labels([seg(S1, BASE, "s1"),
                                  seg(S2_SUPP, "2026-10-08T22:32:40+08:00", "s2", label="non_self")])
    assert len(merged) == 1 and merged[0]["merged_segments"] == 1


def test_interrupting_sentence_breaks_merge():
    gap = seg("然后呢，我们再说别的。", "2026-10-08T22:32:45+08:00", "sx")
    merged = merge_spoken_labels([seg(S1, BASE, "s1"), gap, seg(S2_SUPP, "2026-10-08T22:33:00+08:00", "s2")])
    assert len(merged) == 2


def test_nutrition_lookup_command_priority():
    """含营养词 → nutrition_lookup；不含 → 通用 query。"""
    hit = [c for c in scan("查一下 牛奶 的热量")]
    assert hit and hit[0].trigger_type == "nutrition_lookup"
    assert "每 100 克" in hit[0].metadata["reply"]
    plain = [c for c in scan("查一下明天的天气")]
    assert plain and plain[0].trigger_type == "query"


def test_lookup_says_not_found_without_fabricating():
    hit = scan("查一下 鲸鱼肉罐头 的营养")
    assert hit and hit[0].trigger_type == "nutrition_lookup"
    assert hit[0].metadata["kind"] == "not_found"
    assert "没有" in hit[0].metadata["reply"]


def test_end_to_end_spoken_label_then_next_day_150g(tmp_path):
    """念表 → 写品牌库(user_spoken) → 次日按标签算 150 克。"""
    lib = BrandFoodLibrary(tmp_path / "brand-library.json")
    merged = merge_spoken_labels([seg(S1, BASE, "s1"), seg(S2_SUPP, "2026-10-08T22:33:02+08:00", "s2")])[0]
    entry = spoken_label_entry(merged, spoken_at=BASE, segment_ids=merged["segment_ids"])
    stored = lib.remember(entry, note="念表 2026-10-08")
    assert stored["evidence"] == "user_spoken"

    hit = lib.lookup("面")            # 次日「吃了 150 克面」
    assert hit is not None and hit.source == "user"
    scaled = {k: round(v * 1.5, 1) for k, v in hit.per_100g.items()}
    assert scaled["energy_kcal"] == pytest.approx(228.0, abs=0.2)   # ≈227.9
    assert scaled["protein_g"] == 5.4 and scaled["carbohydrates_g"] == pytest.approx(48.8, abs=0.1)
