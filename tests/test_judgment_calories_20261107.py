"""「热量待定」：不知重量的食物给候选热量，用户在“需要我判断”里决定采用与否（2026-11-07）。

用户决定（2026-11-07 06:10）：**不知重量就不算热量**；按常规份量算出的值只作候选，
晚上复盘时由用户决定要不要采用。所以：

* 队列里的条目来自当天 `nutrition_ledger.unweighed_items`（2 号窗提供），
  只有带 `portion_estimate_candidate.calories_kcal` 的才出现；
* 采用 / 补重量 → 写一条**人工饮食事件**（`category="food"`，`evidence.source="user_judgment"`，
  `data.food.source_event_id` 关联原条目），并且可以撤销；
* 不算 → 只记回答，不写事件；
* 台账字段不存在（2 号窗交付前）→ **0 条，不报错**。

本文件全部用**构造的 ledger**，不读真实产物、不联网、不发微信。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from life_logger import judgment_queue as jq  # noqa: E402

DAY = "2026-11-05"

LEDGER = {
    "unweighed_items": [
        {
            "event_id": "ev-dumpling",
            "item": "猪肉芹菜水饺",
            "quote": "吃了一盘水饺",
            "meal": "dinner",
            "spoken_at": "2026-11-05T19:12:00+08:00",
            "estimate_basis": "standard_serving",
            "portion_estimate_candidate": {
                "grams": 200.0,
                "calories_kcal": 400.0,
                "basis": "standard_serving",
                "nutrients": {
                    "carbohydrates_g": 50.0,
                    "protein_g": 20.0,
                    "fat_g": 10.0,
                    "sodium_mg": 800,
                },
            },
        },
        {
            # 没有候选热量 → 不该出现在队列里
            "event_id": "ev-fries",
            "item": "薯条",
            "quote": "买了薯条",
            "meal": None,
            "spoken_at": "2026-11-05T21:00:00+08:00",
            "estimate_basis": "standard_serving",
            "portion_estimate_candidate": None,
        },
    ],
    "unweighed_candidate_kcal": 400.0,
}


class _Store:
    """最小 JobStore 替身：只实现 answer()/_save_entry() 用到的方法。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.saved: list[dict] = []

    def create_job(self) -> str:
        (self.root / "jobs" / "job-new").mkdir(parents=True, exist_ok=True)
        return "job-new"

    def save_manual_event(self, job_id: str, **kwargs) -> dict:
        event = {"event_id": "manual_1", **kwargs}
        self.saved.append(event)
        path = self.root / "jobs" / job_id / "manual-events.json"
        path.write_text(json.dumps({"events": [{"event_id": "manual_1"}]}), encoding="utf-8")
        return event


@pytest.fixture
def root(tmp_path, monkeypatch):
    jq.invalidate_cache()
    # 队列里没有任何 pending_review 条目，只看「热量待定」这一类
    monkeypatch.setattr(jq, "_collect_pending_items", lambda root, records: ({}, 0, []))
    monkeypatch.setattr(jq, "day_nutrition_ledger", lambda root, day: LEDGER if day == DAY else {})
    return tmp_path


@pytest.fixture
def empty_ledger(root, monkeypatch):
    jq.invalidate_cache()
    monkeypatch.setattr(jq, "day_nutrition_ledger", lambda root, day: {})
    return root


# ─────────────────────────────────────────────────────────── 队列
def test_queue_lists_unweighed_item_with_candidate(root):
    out = jq.build_queue(root, today=DAY, flywheel_root=root / "fw")
    purposes = {row["key"]: row for row in out["purposes"]}
    assert purposes["calories"]["label"] == "热量待定"
    assert purposes["calories"]["total_open"] == 1          # 没有候选的那条不算
    items = [row for row in out["items"] if row["purpose"] == "calories"]
    assert len(items) == 1
    item = items[0]
    assert item["item"] == "猪肉芹菜水饺"
    assert item["quote"] == "吃了一盘水饺"
    assert item["event_id"] == "ev-dumpling"
    assert item["candidate"]["grams"] == 200.0
    assert item["candidate"]["calories_kcal"] == 400.0
    assert [option["op"] for option in item["options"]] == ["adopt_candidate", "enter_weight", "skip"]


