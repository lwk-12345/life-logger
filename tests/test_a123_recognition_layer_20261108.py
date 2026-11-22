"""A1–A3 识别层回归测试（2026-11-08）：助手回合工作归类、空泛标题收敛、小时主标签本人内容优先。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from life_logger import activity_digest as digest
from life_logger import activity_prompt_v3 as v3
from life_logger.activity_timeline_runtime import ActivityTimelineRuntime
from test_activity_v3_shadow import CUTOFF, LOOKBACK, RECORD, response, run, setup as setup, v3_body  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def test_prompt_v33_includes_a1_and_a2_rules(setup) -> None:
    raw = (ROOT / "config/activity-prompt-v3.3.md").read_text(encoding="utf-8")
    for anchor in (
        "## A123候选：先识别对话目的，再给具体标题",
        "A1：对软件/AI助手说的请求、查询、验收反馈是工作·项目",
        "Club 可能是工具名转写，但不能擅自改写成 Claude",
        "A2：标题必须写出具体对象和可确认的动作/结论",
        "禁用“报告事项成功”“凌晨感叹状态”“提及某内容”等空泛模板",
        "反馈这边均成功（事项未明）",
        "称今天崩了（指代未明）",
        "谈主播订阅量及SUVID（含义未明）",
    ):
        assert anchor in raw
    runtime = setup({"version": "v3", "mode": "shadow", "user_profile": ""})
    rendered = v3.build_prompt(runtime.load_config(), CUTOFF, LOOKBACK, [RECORD], [])
    assert "## A123候选：先识别对话目的，再给具体标题" in rendered
    assert "标题≤20 字、动作≤40 字" in rendered


def test_assistant_work_item_two_cue_and_browser_boundary() -> None:
    # 1. Explicit tool name in quote -> True even in browser Zen
    openclaw_item = {
        "fact_type": "other",
        "evidence": [{
            "source_id": "s1",
            "quote": "OpenClaw。",
            "input_context": {"app_label": "Zen", "target_bundle": "app.zen-browser.zen", "kind": "front_app"},
        }],
    }
    assert v3.assistant_work_item(openclaw_item, {}) is True

    # 2. Front-app work software (Alma) + addressed assistant turn -> True
    success_item = {
        "fact_type": "other",
        "evidence": [{
            "source_id": "s2",
            "quote": "我这边都成功了",
            "input_context": {"app_label": "Alma", "target_bundle": "com.yetone.alma", "kind": "front_app"},
        }],
    }
    assert v3.assistant_work_item(success_item, {}) is True

    club_item = {
        "fact_type": "other",
        "evidence": [{
            "source_id": "s3",
            "quote": "Club我现在怎么用啊？",
            "input_context": {"app_label": "Alma", "target_bundle": "com.yetone.alma", "kind": "front_app"},
        }],
    }
    assert v3.assistant_work_item(club_item, {}) is True

    # 3. Browser Zen + ambiguous emotional remark ("今天崩了呀") -> False (do not over-classify browser speech)
    browser_remark = {
        "fact_type": "other",
        "evidence": [{
            "source_id": "s4",
            "quote": "今天崩了呀。凌晨三点。",
            "input_context": {"app_label": "Zen", "target_bundle": "app.zen-browser.zen", "kind": "dictation"},
        }],
    }
    assert v3.assistant_work_item(browser_remark, {}) is False

    # 4. Front-app Alma + personal life reminder -> False
    haircut_todo = {
        "fact_type": "todo",
        "todo": {"text": "提醒我理发", "due": None},
        "evidence": [{
            "source_id": "s5",
            "quote": "提醒我明天下午去理发",
            "input_context": {"app_label": "Alma", "target_bundle": "com.yetone.alma", "kind": "front_app"},
        }],
    }
    assert v3.assistant_work_item(haircut_todo, {}) is False


def test_apply_app_policy_multi_item_boundary_and_life_fact_protection() -> None:
    # Section 5.4: multi-item work activity with trailing acknowledgement ("嗯。") still upgrades to 工作·项目
    multi = {
        "schema_version": 3,
        "activities": [{
            "activity_id": "act-honggujian",
            "title": "请助手查找红谷剑安装包",
            "summary": "在GitHub查找安装包并打开首个网页",
            "status": "completed",
            "lifecycle": "live",
            "category": "生活·日常",
            "start_at": None,
            "end_at": None,
            "items": [
                {
                    "item_id": "i1",
                    "action": "请求查找安装包",
                    "fact_type": "other",
                    "status": "completed",
                    "evidence": [{"source_id": "s1", "quote": "帮我查找一下这个红谷剑APP的安装包"}],
                },
                {
                    "item_id": "i2",
                    "action": "对方回应嗯",
                    "fact_type": "other",
                    "status": "completed",
                    "evidence": [{"source_id": "s2", "quote": "嗯。"}],
                },
            ],
        }],
    }
    apps = {
        "s1": {"kind": "front_app", "app_label": "Terminal", "target_bundle": "com.apple.Terminal", "work_app": False},
        "s2": {"kind": "front_app", "app_label": "Terminal", "target_bundle": "com.apple.Terminal", "work_app": False},
    }
    out = v3.apply_app_policy(multi, apps)
    assert out["activities"][0]["category"] == "工作·项目"

    # Physical/financial facts (_A123_LIFE) are never erased even if dictated while a work app is active
    exp_body = v3_body()
    exp_checked = v3.apply_app_policy(
        exp_body,
        {"source-a": {"kind": "dictation", "app_label": "Alma", "target_bundle": "com.yetone.alma", "work_app": True}},
    )
    exp_item = exp_checked["activities"][0]["items"][0]
    assert exp_item["fact_type"] == "expense"
    assert exp_item["expense"] == {"amount": 16, "currency": "CNY", "item": "物品", "payment_status": "unknown"}
    assert exp_checked["activities"][0]["category"] == "消费·购物"


def test_run_window_persists_policy_body_in_report_result(setup) -> None:
    body = v3_body()
    act = body["activities"][0]
    act["title"] = "报告事项成功"
    act["category"] = "闲聊·其他"
    item = act["items"][0]
    item.update(fact_type="other", expense=None, action="反馈成功")
    rec = copy.deepcopy(RECORD)
    rec["text"] = "我这边都成功了"
    rec["input_context"] = {"kind": "front_app", "app_label": "Alma", "target_bundle": "com.yetone.alma"}
    item["evidence"] = [{"source_id": rec["source_id"], "quote": rec["text"]}]

    runtime = setup({"version": "v3", "mode": "shadow", "user_profile": ""}, lambda _: response(body))
    runtime._process_window_model(CUTOFF, LOOKBACK, [rec], {"activities": []}, lambda _: {"activities": []})
    archive = json.loads(next((runtime.output_root / "activity-shadow-v3").rglob("*.json")).read_text())
    assert archive["status"] == "valid"
    res_act = archive["result"]["activities"][0]
    assert res_act["category"] == "工作·项目"
    assert res_act["title"] == "反馈这边均成功（事项未明）"


def test_demote_agent_and_work_todos_upgrades_persisted_1107_and_1108_cases() -> None:
    payload = {
        "activities": [
            {
                "activity_id": "act_L003",
                "title": "报告事项成功",
                "title_original": "报告事项成功",
                "category": "闲聊·其他",
                "lifecycle": "live",
                "items": [{
                    "item_id": "i1",
                    "action": "表示这边都成功了",
                    "fact_type": "other",
                    "evidence": [{
                        "source_id": "s1",
                        "quote": "我这边都成功了",
                        "input_context": {"app_label": "Alma", "target_bundle": "com.yetone.alma", "kind": "front_app"},
                    }],
                }],
            },
            {
                "activity_id": "act_club_L145",
                "title": "询问Club使用方法",
                "title_original": "询问Club使用方法",
                "category": "闲聊·其他",
                "lifecycle": "live",
                "items": [{
                    "item_id": "i2",
                    "action": "询问转写为Club的对象如何使用",
                    "fact_type": "other",
                    "evidence": [{
                        "source_id": "s2",
                        "quote": "Club我现在怎么用啊？",
                        "input_context": {"app_label": "Alma", "target_bundle": "com.yetone.alma", "kind": "front_app"},
                    }],
                }],
            },
            {
                "activity_id": "act_subscription_L105",
                "title": "提及主播与订阅量",
                "title_original": "提及主播与订阅量",
                "category": "闲聊·其他",
                "lifecycle": "live",
                "items": [{
                    "item_id": "i3",
                    "action": "提及主播、订阅量及不明标识",
                    "fact_type": "other",
                    "evidence": [{"source_id": "s3", "quote": "反正就是订阅，就是订阅量，他不是有个SUVID。"}],
                }],
            },
            {
                "activity_id": "act_remark_L022",
                "title": "凌晨感叹状态",
                "title_original": "凌晨感叹状态",
                "category": "闲聊·其他",
                "lifecycle": "live",
                "items": [{
                    "item_id": "i4",
                    "action": "感叹今天崩了",
                    "fact_type": "other",
                    "evidence": [{
                        "source_id": "s4",
                        "quote": "今天崩了呀。",
                        "input_context": {"app_label": "Zen", "target_bundle": "app.zen-browser.zen", "kind": "dictation"},
                    }],
                }],
            },
        ]
    }
    cleaned = v3.demote_agent_and_work_todos(copy.deepcopy(payload))
    by_id = {a["activity_id"]: a for a in cleaned["activities"]}
    assert by_id["act_L003"]["category"] == "工作·项目"
    assert by_id["act_L003"]["title"] == "反馈这边均成功（事项未明）"
    assert by_id["act_L003"]["title_original"] == "反馈这边均成功（事项未明）"

    assert by_id["act_club_L145"]["category"] == "工作·项目"
    assert by_id["act_club_L145"]["title"] == "询问Club使用方式"

    assert by_id["act_subscription_L105"]["category"] == "闲聊·其他"
    assert by_id["act_subscription_L105"]["title"] == "谈主播订阅量及SUVID"

    assert by_id["act_remark_L022"]["category"] == "闲聊·其他"
    assert by_id["act_remark_L022"]["title"] == "称今天崩了（指代未明）"


def test_real_persisted_1107_and_1108_read_day_projection() -> None:
    output_root = ROOT / "output/local-web"
    if not (output_root / "activity-timeline/jev/2026-11-08.json").exists():
        return
    rt = ActivityTimelineRuntime(output_root=output_root, config_path=ROOT / "config/local.json")
    day_1108 = rt.read_day("2026-11-08", for_display=True)
    by_id_08 = {a["activity_id"]: a for a in day_1108["activities"]}
    assert by_id_08["act_L003"]["category"] == "工作·项目"
    assert by_id_08["act_L003"]["title_original"] == "反馈这边均成功（事项未明）"
    assert by_id_08["act_club_L145"]["category"] == "工作·项目"
    assert by_id_08["act_club_L145"]["title_original"] == "询问Club使用方式"
    assert by_id_08["act_openclaw_L011"]["category"] == "工作·项目"
    assert by_id_08["act_question_L007"]["category"] == "工作·项目"
    assert by_id_08["act_subscription_L105"]["title_original"] == "谈主播订阅量及SUVID"
    assert by_id_08["act_remark_L022"]["title_original"] == "称今天崩了（指代未明）"
    assert by_id_08["act_remark_L022"]["category"] == "闲聊·其他"

    day_1107 = rt.read_day("2026-11-07", for_display=True)
    by_id_07 = {a["activity_id"]: a for a in day_1107["activities"]}
    assert by_id_07["act_app_install_search_01"]["category"] == "工作·项目"
    assert by_id_07["act_food_chaomifen_mention_01"]["speech_target"] == "未知"
    assert by_id_07["act_food_chaomifen_mention_01"]["title"] == "表示不吃炒米粉"
    assert by_id_07["act_store_order_chaofen_01"]["speech_target"] == "商家/店员"


def test_activity_digest_never_downgrades_work_activity_to_idle_chat(tmp_path) -> None:
    day = "2026-11-08"
    acts = [
        {
            "activity_id": "act_openclaw_L011",
            "title": "呼叫OpenClaw",
            "category": "工作·项目",
            "lifecycle": "live",
            "spoken_at": f"{day}T03:02:00+08:00",
            "items": [{
                "item_id": "i1",
                "fact_type": "other",
                "status": "completed",
                "evidence": [{"quote": "OpenClaw。", "source_label": "self"}],
            }],
        },
        {
            "activity_id": "act_club_L145",
            "title": "询问Club使用方法",
            "category": "闲聊·其他",
            "lifecycle": "live",
            "spoken_at": f"{day}T02:51:00+08:00",
            "items": [{
                "item_id": "i2",
                "fact_type": "other",
                "status": "completed",
                "evidence": [{
                    "quote": "Club我现在怎么用啊？",
                    "source_label": "self",
                    "input_context": {"app_label": "Alma", "target_bundle": "com.yetone.alma", "kind": "front_app"},
                }],
            }],
        },
    ]
    prepared = digest.prepare(acts, day, include_media=True)
    cats, errors = digest.validate_categories(
        {
            "categories": [
                {"id": "act_openclaw_L011", "c": "闲聊·其他", "m": False, "why": "短句呼唤"},
                {"id": "act_club_L145", "c": "闲聊·其他", "m": False, "why": "日常提问"},
            ]
        },
        prepared,
    )
    assert not errors
    assert cats["act_openclaw_L011"]["category_model"] == "工作·项目"
    assert cats["act_club_L145"]["category_model"] == "工作·项目"

    # Also verify read_cached sanitizes stale cached "闲聊·其他" for work activities
    src_path = tmp_path / "activity-timeline" / "jev" / f"{day}.json"
    src_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.write_text(json.dumps({"activities": acts}, ensure_ascii=False), encoding="utf-8")
    digest.atomic_save(
        digest.cache_path(tmp_path, day),
        {
            "schema": digest.SCHEMA,
            "date": day,
            "status": "ready",
            "cards": [],
            "categories": {
                "act_openclaw_L011": {"category_model": "闲聊·其他", "looks_like_media": False, "reason": "旧缓存"},
                "act_club_L145": {"category_model": "闲聊·其他", "looks_like_media": False, "reason": "旧缓存"},
            },
        },
    )
    cached = digest.read_cached(tmp_path, day)
    assert cached["categories"]["act_openclaw_L011"]["category_model"] == "工作·项目"
    assert cached["categories"]["act_club_L145"]["category_model"] == "工作·项目"
