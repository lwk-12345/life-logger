"""习惯（habit）唯一事实源：本地台账 + 口令接入 + HTTP 投影。

为什么这些断言重要（写死在这里，防止以后被"顺手优化"掉）：
- 习惯**没有终态**：今天打卡 ≠ 以后都算打卡，第二天必须回到「未打卡」。
- 习惯与待办**两条线**：待办口令不能出现在习惯台账里，反之亦然。
- 删除是 `dismissed`，历史保留；同名重复添加必须幂等（否则会重演 253 条待办的堆积）。
"""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from life_logger.local_habits import (
    BADGE_SLOT_LIMIT,
    LocalHabitStore,
    apply_habit_captures,
)
from life_logger.local_web import JobStore, create_server
from life_logger.voice_triggers import scan_voice_triggers

BJ = timezone(timedelta(hours=8))


def _clock_at(text: str):
    moment = datetime.fromisoformat(text).astimezone(BJ)
    return lambda: moment


def test_add_is_idempotent_and_persists_schema(tmp_path: Path) -> None:
    store = LocalHabitStore(tmp_path, clock=_clock_at("2026-10-13T09:00:00+08:00"))

    first = store.add("冥想 10 分钟")
    assert first["created"] is True
    # 同一天同一句话再记一次（含空白差异）不能造第二条
    second = store.add("冥想10分钟")
    assert second["created"] is False
    assert second["habit"]["habit_key"] == first["habit"]["habit_key"]

    payload = json.loads((tmp_path / "habits.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "local-habits-v1"
    assert payload["backend"] == "local"
    assert len(payload["habits"]) == 1
    record = next(iter(payload["habits"].values()))
    assert record["status"] == "active"
    assert record["checkins"] == {}
    assert record["review_status"] == "confirmed"
    assert record["source_type"] == "manual"


def test_checkin_is_idempotent_per_day_and_resets_next_day(tmp_path: Path) -> None:
    store = LocalHabitStore(tmp_path, clock=_clock_at("2026-10-13T07:00:00+08:00"))
    store.add("早起", habit_key="h-rise")

    first = store.checkin("早起")
    assert first["changed"] is True
    assert first["habit"]["checked_today"] is True
    assert first["habit"]["streak"] == 1

    again = store.checkin("早起")
    assert again["changed"] is False
    assert again["habit"]["total_checkins"] == 1

    # 第二天：同一份数据、未清空，但「今天」重新变成未打卡
    next_day = LocalHabitStore(tmp_path, clock=_clock_at("2026-10-14T07:00:00+08:00"))
    row = next_day.list_habits()[0]
    assert row["checked_today"] is False
    assert row["last_checkin_date"] == "2026-10-13"
    assert row["total_checkins"] == 1
    assert row["streak"] == 1  # 昨天打了，连续天数是 1，不是 0


def test_streak_counts_consecutive_days_and_undo_rolls_back(tmp_path: Path) -> None:
    store = LocalHabitStore(tmp_path, clock=_clock_at("2026-10-16T07:00:00+08:00"))
    store.add("跑步", habit_key="h-run")
    for day in ("2026-10-13", "2026-10-14", "2026-10-16"):
        store.checkin("跑步", date=day)

    row = store.get("跑步")
    assert row is not None and row["streak"] == 1  # 10-15 断了，只算 16 号这一条
    assert row["total_checkins"] == 3

    undone = store.checkin("跑步", date="2026-10-16", undo=True)
    assert undone["changed"] is True
    assert undone["habit"]["checked_today"] is False
    # 撤销后链子断在 10-15，当前连续天数归零（历史仍在，总量不丢）
    assert undone["habit"]["streak"] == 0
    assert undone["habit"]["total_checkins"] == 2
    assert store.checkin("跑步", date="2026-10-16", undo=True)["changed"] is False

    store.checkin("跑步", date="2026-10-15")
    after_repair = store.get("跑步")
    # 13、14、15 连上，今天 16 号还没打
    assert after_repair is not None and after_repair["streak"] == 3


def test_dismissed_keeps_history_and_leaves_the_visible_list(tmp_path: Path) -> None:
    store = LocalHabitStore(tmp_path, clock=_clock_at("2026-10-13T21:00:00+08:00"))
    store.add("喝八杯水", habit_key="h-water")
    store.checkin("喝八杯水")

    store.set_status("喝八杯水", "dismissed")
    assert store.list_habits() == []
    archived = store.list_habits(include_inactive=True)
    assert len(archived) == 1
    assert archived[0]["status"] == "dismissed"
    assert archived[0]["total_checkins"] == 1

    snapshot = store.snapshot()
    assert snapshot["total"] == 0
    assert snapshot["checked_count"] == 0


def test_match_by_index_and_reports_ambiguity(tmp_path: Path) -> None:
    store = LocalHabitStore(tmp_path, clock=_clock_at("2026-10-13T09:00:00+08:00"))
    store.add("读书", habit_key="h-read")
    store.add("背单词", habit_key="h-words")

    by_index = store.checkin("#2", date="2026-10-13")
    assert by_index["habit"]["habit_key"] == "h-words"

    with pytest.raises(LookupError):
        store.checkin("跑马拉松")

    # 标题精确命中优先于"被包含"：说「读书」就是那条「读书」
    store.add("读书 30 页", habit_key="h-read-2")
    exact = store.checkin("读书")
    assert exact["matched"]["ambiguous"] is False
    assert exact["habit"]["habit_key"] == "h-read"

    # 两条都只是"包含"时才算歧义：认创建最早的一条，并把候选带回去
    store.set_status("读书", "dismissed")
    store.add("读书 20 分钟", habit_key="h-read-3")
    ambiguous = store.checkin("读书")
    assert ambiguous["matched"]["ambiguous"] is True
    assert ambiguous["habit"]["habit_key"] == "h-read-2"
    assert ambiguous["matched"]["candidates"] == ["读书 30 页", "读书 20 分钟"]


def test_badge_slots_are_capped_and_carry_the_checkin_flag(tmp_path: Path) -> None:
    store = LocalHabitStore(tmp_path, clock=_clock_at("2026-10-13T09:00:00+08:00"))
    for index in range(1, 6):  # 6 条习惯，徽章只有 4 槽
        store.add(f"第{index}个习惯名称", habit_key=f"h{index}")
    store.checkin("#3", date="2026-10-13")

    slots = store.habits_for_badge()
    assert len(slots) == BADGE_SLOT_LIMIT
    assert [slot["index"] for slot in slots] == [1, 2, 3, 4]
    assert all(slot["total"] == BADGE_SLOT_LIMIT for slot in slots)
    assert [slot["flags"] for slot in slots] == [0x00, 0x00, 0x01, 0x00]
    assert slots[2]["checked_today"] is True
    assert all(len(slot["title"]) <= 20 for slot in slots)


def test_apply_habit_captures_routes_three_commands(tmp_path: Path) -> None:
    store = LocalHabitStore(tmp_path, clock=_clock_at("2026-10-13T09:00:00+08:00"))

    add_captures = scan_voice_triggers("添加一个习惯 冥想 10 分钟")
    assert [c.trigger_type for c in add_captures] == ["habit"]
    add_outcome = apply_habit_captures(store, add_captures)
    assert add_outcome[0]["applied"] is True
    assert add_outcome[0]["action"] == "add"
    assert add_outcome[0]["habit_title"] == "冥想 10 分钟"

    done_captures = scan_voice_triggers("习惯打卡 冥想")
    assert [c.trigger_type for c in done_captures] == ["habit_done"]
    done_outcome = apply_habit_captures(store, done_captures)
    assert done_outcome[0]["applied"] is True
    assert done_outcome[0]["changed"] is True
    assert done_outcome[0]["action"] == "checkin"

    delete_captures = scan_voice_triggers("删除习惯 冥想")
    assert [c.trigger_type for c in delete_captures] == ["habit_delete"]
    delete_outcome = apply_habit_captures(store, delete_captures)
    assert delete_outcome[0]["applied"] is True
    assert delete_outcome[0]["action"] == "dismiss"
    assert store.list_habits() == []

    # 待办口令不进习惯账（两条线互不抢词），也不报错
    todo_outcome = apply_habit_captures(store, scan_voice_triggers("提醒我明天买牛奶"))
    assert todo_outcome == []


def test_apply_habit_captures_reports_unknown_target(tmp_path: Path) -> None:
    store = LocalHabitStore(tmp_path, clock=_clock_at("2026-10-13T09:00:00+08:00"))
    store.add("早起", habit_key="h-rise")

    outcome = apply_habit_captures(store, scan_voice_triggers("习惯打卡 跑马拉松"))
    assert outcome[0]["applied"] is False
    assert outcome[0]["error"] is not None
    assert outcome[0]["candidates"] == ["早起"]


def test_habits_api_roundtrip_and_daily_reset(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    server = create_server(host="127.0.0.1", port=0, store=store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{base}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return json.loads(urllib.request.urlopen(request).read())

    def get(path: str) -> dict:
        return json.loads(urllib.request.urlopen(f"{base}{path}").read())

    try:
        created = post("/api/habits/add", {"title": "每天走一万步"})
        assert created["created"] is True

        first = post(
            "/api/habits/checkin", {"target": "每天走一万步", "date": "2026-10-13"}
        )
        assert first["ok"] is True and first["changed"] is True
        repeat = post(
            "/api/habits/checkin", {"target": "每天走一万步", "date": "2026-10-13"}
        )
        assert repeat["changed"] is False
        post("/api/habits/checkin", {"target": "#1", "date": "2026-10-14"})

        today = get("/api/habits?date=2026-10-14")
        assert today["backend"] == "local"
        assert today["schema_version"] == "local-habits-v1"
        assert today["total"] == 1 and today["checked_count"] == 1
        assert today["habits"][0]["checked_today"] is True
        assert today["habits"][0]["streak"] == 2
        assert today["badge_slots"][0]["flags"] == 0x01

        tomorrow = get("/api/habits?date=2026-10-15")
        assert tomorrow["checked_count"] == 0
        assert tomorrow["habits"][0]["checked_today"] is False
        assert tomorrow["habits"][0]["streak"] == 2
        assert tomorrow["badge_slots"][0]["flags"] == 0x00

        voice = post("/api/habits/voice", {"text": "习惯打卡 每天走一万步"})
        assert voice["applied_count"] == 1
        assert voice["outcomes"][0]["action"] == "checkin"

        todo_voice = post("/api/habits/voice", {"text": "提醒我明天买牛奶"})
        assert todo_voice["applied_count"] == 0

        # 说不清是哪条时：404 + 候选名单（够人一句话纠正）
        with pytest.raises(urllib.error.HTTPError) as missing:
            post("/api/habits/checkin", {"target": "跑马拉松"})
        assert missing.value.code == 404
        body = json.loads(missing.value.read())
        assert body["candidates"] == ["每天走一万步"]

        dismissed = post(
            "/api/habits/status", {"target": "每天走一万步", "status": "dismissed"}
        )
        assert dismissed["habit"]["status"] == "dismissed"
        assert get("/api/habits")["total"] == 0
        assert get("/api/habits?include_inactive=1")["total"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_habits_json_is_included_in_state_backup(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    LocalHabitStore(store.root).add("清晨写日报", habit_key="h-morning")

    archive = zipfile.ZipFile(io.BytesIO(store.build_state_backup()))
    assert "habits.json" in archive.namelist()
