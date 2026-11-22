"""首页饮食条目“改名字”功能、本地查库与 v3 重跑兜底找回测试（2026-11-12）。"""
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from life_logger import day_facts
from life_logger import food_corrections as fc
from life_logger import food_lookup as fl
from life_logger import judgment_queue as jq
from life_logger.job_store import JobStore
from life_logger.web_http import _Handler as LocalWebHandler


def _jev_20261112_activities() -> list[dict]:
    """基于 2026-11-12 13:22 真实口述午餐（jev/2026-11-12.json）构造的 v3 活动样本。"""
    return [
        {
            "activity_id": "act_lunch_20261112_132206",
            "title": "午餐吃米饭与“热力宝”（转写待核）",
            "summary": "13:22 口述中午吃了米饭和“热力宝”（语音转写词存疑）。",
            "category": "饮食",
            "lifecycle": "live",
            "status": "completed",
            "fact_schema_version": 3,
            "occurred_at": "2026-11-12T13:22:06.103000+08:00",
            "spoken_at": "2026-11-12T13:22:06.103000+08:00",
            "spoken_end": "2026-11-12T13:22:28.910000+08:00",
            "items": [
                {
                    "item_id": "item_lunch_132206_01",
                    "action": "午餐吃米饭（未提克重）",
                    "fact_type": "food",
                    "status": "completed",
                    "occurred_at": "2026-11-12T13:22:06.103000+08:00",
                    "spoken_at": "2026-11-12T13:22:06.103000+08:00",
                    "food": {
                        "name": "米饭",
                        "meal": "lunch",
                        "weight_g": None,
                        "quantity": None,
                        "unit": None,
                        "calories_kcal": None,
                    },
                    "evidence": [
                        {
                            "source_id": "seg_20261112_132206_103000",
                            "timestamp": "2026-11-12T13:22:06.103000+08:00",
                            "quote": "中午吃了米饭，吃了热力宝。",
                            "source_label": "self",
                        }
                    ],
                },
                {
                    "item_id": "item_lunch_132206_02",
                    "action": "午餐吃“热力宝”（转写名称待核，未提份量）",
                    "fact_type": "food",
                    "status": "completed",
                    "occurred_at": "2026-11-12T13:22:06.103000+08:00",
                    "spoken_at": "2026-11-12T13:22:06.103000+08:00",
                    "food": {
                        "name": "热力宝（转写名称待核）",
                        "meal": "lunch",
                        "weight_g": None,
                        "quantity": None,
                        "unit": None,
                        "calories_kcal": None,
                    },
                    "evidence": [
                        {
                            "source_id": "seg_20261112_132206_103000",
                            "timestamp": "2026-11-12T13:22:06.103000+08:00",
                            "quote": "中午吃了米饭，吃了热力宝。",
                            "source_label": "self",
                        }
                    ],
                },
            ],
        }
    ]


