"""Read-only nutrition API and the removed Wechat write route; all data is temporary."""
from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request

import pytest

from life_logger import food_lookup
from life_logger.daily_cognitive_session import build_nutrition_ledger
from life_logger.event_view import build_event_view
from life_logger.food_nutrition import INGREDIENTS
from life_logger.local_web import JobStore, create_server

DAY = "2026-11-06"


@pytest.fixture
def api(tmp_path, monkeypatch):
    for feature in ("WATCH_LOOP", "VIP_WARMUP", "REMINDERS", "AUTO_INGEST"):
        monkeypatch.setenv("LIFE_LOGGER_DISABLE_" + feature, "1")
    library = tmp_path / "food"
    library.mkdir()
    (library / "cfct.json").write_text(json.dumps({"entries": [
        {"name": "土豆", "per_100g": {"energy_kcal": 77, "protein_g": 2, "carbohydrates_g": 17.2, "fat_g": 0.1, "sodium_mg": 2.7}},
        {"name": "青椒", "per_100g": {"energy_kcal": 23, "protein_g": 1.4, "carbohydrates_g": 5.4, "fat_g": 0.3, "sodium_mg": 3.3}},
    ]}))
    engine = food_lookup.FoodLookup(library, seed_provider=lambda: INGREDIENTS, off_enabled=True,
        off_fetcher=lambda _: pytest.fail("Nutrition API must not call online food lookup"))
    monkeypatch.setattr(food_lookup, "_DEFAULT_LOOKUP", engine)
    store = JobStore(tmp_path / "local-web", recover=False)
    config = tmp_path / "test-config.json"
    config.write_text(json.dumps({"activity_timeline": {"enabled": False}, "input_context_capture": {"enabled": False}}))
    token = "nutrition-test-only-access"
    server = create_server(host="127.0.0.1", port=0, store=store, access_token=token, config_path=config)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    def request(method, path, value=None, *, auth=True, csrf=True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + token
        if csrf:
            headers["X-Life-Logger-CSRF"] = "1"
            headers["Origin"] = base
        data = json.dumps(value, ensure_ascii=False).encode() if value is not None else None
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as reply:
                return reply.status, json.load(reply)
        except urllib.error.HTTPError as reply:
            return reply.code, json.load(reply)
    yield store, request
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    assert not thread.is_alive()


def day(api):
    return api[1]("GET", "/api/nutrition/day?date=" + DAY)


def fingerprint(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def food_event(name="土豆", *, calories=77, weight=100, nutrients=None, event_id="manual_evt_fixture_1"):
    return {"event_id": event_id, "artifact_path": f"{DAY}/events.json", "categories": ["food"],
        "activity_type": "meal", "title": name,
        "time": {"absolute": DAY + "T12:00:00+08:00", "precision": "exact", "relative_seconds": None},
        "data": {"food": {"item": name, "weight_g": weight, "meal": "lunch", "estimated_calories": calories,
            "nutrients": nutrients if nutrients is not None else {"protein_g": 2.0, "carbohydrates_g": 17.2, "fat_g": 0.1, "sodium_mg": 2.7}}},
        "source_type": "manual", "capture_mode": "active", "confidence": 1.0,
        "evidence": {"quote": "中午吃了" + name, "source_id": event_id, "source_label": "self"},
        "review_status": "confirmed"}


def seed_events(api, events):
    store = api[0]
    path = store.manual_events_path("job-fixture")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "0.1", "events": events}, ensure_ascii=False))
    artifact = store.job_path("job-fixture") / DAY / "events.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"schema_version": "0.1", "events": []}))


def test_get_reads_ordinary_events_from_canonical_ledger_without_writes(api):
    seed_events(api, [food_event()])
    before = fingerprint(api[0].root.parent)
    status, result = day(api)
    assert status == 200 and result["ok"] is True and result["has_records"] is True
    view = build_event_view(api[0].root, date_from=DAY, date_to=DAY)
    ledger = build_nutrition_ledger(view["events"], day=DAY, activities=[], web_root=api[0].root)
    assert result["totals"] == ledger["totals"]
    # 2026-11-07 用户决定：不知重量就不算（本 fixture 的原话「中午吃了土豆」没有克数）→ 不计热，只留候选
    assert result["totals"]["calories_kcal"] is None
    assert ledger["unweighed_candidate_kcal"] == pytest.approx(77.0)
    assert result["meals"]["lunch"][0]["event_id"] == "manual_evt_fixture_1"
    assert fingerprint(api[0].root.parent) == before
    assert not list(api[0].jobs_dir.glob("job-nutrition-*"))


def test_post_statements_is_404_without_any_write(api):
    before = fingerprint(api[0].root.parent)
    status, _ = api[1]("POST", "/api/nutrition/statements", {"date": DAY, "source": "wechat", "items": []})
    assert status == 404
    assert fingerprint(api[0].root.parent) == before


def test_missing_auth_is_401_and_does_not_write(api):
    before = fingerprint(api[0].root)
    assert api[1]("GET", "/api/nutrition/day?date=" + DAY, auth=False)[0] == 401
    assert fingerprint(api[0].root) == before