def test_missing_ledger_fields_returns_no_items_and_no_error(empty_ledger):
    out = jq.build_queue(empty_ledger, today=DAY, flywheel_root=empty_ledger / "fw")
    assert out["total_open"] == 0
    assert [row for row in out["items"] if row["purpose"] == "calories"] == []
    # 没有待定热量时，这一类干脆不显示（不制造恒为 0 的分区）
    assert "calories" not in {row["key"] for row in out["purposes"]}
    # 直接调用也不许抛
    assert jq.calorie_items(empty_ledger, DAY) == []


# ─────────────────────────────────────────────────────────── 采用
def test_adopt_candidate_writes_food_event_with_candidate_numbers(root):
    store = _Store(root)
    fw = root / "fw"
    out = jq.answer(store, {"source_id": jq.calorie_source_id(DAY, "ev-dumpling"),
                            "operation": "adopt_candidate", "op_id": "op-adopt"}, flywheel_root=fw)
    assert out["state"] == "answered"
    assert len(store.saved) == 1
    event = store.saved[0]
    assert event["categories"] == ["food"]
    assert event["source_type"] == "user_supplement"
    assert event["evidence"]["source"] == "user_judgment"
    food = event["data"]["food"]
    assert food["item"] == "猪肉芹菜水饺"
    assert food["weight_g"] == 200.0
    assert food["estimated_calories"] == 400.0
    assert food["estimate_basis"] == "user_adopted_candidate"
    assert food["source_event_id"] == "ev-dumpling"
    assert food["nutrients"]["protein_g"] == 20.0
    # 答过之后不再出现在队列里
    again = jq.build_queue(root, today=DAY, flywheel_root=fw)
    assert [row for row in again["items"] if row["purpose"] == "calories"] == []


def test_enter_weight_scales_candidate_calories(root):
    store = _Store(root)
    out = jq.answer(store, {"source_id": jq.calorie_source_id(DAY, "ev-dumpling"),
                            "operation": "enter_weight", "op_id": "op-weight", "grams": "150"},
                    flywheel_root=root / "fw")
    food = store.saved[0]["data"]["food"]
    assert food["weight_g"] == 150.0
    assert food["estimated_calories"] == 300.0        # 400 kcal / 200 g × 150 g
    assert food["estimate_basis"] == "user_weight"
    assert food["source_event_id"] == "ev-dumpling"
    assert out["calories_kcal"] == 300.0


@pytest.mark.parametrize("grams", ["", "0", "-3", "abc", None, "20000"])
def test_enter_weight_rejects_bad_grams(root, grams):
    store = _Store(root)
    with pytest.raises(ValueError):
        jq.answer(store, {"source_id": jq.calorie_source_id(DAY, "ev-dumpling"),
                          "operation": "enter_weight", "op_id": "op-bad", "grams": grams},
                  flywheel_root=root / "fw")
    assert store.saved == []


# ─────────────────────────────────────────────────────────── 不算 / 撤销 / 幂等
def test_skip_records_answer_without_writing_event(root):
    store = _Store(root)
    fw = root / "fw"
    jq.answer(store, {"source_id": jq.calorie_source_id(DAY, "ev-dumpling"),
                      "operation": "skip", "op_id": "op-skip"}, flywheel_root=fw)
    assert store.saved == []
    assert (fw / "answers" / "op-skip.json").is_file()
    record = json.loads((fw / "answers" / "op-skip.json").read_text())
    assert record["purpose"] == "calories"
    assert record["operation"] == "skip"
    assert [row for row in jq.build_queue(root, today=DAY, flywheel_root=fw)["items"]
            if row["purpose"] == "calories"] == []


def test_undo_removes_the_written_food_event(root):
    store = _Store(root)
    fw = root / "fw"
    source_id = jq.calorie_source_id(DAY, "ev-dumpling")
    jq.answer(store, {"source_id": source_id, "operation": "adopt_candidate", "op_id": "op-u"},
              flywheel_root=fw)
    path = root / "jobs" / "job-new" / "manual-events.json"
    assert json.loads(path.read_text())["events"] != []
    jq.undo(store, {"source_id": source_id, "op_id": "op-u"}, flywheel_root=fw)
    assert json.loads(path.read_text())["events"] == []
    # 撤销后重新回到待判断
    back = jq.build_queue(root, today=DAY, flywheel_root=fw)
    assert [row["event_id"] for row in back["items"] if row["purpose"] == "calories"] == ["ev-dumpling"]


