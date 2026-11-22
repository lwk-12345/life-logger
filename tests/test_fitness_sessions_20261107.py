"""Fitness sessions: grouping, retrospective verification, v3 passthrough."""
from __future__ import annotations

import copy

from life_logger import fitness_sessions as fs
from life_logger.daily_cognitive_session import build_fitness_ledger


def workout(event_id, at, title, quote="", *, exercises=None, spoken=True):
    entry = {"event_id": event_id, "title": title, "quote": quote,
             "spoken_at": f"2026-11-05T{at}:00+08:00"}
    if exercises is not None:
        entry["exercises"] = exercises
    return entry


LIVE = [
    workout("a", "18:33", "今天练胸", "今天练胸"),
    workout("b", "18:38", "第三组单边10千克", "第三组 一边加十千克", exercises=[{"name": "卧推", "sets": 1, "weight_value": 10, "side": "per_side", "weight_unit": "kg"}]),
    workout("c", "19:12", "哑铃推肩", "桌子哑铃推肩", exercises=[{"name": "哑铃推肩", "sets": 3}]),
]
REVIEW = [workout("r1", "23:04", "回顾称做了一个半小时有氧", "去健身房做了一个半小时的有氧", exercises=[{"name": "有氧", "duration_seconds": 5400}])]


def test_same_workout_is_one_session_and_late_review_attaches():
    sessions = fs.structure_sessions(LIVE + REVIEW)
    assert len(sessions) == 1
    session = sessions[0]
    assert session["start"].startswith("2026-11-05T18:33")
    assert {a["name"] for a in session["actions"]} >= {"卧推", "哑铃推肩"}


def test_gap_over_90_minutes_or_explicit_exit_splits_sessions():
    morning = [workout("m", "08:00", "晨练", "晨练"), workout("n", "09:20", "继续练", "继续练")]
    assert len(fs.structure_sessions(morning)) == 1
    late = copy.deepcopy(morning)
    late[1]["spoken_at"] = "2026-11-05T09:31:00+08:00"
    assert len(fs.structure_sessions(late)) == 2
    left = [workout("x", "10:00", "练完离开健身房", "练完离开健身房"),
            workout("y", "10:10", "又练了一组", "又练了一组")]
    assert len(fs.structure_sessions(left)) == 2


def test_review_does_not_add_to_totals_and_confirms_when_consistent():
    live = [workout("a", "18:00", "卧推", "卧推",
                    exercises=[{"name": "卧推", "sets": 3, "duration_seconds": 1800}])]
    review = [workout("r", "23:00", "回顾：卧推半小时", "卧推做了半个小时",
                      exercises=[{"name": "卧推", "duration_seconds": 1800}])]
    session = fs.structure_sessions(live + review)[0]
    bench = next(a for a in session["actions"] if a["name"] == "卧推")
    assert bench["sets"] == 3 and bench["duration_seconds"] == 1800   # 回顾没有把组数/时长再加一遍
    assert session["verification"]["status"] == "confirmed"
    assert session["verification"]["differences"] == []


def test_review_difference_is_reported_not_hidden():
    live = [workout("a", "18:00", "卧推", "卧推",
                    exercises=[{"name": "卧推", "sets": 3, "duration_seconds": 1200}])]
    review = [workout("r", "23:00", "回顾：卧推半小时", "卧推做了半个小时",
                      exercises=[{"name": "卧推", "duration_seconds": 1800}])]
    session = fs.structure_sessions(live + review)[0]
    assert session["verification"]["status"] == "differences"
    assert any("卧推" in d for d in session["verification"]["differences"])


def test_review_only_session_is_labelled_and_still_counted_once():
    sessions = fs.structure_sessions(REVIEW)
    assert len(sessions) == 1
    assert sessions[0]["verification"]["status"] == "review_only"
    assert sessions[0]["verification"]["source"] == "review"
    assert sessions[0]["total_duration_seconds"] == 5400


def test_v3_structured_fields_pass_through_without_model(monkeypatch):
    def explode(_session):
        raise AssertionError("v3 structured entries must not call the model")
    v3 = [workout("v", "18:30", "健身房练胸", "第三组", exercises=[
        {"name": "杠铃卧推", "sets": 1, "reps": 8, "weight_value": 12.5,
         "weight_unit": "kg", "side": "per_side", "duration_seconds": None}])]
    v3[0]["fact_schema_version"] = 3
    session = fs.structure_sessions(v3, structurer=explode)[0]
    action = session["actions"][0]
    assert action["name"] == "杠铃卧推" and action["weight_value"] == 12.5
    assert action["side"] == "per_side"


