"""Tests for orthogonal 3D activity classification (speaker × target × topic).

Verifies isolated counterexamples:
1. Media talking about models/code (e.g. 29 yuan Cloud Opus/Gemini toolkit pitch)
   -> speech_target="媒体内容", category="浏览·视频" (NOT Codex).
2. Media video reviewing flash models (e.g. 赛博逗蛐蛐 Flash模型大乱斗)
   -> speech_target="媒体内容", category="浏览·视频" (NOT Codex).
3. User talking to DeepSeek Harness about BOM cost:
   - Without hardcoding BOM or guessing Harness;
   - Structural user correction by activity_id overrides target="DeepSeek Harness" with source="user_correction".
4. "帮我查一下" directive with different targets:
   - 4a: "帮我查一下之前豆包ASR多少钱" without explicit target -> speech_target="未知" (no guessing).
   - 4b: "帮我查一下" to a friend/colleague with user correction -> speech_target="朋友/同事".
   - 4c: "帮我查一下" to DeepSeek Harness with user correction -> speech_target="DeepSeek Harness".
5. Explicit structural target Codex with verifiable source:
   - Contains explicit speech_target="Codex" and speech_target_source="explicit_intent" (or structural_evidence)
   - -> speech_target="Codex", category="编程·项目优化".
6. Non-self / third-party discussion or unproven target:
   - -> speech_target="未知", NOT automatically guessed as self or Codex.
"""

from __future__ import annotations

import json
from pathlib import Path

from life_logger.activity_timeline_runtime import (
    classify_activity_target_and_category,
    apply_activity_corrections,
    ActivityTimelineRuntime,
)


def test_counterexample_1_media_pitch_talking_about_models_not_codex():
    """Case 1: 媒体口播推销大模型/代码工具，绝不因含'模型'/'写码'被误归为 Codex。"""
    act = {
        "activity_id": "act_purchase_29yuan_toolkit_pitch",
        "title": "复述29元购买Cloud Opus、Gemini Pro及反重力全家桶的推销内容",
        "summary": "属视频/广告口播内容转述，非本人实际购买或使用。",
        "items": [
            {
                "action": "口播包含OpenClaw 4.6 OpenS3、带思考模型、写码改码、Gemini顶级算力、反重力IDE全功能",
                "evidence": [
                    {"quote": "二十九块钱能买到什么？"},
                    {"quote": "Cloud Opus、Gemini Pro，还有反重力全家桶。"},
                    {"quote": "写码改码样样能打"},
                    {"quote": "多智能体编排全解锁"},
                ],
                "uncertainties": [
                    "为广告/推销口播内容转述，非本人实际购买",
                ],
            }
        ],
        "uncertainties": [
            "整体为广告/推销口播内容转述，非本人实际购买或使用",
        ],
    }
    target, cat = classify_activity_target_and_category(act)
    assert target == "媒体内容"
    assert cat == "浏览·视频"


def test_counterexample_2_media_cyber_cricket_flash_contest():
    """Case 2: 媒体节目讨论Flash模型评测，绝不误标为 Codex。"""
    act = {
        "activity_id": "act_cyber_cricket_flash_contest",
        "title": "复述《赛博逗蛐蛐》节目开场及Flash模型大乱斗参赛选手",
        "summary": "均属视频/节目内容转述，非本人实际评测。",
        "items": [
            {
                "action": "开场介绍赛博逗蛐蛐、节目宗旨及本期主题Flash模型大乱斗",
                "evidence": [
                    {"quote": "欢迎来到本期的赛博逗蛐蛐"},
                    {"quote": "本期的主题是Flash模型大乱斗"},
                    {"quote": "有请DIPSICK V4.1 Flash"},
                ],
            }
        ],
        "uncertainties": [
            "整体为视频/节目内容转述，非本人实际评测",
        ],
    }
    target, cat = classify_activity_target_and_category(act)
    assert target == "媒体内容"
    assert cat == "浏览·视频"