def test_same_op_id_does_not_write_two_events(root):
    store = _Store(root)
    payload = {"source_id": jq.calorie_source_id(DAY, "ev-dumpling"),
               "operation": "adopt_candidate", "op_id": "op-same"}
    jq.answer(store, payload, flywheel_root=root / "fw")
    second = jq.answer(store, payload, flywheel_root=root / "fw")
    assert len(store.saved) == 1
    assert second.get("idempotent") is True


def test_unknown_source_id_is_rejected(root):
    with pytest.raises(ValueError):
        jq.answer(_Store(root), {"source_id": "cal|2026-11-05|ev-none",
                                 "operation": "adopt_candidate", "op_id": "op-x"},
                  flywheel_root=root / "fw")


# ─────────────────────────────────────────────────────────── 21:00 推送文案
def _load_evening():
    spec = importlib.util.spec_from_file_location("evening_alignment", ROOT / "scripts" / "evening_alignment.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _payload(nutrition):
    return {
        "expense_ledger": {
            "total_amount": 0.0, "currency": "CNY", "payment_transaction_count": 0,
            "payment_transactions": [], "pending_count": 0,
        },
        "nutrition_ledger": nutrition,
    }


def test_evening_push_mentions_candidates_when_present():
    ea = _load_evening()
    nutrition = {
        "unweighed_items": [{"event_id": "a"}, {"event_id": "b"}],
        "unweighed_candidate_kcal": 730.4,
    }
    headline, body = ea.build_message(DAY, _payload(nutrition))
    assert "今天有 2 条食物没说重量" in body
    assert "候选合计约 730 千卡" in body
    assert "需要我判断" in body
    assert "730" not in headline            # 结论行不变，细节才加


def test_evening_push_says_nothing_when_zero_or_missing():
    ea = _load_evening()
    for nutrition in ({"unweighed_items": [], "unweighed_candidate_kcal": 0},
                      {"unweighed_items": [], "unweighed_candidate_kcal": None},
                      None,
                      {}):
        _, body = ea.build_message(DAY, _payload(nutrition))
        assert "没说重量" not in body


def test_inline_enter_weight_and_skip_work_even_without_candidate(root):
    store = _Store(root)
    fw = root / "fw"
    # ev-fries 在 LEDGER 里 portion_estimate_candidate=None：内联面板仍允许补重量+千卡或点「不算」
    out = jq.answer(
        store,
        {
            "source_id": jq.calorie_source_id(DAY, "ev-fries"),
            "operation": "enter_weight",
            "op_id": "op-fries-w",
            "grams": "120",
            "calories_kcal": "360",
        },
        flywheel_root=fw,
    )
    assert out["calories_kcal"] == 360.0
    assert store.saved[-1]["data"]["food"]["weight_g"] == 120.0
    assert store.saved[-1]["data"]["food"]["source_event_id"] == "ev-fries"

    # skip 记录进 review_questions_state，skipped_calorie_event_ids 能读出来供台账剔除
    jq.answer(
        store,
        {
            "source_id": jq.calorie_source_id(DAY, "ev-dumpling"),
            "operation": "skip",
            "op_id": "op-dumpling-skip",
        },
        flywheel_root=fw,
    )
    assert "ev-dumpling" in jq.skipped_calorie_event_ids(root, DAY)


def test_inline_per_100g_label_edit_scales_macros_and_saves_brand_entry(root):
    store = _Store(root)
    fw = root / "fw"
    food_dir = root / "food-data"
    food_dir.mkdir(parents=True, exist_ok=True)
    out = jq.answer(
        store,
        {
            "source_id": jq.calorie_source_id(DAY, "ev-fries"),
            "operation": "enter_weight",
            "op_id": "op-fries-p100",
            "grams": "200",
            "per_100g": {
                "energy_kj": 836.8,  # 200 kcal / 100g
                "protein_g": 12.5,
                "fat_g": 5.0,
                "carbohydrates_g": 20.0,
                "sodium_mg": 300,
            },
        },
        flywheel_root=fw,
    )
    assert out["calories_kcal"] == 400.0
    food = store.saved[-1]["data"]["food"]
    assert food["weight_g"] == 200.0
    assert food["nutrients"] == {
        "carbohydrates_g": 40.0,
        "protein_g": 25.0,
        "fat_g": 10.0,
        "sodium_mg": 600.0,
    }
    assert food["nutrition_per_100g"]["energy_kcal"] == 200.0
    saved_lib = json.loads((food_dir / "brand-library.json").read_text(encoding="utf-8"))
    assert any(entry["name"] == "薯条" and entry["per_100g"]["energy_kcal"] == 200.0 for entry in saved_lib["entries"])
