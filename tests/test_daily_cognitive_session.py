import json
from unittest.mock import MagicMock, patch

import pytest

from life_logger.daily_cognitive_session import (
    build_expense_ledger,
    build_fitness_ledger,
    build_nutrition_ledger,
    build_pinned_memos,
    build_smoking_ledger,
    build_work_cognition_ledger,
    generate_daily_cognitive_synthesis,
    ask_daily_cognitive_session,
)


@pytest.fixture
def sample_events():
    return [
        {
            "event_id": "evt_workout",
            "title": "健身锻炼：练腿（昨天下午5点，体感好爽）",
            "categories": ["exercise"],
            "time": {"clock": "17:00"},
            "data": {
                "exercise": {
                    "body_part": "腿部",
                    "exercise_name": "腿部力量训练",
                    "time_of_day": "17:00左右",
                    "feeling": "体感酸爽充实",
                    "day": "昨天",
                }
            },
            "evidence": {"quote": "我健身是在呃5点钟左右。啊，昨天练的腿好爽。"},
        },
        {
            "event_id": "evt_udon",
            "title": "早饭：一份乌冬面（净重200g）",
            "categories": ["food"],
            "time": {"clock": "09:00"},
            "data": {
                "food": {
                    "meal": "早饭",
                    "item": "乌冬面",
                    "weight_g": 200,
                    "estimated_calories": 375,
                    "nutrients": {
                        "energy_kcal": 375,
                        "protein_g": 9.2,
                        "fat_g": 6.4,
                        "carbohydrates_g": 66.0,
                        "sodium_mg": 1200,
                    },
                }
            },
            "evidence": {"quote": "早饭吃了一份乌冬面"},
        },
        {
            "event_id": "evt_memo_smoke",
            "title": "便签：抽根烟",
            "categories": ["idea"],
            "time": {"clock": "21:00"},
            "data": {"memo": {"content": "抽根烟", "type": "pinned_voice_memo"}},
            "evidence": {"quote": "记录一下，抽根烟", "pinned_memo": True},
        },
        {
            "event_id": "evt_smoke_relapse",
            "title": "戒烟心理博弈：反思三天承诺，妥协定为吸烟日并限时至午夜",
            "categories": ["routine"],
            "time": {"clock": "22:18"},
            "data": {
                "smoking": {
                    "action": "relapse_reflection",
                    "commitment": "戒烟三天至下周一",
                    "compromise": "算吸烟日，只吸到今天晚上凌晨12点",
                    "brand": "芒果味细支",
                }
            },
            "evidence": {"quote": "我本来答应戒烟三天的，但今天算吸烟日，只抽到凌晨12点。"},
        },
        {
            "event_id": "evt_tired_routine",
            "title": "作息调整：练腿与缺觉后精力疲惫，放弃做饭决定早睡",
            "categories": ["routine"],
            "time": {"clock": "22:19"},
            "data": {
                "energy": {"feeling": "疲劳不想做饭", "decision": "早点睡，明天早上看"},
                "routine": {"status": "sleep_deprived"},
            },
            "evidence": {"quote": "今天早点睡吧，不想做饭了，真的不想做。"},
        },
        {
            "event_id": "evt_buy_cig",
            "title": "消费支出：购买芒果味细支香烟与打火机零钱",
            "categories": ["expense", "routine"],
            "time": {"clock": "22:22"},
            "data": {
                "expense": {
                    "amount": 10.0,
                    "currency": "CNY",
                    "item": "芒果香烟（10元档）",
                    "payment_context": "便利店现金/零钱",
                },
                "smoking": {
                    "item": "芒果香烟（10元档）",
                    "action": "smoke",
                    "feeling": "就是这个味",
                },
            },
            "evidence": {"quote": "买包芒果香烟，十块钱的那个。"},
        },
        {
            "event_id": "evt_sausage",
            "title": "加餐夜宵：便利店现烤大肉粒香肠（原味/黑椒）",
            "categories": ["food"],
            "time": {"clock": "22:27"},
            "data": {
                "food": {
                    "meal": "snacks",
                    "item": "大肉粒烤肠（原味/黑椒）",
                    "portion": "1根",
                    "weight_g": 60,
                    "estimated_calories": 160,
                    "nutrients": {
                        "energy_kcal": 160.0,
                        "protein_g": 8.0,
                        "fat_g": 11.0,
                        "carbohydrates_g": 6.0,
                        "sodium_mg": 450.0,
                    },
                }
            },
            "evidence": {"quote": "吃根烤肠，热乎的肉粒肠。"},
        },
        {
            "event_id": "evt_arch_review",
            "title": "架构复盘：CAM显存卡死切火山ASR，DeepSeek升级为全天认知Session",
            "categories": ["work_communication", "idea"],
            "time": {"clock": "22:31"},
            "data": {
                "work": {
                    "domain": "Life Logger 架构重构",
                    "decisions": [
                        "电脑CAM卡死，直接全面使用火山云端ASR API",
                        "DeepSeek升级为全天Session计算一整天轨迹",
                    ],
                }
            },
            "evidence": {"quote": "本地跑CAM太吃显存了直接切火山云端ASR，DeepSeek换成全天Session。"},
        },
    ]