def test_ledger_exposes_sessions_and_counts_sessions(monkeypatch):
    activities = [{"activity_id": "act", "title": "健身房", "lifecycle": "live",
                   "occurrence_start": "2026-11-05T18:30:00+08:00",
                   "reported_at": "2026-11-05T18:45:00+08:00", "items": [
        {"item_id": "i1", "action": "卧推三组", "fact_type": "exercise", "status": "completed",
         "spoken_at": "2026-11-05T18:30:00+08:00",
         "exercise": {"name": "卧推", "sets": 3, "duration_seconds": 1800},
         "evidence": [{"quote": "卧推三组", "source_id": "s1", "source_label": "self"}]},
        {"item_id": "i2", "action": "推肩三组", "fact_type": "exercise", "status": "completed",
         "spoken_at": "2026-11-05T18:40:00+08:00",
         "exercise": {"name": "哑铃推肩", "sets": 3},
         "evidence": [{"quote": "推肩三组", "source_id": "s2", "source_label": "self"}]},
    ]}]
    ledger = build_fitness_ledger([], activities=activities, day="2026-11-05")
    assert ledger["workout_count"] == 1
    assert len(ledger["sessions"]) == 1
    session = ledger["sessions"][0]
    assert session["action_count"] == 2
    assert session["start"].startswith("2026-11-05T18:30")


def test_structurer_output_drives_phases(monkeypatch):
    calls = []
    def structurer(session):
        calls.append(session["index"])
        return {"items": [
            {"event_id": "a", "phase": "live", "name": "卧推", "sets": 3},
            {"event_id": "r", "phase": "review", "name": "卧推", "duration_seconds": 1800},
        ]}
    data = [workout("a", "18:00", "卧推", "卧推"), workout("r", "23:00", "回顾卧推", "卧推半小时")]
    session = fs.structure_sessions(data, structurer=structurer)[0]
    assert calls == [0]
    assert session["verification"]["status"] in {"confirmed", "differences"}
    assert session["actions"][0]["sets"] == 3


def test_software_antigravity_talk_is_not_an_exercise():
    records = [
        {"source_id": "a", "text": "今天练胸", "source_label": "self", "timestamp": "2026-11-05T18:33:00+08:00"},
        {"source_id": "b", "text": "反重力蹬不上了，反重力蹬不上了。", "source_label": "self", "timestamp": "2026-11-05T19:30:56+08:00"},
        {"source_id": "c", "text": "国家的单词", "source_label": "self", "timestamp": "2026-11-05T19:33:00+08:00"},
        {"source_id": "d", "text": "谷歌的地区设置", "source_label": "self", "timestamp": "2026-11-05T19:34:00+08:00"},
    ]
    kept = {r["source_id"] for r in fs.filter_exercise_records(records)}
    assert "b" not in kept        # 反重力+登录语境 = 软件，不是器材
    assert "a" in kept


def test_all_self_records_in_gym_span_go_to_model_and_only_later_is_review(monkeypatch):
    workouts = [workout("a", "18:33", "练胸", "练胸"),
                workout("r", "23:04", "回顾有氧", "去健身房做了一个半小时的有氧")]
    records = [
        {"source_id": "s1", "text": "今天练胸", "source_label": "self", "timestamp": "2026-11-05T18:33:10+08:00"},
        {"source_id": "s2", "text": "第二组。", "source_label": "self", "timestamp": "2026-11-05T18:38:23+08:00"},
        {"source_id": "s3", "text": "第二图做完了", "source_label": "self", "timestamp": "2026-11-05T18:46:04+08:00"},
        {"source_id": "s4", "text": "去健身房做了一个半小时的有氧", "source_label": "self", "timestamp": "2026-11-05T23:04:43+08:00"},
        {"source_id": "s5", "text": "广告", "source_label": "non_self", "timestamp": "2026-11-05T18:40:00+08:00"},
    ]
    seen = {}
    def structurer(session):
        seen["ids"] = [e["event_id"] for e in session["entries"]]
        return {"items": [
            {"event_id": "s2", "phase": "live", "name": "卧推", "set_number": 2},
            {"event_id": "s3", "phase": "live", "name": "卧推", "set_number": 2},
            {"event_id": "s4", "phase": "live", "name": "有氧", "duration_seconds": 5400},
        ]}
    session = fs.structure_sessions(workouts, structurer=structurer, records=records)[0]
    assert "s1" in seen["ids"] and "s2" in seen["ids"] and "s3" in seen["ids"]
    assert "s5" not in seen["ids"]
    # 23:04 的记录按时间改判 review，不再加进现场总时长
    assert session["cardio_duration_seconds"] is None
    assert session["review_duration_seconds"] == 5400


def test_explicit_set_numbers_use_max_and_record_conflict():
    live = [workout("a", "18:30", "卧推", "卧推",
                    exercises=[{"name": "卧推", "set_number": 1},
                               {"name": "卧推", "set_number": 2},
                               {"name": "卧推", "set_number": 3},
                               {"name": "卧推", "set_number": 4},
                               {"name": "卧推", "sets": 3}])]
    action = fs.structure_sessions(live)[0]["actions"][0]
    assert action["sets"] == 4
    assert "第 4 组" in action.get("set_conflict", "")


def test_fitness_reasoning_effort_defaults_medium_and_reads_own_key():
    assert fs.fitness_reasoning_effort({}) == "medium"
    assert fs.fitness_reasoning_effort({"fitness": {"reasoning_effort": "none"}}) == "none"
    assert fs.fitness_reasoning_effort({"deepseek": {"reasoning_effort": "none"}}) == "medium"
    assert fs.fitness_reasoning_effort({"fitness": {"reasoning_effort": "bogus"}}) == "medium"


