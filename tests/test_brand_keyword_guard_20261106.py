"""品牌库单字关键词守卫：'面'/'水' 不能把任何条目认成原味白奶酪。"""
from pathlib import Path

import pytest

from life_logger.food_lookup import BrandFoodLibrary


@pytest.fixture()
def lib(tmp_path: Path) -> BrandFoodLibrary:
    path = tmp_path / "brand-library.json"
    path.write_text(
        '{"source":"brand","version":1,"entries":[{"name":"原味白奶酪",'
        '"per_100g":{"energy_kcal":80.6,"protein_g":7.3,"fat_g":12.0,"carbohydrates_g":6.1,"sodium_mg":100.0},'
        '"keywords":["面","水"],"evidence":"open_food_facts","origin":"off"}]}',
        encoding="utf-8",
    )
    return BrandFoodLibrary(path)


@pytest.mark.parametrize("query", ["面", "水", "汤", "饭"])
def test_single_char_query_does_not_match_brand(lib, query):
    assert lib.lookup(query) is None


@pytest.mark.parametrize("query", ["西北牛筋面", "绿豆汤水", "牛筋面", "汤水"])
def test_real_food_names_still_unmatched_by_bad_keywords(lib, query):
    """这些名字本来就不该匹配到奶酪；修复后行为保持一致（不是新增误伤）。"""
    assert lib.lookup(query) is None


def test_exact_name_still_matches(lib):
    hit = lib.lookup("原味白奶酪")
    assert hit is not None and hit.name == "原味白奶酪" and hit.match_kind == "exact"


def test_multi_char_alias_still_works(lib, tmp_path):
    path = tmp_path / "brand2.json"
    path.write_text(
        '{"source":"brand","version":1,"entries":[{"name":"双柚风味无醇拉格啤酒",'
        '"per_100g":{"energy_kcal":67,"protein_g":0,"fat_g":0,"carbohydrates_g":0,"sodium_mg":0},'
        '"keywords":["啤酒"],"origin":"off"}]}',
        encoding="utf-8",
    )
    hit = BrandFoodLibrary(path).lookup("啤酒")
    assert hit is not None and hit.name == "双柚风味无醇拉格啤酒"