def test_build_nutrition_ledger(sample_events):
    ledger = build_nutrition_ledger(sample_events)
    assert ledger["has_records"] is True
    # 2026-11-07 用户决定：不知重量就不算（只说“吃根烤肠”无克数，160 kcal 转入候选，不进 totals）
    assert ledger["totals"]["calories_kcal"] == 375.0
    assert ledger["unweighed_candidate_kcal"] == 160.0
    assert ledger["totals"]["protein_g"] == 9.2
    assert ledger["totals"]["carbohydrates_g"] == 66.0
    assert ledger["totals"]["fat_g"] == 6.4
    assert ledger["totals"]["sodium_mg"] == 1200.0
    assert len(ledger["meals"]["breakfast"]) == 1
    assert len(ledger["meals"]["snacks"]) == 1


def test_build_fitness_ledger(sample_events):
    ledger = build_fitness_ledger(sample_events)
    assert ledger["has_records"] is True
    assert ledger["workout_count"] == 1
    assert "腿部" in ledger["target_body_parts"]
    assert ledger["workouts"][0]["feeling"] == "体感酸爽充实"
    assert len(ledger["recovery_reflections"]) == 1
    assert "疲劳不想做饭" in ledger["recovery_reflections"][0]["feeling"]


def test_build_smoking_ledger(sample_events):
    """2026-11-06 语义变更：买烟/念头不再计入吸烟次数。

    这份 fixture 里**没有任何"确实抽了"的原话**：
      * evt_buy_cig「消费支出：购买芒果味细支香烟与打火机零钱」= 买烟
      * evt_memo_smoke「便签：抽根烟」= 提醒自己（念头）
      * evt_smoke_relapse「戒烟心理博弈…」= 承诺/妥协
    旧断言 `total_count >= 2  # memo + smoke` 把这个 bug 固化成了期望值
    （"购买"被算成吸烟、"抽根烟"便签被算成吸烟）。新语义下三者的
    action 分别是 purchase / urge / relapse_reflection，都**不计数**。
    """
    ledger = build_smoking_ledger(sample_events)
    assert ledger["has_records"] is True
    assert ledger["total_count"] == 0
    assert ledger["purchase_count"] == 1
    assert ledger["urge_count"] == 1
    actions = {row["event_id"]: row["action"] for row in ledger["sessions"]}
    assert actions["evt_buy_cig"] == "purchase"
    assert actions["evt_memo_smoke"] == "urge"
    assert actions["evt_smoke_relapse"] == "relapse_reflection"
    assert any("芒果" in b for b in ledger["brands"])
    psychology = ledger["psychology"]
    assert psychology["has_relapse_conflict"] is True
    assert "戒烟三天至下周一" in psychology["commitments"]
    assert "算吸烟日，只吸到今天晚上凌晨12点" in psychology["compromises"]
    assert psychology["cutoff_rule"] == "限时至午夜24:00后停止"


def test_build_expense_ledger(sample_events):
    ledger = build_expense_ledger(sample_events)
    assert ledger["has_records"] is True
    assert ledger["transaction_count"] == 1
    assert ledger["total_amount"] == 10.0
    assert ledger["category_breakdown"]["tobacco"] == 10.0
    assert "beverage" not in ledger["category_breakdown"]
    tx_payment_methods = [tx["payment_context"] for tx in ledger["transactions"]]
    assert any("现金/零钱" in p for p in tx_payment_methods)


def test_build_work_cognition_ledger(sample_events):
    ledger = build_work_cognition_ledger(sample_events)
    assert ledger["has_records"] is True
    assert len(ledger["decisions"]) >= 2
    assert any("火山云端ASR" in d for d in ledger["decisions"])
    assert any("全天Session" in d for d in ledger["decisions"])


def test_build_pinned_memos(sample_events):
    memos = build_pinned_memos(sample_events)
    assert len(memos) >= 1
    assert memos[0]["text"] == "抽根烟"