def test_counterexample_3_bom_cost_no_hardcoding_and_structured_correction():
    """Case 3: BOM成本讨论禁止硬编码关键字；通用下标未知，通过单条结构化纠正标DeepSeek Harness。"""
    act = {
        "activity_id": "act_recording_card_cost_discussion",
        "title": "讨论录音卡BOM成本：华强北约两块、己方一百二十三、成本需优化",
        "summary": "说话人一转到录音卡成本话题，提到音源成本、BOM成本；说华强北差不多卖两块钱，自己这边有一百二十三这样子。",
        "items": [
            {
                "action": "说华强北差不多卖两块钱、自己这边约一百二十三，具体成本不知道需优化",
                "evidence": [
                    {"quote": "呃，因为确实是华强北是差不多卖两块钱，然后我这边的话就会说是。"},
                    {"quote": "有一百二十三这样子。"},
                    {"quote": "具体的一个成本，我只能说不知道，这这块儿需要优化一下。"},
                ],
            }
        ],
    }
    # 默认情况下：无硬编码映射，不凭BOM推导Harness或Codex，未证明接收对象标未知
    target_default, cat_default = classify_activity_target_and_category(act)
    assert target_default == "未知"
    assert cat_default == "编程·项目优化"

    # 单条明确结构化用户纠正：仅匹配 activity_id
    corrections = {
        "act_recording_card_cost_discussion": {
            "speech_target": "DeepSeek Harness",
            "category": "编程·项目优化",
            "note": "用户逐条纠正确认：该讨论是本人对DeepSeek Harness说的",
        }
    }
    target_corr, cat_corr = classify_activity_target_and_category(act, corrections)
    assert target_corr == "DeepSeek Harness"
    assert cat_corr == "编程·项目优化"

    # 通过 apply_activity_corrections 挂载并标记来源为 user_correction
    day_payload = {"activities": [dict(act)]}
    corrected_payload = apply_activity_corrections(day_payload, corrections)
    c_act = corrected_payload["activities"][0]
    assert c_act["speech_target"] == "DeepSeek Harness"
    assert c_act["speech_target_source"] == "user_correction"
    assert any("用户纠正" in u for u in c_act.get("uncertainties", []))


def test_counterexample_4_help_me_check_multi_target():
    """Case 4: '帮我查一下'句式绝对不能自动推导为Codex：
    - 无结构来源时，标为'未知'；
    - 用户纠正/明确目标为 Harness 时，归为 Harness；
    - 用户纠正/明确目标为 朋友/同事 时，归为 朋友/同事。
    """
    act = {
        "activity_id": "act_query_doubao_asr_price",
        "title": "追问豆包/火山ASR价格并更正为零点八元每小时",
        "summary": "说话人一先问豆包ASR多少钱、多少钱一分钟……属口头询价、回顾与自我更正。",
        "items": [
            {
                "action": "询问之前豆包ASR多少钱、多少钱一分钟",
                "evidence": [
                    {"quote": "帮我查一下之前豆包ASR多少钱？"},
                ],
            }
        ],
    }

    # 4a: 绝不因为含有"帮我查一下"或"ASR"就猜测为 Codex，无结构证据统一标未知
    t_unknown, c_unknown = classify_activity_target_and_category(act)
    assert t_unknown == "未知"
    assert c_unknown == "编程·项目优化"

    # 4b: 用户纠正/明确证据对 Harness
    corr_harness = {
        "act_query_doubao_asr_price": {
            "speech_target": "DeepSeek Harness",
            "category": "编程·项目优化",
            "note": "实际是呼唤本地 Harness 协助查询",
        }
    }
    t_harness, _ = classify_activity_target_and_category(act, corr_harness)
    assert t_harness == "DeepSeek Harness"

    # 4c: 用户纠正/明确证据对 朋友/同事
    corr_friend = {
        "act_query_doubao_asr_price": {
            "speech_target": "朋友/同事",
            "category": "编程·项目优化",
            "note": "实际是向同桌同事询问",
        }
    }
    t_friend, _ = classify_activity_target_and_category(act, corr_friend)
    assert t_friend == "朋友/同事"


def test_counterexample_5_verifiable_structural_target_codex():
    """Case 5: 真实明确的结构化 target Codex（附带可核验来源）予以完整保留。"""
    act = {
        "activity_id": "act_extraction_interval_trial",
        "title": "讨论并安排十分钟与五分钟两种事件提取间隔做对比",
        "summary": "属对定时提取方案的配置请求与对比安排，末尾要求查看提示词。",
        "speech_target": "Codex",
        "speech_target_source": "explicit_intent",
        "items": [
            {
                "action": "要求搞两个任务，分别按十分钟和五分钟处理一次，并搞好提示词做对比",
                "evidence": [
                    {"quote": "呃，搞两个任务，让他按，让他按十分钟处理一次。"},
                    {"quote": "提示词让我看一眼。"},
                ],
            }
        ],
    }
    target, cat = classify_activity_target_and_category(act)
    assert target == "Codex"
    assert cat == "编程·项目优化"


