"""饮食事件热量/营养估算的回归测试（离线，无网络调用）。

主用例来自真实事故（2026-10-12 08:37 生椰拿铁）：
用户说了「一百四十克菲诺的椰乳」+「没冰，加上一杯浓缩」，事件层却只凭「生椰拿铁」
四个字按市售标准中杯估成 220 kcal（碳水 30g）——按配料算只有约 135 kcal。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from life_logger.contextual_extraction import (
    CONTEXT_EXTRACTION_PROMPT_VERSION,
    run_contextual_extraction,
)
from life_logger.food_nutrition import (
    BASIS_INGREDIENT_MATH,
    BASIS_LIBRARY_MATH,
    BASIS_STANDARD_SERVING,
    FOOD_NUTRIENT_KEYS,
    INGREDIENTS,
    apply_food_nutrition_calibration,
    extract_ingredient_mentions,
    parse_quantity,
)

TEST_KEY = "test-only-contextual-key"

# 真实事故里模型给的 food 载荷（照抄 output/.../D4A1A35C.../events.json 的 evt_ctx_bf60a264ef6f8663）
STANDARD_CUP_FOOD: dict[str, Any] = {
    "meal": "snack",
    "item": "生椰拿铁",
    "portion": "1杯",
    "weight_g": 350,
    "estimated_calories": 220,
    "nutrients": {
        "energy_kcal": 220,
        "protein_g": 5.0,
        "fat_g": 9.0,
        "carbohydrates_g": 30.0,
        "sodium_mg": 120,
    },
}

# MOSS 转写后的真实原话（配料与食品名分处不同段，模型那一批看不到配料）
REAL_CASE_TEXTS: tuple[tuple[str, str, float], ...] = (
    ("watch_000026_000001", "记录一下,我现在要做一杯生椰拿铁。", 1573.338),
    ("watch_000026_000002", "原料是……叫菲诺的……椰乳。", 1600.0),
    ("watch_000037_000001", "一百四十克菲诺的椰乳。", 1900.0),
    ("watch_000037_000002", "没冰，加上一杯浓缩。", 1950.0),
)


def record(segment_id: str, text: str, offset: float, *, source_label: str = "self") -> dict[str, Any]:
    return {
        "recording_id": "watch_D4A1A35CC987444AB86DD64B0E34411C",
        "segment_id": segment_id,
        "absolute_started_at": "2026-10-12T08:37:28+08:00",
        "start_offset_seconds": offset,
        "end_offset_seconds": offset + 5,
        "source_label": source_label,
        "is_dictation": False,
        "text": text,
    }


def real_case_records() -> list[dict[str, Any]]:
    return [record(segment, text, offset) for segment, text, offset in REAL_CASE_TEXTS]


def food_event(
    *,
    segment_id: str = "watch_000026_000001",
    offset: float = 1573.338,
    food: dict[str, Any] | None = None,
    title: str = "便签：计划喝一杯生椰拿铁",
) -> dict[str, Any]:
    return {
        "event_id": "evt_ctx_bf60a264ef6f8663",
        "categories": ["food"],
        "activity_type": "drink",
        "title": title,
        "time": {"absolute": "2026-10-12T00:37:28.775000+00:00", "relative_seconds": offset, "precision": "exact"},
        "data": {"food": dict(food if food is not None else STANDARD_CUP_FOOD)},
        "evidence": {
            "segment_id": segment_id,
            "start_offset_seconds": offset,
            "quote": "记录一下,我现在要做一杯生椰拿铁。",
        },
    }


# --------------------------------------------------------------------------------------
# 中文数量解析
# --------------------------------------------------------------------------------------


def test_parse_quantity_handles_chinese_and_arabic_numbers() -> None:
    assert parse_quantity("140") == 140
    assert parse_quantity("1.5") == 1.5
    assert parse_quantity("一百四十") == 140
    assert parse_quantity("一百二十") == 120
    assert parse_quantity("两百") == 200
    assert parse_quantity("十") == 10
    assert parse_quantity("半") == 0.5
    assert parse_quantity("") is None
    assert parse_quantity("随便") is None


def test_ingredient_table_covers_common_milk_tea_and_coffee_ingredients() -> None:
    names = {profile.name for profile in INGREDIENTS}
    assert len(INGREDIENTS) >= 20
    for expected in ("菲诺厚椰乳", "意式浓缩", "全脂牛奶", "脱脂牛奶", "燕麦奶", "白砂糖", "蛋白粉", "威士忌"):
        assert expected in names


# --------------------------------------------------------------------------------------
# 配料抽取
# --------------------------------------------------------------------------------------


def test_extract_mentions_from_real_case_sentences() -> None:
    milk = extract_ingredient_mentions("一百四十克菲诺的椰乳")
    assert [(item.name, item.grams) for item in milk] == [("菲诺厚椰乳", 140.0)]

    shot = extract_ingredient_mentions("没冰，加上一杯浓缩")
    assert [(item.name, item.grams) for item in shot] == [("意式浓缩", 30.0)]

    # 只提名字 / 闲聊里的数字 / 查询句 都不能算成配料
    assert extract_ingredient_mentions("记录一下,我现在要做一杯生椰拿铁。") == []
    assert extract_ingredient_mentions("140克是多少热量") == []
    assert extract_ingredient_mentions("140克肥肉的味道。") == []


def test_extract_mentions_does_not_double_count_brand_and_category() -> None:
    mentions = extract_ingredient_mentions("一百四十克菲诺的椰乳")
    assert len(mentions) == 1
    total = sum(item.energy_kcal for item in mentions)
    assert 125 <= total <= 140  # 93.8 kcal/100g × 140g ≈ 131.3


def test_extract_mentions_ignores_qualified_variants() -> None:
    # 「脱脂牛奶」不该被算成「全脂牛奶」两次；「浓缩果汁」不是意式浓缩；「蛋白粉」不是鸡蛋
    assert [item.name for item in extract_ingredient_mentions("250毫升脱脂牛奶")] == ["脱脂牛奶"]
    assert extract_ingredient_mentions("喝了一杯浓缩果汁") == []
    assert [item.name for item in extract_ingredient_mentions("两勺蛋白粉")] == ["蛋白粉"]


def test_per_100g_label_is_not_treated_as_intake() -> None:
    """真实转写里踩到过：`每100克是163千焦` 是成分表规格，不是摄入量。"""
    text = "记录下我的这个豆浆热量。每100克是163千焦。糖是每100克2加5克。"
    assert extract_ingredient_mentions(text) == []


def test_weight_quantity_beats_count_quantity() -> None:
    """`一份豆浆300克` 要取 300 克，不能取「一份」（默认 200g）。"""
    mentions = extract_ingredient_mentions("这一份是300克,一份豆浆300克。")
    assert [(item.name, item.grams) for item in mentions] == [("豆浆", 300.0)]


# --------------------------------------------------------------------------------------
# 主回归：生椰拿铁（标准杯 220 kcal → 按配料 ≈135 kcal）
# --------------------------------------------------------------------------------------


def test_shengye_latte_recomputed_from_ingredients_not_standard_cup() -> None:
    event = food_event()
    summary = apply_food_nutrition_calibration([event], real_case_records())
    food = event["data"]["food"]

    assert summary["ingredient_math"] == 1
    assert 120 <= food["estimated_calories"] <= 160, food
    assert food["estimated_calories"] != 220
    assert 120 <= food["nutrients"]["energy_kcal"] <= 160
    # 碳水不再虚高 20g
    assert food["nutrients"]["carbohydrates_g"] < 20
    assert 8.0 <= food["nutrients"]["fat_g"] <= 11.0
    assert 1.0 <= food["nutrients"]["protein_g"] <= 3.0
    assert 90 <= food["nutrients"]["sodium_mg"] <= 140
    # 结构保留 + 标注来源
    assert food["item"] == "生椰拿铁"
    assert food["meal"] == "snack"
    assert food["portion"] == "1杯"
    assert food["weight_g"] == 170  # 140g 椰乳 + 30ml 浓缩
    assert food["estimate_basis"] == BASIS_INGREDIENT_MATH
    assert food["estimate_confidence"] == "high"
    assert {item["item"] for item in food["ingredients"]} == {"菲诺厚椰乳", "意式浓缩"}


def test_total_nutrition_matches_user_hand_calculation() -> None:
    mentions = [
        *extract_ingredient_mentions("一百四十克菲诺的椰乳"),
        *extract_ingredient_mentions("没冰，加上一杯浓缩"),
    ]
    event = food_event()
    apply_food_nutrition_calibration([event], real_case_records())
    food = event["data"]["food"]
    totals = {key: food["nutrients"][key] for key in FOOD_NUTRIENT_KEYS}
    # 用户自己按配料算的：约 135-137 kcal / 蛋白 1.8g / 脂肪 9.8g / 碳水 10.0g / 钠 115mg
    assert 130 <= totals["energy_kcal"] <= 140
    assert abs(totals["fat_g"] - 9.8) <= 0.5
    assert abs(totals["carbohydrates_g"] - 10.0) <= 0.8
    assert len(mentions) == 2


def test_name_only_event_keeps_estimate_but_is_marked_low_confidence() -> None:
    event = food_event(food={**STANDARD_CUP_FOOD, "item": "拿铁", "estimated_calories": None, "nutrients": {}})
    records = [record("watch_000026_000001", "我喝了杯拿铁。", 1573.338)]
    summary = apply_food_nutrition_calibration([event], records)
    food = event["data"]["food"]
    assert summary["standard_serving"] == 1
    assert summary["ingredient_math"] == 0
    assert food["estimate_basis"] == BASIS_STANDARD_SERVING
    assert food["estimate_confidence"] == "low"
    assert "ingredients" not in food


def test_espresso_only_never_shrinks_the_drink_to_a_single_shot() -> None:
    """只有「一杯浓缩」这类计数单位时，不能把整杯按一小杯浓缩算（否则生椰拿铁被算没）。

    [2026-10-13 行为变更] 旧版这里保留模型估值 220；层① 现在有「生椰拿铁」复合配方，
    整杯按配方重算（200g 椰乳 + 30ml 浓缩 ≈ 192 kcal）—— 这正是本次要修的能力
    「没报克重也能按名字查到可靠数值」。底线没变：**不能变成一杯浓缩的 4 kcal**。
    """
    event = food_event()
    records = [record("watch_000026_000001", "没冰，加上一杯浓缩。", 1573.338)]
    summary = apply_food_nutrition_calibration([event], records)
    food = event["data"]["food"]
    assert summary["ingredient_math"] == 0  # 计数单位不触发配料乘除
    assert "ingredients" not in food  # 也没把「一杯浓缩」写进配料表
    assert food["estimate_basis"] == BASIS_LIBRARY_MATH
    assert food["nutrition_source"] == "brand"
    assert food["nutrition_matched_name"] == "菲诺厚椰乳+意式浓缩"
    assert food["nutrition_serving_source"] == "recipe"
    assert 150 <= food["estimated_calories"] <= 230  # 一整杯的量级
    assert food["estimated_calories"] > 100


def test_non_self_speech_does_not_drive_my_calories() -> None:
    event = food_event()
    records = [
        *real_case_records(),
        record("watch_000037_000003", "他喝了一百克菲诺的椰乳。", 1955.0, source_label="non_self"),
    ]
    apply_food_nutrition_calibration([event], records)
    food = event["data"]["food"]
    # non_self 那句不能进配料表（否则重量变成 240g）
    assert food["weight_g"] == 170
    assert 120 <= food["estimated_calories"] <= 160


def test_mention_outside_window_is_not_attached() -> None:
    event = food_event()
    records = [record("watch_000099_000001", "一百四十克菲诺的椰乳。", 9000.0)]
    apply_food_nutrition_calibration([event], records)
    food = event["data"]["food"]
    # 时间窗外的配料绝不进配料表（旧断言是「保留模型估值 220」，现在改由层① 按名字兜底）
    assert "ingredients" not in food
    assert food["weight_g"] != 170  # 那 140g 没被算进来
    assert food["estimate_basis"] == BASIS_LIBRARY_MATH
    assert food["nutrition_serving_source"] == "recipe"  # 用的是配方默认克重，不是那 140g
    assert 150 <= food["estimated_calories"] <= 230


def test_mention_goes_to_the_nearest_food_event_only() -> None:
    breakfast = food_event(
        segment_id="watch_000010_000001",
        offset=100.0,
        food={"meal": "breakfast", "item": "燕麦片", "portion": "1份", "estimated_calories": 150,
              "nutrients": {"energy_kcal": 150, "protein_g": 5.0, "fat_g": 3.0, "carbohydrates_g": 27.0, "sodium_mg": 2}},
        title="早饭：燕麦片",
    )
    latte = food_event()
    records = [
        record("watch_000010_000001", "早饭吃了一份燕麦片。", 100.0),
        *real_case_records(),
    ]
    summary = apply_food_nutrition_calibration([breakfast, latte], records)
    assert summary["ingredient_math"] == 1
    breakfast_food = breakfast["data"]["food"]
    # 椰乳那 140g 只归给拿铁那一杯：早餐事件里没有配料，重量是燕麦片的 40g/份
    assert "ingredients" not in breakfast_food
    assert breakfast_food["weight_g"] == 40
    assert breakfast_food["nutrition_source"] == "brand"
    assert 120 <= latte["data"]["food"]["estimated_calories"] <= 160


def test_label_text_keeps_high_confidence() -> None:
    event = food_event(food={"meal": "snack", "item": "菲诺厚椰乳", "portion": "1份",
                             "estimated_calories": 131, "nutrients": {"energy_kcal": 131}})
    records = [record("watch_000026_000001", "每100克能量392千焦，我喝了140克。", 1573.338)]
    apply_food_nutrition_calibration([event], records)
    food = event["data"]["food"]
    assert food["estimate_basis"] != BASIS_STANDARD_SERVING
    assert food["estimate_confidence"] == "high"


def test_events_without_food_payload_are_untouched() -> None:
    event = {"event_id": "evt_ctx_x", "categories": ["exercise"], "data": {"exercise": {"body_part": "胸部"}}}
    before = json.dumps(event, ensure_ascii=False, sort_keys=True)
    summary = apply_food_nutrition_calibration([event], real_case_records())
    assert summary["food_events"] == 0
    assert json.dumps(event, ensure_ascii=False, sort_keys=True) == before


# --------------------------------------------------------------------------------------
# 端到端：接进 run_contextual_extraction，并校验 prompt 已改
# --------------------------------------------------------------------------------------


class _Response:
    def __init__(self, content: dict[str, Any]) -> None:
        self.body = json.dumps(
            {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]},
            ensure_ascii=False,
        ).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


def _chat_stub(
    response_factory: Callable[[dict[str, Any], int], dict[str, Any]],
    captured: list[dict[str, Any]],
):
    def opener(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        captured.append(body)
        return _Response(response_factory(body, len(captured) - 1))

    return opener


def _packet_records(body: dict[str, Any]) -> list[dict[str, Any]]:
    prompt = body["messages"][0]["content"]
    return json.loads(prompt.split("输入记录：\n", 1)[1])


def _real_case_responder(body: dict[str, Any], _index: int) -> dict[str, Any]:
    """按批次真实作答：只有看到「生椰拿铁」那句的批次才给出标准杯估算（= 事故当天的真实行为）。"""
    records: list[dict[str, Any]] = []
    for row in _packet_records(body):
        if "生椰拿铁" in row["text"]:
            records.append(
                {
                    "source_id": row["source_id"],
                    "is_event": True,
                    "relations": [],
                    "events": [
                        {
                            "category": "food",
                            "event_state": "planned",
                            "temporal_relation": "at_speech",
                            "occurrence_time": None,
                            "title": "计划喝一杯生椰拿铁",
                            "activity_type": "drink",
                            "topic": "饮品计划",
                            "claim_type": "plan",
                            "quote": row["text"],
                            "confidence": 0.9,
                            "data": {"food": dict(STANDARD_CUP_FOOD)},
                        }
                    ],
                }
            )
        else:
            records.append(
                {"source_id": row["source_id"], "is_event": False, "relations": [], "events": []}
            )
    return {"records": records}


def test_pipeline_recomputes_latte_from_ingredients_in_another_batch() -> None:
    records = real_case_records()
    requests: list[dict[str, Any]] = []
    result = run_contextual_extraction(
        records,
        deepseek_api_key=TEST_KEY,
        batch_size=1,  # 强制让「生椰拿铁」与「一百四十克菲诺的椰乳」落在不同批次
        deepseek_opener=_chat_stub(_real_case_responder, requests),
    )

    assert len(result["batches"]) == len(records)  # 确实一条一批，配料不在同一批里
    formal = [event for event in result["formal_events"] if event["categories"] == ["food"]]
    assert formal, result["pending_sources"]
    food = formal[0]["data"]["food"]
    assert 120 <= food["estimated_calories"] <= 160, food
    assert food["estimated_calories"] != 220
    assert food["estimate_basis"] == BASIS_INGREDIENT_MATH
    assert result["food_nutrition"]["ingredient_math"] == 1
    # 事件结构没变（下游看板/ledger 依赖这些字段）
    for key in ("item", "meal", "portion", "estimated_calories", "nutrients", "weight_g"):
        assert key in food
    assert set(FOOD_NUTRIENT_KEYS) <= set(food["nutrients"])
    # 原始证据链没被动过
    assert formal[0]["evidence"]["quote"] == "记录一下,我现在要做一杯生椰拿铁。"


def test_prompt_leaves_nutrition_math_to_code() -> None:
    """[2026-11-06] 提示词不再要求模型按 per-100g 乘除；只要食物名、克数、餐次。

    营养换算由 food_lookup / food_nutrition 代码完成，模型只负责抽取。
    """
    requests: list[dict[str, Any]] = []
    run_contextual_extraction(
        real_case_records(),
        deepseek_api_key=TEST_KEY,
        batch_size=4,
        deepseek_opener=_chat_stub(lambda _body, _index: {"records": []}, requests),
    )
    prompt = requests[0]["messages"][0]["content"]
    assert "配料优先铁律" not in prompt
    assert "ingredient_math" not in prompt
    assert "standard_serving" not in prompt
    # 「生椰拿铁」会出现在输入记录的 JSON 里（那是数据），只断言指令里没有这段示例
    assert "配料优先铁律" not in prompt
    assert "食物名" in prompt and "weight_g" in prompt and "meal" in prompt
    assert "不要自己乘除" in prompt
    # 本轮只改提示词文本，未升版本号（升版本会让批次缓存全失效，需另行决定）
    assert CONTEXT_EXTRACTION_PROMPT_VERSION.endswith("-v9-expense-evidence")