def test_review_overrides_live_value_and_marks_source():
    live = [workout("a", "18:00", "卧推", "卧推",
                    exercises=[{"name": "卧推"}])]
    review = [workout("r", "23:00", "回顾", "卧推做了五组",
                      exercises=[{"name": "卧推", "sets": 5}])]
    session = fs.structure_sessions(live + review)[0]
    action = next(a for a in session["actions"] if a["name"] == "卧推")
    assert action["sets"] == 5 and action["source"] == "review"
    assert "sets" in action["review_fields"]
    assert session["source"] == "live+review"
    assert session["verification"]["differences"] == []


def test_live_only_fields_survive_when_review_omits_them():
    live = [workout("a", "18:00", "卧推", "卧推",
                    exercises=[{"name": "卧推", "weight_value": 10, "sets": 3}])]
    review = [workout("r", "23:00", "回顾", "卧推五组",
                      exercises=[{"name": "卧推", "sets": 5}])]
    session = fs.structure_sessions(live + review)[0]
    action = session["actions"][0]
    assert action["sets"] == 5 and action["weight_value"] == 10
    assert action["source"] == "review"
    assert "weight_value" in action["live_fields"]


def test_conflict_uses_review_and_keeps_live_in_differences():
    live = [workout("a", "18:00", "卧推", "卧推",
                    exercises=[{"name": "卧推", "sets": 3}])]
    review = [workout("r", "23:00", "回顾", "卧推五组",
                      exercises=[{"name": "卧推", "sets": 5}])]
    session = fs.structure_sessions(live + review)[0]
    action = session["actions"][0]
    assert action["sets"] == 5 and action["source"] == "review"
    assert any("现场 3" in d and "回顾 5" in d for d in session["verification"]["differences"])


def test_review_only_action_is_added_once_with_review_source():
    live = [workout("a", "18:00", "卧推", "卧推",
                    exercises=[{"name": "卧推", "sets": 3}])]
    review = [workout("r", "23:00", "回顾有氧", "做了一个半小时有氧",
                      exercises=[{"name": "有氧", "duration_seconds": 5400}])]
    session = fs.structure_sessions(live + review)[0]
    cardio = [a for a in session["actions"] if a["name"] == "有氧"]
    assert len(cardio) == 1 and cardio[0]["source"] == "review"
    assert cardio[0]["duration_seconds"] == 5400
    assert session["review_prioritized_action_count"] == 1


def test_review_does_not_duplicate_existing_action():
    live = [workout("a", "18:00", "卧推", "卧推",
                    exercises=[{"name": "卧推", "sets": 3, "weight_value": 10}])]
    review = [workout("r", "23:00", "回顾", "卧推三组十公斤",
                      exercises=[{"name": "卧推", "sets": 3, "weight_value": 10}])]
    session = fs.structure_sessions(live + review)[0]
    assert len(session["actions"]) == 1
    assert session["actions"][0]["source"] == "review"
    assert session["verification"]["differences"] == []


def test_repeated_cardio_review_counts_once_with_breakdown():
    live = [workout("a", "18:00", "卧推", "卧推", exercises=[{"name": "卧推", "sets": 3}])]
    review = [
        workout("r1", "23:04", "回顾有氧", "做了一个半小时有氧",
                exercises=[{"name": "有氧", "duration_seconds": 5400}]),
        workout("r2", "23:18", "又说有氧", "有氧做了一个半小时",
                exercises=[{"name": "有氧", "duration_seconds": 5400},
                           {"name": "爬坡", "duration_seconds": 1800}]),
    ]
    session = fs.structure_sessions(live + review)[0]
    cardio = [a for a in session["actions"] if a["name"] == "有氧"]
    assert len(cardio) == 1
    assert cardio[0]["duration_seconds"] == 5400
    assert cardio[0]["source"] == "review"
    assert session["review_duration_seconds"] == 5400
    assert sum(a.get("duration_seconds") or 0 for a in session["actions"]) == 5400  # 只算有氧总数，不含分解重复
    assert [b["name"] for b in cardio[0].get("breakdown") or []] == ["爬坡", "其余有氧"]
    assert sum(b["duration_seconds"] for b in cardio[0]["breakdown"]) == 5400


def test_generic_ability_statement_does_not_override_live_weight():
    live = [workout("a", "18:00", "未命名动作", "未命名动作",
                    exercises=[{"name": "未命名动作", "weight_value": 60}])]
    review = [{"event_id": "r", "phase": "review", "name": None, "weight_value": 80,
               "title": "回顾之前重量", "quote": "以前约八十做组",
               "spoken_at": "2026-11-05T23:18:00+08:00", "_quote": "以前约八十做组",
               "exercises": [{"name": None, "weight_value": 80}]}]
    session = fs.structure_sessions(live + review)[0]
    action = session["actions"][0]
    assert action["weight_value"] == 60
    assert action["source"] == "live"
    assert any("以前约八十" in note for note in session["review_notes"])