def test_counterexample_6_non_self_unproven_target():
    """Case 6: 无指向智能体明确指令的第三方讨论/复述，对象未证明标为未知，不自动标本人或Codex。"""
    act = {
        "activity_id": "act_third_party_labor_talk",
        "title": "复述关于企业用工与劳动观念的视频片段",
        "summary": "整体为non_self远场复述，无法确认说话人身份及是否为视频内容。",
        "items": [
            {
                "action": "复述关于企业用工的相关说法",
                "evidence": [
                    {"quote": "我们以前的企业用工标准不是这样子规定的。"},
                ],
            }
        ],
    }
    target, cat = classify_activity_target_and_category(act)
    assert target in {"媒体内容", "未知"}
    assert target != "Codex"


def test_runtime_read_day_with_corrections(tmp_path: Path):
    """验证 read_day 读取持久化日文件时自动加载结构化纠正文件。"""
    out_root = tmp_path / "out"
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}", encoding="utf-8")
    runtime = ActivityTimelineRuntime(output_root=out_root, config_path=cfg_file)

    jev_dir = out_root / "activity-timeline" / "jev"
    jev_dir.mkdir(parents=True, exist_ok=True)

    day_payload = {
        "schema": "activity-timeline-v1",
        "date": "2026-10-26",
        "status": "ready",
        "activities": [
            {
                "activity_id": "act_recording_card_cost_discussion",
                "title": "讨论录音卡BOM成本",
                "speech_target": "未知",
                "category": "编程·项目优化",
            }
        ],
        "unassociated_items": [],
        "conflicts": [],
    }
    (jev_dir / "2026-10-26.json").write_text(json.dumps(day_payload, ensure_ascii=False), encoding="utf-8")

    corrections = {
        "act_recording_card_cost_discussion": {
            "speech_target": "DeepSeek Harness",
            "category": "编程·项目优化",
            "note": "用户逐条纠正确认：该讨论是本人对DeepSeek Harness说的",
        }
    }
    (jev_dir / "2026-10-26.corrections.json").write_text(json.dumps(corrections, ensure_ascii=False), encoding="utf-8")

    result = runtime.read_day("2026-10-26", route="jev")
    assert len(result["activities"]) == 1
    act = result["activities"][0]
    assert act["speech_target"] == "DeepSeek Harness"
    assert act["speech_target_source"] == "user_correction"


def test_counterexample_7_source_media_without_keywords():
    """Case 7: source/speaker/source_label 为 media 但无中文'视频/口播'关键词，
    必须识别为 speech_target='媒体内容'，category='浏览·视频'。
    """
    act = {
        "activity_id": "act_pure_media_speaker",
        "title": "介绍大模型前沿进展",
        "summary": "详细讨论了模型的参数与性能表现。",
        "speaker": "media",
        "source": "media",
        "items": [
            {
                "action": "讨论模型参数",
                "evidence": [
                    {"quote": "该模型拥有两万亿参数，在多项基准测试中拔得头筹。", "source_label": "media"}
                ]
            }
        ]
    }
    target, cat = classify_activity_target_and_category(act)
    assert target == "媒体内容"
    assert cat == "浏览·视频"


def test_counterexample_8_media_content_overrides_stale_explicit_intent_codex():
    """Case 8: is_media_content 存在时，旧 speech_target='Codex' + target_source='explicit_intent'
    绝不能优先返回 Codex，媒体内容判定必须压过旧 explicit_intent。
    """
    act = {
        "activity_id": "act_media_with_stale_intent",
        "title": "复述29元购买Cloud Opus、Gemini Pro推销内容",
        "summary": "属视频/广告口播内容转述，非本人实际购买。",
        "speech_target": "Codex",
        "target_source": "explicit_intent",
        "items": [
            {
                "action": "口播推销",
                "evidence": [{"quote": "二十九块钱能买到什么？"}]
            }
        ],
        "uncertainties": ["整体为视频/广告口播内容转述"]
    }
    target, cat = classify_activity_target_and_category(act)
    assert target == "媒体内容"
    assert cat == "浏览·视频"
