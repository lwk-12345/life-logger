# -*- coding: utf-8 -*-
"""四层食物营养查询链的测试（全部离线：层④ 用注入的抓取函数，不打真网）。

覆盖：
* 四层优先级（品牌库 → 中国食物成分表 → USDA → OFF）
* OFF 查到写回层①（「后台记忆」）：第二次查询不再联网
* 中文模糊匹配：精确 / 去括号 / 别名表 / 编辑距离 / 复合词
* 降级容错：数据文件缺失或读坏 → 静默跳过，不抛错
* 数值换算：每 100g × 克数、份量来源标注
* 「念一次存一次」：营养口令解析 + 写进品牌库
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from life_logger.food_lookup import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    SOURCE_BRAND,
    SOURCE_CFCT,
    SOURCE_FOODWAKE,
    SOURCE_OFF,
    SOURCE_USDA,
    FoodLookup,
    apply_nutrition_captures,
    fetch_openfoodfacts,
    normalize_name,
    parse_nutrition_label_text,
    source_label,
    source_tag,
    strip_brackets,
)
from life_logger.food_nutrition import (
    BASIS_LIBRARY_MATH,
    BASIS_STANDARD_SERVING,
    apply_food_nutrition_calibration,
)

# --------------------------------------------------------------------------------------
# 夹具：造一个只含测试条目的数据目录（完全不碰真实数据）
# --------------------------------------------------------------------------------------


def _write(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")


BRAND_SEED: list[dict[str, Any]] = [
    {
        "name": "菲诺厚椰乳",
        "keywords": ["菲诺", "厚椰乳", "椰乳"],
        "per_100g": {"energy_kcal": 93.8, "protein_g": 1.0, "fat_g": 7.0, "carbohydrates_g": 6.8, "sodium_mg": 82.0},
        "serving_g": 200.0,
        "serving_label": "杯",
        "evidence": "user_verified",
    },
    {
        "name": "意式浓缩",
        "keywords": ["意式浓缩", "浓缩"],
        "per_100g": {"energy_kcal": 13.0, "protein_g": 0.7, "fat_g": 0.2, "carbohydrates_g": 2.0, "sodium_mg": 10.0},
        "serving_g": 30.0,
        "serving_label": "杯",
        "evidence": "user_verified",
    },
    {
        "name": "米饭",
        "keywords": ["米饭", "白饭"],
        "per_100g": {"energy_kcal": 116.0, "protein_g": 2.6, "fat_g": 0.3, "carbohydrates_g": 25.9, "sodium_mg": 2.0},
        "serving_g": 150.0,
        "serving_label": "碗",
    },
]

CFCT_ENTRIES: list[dict[str, Any]] = [
    {
        "name": "鸡胸脯肉",
        "per_100g": {"energy_kcal": 118.0, "protein_g": 24.6, "fat_g": 1.9, "carbohydrates_g": 0.6, "sodium_mg": 44.8},
        "category": "禽肉类及其制品-鸡",
    },
    {
        "name": "豆腐（代表值）",
        "per_100g": {"energy_kcal": 84.0, "protein_g": 6.6, "fat_g": 5.3, "carbohydrates_g": 3.4, "sodium_mg": 7.2},
        "category": "干豆类及其制品-大豆",
    },
]

FOODWAKE_ENTRIES: list[dict[str, Any]] = [
    {
        "name": "乌冬面",
        "per_100g": {"energy_kcal": 140.0, "protein_g": 3.6, "fat_g": 0.4, "carbohydrates_g": 29.0, "sodium_mg": 380.0},
        "category": "谷类",
    },
]

USDA_ENTRIES: list[dict[str, Any]] = [
    {
        "name": "Chicken breast, stewed, skin eaten",
        "per_100g": {"energy_kcal": 181.0, "protein_g": 27.4, "fat_g": 7.4, "carbohydrates_g": 0.0, "sodium_mg": 63.0},
        "data_type": "survey_fndds_food",
    },
    {
        "name": "Chicken breast, roll, oven-roasted",
        "per_100g": {"energy_kcal": 134.0, "protein_g": 22.0, "fat_g": 4.0, "carbohydrates_g": 2.0, "sodium_mg": 700.0},
        "data_type": "sr_legacy_food",
    },
]


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    _write(tmp_path / "cfct.json", CFCT_ENTRIES)
    _write(tmp_path / "foodwake.json", FOODWAKE_ENTRIES)
    _write(tmp_path / "usda-subset.json", USDA_ENTRIES)
    # brand-library.json 故意**不创建** —— 种子由 seed_provider 提供（真实链路就是这样）
    return tmp_path


def _engine(tmp_path: Path, data_dir: Path, **kwargs: Any) -> FoodLookup:
    fetcher = kwargs.pop("off_fetcher", None)
    return FoodLookup(
        data_dir,
        seed_provider=lambda: kwargs.pop("_seed", BRAND_SEED),
        off_fetcher=fetcher or kwargs.pop("_fetcher_default", lambda name: None),
        **kwargs,
    )


@pytest.fixture()
def engine(tmp_path: Path, data_dir: Path) -> FoodLookup:
    calls: list[str] = []

    def fetcher(name: str) -> dict[str, Any] | None:
        calls.append(name)
        if name == "椰树椰汁":
            return {
                "name": "椰树椰汁",
                "per_100g": {"energy_kcal": 60.0, "protein_g": 0.5, "fat_g": 2.6, "carbohydrates_g": 8.0, "sodium_mg": 40.0},
                "evidence": "open_food_facts",
                "origin": SOURCE_OFF,
            }
        return None

    lookup = _engine(tmp_path, data_dir, off_fetcher=fetcher)
    lookup.off_calls = calls  # type: ignore[attr-defined]
    return lookup


# --------------------------------------------------------------------------------------
# 名称归一化
# --------------------------------------------------------------------------------------


def test_normalize_and_strip_brackets() -> None:
    assert normalize_name("  米饭 ") == "米饭"
    assert normalize_name("ＯＡＴ　Ｍｉｌｋ") == "oatmilk"
    assert strip_brackets("米饭（蒸，代表值）") == "米饭"
    assert strip_brackets("纯牛奶（全脂，光明牌）") == "纯牛奶"
    assert strip_brackets("香蕉［甘蕉］") == "香蕉"


# --------------------------------------------------------------------------------------
# 四层优先级
# --------------------------------------------------------------------------------------


def test_layer1_brand_wins_over_official_tables(engine: FoodLookup) -> None:
    result = engine.lookup("米饭")
    assert result is not None
    assert result.source == SOURCE_BRAND
    assert result.matched_name == "米饭"
    assert result.per_100g["energy_kcal"] == 116.0
    assert result.serving_g == 150.0
    assert result.serving_source == "library"


def test_layer1_keyword_and_alias_match(engine: FoodLookup) -> None:
    # 「菲诺的椰乳」走品牌关键词；「椰乳」也算菲诺厚椰乳（用户自己的叫法）
    for probe in ("菲诺的椰乳", "椰乳", "厚椰乳"):
        result = engine.lookup(probe)
        assert result is not None, probe
        assert result.source == SOURCE_BRAND, probe
        assert result.matched_name == "菲诺厚椰乳"


def test_layer2_cfct_after_brand_miss(engine: FoodLookup) -> None:
    result = engine.lookup("鸡胸肉")  # 品牌库里没有 → 落到中国食物成分表
    assert result is not None
    assert result.source == SOURCE_CFCT
    assert result.matched_name == "鸡胸脯肉"
    assert result.per_100g["energy_kcal"] == 118.0


def test_layer2_bracket_stripped_exact(engine: FoodLookup) -> None:
    result = engine.lookup("豆腐")
    assert result is not None
    assert result.source == SOURCE_CFCT
    assert result.matched_name == "豆腐（代表值）"


def test_layer2_foodwake_before_usda(engine: FoodLookup) -> None:
    result = engine.lookup("乌冬面")
    assert result is not None
    assert result.source == SOURCE_FOODWAKE


def test_layer3_usda_for_english(engine: FoodLookup) -> None:
    result = engine.lookup("chicken breast")
    assert result is not None
    assert result.source == SOURCE_USDA
    # 「另一道菜」词（roll）降权，选中「同族 + 熟食」的那条
    assert result.matched_name == "Chicken breast, stewed, skin eaten"
    assert result.confidence == CONFIDENCE_MEDIUM


def test_unknown_name_hits_nothing(engine: FoodLookup) -> None:
    assert engine.lookup("莫须有的食物名") is None
    assert engine.lookup("") is None


# --------------------------------------------------------------------------------------
# 层④ OFF + 写回缓存（「后台记忆」）
# --------------------------------------------------------------------------------------


def test_off_lookup_writes_back_into_brand_library(engine: FoodLookup, data_dir: Path) -> None:
    first = engine.lookup("椰树椰汁")
    assert first is not None
    assert first.source == SOURCE_OFF
    assert first.confidence == CONFIDENCE_MEDIUM
    assert engine.off_calls == ["椰树椰汁"]  # type: ignore[attr-defined]

    library_file = data_dir / "brand-library.json"
    assert library_file.is_file(), "OFF 查到的结果必须写回层①"
    stored = json.loads(library_file.read_text(encoding="utf-8"))["entries"]
    assert [entry["name"] for entry in stored] == ["椰树椰汁"]

    # 第二次：直接命中层①，不再联网
    cached = FoodLookup(
        data_dir,
        seed_provider=lambda: BRAND_SEED,
        off_fetcher=lambda name: pytest.fail("第二次不该联网"),
    ).lookup("椰树椰汁")
    assert cached is not None
    assert cached.source == SOURCE_OFF  # 来源仍是 OFF（可追溯），但已缓存
    assert cached.cached is True
    assert cached.per_100g["energy_kcal"] == 60.0


def test_off_disabled_and_failure_are_silent(data_dir: Path) -> None:
    disabled = FoodLookup(data_dir, seed_provider=lambda: BRAND_SEED, off_enabled=False)
    assert disabled.lookup("椰树椰汁") is None  # 关掉联网层：查不到就是查不到

    def boom(_name: str) -> dict[str, Any] | None:
        raise RuntimeError("网络炸了")

    broken = FoodLookup(data_dir, seed_provider=lambda: BRAND_SEED, off_fetcher=boom)
    assert broken.lookup("椰树椰汁") is None  # 抓取抛错 → 当作没命中，不往上冒


# --------------------------------------------------------------------------------------
# 复合词与拆词
# --------------------------------------------------------------------------------------


def test_composite_recipe_by_name(engine: FoodLookup) -> None:
    """「生椰拿铁」没报克重也能算：层① 配方（200g 椰乳 + 30ml 浓缩）。"""
    result = engine.lookup("生椰拿铁")
    assert result is not None
    assert result.source == SOURCE_BRAND
    assert result.match_kind == "recipe"
    assert result.matched_name == "菲诺厚椰乳+意式浓缩"
    assert result.serving_g == 230.0
    assert result.serving_source == "recipe"
    # (93.8×200 + 13×30) / 230 = 83.26 kcal/100g
    assert result.per_100g["energy_kcal"] == pytest.approx(83.26, abs=0.01)
    assert result.scale(230.0)["energy_kcal"] == pytest.approx(191.5, abs=0.1)


def test_decompose_compound_without_recipe(engine: FoodLookup) -> None:
    """没写死配方的复合词：用层① 词表做最长匹配切分（椰乳 + 浓缩）。"""
    result = engine.lookup("椰乳浓缩")
    assert result is not None
    assert result.match_kind == "recipe"
    assert set(result.matched_name.split("+")) == {"菲诺厚椰乳", "意式浓缩"}


def test_decompose_refuses_random_long_words(engine: FoodLookup) -> None:
    """切不干净就不拆：宁可退回「子串关键词」的弱命中，也不硬凑一个配方。"""
    assert engine._decompose("椰乳拿铁莫须有") is None
    result = engine.lookup("椰乳拿铁莫须有")
    assert result is not None
    assert result.match_kind != "recipe"  # 不是配方组合
    assert result.confidence == CONFIDENCE_MEDIUM  # 弱命中：只做参考，不覆盖模型估值


# --------------------------------------------------------------------------------------
# 降级容错
# --------------------------------------------------------------------------------------


def test_missing_or_broken_data_files_degrade_silently(tmp_path: Path) -> None:
    empty = tmp_path / "nope"
    engine = FoodLookup(empty, seed_provider=lambda: BRAND_SEED, off_enabled=False)
    # 种子还在，所以品牌层命中；其余三层缺文件 → 当不存在
    assert engine.lookup("椰乳") is not None
    assert engine.lookup("鸡胸肉") is None
    assert engine.lookup("chicken breast") is None

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "cfct.json").write_text("{ 这不是 JSON", encoding="utf-8")
    ok = FoodLookup(broken, seed_provider=lambda: [], off_enabled=False)
    assert ok.lookup("米饭") is None


def test_entries_without_energy_are_skipped(tmp_path: Path) -> None:
    _write(
        tmp_path / "cfct.json",
        [{"name": "怪条目", "per_100g": {"protein_g": 3.0}}, {"name": "正常", "per_100g": {"energy_kcal": 10.0}}],
    )
    engine = FoodLookup(tmp_path, seed_provider=lambda: [], off_enabled=False)
    assert engine.lookup("怪条目") is None
    assert engine.lookup("正常") is not None


# --------------------------------------------------------------------------------------
# 数值换算与份量来源
# --------------------------------------------------------------------------------------


def test_scale_uses_per_100g(engine: FoodLookup) -> None:
    result = engine.lookup("菲诺厚椰乳")
    assert result is not None
    values = result.scale(140.0)
    assert values["energy_kcal"] == pytest.approx(131.32, abs=0.01)
    assert values["fat_g"] == pytest.approx(9.8, abs=0.01)


def test_serving_hint_filled_when_library_has_none(data_dir: Path) -> None:
    _write(
        data_dir / "cfct.json",
        [{"name": "油条", "per_100g": {"energy_kcal": 388.0, "protein_g": 6.9, "fat_g": 17.6, "carbohydrates_g": 51.0, "sodium_mg": 585.0}}],
    )
    engine = FoodLookup(data_dir, seed_provider=lambda: [], off_enabled=False)
    result = engine.lookup("油条")
    assert result is not None
    assert result.serving_g == 60.0  # 常见份量经验表
    assert result.serving_source == "hint"


def test_source_labels_for_frontend() -> None:
    assert source_label(SOURCE_CFCT) == "中国食物成分表"
    assert source_label(SOURCE_OFF) == "Open Food Facts"
    assert source_tag(SOURCE_CFCT) == "（中国食物成分表）"
    assert source_tag(SOURCE_OFF, cached=True) == "（Open Food Facts，已缓存）"


# --------------------------------------------------------------------------------------
# 「念一次存一次」：营养口令
# --------------------------------------------------------------------------------------


def test_parse_nutrition_label_text() -> None:
    parsed = parse_nutrition_label_text(
        "菲诺厚椰乳 每100克 94千卡 蛋白质1克 脂肪7克 碳水6.8克 钠82毫克"
    )
    assert parsed is not None
    assert parsed["item"] == "菲诺厚椰乳"
    assert parsed["per_100g"] == {
        "energy_kcal": 94.0,
        "protein_g": 1.0,
        "fat_g": 7.0,
        "carbohydrates_g": 6.8,
        "sodium_mg": 82.0,
    }
    # 千焦要换算成千卡；「一份150克」当份量
    parsed = parse_nutrition_label_text("酸奶，每100克能量418千焦，蛋白质2.5克，一份150克")
    assert parsed is not None
    assert parsed["per_100g"]["energy_kcal"] == pytest.approx(99.9, abs=0.2)
    assert parsed["serving_g"] == 150.0
    # 报不出千卡/千焦 → 不猜
    assert parse_nutrition_label_text("菲诺厚椰乳") is None


def test_nutrition_voice_command_capture() -> None:
    from life_logger.voice_triggers import scan_voice_triggers

    captures = scan_voice_triggers(
        "记录营养 菲诺厚椰乳 每100克 94千卡 蛋白质1克 脂肪7克 碳水6.8克 钠82毫克",
        timestamp="2026-10-13T18:00:00",
    )
    assert len(captures) == 1
    capture = captures[0]
    assert capture.trigger_type == "nutrition_label"
    assert capture.metadata["item"] == "菲诺厚椰乳"
    assert capture.metadata["per_100g"]["energy_kcal"] == 94.0
    assert "菲诺厚椰乳" in capture.extracted_content

    # 既有口令不受影响
    assert scan_voice_triggers("记一下：周五下午五点去修车")[0].trigger_type == "todo"
    assert scan_voice_triggers("添加一个习惯 每天喝两升水")[0].trigger_type == "habit"
    assert scan_voice_triggers("午饭花了32块")[0].trigger_type == "expense"


def test_apply_nutrition_captures_writes_brand_library(data_dir: Path) -> None:
    from life_logger.voice_triggers import scan_voice_triggers

    engine = FoodLookup(data_dir, seed_provider=lambda: BRAND_SEED, off_enabled=False)
    captures = [
        capture.to_dict()
        for capture in scan_voice_triggers(
            "记录营养 刚买的品牌酸奶 每100克 99千卡 蛋白质3克 一份120克"
        )
    ]
    outcomes = apply_nutrition_captures(engine, captures)
    assert outcomes and outcomes[0]["applied"] is True

    # 写回后立刻能查到（新条目优先级高于种子）
    found = FoodLookup(data_dir, seed_provider=lambda: BRAND_SEED, off_enabled=False).lookup("刚买的品牌酸奶")
    assert found is not None
    assert found.source == SOURCE_BRAND
    assert found.per_100g["energy_kcal"] == 99.0
    assert found.serving_g == 120.0

    # 非营养口令的 capture 一律忽略
    assert apply_nutrition_captures(engine, [{"trigger_type": "todo", "metadata": {}}]) == []


def test_real_openfoodfacts_parser_handles_missing_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """``fetch_openfoodfacts`` 无网时必须返回 None（静默降级），且能解析 OFF 的字段。"""

    class _Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def read(self) -> bytes:
            return self._payload

    payload = json.dumps(
        {
            "products": [
                {
                    "code": "6900000000001",
                    "product_name_zh": "椰树椰汁",
                    "nutriments": {
                        "energy-kj_100g": 251.0,
                        "proteins_100g": 0.5,
                        "fat_100g": 2.6,
                        "carbohydrates_100g": 8.0,
                        "sodium_100g": 0.04,
                    },
                    "serving_quantity": 245,
                }
            ]
        }
    ).encode("utf-8")

    entry = fetch_openfoodfacts("椰树椰汁", opener=lambda *_a, **_k: _Response(payload))
    assert entry is not None
    assert entry["name"] == "椰树椰汁"
    assert entry["per_100g"]["energy_kcal"] == pytest.approx(60.0, abs=0.1)  # 251kJ → 60kcal
    assert entry["per_100g"]["sodium_mg"] == pytest.approx(40.0, abs=0.1)  # 0.04g → 40mg
    assert entry["serving_g"] == 245.0
    assert entry["origin"] == SOURCE_OFF
    assert "6900000000001" in entry["note"]

    def boom(*_args: object, **_kwargs: object) -> Any:
        raise OSError("no network")

    assert fetch_openfoodfacts("椰树椰汁", opener=boom) is None
    assert fetch_openfoodfacts("") is None


# --------------------------------------------------------------------------------------
# 接进 food_nutrition：library_math 这条路
# --------------------------------------------------------------------------------------


def _food_event(item: str, kcal: int | None = 220) -> dict[str, Any]:
    return {
        "event_id": "evt_1",
        "categories": ["food"],
        "title": f"吃了{item}",
        "time": {"relative_seconds": 100.0},
        "data": {
            "food": {
                "meal": "snack",
                "item": item,
                "portion": "1杯",
                "estimated_calories": kcal,
                "nutrients": {"energy_kcal": kcal, "protein_g": 5.0, "fat_g": 9.0, "carbohydrates_g": 30.0, "sodium_mg": 120},
            }
        },
        "evidence": {"segment_id": "seg_1", "start_offset_seconds": 100.0, "quote": f"吃了{item}"},
    }


def test_food_nutrition_uses_library_for_name_only_event(engine: FoodLookup) -> None:
    event = _food_event("生椰拿铁")
    summary = apply_food_nutrition_calibration(
        [event], [{"segment_id": "seg_1", "text": "喝了个生椰拿铁。", "start_offset_seconds": 100.0}], lookup=engine
    )
    food = event["data"]["food"]
    assert summary["library_math"] == 1
    assert food["estimate_basis"] == BASIS_LIBRARY_MATH
    assert food["estimated_calories"] == 192  # 配方标准份，不再是模型瞎估的 220
    assert food["nutrition_source"] == SOURCE_BRAND
    assert food["nutrition_source_label"] == "自建品牌库"
    assert food["nutrition_matched_name"] == "菲诺厚椰乳+意式浓缩"
    assert food["nutrition_serving_source"] == "recipe"
    assert food["item"] == "生椰拿铁"  # 原结构未动
    assert food["meal"] == "snack"
    assert food["portion"] == "1杯"


def test_food_nutrition_only_annotates_on_weak_match(data_dir: Path) -> None:
    """弱命中（low）不许覆盖模型估值，只挂参考块。"""
    _write(
        data_dir / "cfct.json",
        [{"name": "某个很长的食物名字", "per_100g": {"energy_kcal": 100.0, "protein_g": 1.0, "fat_g": 1.0, "carbohydrates_g": 1.0, "sodium_mg": 1.0}}],
    )
    engine = FoodLookup(data_dir, seed_provider=lambda: [], off_enabled=False)
    event = _food_event("食物名")  # 只占名字一角 → 包含命中但长度差太远 → low
    summary = apply_food_nutrition_calibration([event], [], lookup=engine)
    food = event["data"]["food"]
    assert summary["library_reference"] == 1
    assert summary["library_math"] == 0
    assert food["estimated_calories"] == 220  # 模型估值保留
    assert food["estimate_basis"] == BASIS_STANDARD_SERVING
    assert food["nutrition_reference"]["matched_name"] == "某个很长的食物名字"


def test_food_nutrition_uses_spoken_grams_for_known_name(engine: FoodLookup) -> None:
    """配料表里没有的食物，但原话报了克重（「两百克乌冬面」）→ 用原话克重，不用经验份量。"""
    event = _food_event("乌冬面", kcal=None)
    records = [{"segment_id": "seg_1", "text": "两百克乌冬面。", "start_offset_seconds": 100.0}]
    summary = apply_food_nutrition_calibration([event], records, lookup=engine)
    food = event["data"]["food"]
    assert summary["library_math"] == 1
    assert food["nutrition_serving_source"] == "spoken"
    assert food["weight_g"] == 200
    assert food["estimated_calories"] == 280  # 140 kcal/100g × 200g
    assert food["nutrition_source"] == SOURCE_FOODWAKE


def test_food_nutrition_keeps_standard_serving_when_lookup_misses(engine: FoodLookup) -> None:
    event = _food_event("莫须有的食物名", kcal=333)
    summary = apply_food_nutrition_calibration([event], [], lookup=engine)
    food = event["data"]["food"]
    assert summary["standard_serving"] == 1
    assert food["estimated_calories"] == 333
    assert food["estimate_basis"] == BASIS_STANDARD_SERVING
    assert food["estimate_confidence"] == CONFIDENCE_LOW


def test_food_nutrition_survives_broken_lookup() -> None:
    """查询层整个坏掉时，校准必须退回旧行为（静默降级）。"""

    class _Boom:
        def lookup(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("查询层炸了")

    event = _food_event("生椰拿铁")
    apply_food_nutrition_calibration([event], [], lookup=_Boom())  # type: ignore[arg-type]
    food = event["data"]["food"]
    assert food["estimated_calories"] == 220
    assert food["estimate_basis"] == BASIS_STANDARD_SERVING


# --------------------------------------------------------------------------------------
# 真实数据（本机已编译的库）—— 文件不在就跳过，保证换机器也不红
# --------------------------------------------------------------------------------------

REAL_DATA_DIR = Path(__file__).resolve().parents[1] / "output" / "local-web" / "food-data"
_real_data = pytest.mark.skipif(
    not (REAL_DATA_DIR / "cfct.json").is_file(),
    reason="本机还没跑 scripts/build_food_data.py（真实数据缺失）",
)


@_real_data
def test_real_cfct_entries_hit_the_second_layer() -> None:
    # 没有品牌种子 → 直接看第②层（中国食物成分表 / foodwake）在真数据上能不能查出来
    engine = FoodLookup(REAL_DATA_DIR, seed_provider=lambda: [], off_enabled=False)
    rice = engine.lookup("米饭")
    assert rice is not None and rice.source == SOURCE_CFCT
    assert rice.matched_name == "米饭（蒸，代表值）"
    assert rice.per_100g["energy_kcal"] == 116.0
    chicken = engine.lookup("鸡胸肉")
    assert chicken is not None and chicken.source == SOURCE_CFCT
    assert chicken.matched_name == "鸡胸脯肉"
    tofu = engine.lookup("豆腐")
    assert tofu is not None and tofu.source == SOURCE_CFCT
    salmon = engine.lookup("三文鱼")
    assert salmon is not None and salmon.source == SOURCE_FOODWAKE


@_real_data
def test_real_brand_seed_keeps_priority_over_official_tables() -> None:
    """26 条硬编码表仍是最高优先（现在它是层① 的种子条目）。"""
    from life_logger.food_nutrition import INGREDIENTS

    engine = FoodLookup(REAL_DATA_DIR, seed_provider=lambda: list(INGREDIENTS), off_enabled=False)
    rice = engine.lookup("米饭")
    assert rice is not None and rice.source == SOURCE_BRAND
    assert rice.serving_g == 150.0  # 自建表的「一碗 150g」
    camellia = engine.lookup("菲诺厚椰乳")
    assert camellia is not None and camellia.source == SOURCE_BRAND
    assert camellia.per_100g["energy_kcal"] == 93.8
    assert camellia.evidence == "user_verified"


@_real_data
def test_real_usda_subset_is_usable_for_english() -> None:
    engine = FoodLookup(REAL_DATA_DIR, seed_provider=lambda: [], off_enabled=False)
    result = engine.lookup("chicken breast")
    assert result is not None and result.source == SOURCE_USDA
    assert result.per_100g["energy_kcal"] > 100


def test_source_constants_stay_in_sync() -> None:
    """层名常量与前端标签一一对应（加层时别忘了补 label）。"""
    for source in (SOURCE_BRAND, SOURCE_CFCT, SOURCE_FOODWAKE, SOURCE_USDA, SOURCE_OFF):
        assert source_label(source) != source
    assert CONFIDENCE_HIGH == "high"
