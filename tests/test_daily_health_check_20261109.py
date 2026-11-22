from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import daily_health_check as dhc


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _init_review_questions_db(root: Path, rows: list[tuple[str, str, str, float]]) -> None:
    db_path = root / "review-questions" / "review-questions.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS question_state (
                source_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                prior_status TEXT,
                reason TEXT,
                evidence_json TEXT,
                revision INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL
            )
            """
        )
        for sid, state, reason, upd in rows:
            conn.execute(
                "INSERT INTO question_state (source_id, state, reason, updated_at) VALUES (?, ?, ?, ?)",
                (sid, state, reason, upd),
            )
        conn.commit()
    finally:
        conn.close()


def _build_fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "local-web"
    root.mkdir(parents=True, exist_ok=True)

    # 1. secrets.json：验证绝不泄露令牌
    _write_json(
        root / "secrets.json",
        {"mobile_access_token": "SUPER_SECRET_TOKEN_NEVER_PRINT_12345"},
    )

    # 2. 2026-11-06 JEV 活动（含自动消费1笔、自动饮食1笔、自动运动1笔、自动吸烟1笔 + 购买烟1笔不计入抽烟）
    _write_json(
        root / "activity-timeline" / "jev" / "2026-11-06.json",
        {
            "date": "2026-11-06",
            "activities": [
                {
                    "id": "act_1106_01",
                    "activity_id": "act_1106_01",
                    "title": "日常买水与吃饭锻炼",
                    "category": "饮食与消费",
                    "lifecycle": "live",
                    "occurrence_start": "2026-11-06T12:00:00+08:00",
                    "spoken_at": "2026-11-06T12:00:00+08:00",
                    "items": [
                        {
                            "id": "it_exp_1",
                            "item_id": "it_exp_1",
                            "fact_type": "expense",
                            "status": "completed",
                            "action": "买矿泉水",
                            "amount": 2.0,
                            "currency": "CNY",
                            "spoken_at": "2026-11-06T12:01:00+08:00",
                            "occurred_at": "2026-11-06T12:01:00+08:00",
                            "transaction_evidence": {
                                "claim": "paid",
                                "state": "completed",
                                "amount_quote": "花了两块钱买矿泉水",
                                "transaction_quote": "花了两块钱买矿泉水",
                            },
                            "evidence": [
                                {
                                    "source_id": "src_1106_exp1",
                                    "source_label": "self",
                                    "quote": "花了两块钱买矿泉水",
                                    "reported_at": "2026-11-06T12:01:00+08:00",
                                }
                            ],
                        },
                        {
                            "id": "it_food_1",
                            "item_id": "it_food_1",
                            "fact_type": "food",
                            "status": "completed",
                            "action": "吃了100克鸡胸肉",
                            "food": {"name": "鸡胸肉", "grams": 100, "meal": "lunch"},
                            "spoken_at": "2026-11-06T12:10:00+08:00",
                            "occurred_at": "2026-11-06T12:10:00+08:00",
                            "evidence": [
                                {
                                    "source_id": "src_1106_food1",
                                    "source_label": "self",
                                    "quote": "中午吃了100克鸡胸肉",
                                    "reported_at": "2026-11-06T12:10:00+08:00",
                                }
                            ],
                        },
                        {
                            "id": "it_fit_1",
                            "item_id": "it_fit_1",
                            "fact_type": "exercise",
                            "status": "completed",
                            "action": "回顾昨天练了胸",
                            "spoken_at": "2026-11-06T18:00:00+08:00",
                            "occurred_at": "2026-11-06T18:00:00+08:00",
                            "evidence": [
                                {
                                    "source_id": "src_1106_fit1",
                                    "source_label": "self",
                                    "quote": "回顾昨天练了胸",
                                    "reported_at": "2026-11-06T18:00:00+08:00",
                                }
                            ],
                        },
                        {
                            "id": "it_smk_1",
                            "item_id": "it_smk_1",
                            "fact_type": "smoke",
                            "status": "completed",
                            "action": "抽了一根烟",
                            "spoken_at": "2026-11-06T19:00:00+08:00",
                            "occurred_at": "2026-11-06T19:00:00+08:00",
                            "evidence": [
                                {
                                    "source_id": "src_1106_smk1",
                                    "source_label": "self",
                                    "quote": "刚抽了一根烟",
                                    "reported_at": "2026-11-06T19:00:00+08:00",
                                }
                            ],
                        },
                    ],
                }
            ],
        },
    )

    # 3. 2026-11-06 手动补录 1 笔消费 + 1 笔用户改过/确认的自动消费记录
    _write_json(
        root / "jobs" / "job-20261106-200000-aaaa1111" / "manual-events.json",
        {
            "events": [
                {
                    "event_id": "manual_evt_1106_coffee",
                    "artifact_path": "2026-11-06/events.json",
                    "title": "手冲咖啡",
                    "categories": ["expense"],
                    "activity_type": "purchase",
                    "status": "completed",
                    "source_type": "user_supplement",
                    "time": {"absolute": "2026-11-06T15:00:00+08:00", "precision": "exact"},
                    "data": {"expense": {"amount": 18.0, "currency": "CNY", "item": "手冲咖啡"}},
                    "evidence": {"source": "user_judgment", "quote": "买杯手冲咖啡十八"},
                }
            ]
        },
    )

    _write_json(
        root / "ledger-review.json",
        {
            "schema": "ledger-review-v1",
            "decisions": {
                "act_1106_01#it_exp_1": {
                    "action": "confirm",
                    "date": "2026-11-06",
                    "amount": 2.5,
                    "label": "买矿泉水（改价）",
                    "decided_by": "user",
                    "decided_at": "2026-11-06T21:00:00+08:00",
                },
                # 针对 manual_evt 的操作不应重复算进“3.用户改过/删过的自动记录”
                "manual_evt_1106_coffee": {
                    "action": "confirm",
                    "date": "2026-11-06",
                    "amount": 18.0,
                    "decided_by": "user",
                    "decided_at": "2026-11-06T21:05:00+08:00",
                },
            },
        },
    )

    # 4. Watch 会话与 Formal ASR 转写分段（3段：1段 completed，1段 no_speech，1段 failed）
    sess_dir = root / "watch-sessions" / "watch-dev1" / "sess-1106"
    _write_json(
        sess_dir / "manifest.json",
        {
            "schema_version": "watch-session-v1",
            "device_id": "watch-dev1",
            "session_id": "sess-1106",
            "started_at": "2026-11-06T10:00:00+08:00",
            "ended_at": "2026-11-06T10:03:00+08:00",
            "state": "complete",
            "segments": [
                {"sequence": 0, "start_offset_ms": 0, "duration_ms": 60_000},
                {"sequence": 1, "start_offset_ms": 60_000, "duration_ms": 60_000},
                {"sequence": 2, "start_offset_ms": 120_000, "duration_ms": 60_000},
            ],
        },
    )
    _write_json(
        sess_dir / "formal-asr" / "segments" / "000000" / "state.json",
        {"sequence": 0, "status": "completed", "rows": [{"text": "花了两块钱买矿泉水"}]},
    )
    _write_json(
        sess_dir / "formal-asr" / "segments" / "000001" / "state.json",
        {"sequence": 1, "status": "no_speech", "rows": []},
    )
    _write_json(
        sess_dir / "formal-asr" / "segments" / "000002" / "state.json",
        {"sequence": 2, "status": "failed", "error": "asr timeout", "rows": []},
    )

    # 5. 待判断队列产物（2026-11-06 新进 2 条 needs_review，其中 1 条已被用户在 sqlite 回答，剩 1 条 open）
    _write_json(
        sess_dir / "formal-asr" / "live-context" / "context-extraction.json",
        {
            "pending_sources": [
                {
                    "source_id": "src_1106_q_open",
                    "segment_id": "seg_0001",
                    "quote": "今天还买了个小面包",
                    "reason": "low_confidence",
                    "reported_at": "2026-11-06T10:01:00+08:00",
                },
                {
                    "source_id": "src_1106_q_answered",
                    "segment_id": "seg_0002",
                    "quote": "顺便去买包烟",
                    "reason": "low_confidence",
                    "reported_at": "2026-11-06T10:02:00+08:00",
                },
            ]
        },
    )
    _init_review_questions_db(
        root,
        [
            ("src_1106_q_answered", "answered", "需要我判断：personal", 1793970000.0),
        ],
    )

    return root


def test_compute_daily_health_metrics_and_formulas(tmp_path: Path) -> None:
    root = _build_fixture_root(tmp_path)
    report = dhc.compute_daily_health(
        root,
        date_from="2026-11-06",
        date_to="2026-11-06",
        offline=True,
    )

    assert report["schema"] == "daily-health-check-v1"
    assert len(report["days"]) == 1
    day = report["days"][0]
    assert day["date"] == "2026-11-06"

    # 1. 自动入账：消费1 + 饮食1 + 运动1 + 吸烟1 = 4笔
    auto = day["auto_entries"]
    assert auto["expense"] == 1
    assert auto["nutrition"] == 1
    assert auto["fitness"] == 1
    assert auto["smoking"] == 1
    assert auto["total"] == 4
    # 运动里“回顾昨天练了胸”被标记为口述复盘难分提示
    assert auto["spoken_recap_in_auto"]["fitness"] == 1
    assert auto["spoken_recap_in_auto"]["total"] == 1

    # 2. 用户手动补录：manual_evt_1106_coffee 消费 1 笔
    manual = day["manual_entries"]
    assert manual["expense"] == 1
    assert manual["nutrition"] == 0
    assert manual["fitness"] == 0
    assert manual["smoking"] == 0
    assert manual["total"] == 1
    assert "区分不了" in manual["distinguishability"]["overall"]
    assert len(manual["distinguishability"]["missing_fields_to_add"]) >= 3

    # 3. 用户改过/删过的自动记录：ledger-review.json 中对 act_1106_01#it_exp_1 确认改价 1 笔
    mod = day["modified_or_deleted_auto"]
    assert mod["expense"] == 1
    assert mod["total"] == 1

    # 4. 待你确认：当天新进 2 条，1 条已 answered，剩余 1 条
    jq = day["judgment_queue"]
    assert jq["new_count"] == 2
    assert jq["open_count"] == 1

    # 5. 录音与转写：3 分钟，3 段（1 有效 / 1 失败 / 1 空段）
    asr = day["recording_and_asr"]
    assert asr["recording_minutes"] == pytest.approx(3.0)
    assert asr["segment_total"] == 3
    assert asr["segment_completed"] == 1
    assert asr["segment_failed"] == 1
    assert asr["segment_empty"] == 1
    assert asr["failed_or_empty_total"] == 2

    # 派生指标：
    # 被用户纠正比例 = (2 + 3) / (1 + 2) = (1 + 1) / (4 + 1) = 2 / 5 = 0.4
    # 操心次数 = 2 + 3 + 4当天新进 = 1 + 1 + 2 = 4
    drv = day["derived"]
    assert drv["correction_ratio"] == pytest.approx(0.4)
    assert drv["correction_ratio_display"] == "40.0% (2/5)"
    assert drv["worry_count"] == 4


def test_cli_table_and_json_no_token_leak(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _build_fixture_root(tmp_path)

    # 表格输出模式
    rc_table = dhc.main(
        ["--from", "2026-11-06", "--to", "2026-11-06", "--root", str(root), "--offline"]
    )
    assert rc_table == 0
    out_table = capsys.readouterr().out
    assert "2026-11-06" in out_table
    assert "1/1/1/1=4笔" in out_table
    assert "40.0% (2/5)" in out_table
    assert "SUPER_SECRET_TOKEN_NEVER_PRINT_12345" not in out_table

    # JSON 输出模式
    rc_json = dhc.main(
        ["--from", "2026-11-06", "--to", "2026-11-06", "--root", str(root), "--offline", "--json"]
    )
    assert rc_json == 0
    out_json = capsys.readouterr().out
    assert "SUPER_SECRET_TOKEN_NEVER_PRINT_12345" not in out_json
    parsed = json.loads(out_json)
    assert parsed["days"][0]["derived"]["worry_count"] == 4


def test_nutrition_user_adoption_and_zero_denominator_day(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    root.mkdir(parents=True, exist_ok=True)

    # 2026-11-07：测试饮食账中 user_confirmed=True（手动采纳候选）与 superseded_by_user_adoption=True（原自动记录被替换）
    daily_payload = {
        "date": "2026-11-07",
        "expense_ledger": {"transactions": []},
        "nutrition_ledger": {
            "food_records": [
                {
                    "event_id": "manual_evt_food_adopted",
                    "item": "炒河粉",
                    "is_consumed": True,
                    "origin": "event",
                    "user_confirmed": True,
                    "estimate_basis": "user_adopted_candidate",
                    "nutrition_source": "user_judgment",
                    "calories_kcal": 1077.0,
                    "quote": "一份炒河粉",
                }
            ],
            "other_records": [
                {
                    "event_id": "activity:2026-11-07:act_01:it_01",
                    "item": "炒河粉",
                    "is_consumed": False,
                    "origin": "activity",
                    "superseded_by_user_adoption": True,
                }
            ],
            "unweighed_items": [
                {
                    "event_id": "activity:2026-11-07:act_02:it_02",
                    "item": "炸鸡排",
                    "portion_estimate_candidate": {"grams": 200, "calories_kcal": 520.0},
                }
            ],
        },
        "fitness_ledger": {"workouts": []},
        "smoking_ledger": {"sessions": [{"event_id": "activity:2026-11-07:act_s:it_s", "action": "purchase"}]},
    }

    ledger_res = dhc._analyze_ledgers_for_day(root, "2026-11-07", daily_payload, {}, {})
    assert ledger_res["auto_entries"]["total"] == 0
    assert ledger_res["manual_entries"]["nutrition"] == 1
    assert ledger_res["manual_entries"]["total"] == 1
    assert ledger_res["modified_or_deleted_auto"]["nutrition"] == 1
    assert ledger_res["modified_or_deleted_auto"]["total"] == 1

    jq_res = dhc._analyze_judgment_queue(
        root, ["2026-11-07"], {"2026-11-07": daily_payload}, offline=True
    )
    assert jq_res["2026-11-07"]["open_calories"] == 1
    assert jq_res["2026-11-07"]["new_count"] == 1

    # 2026-11-08：空日测试分母为 0 时的派生指标
    empty_report = dhc.compute_daily_health(
        root, date_from="2026-11-08", date_to="2026-11-08", offline=True
    )
    d08 = empty_report["days"][0]
    assert d08["auto_entries"]["total"] == 0
    assert d08["manual_entries"]["total"] == 0
    assert d08["derived"]["correction_ratio"] is None
    assert d08["derived"]["correction_ratio_display"] == "N/A (0/0)"
    assert d08["derived"]["worry_count"] == 0

