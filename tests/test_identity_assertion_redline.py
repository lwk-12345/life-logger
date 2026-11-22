"""红线测试：身份未知时，不得把「用户本人做了 X」写成确定断言。

背景（architecture audit P0 / safe_identity 红线）：
`source_label`（声纹标签）只算候选，不足以认定「本人」。模型会在
title / summary / uncertainties / items[].action 里写成「用户本人询问…」这种
确定的第一人称行为断言。展示层必须把它降级为 hedge 表述，或至少加上否定/不确定语境。

本测试**只用 fixture**，不读 output/、不联网、不调模型：
  · unknown 活动：items[].identity_safe.identity 全为 "unknown"，
    title / summary / uncertainties / items[].action 各放一条确定断言；
  · self 活动（对照组）：identity_safe 全为 "self"，同样文案 → **必须逐字不变**；
  · 另有一条已经 hedge 的文案，用于证明判据不会「见本人就报」（防止测试过宽）。

判据（与审计一致）：
  ASSERT = (用户本人|本人)(?=在|说|问|买|吃|喝|去|看|听|做|用|叫|让|要|想|觉得|记得|提|询|称|讲|道|刷)
  NEG    = 疑似|据称|转述|来自媒体|录音中|可能|不确定|无法确认|无法可靠|无证据|
           不代表|不能认定|待确认|并非本人|未能确认|无法判定
  一个 ASSERT 命中，如果它前面 12 个字符内没有任何 NEG 语境 → 判为「裸断言」。
  （fixture 里不写「不确定项：」这类前缀 —— 它本身含 NEG 词，会让判据假通过。）
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.activity_timeline_runtime import ActivityTimelineRuntime  # noqa: E402

DATE = "2026-10-26"
ASSERT = re.compile(
    r"(用户本人|本人)(?=在|说|问|买|吃|喝|去|看|听|做|用|叫|让|要|想|觉得|记得|提|询|称|讲|道|刷)"
)
NEG_MARKERS = (
    "疑似", "据称", "转述", "来自媒体", "录音中", "可能", "不确定", "无法确认",
    "无法可靠", "无证据", "不代表", "不能认定", "待确认", "并非本人", "未能确认", "无法判定",
)
NEG_WINDOW = 12

BARE = "用户本人询问明天的安排"          # 确定的第一人称行为断言（不得原样出现）
HEDGED = "录音中疑似本人询问明天的安排"   # 已降级表述（合法）


def bare_assertions(text: str) -> list[str]:
    """返回文本里「没有否定/不确定语境」的确定断言片段。"""
    if not isinstance(text, str):
        return []
    out: list[str] = []
    for m in ASSERT.finditer(text):
        prefix = text[max(0, m.start() - NEG_WINDOW):m.start()]
        if not any(marker in prefix for marker in NEG_MARKERS):
            out.append(text[m.start():m.end() + 12])
    return out


def activity(aid: str, identity: str, *, title: str, summary: str, note: str, action: str) -> dict:
    return {
        "activity_id": aid,
        "title": title,
        "summary": summary,
        "uncertainties": [note],
        "speech_target": "未知",
        "category": "生活·日常",
        "status": "ready",
        "occurrence_start": None,
        "occurrence_end": None,
        "items": [{
            "item_id": f"it_{aid}",
            "action": action,
            "evidence": [{
                "source_id": "src_fixture_1", "segment_id": "seg_fixture_1", "row_id": 1,
                "quote": "用户本人询问明天的安排",          # 证据引用必须原文保留
            }],
            "identity_safe": {"identity": identity, "usable": identity == "self"},
        }],
    }


@pytest.fixture()
def read_payload(tmp_path: Path) -> dict:
    root = tmp_path / "output" / "local-web"
    jev = root / "activity-timeline" / "jev"
    jev.mkdir(parents=True)
    (tmp_path / "local-config.json").write_text(json.dumps({
        "activity_timeline": {"enabled": True, "cadence_minutes": 10,
                              "context_minutes": 30, "route": "jev"}}), encoding="utf-8")
    day = {
        "schema": "activity-timeline-v1", "date": DATE, "route": "jev",
        "timezone": "Asia/Shanghai", "cadence_minutes": 10, "status": "ready",
        "activities": [
            activity("act_unknown", "unknown", title=BARE, summary=f"摘要：{BARE}",
                     note=BARE, action=BARE),
            activity("act_self", "self", title=BARE, summary=f"摘要：{BARE}",
                     note=BARE, action=BARE),
            activity("act_hedged", "unknown", title=HEDGED, summary=HEDGED,
                     note=HEDGED, action=HEDGED),
        ],
        "unassociated_items": [], "conflicts": [],
        "coverage": {"through": None, "source_count": 0},
        "errors": [], "metadata": {"processed_source_ids": []},
    }
    (jev / f"{DATE}.json").write_text(json.dumps(day, ensure_ascii=False, indent=2), encoding="utf-8")
    runtime = ActivityTimelineRuntime(root, tmp_path / "local-config.json")
    shown = runtime.read_day(DATE, "jev", for_display=True)
    return {"input": day, "shown": shown, "by_id": {a["activity_id"]: a for a in shown["activities"]}}


def test_unknown_identity_has_no_bare_assertion_in_any_field(read_payload):
    """红线 1：identity=unknown 时，四个用户可见字段都不得留裸断言。"""
    act = read_payload["by_id"]["act_unknown"]
    violations = {
        "title": bare_assertions(act.get("title")),
        "summary": bare_assertions(act.get("summary")),
        "uncertainties": [v for note in act.get("uncertainties") or [] for v in bare_assertions(note)],
        "action": [v for item in act.get("items") or [] for v in bare_assertions(item.get("action"))],
    }
    violations = {k: v for k, v in violations.items() if v}
    assert not violations, f"identity=unknown 仍有确定断言（未降级）: {violations}"


def test_self_identity_activity_is_untouched(read_payload):
    """红线 2（反向）：identity=self 的真本人发言不得被误降级 —— 逐字不变。"""
    before = {a["activity_id"]: a for a in read_payload["input"]["activities"]}["act_self"]
    after = read_payload["by_id"]["act_self"]
    for field in ("title", "summary"):
        assert after[field] == before[field], f"{field} 被改写: {before[field]!r} → {after[field]!r}"
    assert after["uncertainties"] == before["uncertainties"]
    assert [i["action"] for i in after["items"]] == [i["action"] for i in before["items"]]
    assert "title_hedged" not in after


def test_evidence_quotes_are_never_rewritten(read_payload):
    """红线 3：证据原文不可被任何标题降级改写。"""
    before = {a["activity_id"]: a for a in read_payload["input"]["activities"]}
    for aid, act in read_payload["by_id"].items():
        before_items = {i["item_id"]: i for i in before[aid]["items"]}
        for item in act["items"]:
            old = before_items[item["item_id"]]
            assert [e.get("quote") for e in item["evidence"]] == \
                   [e.get("quote") for e in old["evidence"]], f"{aid} 的证据原文被改写"


def test_already_hedged_wording_is_accepted(read_payload):
    """判据自检：已 hedge 的文案不得被判成违规（防止测试过宽）。"""
    act = read_payload["by_id"]["act_hedged"]
    assert not bare_assertions(act["title"])
    assert not bare_assertions(act["summary"])
