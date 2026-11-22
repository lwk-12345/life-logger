"""OFF 补查守卫：单字不联网、无关商品不写回、不写单字关键词、只读不脏数据。"""
from __future__ import annotations

import hashlib
from pathlib import Path


from life_logger import food_lookup as fl

ROOT = Path(__file__).resolve().parents[1]
FOOD_DATA = ROOT / "output" / "local-web" / "food-data"


def _fetcher(product, calls=None):
    def fetch(query):
        if calls is not None:
            calls.append(query)
        return product
    return fetch


def _lib(tmp_path, product, calls=None, **kw):
    return fl.FoodLookup(data_dir=tmp_path, off_fetcher=_fetcher(product, calls), **kw)


CHEESE = {"name": "原味白奶酪", "per_100g": {"energy_kcal": 80.6}, "keywords": ["面", "水", "粉"],
          "origin": "off", "evidence": "open_food_facts"}
BEER = {"name": "双柚风味无醇拉格啤酒", "per_100g": {"energy_kcal": 67.0},
        "keywords": ["啤酒"], "origin": "off", "evidence": "open_food_facts"}


def test_single_char_query_never_calls_off(tmp_path):
    calls = []
    lib = _lib(tmp_path, CHEESE, calls)
    assert lib.lookup("虾") is None
    assert lib.lookup("鱼") is None
    assert calls == [], f"单字查询不该联网：{calls}"


def test_unrelated_product_is_rejected_and_not_written(tmp_path):
    lib = _lib(tmp_path, CHEESE)
    assert lib.lookup("虾皮") is None
    assert lib.brand.entries() == [], "无关商品不许写回品牌库"


def test_related_product_is_accepted_and_written(tmp_path):
    lib = _lib(tmp_path, BEER)
    hit = lib.lookup("无醇啤酒")
    assert hit is not None and hit.source == fl.SOURCE_OFF
    names = [e["name"] for e in lib.brand.entries()]
    assert names == ["双柚风味无醇拉格啤酒"]


def test_single_char_keywords_are_never_written(tmp_path):
    product = dict(CHEESE, keywords=["面", "水", "奶", "奶酪"])
    lib = _lib(tmp_path, product)
    hit = lib.lookup("原味白奶酪")
    assert hit is not None
    entry = lib.brand.entries()[0]
    assert all(len(fl.normalize_name(k)) >= 2 for k in entry["keywords"]), entry["keywords"]
    assert "奶酪" in entry["keywords"]


def test_name_match_function():
    assert fl._off_name_matches("无醇啤酒", "双柚风味无醇拉格啤酒") is True
    assert fl._off_name_matches("原味白奶酪", "原味白奶酪") is True
    assert fl._off_name_matches("虾", "原味白奶酪") is False
    assert fl._off_name_matches("牛奶", "牛肉") is False
    assert fl._off_name_matches("包子", "烘烤混合坚果仁") is False


def test_off_disabled_under_pytest_by_default(monkeypatch):
    monkeypatch.delenv("LL_FOOD_OFF", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_off_guard_20261106.py::x")
    assert fl.FoodLookup(data_dir=Path("/tmp/nonexistent-food")).off_enabled is False
    monkeypatch.setenv("LL_FOOD_OFF", "1")
    assert fl.FoodLookup(data_dir=Path("/tmp/nonexistent-food")).off_enabled is True


def _digest_dir(path: Path) -> dict[str, str]:
    if not path.is_dir():
        return {}
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.glob("*.json"))}


def test_lookup_does_not_touch_real_food_data():
    """只读铁律：在 pytest 下跑一串 lookup，output/local-web/food-data 一个字节都不许变。"""
    before = _digest_dir(FOOD_DATA)
    for name in ("虾", "鱼", "粥", "粉", "羹", "面条", "米饭", "牛奶", "无醇啤酒"):
        fl.lookup_food(name)
    assert _digest_dir(FOOD_DATA) == before