def test_generate_daily_cognitive_synthesis_fallback(sample_events, tmp_path):
    # When DeepSeek API call fails or is unconfigured, fallback should include all ledgers
    with patch("life_logger.daily_cognitive_session._get_deepseek_settings", return_value=("https://api.deepseek.com", "fake_key", "deepseek-chat", None, None)):
        with patch("urllib.request.urlopen", side_effect=Exception("API connection timeout")):
            synthesis = generate_daily_cognitive_synthesis("2026-10-09", sample_events, force=True, cache_dir=tmp_path)
            assert synthesis["date"] == "2026-10-09"
            assert synthesis["fallback"] is True
            assert "nutrition_ledger" in synthesis
            assert "fitness_ledger" in synthesis
            assert "smoking_ledger" in synthesis
            assert "expense_ledger" in synthesis
            assert "work_cognition_ledger" in synthesis
            assert synthesis["expense_analysis"]["total_amount_cny"] == 10.0
            assert "芒果" in synthesis["habit_and_smoking_analysis"]["brand"]


def test_ask_daily_cognitive_session_integration(sample_events):
    mock_resp_obj = MagicMock()
    mock_resp_obj.read.return_value = json.dumps({
        "choices": [{
            "message": {
                "content": "您今天总共消费了10.00元，在便利店购买了1包芒果香烟（10元档）。"
            }
        }]
    }).encode("utf-8")
    mock_resp_obj.__enter__.return_value = mock_resp_obj

    with patch("life_logger.daily_cognitive_session._get_deepseek_settings", return_value=("https://api.deepseek.com", "fake_key", "deepseek-chat", None, None)):
        with patch("urllib.request.urlopen", return_value=mock_resp_obj):
            res = ask_daily_cognitive_session("2026-10-09", "我今天花了多少钱？", sample_events)
            assert "10.00元" in res["answer"]
            assert "芒果香烟" in res["answer"]


