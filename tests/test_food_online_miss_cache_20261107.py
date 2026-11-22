"""联网层查不到的名字 7 天内不再联网（/api/daily 每次读都会查，曾因此读超时）。"""

from __future__ import annotations

from life_logger import food_lookup


def test_online_miss_is_remembered(tmp_path, monkeypatch) -> None:
    lookup = food_lookup.FoodLookup(data_dir=tmp_path, boohee_enabled=False)
    lookup.off_enabled = True
    calls: list[str] = []
    monkeypatch.setattr(lookup, "_from_off", lambda q: calls.append(q) or None)
    assert lookup.lookup("陈述自己但记录中未显示") is None
    assert lookup.lookup("陈述自己但记录中未显示") is None
    assert calls == ["陈述自己但记录中未显示"]


def test_expired_miss_is_retried(tmp_path, monkeypatch) -> None:
    lookup = food_lookup.FoodLookup(data_dir=tmp_path, boohee_enabled=False)
    lookup.off_enabled = True
    calls: list[str] = []
    monkeypatch.setattr(lookup, "_from_off", lambda q: calls.append(q) or None)
    lookup.lookup("某个查不到的东西")
    monkeypatch.setattr(food_lookup, "ONLINE_MISS_TTL_SECONDS", -1)
    lookup.lookup("某个查不到的东西")
    assert len(calls) == 2


def test_offline_only_lookup_does_not_record_miss(tmp_path) -> None:
    lookup = food_lookup.FoodLookup(data_dir=tmp_path, boohee_enabled=False)
    lookup.off_enabled = False
    assert lookup.lookup("某个查不到的东西") is None
    assert not (tmp_path / "online-misses.json").exists()
