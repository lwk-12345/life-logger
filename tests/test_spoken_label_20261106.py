"""念营养成分表 → 品牌库条目 / 待确认 / 查库口令（2026-11-06）。"""
import pytest

from life_logger.food_lookup import (
    lookup_command_reply,
    parse_spoken_label,
    spoken_label_entry,
    labels_conflict,
)

# 2026-10-08 22:32 真实原话
REAL_1 = ("首先是面的营养成分，呃，是每一百克636千焦，蛋白质3.6克，脂肪0.6克，"
          "碳水化合物是32.5克。那是125毫克。")
# 2026-10-08 22:33 真实原话（缺食物名）
REAL_2 = "1879千焦，蛋白质11.5，脂肪30克，然后糖是5.6克，钠是5450毫克。"
# 2026-10-11 真实原话（每100克 + 糖单列）
REAL_3 = "豆浆热量。每100克是163千焦。糖是每100克2.5克，蛋白质3克，脂肪1.6克。"


def test_real_label_parsed_and_scaled_to_150g():
    parsed = parse_spoken_label(REAL_1)
    assert parsed["status"] == "ok" and parsed["item"] == "面"
    entry = spoken_label_entry(parsed, spoken_at="2026-10-08T22:32:30+08:00")
    assert entry["source"] == "user_spoken" and entry["spoken_at"].startswith("2026-10-08")
    assert "636千焦" in entry["quote"]
    per100 = entry["per_100g"]
    assert per100["energy_kcal"] == pytest.approx(152.0, abs=0.1)      # 636 / 4.184
    assert per100["protein_g"] == 3.6 and per100["fat_g"] == 0.6 and per100["carbohydrates_g"] == 32.5
    scaled = {k: round(v * 1.5, 1) for k, v in per100.items()}
    assert scaled["energy_kcal"] == pytest.approx(228.0, abs=0.2)      # ≈227.9 千卡
    assert scaled["protein_g"] == 5.4 and scaled["carbohydrates_g"] == pytest.approx(48.8, abs=0.1)


def test_second_segment_without_item_name_goes_to_review():
    parsed = parse_spoken_label(REAL_2)
    assert parsed["status"] == "needs_review"
    assert "missing_item_name" in parsed["reasons"]
    assert spoken_label_entry(parsed) is None
    assert parsed["per_100g"]["sugar_g"] == 5.6


def test_sugar_is_separate_from_carbs():
    parsed = parse_spoken_label(REAL_3)
    assert parsed["status"] == "ok"
    assert parsed["per_100g"]["sugar_g"] == 2.5          # 不是 100（「每100克」不能被当成糖量）
    assert "carbohydrates_g" not in parsed["per_100g"]  # 没念碳水就不编
    assert parsed["per_100g"]["energy_kcal"] == pytest.approx(38.96, abs=0.05)


def test_per_serving_without_net_weight_is_review():
    parsed = parse_spoken_label("某饼干 每份能量120千卡，蛋白质5克，脂肪3克，碳水20克")
    assert parsed["status"] == "needs_review"
    assert "serving_without_net_weight" in parsed["reasons"]


def test_per_serving_with_net_weight_normalises_to_100g():
    parsed = parse_spoken_label("某饼干 能量120千卡，蛋白质5克，脂肪3克，碳水20克，每份30克")
    assert parsed["status"] == "ok" and parsed["basis"] == "per_serving"
    assert parsed["serving_g"] == 30
    assert parsed["per_100g"]["energy_kcal"] == pytest.approx(400.0, abs=0.1)


def test_conflicting_numbers_detected():
    a = {"per_100g": {"energy_kcal": 100.0, "protein_g": 5.0}}
    b = {"per_100g": {"energy_kcal": 130.0, "protein_g": 5.2}}
    conflicts = labels_conflict(a, b)
    assert any("energy_kcal" in c for c in conflicts)
    assert not any("protein_g" in c for c in conflicts)


def test_lookup_command_reports_source_or_says_not_found(tmp_path):
    from pathlib import Path
    from life_logger.food_lookup import BrandFoodLibrary, FoodLookup

    lib = BrandFoodLibrary(Path(tmp_path) / "b.json")
    lib.remember({"name": "面", "per_100g": {"energy_kcal": 152.0, "protein_g": 3.6,
                                            "fat_g": 0.6, "carbohydrates_g": 32.5},
                  "source": "user_spoken", "origin": "user"})
    lookup = FoodLookup(data_dir=Path(tmp_path))

    hit = lookup_command_reply("查一下 面 的营养", lib)
    assert hit["kind"] == "found" and "152" in hit["reply"] and "你念的营养表" in hit["reply"]
    miss = lookup_command_reply("查一下 榴莲披萨 的营养", lookup)
    assert miss["kind"] == "not_found" and "没有" in miss["reply"]
    assert lookup_command_reply("今天天气不错", lookup) is None


def test_lookup_command_does_not_write(tmp_path):
    """查询口令只读：给一个只读的假库，确认调用前后目录内容不变。"""
    from types import SimpleNamespace

    class FakeLookup:
        def __init__(self):
            self.calls = 0

        def lookup(self, query):
            self.calls += 1
            return SimpleNamespace(name="牛奶", matched_name="牛奶", source="usda", match_kind="exact",
                                   per_100g={"energy_kcal": 61.0, "protein_g": 3.2})

    fake = FakeLookup()
    before = sorted(p.name for p in tmp_path.glob("*"))
    out = lookup_command_reply("查一下 牛奶 的营养", fake)
    assert out["kind"] == "found" and fake.calls == 1
    assert sorted(p.name for p in tmp_path.glob("*")) == before, "查询口令不许写文件"