def test_unknown_food_preserves_null_nutrition_and_coverage(api):
    seed_events(api, [food_event("未知测试食品ZXQ987", calories=None, nutrients={})])
    status, snapshot = day(api)
    assert status == 200 and snapshot["has_records"] is True
    assert snapshot["unknown_nutrition_count"] == 1
    assert snapshot["coverage"]["calories_unknown"] == 1
    assert all(value is None for value in snapshot["totals"].values())
    assert snapshot["meals"]["lunch"][0]["basis"] == "unknown"


def test_empty_day_returns_null_totals_and_get_is_read_only(api):
    before = fingerprint(api[0].root.parent)
    status, snapshot = api[1]("GET", "/api/nutrition/day?date=" + DAY, csrf=False)
    assert status == 200 and snapshot["has_records"] is False
    assert snapshot["totals"] is None
    assert snapshot["unknown_nutrition_count"] == 0
    assert fingerprint(api[0].root.parent) == before


@pytest.mark.parametrize("raw", [b'{"events": broken', b'{"events": {}}'])
def test_corrupt_manual_file_is_not_overwritten(api, raw):
    path = api[0].manual_events_path("job-nutrition-20261106")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    before = fingerprint(api[0].root.parent)
    assert day(api)[0] == 503
    assert fingerprint(api[0].root.parent) == before
    assert path.read_bytes() == raw


@pytest.mark.parametrize("query", ["", "?date=../bad", "?date=2026-02-30"])
def test_invalid_get_date_is_400_and_read_only(api, query):
    before = fingerprint(api[0].root.parent)
    assert api[1]("GET", "/api/nutrition/day" + query)[0] == 400
    assert fingerprint(api[0].root.parent) == before


# ─────────────────────────────────────────────────────────────────────────────
# [2026-11-07] /api/daily 营养 totals 与 /api/nutrition/day 口径统一：
#   没有已知值 → null（不写 0）；部分已知 → 只汇总已知项，缺项 null。
# ─────────────────────────────────────────────────────────────────────────────
def test_daily_totals_are_null_when_day_has_no_records(api):
    """没有记录 → 每一项都是 null（不是 0），与 /api/nutrition/day 的 totals=None 同义。"""
    status, snapshot = day(api)
    assert status == 200 and snapshot["has_records"] is False
    assert snapshot["totals"] is None

    view = build_event_view(api[0].root, date_from=DAY, date_to=DAY)
    ledger = build_nutrition_ledger(view["events"], day=DAY, activities=[], web_root=api[0].root)
    assert ledger["has_records"] is False
    assert set(ledger["totals"]) == {"calories_kcal", "carbohydrates_g", "protein_g", "fat_g", "sodium_mg"}
    assert all(value is None for value in ledger["totals"].values()), \
        f"无记录时不得出现 0：{ledger['totals']}"
    assert ledger["estimated_share"] is None


def test_daily_totals_only_sum_known_items_and_keep_missing_as_null(api):
    """一条已知 + 一条未知 → 只汇总已知那条；未知项不参与，计数里标出来。"""
    seed_events(api, [
        food_event("土豆", calories=77, weight=100),                       # 已知
        food_event("未知测试食品ZXQ987", calories=None, nutrients={},      # 未知
                   event_id="manual_evt_fixture_unknown"),
    ])
    view = build_event_view(api[0].root, date_from=DAY, date_to=DAY)
    ledger = build_nutrition_ledger(view["events"], day=DAY, activities=[], web_root=api[0].root)
    assert ledger["consumed_record_count"] == 2
    # 2026-11-07 用户决定：不知重量就不算：两条原话都没有克数 → 都不计热，合计为 None（候选各自保留）
    assert ledger["totals"]["calories_kcal"] is None, "没重量的条目不能计入合计"
    assert ledger["totals"]["carbohydrates_g"] is None
    assert ledger["totals"]["protein_g"] is None
    assert ledger["totals"]["fat_g"] is None
    assert ledger["unweighed_candidate_kcal"] == pytest.approx(77.0)
    # 2026-11-07 用户决定：两条现在都算「未知」（没有原话重量），所以未知计数 = 2
    assert ledger["totals_unknown_counts"]["calories_kcal"] == 2
    assert ledger["totals_complete"] is False


def test_daily_totals_match_nutrition_day_for_same_day(api):
    """同一天：/api/daily 的 totals 与 /api/nutrition/day 的 totals 必须一致。"""
    seed_events(api, [food_event("土豆", calories=77, weight=100)])
    status, snapshot = day(api)
    assert status == 200
    view = build_event_view(api[0].root, date_from=DAY, date_to=DAY)
    ledger = build_nutrition_ledger(view["events"], day=DAY, activities=[], web_root=api[0].root)
    # 2026-11-07 用户决定：不知重量就不算：两边都应为 None（口径一致）
    assert snapshot["totals"]["calories_kcal"] is None
    assert ledger["totals"]["calories_kcal"] is None
    for key in ("carbohydrates_g", "protein_g", "fat_g"):
        assert snapshot["totals"][key] == ledger["totals"][key]