# ---------------------------------------------------------------------------
# 2026-10-19 回归：日报金额必须等于账本金额（不许用模型自己"总结"的数）
#
# 事故：synthesis-2026-10-17.json 里 expense_analysis.total_amount_cny = 16.6，
# 同文件 expense_ledger.total_amount = 26.8 —— 一份文件两个数，日报和账本互相打脸。
# 根因：生成时把模型返回的 JSON 原样写盘，模型"总结"的金额直接覆盖了展示口径。
# ---------------------------------------------------------------------------
def test_synthesis_expense_total_always_equals_ledger(tmp_path):
    """模型在 expense_analysis 里报 16.6，账本是 14.0 → 必须以后者为准。"""
    from life_logger.daily_cognitive_session import build_expense_ledger

    events = [
        {
            "event_id": "e1", "title": "购买烧鸭饭 (¥12.00)", "categories": ["expense", "food"],
            "activity_type": "meal", "confidence": 0.95,
            "time": {"absolute": "2026-10-17T14:56:32+08:00"},
            "evidence": {"quote": "要一份烧鸭饭", "reported_at": "2026-10-17T14:56:32+08:00"},
            "data": {"expense": {"amount": 12.0, "currency": "CNY", "item": "烧鸭饭"}},
        },
        {
            "event_id": "e2", "title": "购买两个冰杯 (¥2.00)", "categories": ["expense", "food"],
            "activity_type": "purchase", "confidence": 0.95,
            "time": {"absolute": "2026-10-17T15:15:24+08:00"},
            "evidence": {"quote": "帮我点两个冰杯", "reported_at": "2026-10-17T15:15:24+08:00"},
            "data": {"expense": {"amount": 2.0, "currency": "CNY", "item": "冰杯"}},
        },
        {
            "event_id": "e3", "title": "线下支付：红塔山 (¥11.00)", "categories": ["expense"],
            "activity_type": "pos_payment_broadcast", "confidence": 0.95,
            "time": {"absolute": "2026-10-17T15:19:39+08:00"},
            "evidence": {"quote": "支付到账十一元", "reported_at": "2026-10-17T15:19:39+08:00"},
            "data": {"expense": {"amount": 11.0, "currency": "CNY", "item": "红塔山"}},
        },
        {
            "event_id": "e4", "title": "线下支付：把冰水 (¥1.80)", "categories": ["expense"],
            "activity_type": "pos_payment_broadcast", "confidence": 0.95,
            "time": {"absolute": "2026-10-17T14:49:10+08:00"},
            "evidence": {"quote": "支付到账一点八元", "reported_at": "2026-10-17T14:49:10+08:00"},
            "data": {"expense": {"amount": 1.8, "currency": "CNY", "item": "冰水"}},
        },
    ]
    ledger = build_expense_ledger(events)
    # [2026-10-23 P-50] 收款播报（「支付到账十一元 / 一点八元」）降级为 B 档待确认，
    # 不进总额；只有本人自述的两笔 ¥12 + ¥2 计入 → 14.0（原口径 26.8 已作废）。
    assert ledger["total_amount"] == 14.0  # 账本真值
    assert ledger["pending_broadcast_count"] == 2

    # 模型返回的 JSON 自己在 expense_analysis 里写了个错的 16.6
    model_payload = {
        "executive_summary": "测试",
        "expense_analysis": {"total_amount_cny": 16.6, "items_summary": "模型自己总结的"},
        "nutrition_analysis": {}, "fitness_analysis": {}, "habit_and_smoking_analysis": {},
        "work_and_cognition_analysis": {}, "attention_and_energy": {},
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(
        {"choices": [{"message": {"content": json.dumps(model_payload, ensure_ascii=False)}}]}
    ).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch(
        "life_logger.daily_cognitive_session._get_deepseek_settings",
        return_value=("https://api.deepseek.com", "fake_key", "deepseek-chat", None, None),
    ):
        with patch("urllib.request.urlopen", return_value=mock_resp):
            synthesis = generate_daily_cognitive_synthesis(
                "2026-10-17", events, force=True, cache_dir=tmp_path
            )

    # 展示口径 = 账本
    assert synthesis["expense_analysis"]["total_amount_cny"] == 14.0
    assert synthesis["expense_analysis"]["source"] == "expense_policy.build_expense_ledger"
    assert synthesis["expense_analysis"]["policy_version"] == ledger["policy_version"]
    # 模型原话留档，但不再参与口径
    assert synthesis["expense_analysis"]["model_reported_total"] == 16.6
    # 落盘缓存也必须是对齐后的
    cached = json.loads((tmp_path / "synthesis-2026-10-17.json").read_text())
    assert cached["expense_analysis"]["total_amount_cny"] == 14.0
    assert cached["expense_ledger"]["total_amount"] == 14.0


def test_synthesis_expense_total_matches_ledger_when_model_omits_section(tmp_path, sample_events):
    """模型没给 expense_analysis 时也要按账本补齐，而不是缺字段。"""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(
        {"choices": [{"message": {"content": json.dumps({"executive_summary": "无消费段"}, ensure_ascii=False)}}]}
    ).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch(
        "life_logger.daily_cognitive_session._get_deepseek_settings",
        return_value=("https://api.deepseek.com", "fake_key", "deepseek-chat", None, None),
    ):
        with patch("urllib.request.urlopen", return_value=mock_resp):
            synthesis = generate_daily_cognitive_synthesis(
                "2026-10-09", sample_events, force=True, cache_dir=tmp_path
            )
    ledger = build_expense_ledger(sample_events)
    assert synthesis["expense_analysis"]["total_amount_cny"] == ledger["total_amount"]
    assert "model_reported_total" not in synthesis["expense_analysis"]


def test_cached_synthesis_is_reconciled_at_read_time_without_touching_file(tmp_path):
    """历史缓存金额过期时，读层对齐到当前账本，且**不改文件**。

    现场：synthesis-2026-10-17.json 存的是 16.6（生成时烧鸭饭还没合成、
    复盘可乐还重复计账），当前账本是 26.8。
    """
    from life_logger.daily_cognitive_session import (
        build_expense_ledger,
        reconcile_synthesis_expense,
    )

    cached = {
        "date": "2026-10-17",
        "expense_analysis": {"total_amount_cny": 16.6, "items_summary": "四项小额"},
        "expense_ledger": {"total_amount": 16.6},
    }
    ledger = build_expense_ledger([
        {
            "event_id": "x1", "title": "购买烧鸭饭 (¥12.00)", "categories": ["expense", "food"],
            "confidence": 0.95, "time": {"absolute": "2026-10-17T14:56:32+08:00"},
            "evidence": {"quote": "要一份烧鸭饭", "reported_at": "2026-10-17T14:56:32+08:00"},
            "data": {"expense": {"amount": 12.0, "currency": "CNY", "item": "烧鸭饭"}},
        }
    ])
    out = reconcile_synthesis_expense(cached, ledger)

    assert out["expense_analysis"]["total_amount_cny"] == 12.0  # 展示=当前账本
    assert out["stale_ledger_snapshot"]["cached_expense_total"] == 16.6
    assert out["stale_ledger_snapshot"]["current_expense_total"] == 12.0
    # 传入对象不被就地篡改（缓存文件内容不变）
    assert cached["expense_analysis"]["total_amount_cny"] == 16.6


def test_reconcile_is_noop_when_cache_already_matches(tmp_path):
    from life_logger.daily_cognitive_session import (
        build_expense_ledger,
        reconcile_synthesis_expense,
    )

    ledger = build_expense_ledger([])
    cached = {"expense_analysis": {"total_amount_cny": 0.0}}
    out = reconcile_synthesis_expense(cached, ledger)
    assert "stale_ledger_snapshot" not in out