def _write_jev_day(root: Path, day: str, activities: list[dict]) -> None:
    jev_dir = root / "activity-timeline" / "jev"
    jev_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "activity-timeline-v1",
        "date": day,
        "route": "jev",
        "status": "ready",
        "activities": activities,
    }
    (jev_dir / f"{day}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(day_facts, "PROJECT_ROOT", tmp_path)
    root = tmp_path / "output" / "local-web"
    root.mkdir(parents=True, exist_ok=True)
    _write_jev_day(root, "2026-11-12", _jev_20261112_activities())
    return root


def test_rename_strips_pending_suffix_without_network_and_undo_restores(temp_root, monkeypatch):
    # 严禁联网：如果触发 OpenFoodFacts / 薄荷健康 联网请求直接报错
    def _forbid_network(*args, **kwargs):
        raise AssertionError("改名查库禁止发起任何联网请求")

    monkeypatch.setattr(fl.FoodLookup, "_from_off", _forbid_network)
    monkeypatch.setattr(fl.FoodLookup, "_from_boohee", _forbid_network)
    # 隔离真实食物库：主目录 output/local-web/food-data/brand-library.json 里用户念过
    # “香辣土豆牛肉料理包”，不隔离的话本测试“本地库查不到”的前提会被真实数据打破。
    isolated_food_dir = Path(temp_root) / "isolated-food-data"
    isolated_food_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LIFE_LOGGER_FOOD_DATA", str(isolated_food_dir))
    monkeypatch.setattr(fl, "_DEFAULT_LOOKUP", None)

    event_id = "activity:2026-11-12:act_lunch_20261112_132206:item_lunch_132206_02"
    before = day_facts.domain_ledgers([], "2026-11-12", root=temp_root)["nutrition"]
    lunch_before = before["meals"]["lunch"]
    assert [it["item"] for it in lunch_before] == ["米饭", "热力宝（转写名称待核）"]

    # 改名为本地库未知的“土豆牛肉料理包”：去掉待核后缀，保持热量待定，不瞎估
    res = fc.rename_food(
        temp_root,
        date="2026-11-12",
        event_id=event_id,
        name="土豆牛肉料理包（转写名称待核）",
    )
    assert res["ok"] is True
    assert res["name"] == "土豆牛肉料理包"
    assert res["decision"]["name"] == "土豆牛肉料理包"
    assert res["decision"]["original_name"] == "热力宝"
    assert res["decision"]["raw_original_name"] == "热力宝（转写名称待核）"
    assert res["decision"]["quote"] == "中午吃了米饭，吃了热力宝。"
    assert res["decision"]["clock"] == "13:22"
    assert res["decision"]["decided_by"] == "user"

    # 原 jev/2026-11-12.json 文件绝不被修改
    jev_raw = json.loads((temp_root / "activity-timeline" / "jev" / "2026-11-12.json").read_text(encoding="utf-8"))
    assert jev_raw["activities"][0]["items"][1]["food"]["name"] == "热力宝（转写名称待核）"

    ledger = res["nutrition_ledger"]
    renamed_item = next(it for it in ledger["meals"]["lunch"] if it["event_id"] == event_id)
    assert renamed_item["item"] == "土豆牛肉料理包"
    assert renamed_item["renamed"] is True
    assert renamed_item["original_name"] == "热力宝"
    assert renamed_item["raw_original_name"] == "热力宝（转写名称待核）"
    assert renamed_item["calories_kcal"] is None
    assert (renamed_item.get("portion_estimate_candidate") or {}).get("calories_kcal") is None
    assert ledger["unmatched_renames"] == []

    uw_item = next(u for u in ledger["unweighed_items"] if u["event_id"] == event_id)
    assert uw_item["item"] == "土豆牛肉料理包"
    assert uw_item["renamed"] is True
    assert uw_item["original_name"] == "热力宝"

    # 撤销后恢复原名“热力宝（转写名称待核）”
    undo_res = fc.undo_food_rename(temp_root, date="2026-11-12", event_id=event_id)
    assert undo_res["ok"] is True
    after_undo = next(
        it for it in undo_res["nutrition_ledger"]["meals"]["lunch"] if it["event_id"] == event_id
    )
    assert after_undo["item"] == "热力宝（转写名称待核）"
    assert not after_undo.get("renamed")


def test_rename_to_local_library_food_updates_candidate_and_preserves_weight(temp_root):
    event_id = "activity:2026-11-12:act_lunch_20261112_132206:item_lunch_132206_02"

    # 1. 未填重量时改名为本地库已有的“鸡胸肉”（165 kcal/100g，常规份 100g）：候选热量与每100g成分表按新名字更新
    res = fc.rename_food(temp_root, date="2026-11-12", event_id=event_id, name="鸡胸肉")
    item = next(it for it in res["nutrition_ledger"]["meals"]["lunch"] if it["event_id"] == event_id)
    assert item["item"] == "鸡胸肉"
    assert item["calories_kcal"] is None
    cand = item["portion_estimate_candidate"]
    assert cand is not None
    assert cand["grams"] == 100
    assert cand["calories_kcal"] == 165.0
    assert item["nutrition_per_100g"]["energy_kcal"] == 165.0

    # 2. 用户之前已补过重量（如 200 克）后再改名：保留 200 克，并按新名字本地库自动算出热量
    fc.undo_food_rename(temp_root, date="2026-11-12", event_id=event_id)
    store = JobStore(temp_root, recover=False)
    jq.answer(
        store,
        {
            "source_id": f"cal|2026-11-12|{event_id}",
            "operation": "enter_weight",
            "op_id": "op_weight_before_rename",
            "grams": 200,
            "calories_kcal": 200,
        },
        flywheel_root=temp_root / "fw",
    )
    mid_ledger = fc._current_day_nutrition_ledger(temp_root, "2026-11-12")
    mid_item = next(it for it in mid_ledger["meals"]["lunch"] if it["item"] == "热力宝（转写名称待核）")
    assert mid_item["weight_g"] == 200.0
    assert mid_item["calories_kcal"] == 200.0

    # 此时把名字改为本地库中的“鸡胸肉”（165 kcal/100g），保留 200g 重量并重算为 330 kcal
    res_weighed = fc.rename_food(temp_root, date="2026-11-12", event_id=event_id, name="鸡胸肉")
    weighed_item = next(
        it for it in res_weighed["nutrition_ledger"]["meals"]["lunch"] if it["item"] == "鸡胸肉"
    )
    assert weighed_item["item"] == "鸡胸肉"
    assert weighed_item["renamed"] is True
    assert weighed_item["original_name"] == "热力宝"
    assert weighed_item["weight_g"] == 200.0
    assert weighed_item["calories_kcal"] == 330.0
    assert weighed_item["nutrients"]["protein_g"] == 62.0


def test_v3_rerun_id_change_matches_by_fallback_without_contaminating_sibling(temp_root):
    old_id = "activity:2026-11-12:act_lunch_20261112_132206:item_lunch_132206_02"
    fc.rename_food(temp_root, date="2026-11-12", event_id=old_id, name="土豆牛肉料理包")

    # 模拟 v3 重跑同一天：activity_id 和 item_id 全变了，顺序也调换了，且米饭共享同一段 quote
    base_items = _jev_20261112_activities()[0]["items"]
    rerun_item_food = {**base_items[1], "item_id": "item_rerun_mystery_01"}
    rerun_item_rice = {**base_items[0], "item_id": "item_rerun_rice_02"}
    rerun_activities = [
        {
            "activity_id": "act_20261112_lunch_rerun_99",
            "title": "重跑后的午餐记录",
            "category": "饮食",
            "lifecycle": "live",
            "status": "completed",
            "fact_schema_version": 3,
            "occurred_at": "2026-11-12T13:22:06.103000+08:00",
            "spoken_at": "2026-11-12T13:22:06.103000+08:00",
            "items": [rerun_item_food, rerun_item_rice],
        }
    ]
    _write_jev_day(temp_root, "2026-11-12", rerun_activities)

    ledger = day_facts.domain_ledgers([], "2026-11-12", root=temp_root)["nutrition"]
    assert ledger["unmatched_renames"] == []
    new_food_id = "activity:2026-11-12:act_20261112_lunch_rerun_99:item_rerun_mystery_01"
    new_rice_id = "activity:2026-11-12:act_20261112_lunch_rerun_99:item_rerun_rice_02"
    lunch_by_id = {it["event_id"]: it for it in ledger["meals"]["lunch"]}

    # “热力宝”被准确找回并改为“土豆牛肉料理包”，而同录音的“米饭”绝不被误改
    assert lunch_by_id[new_food_id]["item"] == "土豆牛肉料理包"
    assert lunch_by_id[new_food_id]["renamed"] is True
    assert lunch_by_id[new_food_id]["original_name"] == "热力宝"
    assert lunch_by_id[new_food_id]["rename_matched_by"] == "fallback"
    assert lunch_by_id[new_food_id]["rename_key"] == old_id
    assert lunch_by_id[new_rice_id]["item"] == "米饭"
    assert not lunch_by_id[new_rice_id].get("renamed")

    # 前端传新 event_id 撤销，也能准确撤掉旧键上的改名记录
    undo_res = fc.undo_food_rename(temp_root, date="2026-11-12", event_id=new_food_id)
    assert undo_res["ok"] is True
    assert undo_res["event_id"] == old_id
    lunch_after = {it["event_id"]: it for it in undo_res["nutrition_ledger"]["meals"]["lunch"]}
    assert lunch_after[new_food_id]["item"] == "热力宝（转写名称待核）"


def test_unmatched_rename_is_exposed_and_can_be_undone(temp_root):
    old_id = "activity:2026-11-12:act_lunch_20261112_132206:item_lunch_132206_02"
    fc.rename_food(temp_root, date="2026-11-12", event_id=old_id, name="土豆牛肉料理包")

    # 模拟 v3 重跑后该条消失（只剩米饭）
    only_rice = _jev_20261112_activities()
    only_rice[0]["items"] = [only_rice[0]["items"][0]]
    _write_jev_day(temp_root, "2026-11-12", only_rice)

    ledger = day_facts.domain_ledgers([], "2026-11-12", root=temp_root)["nutrition"]
    assert len(ledger["unmatched_renames"]) == 1
    unmatched = ledger["unmatched_renames"][0]
    assert unmatched["event_id"] == old_id
    assert unmatched["name"] == "土豆牛肉料理包"
    assert unmatched["original_name"] == "热力宝"
    assert unmatched["clock"] == "13:22"
    assert unmatched["quote"] == "中午吃了米饭，吃了热力宝。"

    undo_res = fc.undo_food_rename(temp_root, date="2026-11-12", event_id=old_id)
    assert undo_res["ok"] is True
    assert undo_res["unmatched_renames"] == []


def test_rename_does_not_break_enter_weight_per100g_adopt_skip_or_duplicate(temp_root):
    rice_id = "activity:2026-11-12:act_lunch_20261112_132206:item_lunch_132206_01"
    food_id = "activity:2026-11-12:act_lunch_20261112_132206:item_lunch_132206_02"
    store = JobStore(temp_root, recover=False)
    fw = temp_root / "fw"

    # 1. 先改名“热力宝” -> “土豆牛肉料理包”
    fc.rename_food(temp_root, date="2026-11-12", event_id=food_id, name="土豆牛肉料理包")

    # 2. 对改名后的“土豆牛肉料理包”填每100g成分表 + 按重量补算 200g
    ans_weight = jq.answer(
        store,
        {
            "source_id": f"cal|2026-11-12|{food_id}",
            "operation": "enter_weight",
            "op_id": "cal_q1",
            "grams": 200,
            "per_100g": {
                "energy_kcal": 135,
                "protein_g": 11,
                "fat_g": 6,
                "carbohydrates_g": 9,
            },
        },
        flywheel_root=fw,
    )
    assert ans_weight["state"] == "answered"
    assert ans_weight["calories_kcal"] == 270.0

    # 3. 对“米饭”点“按常规份量算”（adopt_candidate）
    ans_adopt = jq.answer(
        store,
        {
            "source_id": f"cal|2026-11-12|{rice_id}",
            "operation": "adopt_candidate",
            "op_id": "cal_q2",
        },
        flywheel_root=fw,
    )
    assert ans_adopt["state"] == "answered"

    ledger = fc._current_day_nutrition_ledger(temp_root, "2026-11-12")
    lunch_by_item = {it["item"]: it for it in ledger["meals"]["lunch"]}
    assert lunch_by_item["土豆牛肉料理包"]["renamed"] is True
    assert lunch_by_item["土豆牛肉料理包"]["calories_kcal"] == 270.0
    assert lunch_by_item["米饭"]["calories_kcal"] == 174.0
    assert ledger["totals"]["calories_kcal"] == 444.0

    # 4. 测试 skip 与 decide_duplicate 不受影响
    jq.undo(store, {"source_id": f"cal|2026-11-12|{rice_id}", "op_id": "cal_q2"}, flywheel_root=fw)
    ans_skip = jq.answer(
        store,
        {
            "source_id": f"cal|2026-11-12|{rice_id}",
            "operation": "skip",
            "op_id": "cal_q3",
        },
        flywheel_root=fw,
    )
    assert ans_skip["state"] == "answered"
    dup_res = jq.decide_duplicate(temp_root, event_id="dup_test_event", action="separate", date="2026-11-12")
    assert dup_res["ok"] is True


def _invoke_post(root: Path, path: str, payload: dict) -> tuple[int, dict]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sent: dict = {}
    handler = LocalWebHandler.__new__(LocalWebHandler)
    handler.path = path
    handler.headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json"}
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.server = SimpleNamespace(store=SimpleNamespace(root=root))
    handler._require_api_auth = lambda write=False: True
    handler._send_json = lambda data, status=200: sent.update(status=status, data=data)
    handler.do_POST()
    return sent["status"], sent["data"]


def test_http_post_food_rename_and_undo_routes(temp_root):
    event_id = "activity:2026-11-12:act_lunch_20261112_132206:item_lunch_132206_02"
    status, data = _invoke_post(
        temp_root,
        "/api/food/rename",
        {
            "date": "2026-11-12",
            "event_id": event_id,
            "name": "土豆牛肉料理包",
        },
    )
    assert status == 200
    assert data["ok"] is True
    assert data["name"] == "土豆牛肉料理包"
    renamed_item = next(
        it for it in data["nutrition_ledger"]["meals"]["lunch"] if it["event_id"] == event_id
    )
    assert renamed_item["item"] == "土豆牛肉料理包"
    assert renamed_item["renamed"] is True
    assert renamed_item["original_name"] == "热力宝"

    status_u, data_u = _invoke_post(
        temp_root,
        "/api/food/rename/undo",
        {
            "date": "2026-11-12",
            "event_id": event_id,
        },
    )
    assert status_u == 200
    assert data_u["ok"] is True
    restored_item = next(
        it for it in data_u["nutrition_ledger"]["meals"]["lunch"] if it["event_id"] == event_id
    )
    assert restored_item["item"] == "热力宝（转写名称待核）"
    assert not restored_item.get("renamed")

    status_bad, _ = _invoke_post(
        temp_root,
        "/api/food/rename",
        {
            "date": "2026-11-12",
            "event_id": event_id,
            "name": "   ",
        },
    )
    assert status_bad == 400
