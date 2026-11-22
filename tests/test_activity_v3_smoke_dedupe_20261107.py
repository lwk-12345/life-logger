"""v3 活动层两处修复（2026-11-07）：

1. 「买烟」不是吸烟 —— `demote_smoke_purchases()`
2. 跨窗口日级去重 —— `merge_v3_window_reports()`

真实事故：11-07 03:40 / 03:50 两个窗口把「下楼丢个垃圾，然后买包烟，然后回来睡觉。」
拆成 5 条（todo×2 + smoke×2 + other×1），其中 `buying cigarettes` 被标成 `fact_type="smoke"`。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from life_logger.activity_prompt_v3 import (
    SMOKE_DEMOTED_FLAG,
    SMOKE_PURCHASE_REASON,
    apply_app_policy,
    demote_smoke_purchases,
    merge_v3_window_reports,
    project_live,
)

ROOT = Path(__file__).resolve().parents[1]
SHADOW = ROOT / "output" / "local-web" / "activity-shadow-v3" / "2026-11-07"
QUOTE = "下楼丢个垃圾，然后买包烟，然后回来睡觉。"


def item(fact_type="smoke", action="买一包烟", quote=QUOTE, smoke=None, **kw):
    base = {
        "action": action,
        "fact_type": fact_type,
        "smoke": smoke if smoke is not None else {"count": 1, "brand": None, "kind": "bought"},
        "evidence": [{"source_id": "src_1", "quote": quote}],
    }
    base.update(kw)
    return base


def body(*items):
    return {"schema_version": 3, "activities": [
        {"activity_id": "act-1", "category": "生活·日常", "title": "下楼丢垃圾", "items": list(items)}]}


# ---------------------------------------------------------------- 1. 买烟拦截

@pytest.mark.parametrize("action", ["买一包烟", "买包烟", "买盒烟", "买条烟", "买香烟", "买了包烟"])
def test_cigarette_purchase_demoted(action):
    payload = demote_smoke_purchases(body(item(action=action)))
    result = payload["activities"][0]["items"][0]
    assert result["fact_type"] == "other"
    assert result["smoke"] is None
    assert result[SMOKE_DEMOTED_FLAG] is True
    assert SMOKE_PURCHASE_REASON in result["reason"]


@pytest.mark.parametrize("quote,action", [
    ("就是刚才那会儿我已经抽了三根烟了。", "已经抽了三根烟"),
    ("刚抽了一根。", "刚抽了一根"),
    ("点了一根烟，缓一缓。", "点了一根烟"),
])
def test_real_smoking_is_kept(quote, action):
    payload = demote_smoke_purchases(body(item(action=action, quote=quote)))
    result = payload["activities"][0]["items"][0]
    assert result["fact_type"] == "smoke", result
    assert result["smoke"] == {"count": 1, "brand": None, "kind": "bought"}
    assert SMOKE_DEMOTED_FLAG not in result


def test_purchase_plus_smoking_stays_smoke():
    """同一句里既有"买"又有"抽" → 吸烟事实成立，不能降级。"""
    payload = demote_smoke_purchases(body(item(action="买了包烟抽了一根", quote="买了包烟，抽了一根。")))
    assert payload["activities"][0]["items"][0]["fact_type"] == "smoke"


def test_demotion_is_idempotent():
    once = demote_smoke_purchases(body(item()))
    twice = demote_smoke_purchases(copy.deepcopy(once))
    reason = twice["activities"][0]["items"][0]["reason"]
    assert reason.count(SMOKE_PURCHASE_REASON) == 1, reason


def test_demotion_runs_inside_apply_app_policy():
    """run_v3 的路径：政策体（apply_app_policy）里就要降级，不能等合并层。"""
    payload = apply_app_policy(body(item()), {})
    result = payload["activities"][0]["items"][0]
    assert result["fact_type"] == "other" and result["smoke"] is None


def test_demotion_runs_inside_project_live():
    """修复前落盘的影子报告：project_live 时也要被纠正。"""
    report = {
        "status": "valid",
        "confirmed_result": body(item(action="买包烟")),
        "input_apps": {},
        "non_confirmed": [],
    }
    records = [{"source_id": "src_1", "segment_id": "seg_1", "row_id": 1,
                "source_label": "self", "timestamp": "2026-11-07T03:40:00+08:00"}]
    result = project_live(report, records)
    projected = result["activities"][0]["items"][0]
    assert projected["fact_type"] == "other" and projected["smoke"] is None


def test_non_confirmed_wrapped_items_are_demoted_too():
    payload = demote_smoke_purchases({
        "schema_version": 3, "activities": [],
        "unassociated_items": [{"item": item(action="买包烟"), "reason": "low_confidence"}],
    })
    pending = payload["unassociated_items"][0]["item"]
    assert pending["fact_type"] == "other" and pending["smoke"] is None


# ---------------------------------------------------------------- 2. 跨窗口合并

def window(cutoff, *items, activity_id="act-1"):
    return {
        "cutoff": cutoff,
        "confirmed_result": {"schema_version": 3, "activities": [
            {"activity_id": activity_id, "category": "生活·日常", "title": "T", "items": list(items)}]},
    }


def test_merge_dedupes_same_quote_with_different_action_wording():
    early = window("2026-11-07T03:40:00+08:00", item(action="买一包烟"))
    late = window("2026-11-07T03:50:00+08:00", item(action="买包烟"))
    merged = merge_v3_window_reports([early, late])
    items = [i for a in merged["activities"] for i in a["items"]]
    assert len(items) == 1, items
    # 后窗覆盖前窗
    assert items[0]["action"] == "买包烟"
    assert merged["windows"] == 2


def test_merge_keeps_different_source_ids_because_they_are_window_scoped():
    """source_id 在影子层是窗口内编号：同一句在两个窗口 id 不同，仍要能去重。"""
    early = window("2026-11-07T03:40:00+08:00", item(action="买一包烟"))
    early["confirmed_result"]["activities"][0]["items"][0]["evidence"][0]["source_id"] = "src_aaa"
    late = window("2026-11-07T03:50:00+08:00", item(action="买包烟"))
    late["confirmed_result"]["activities"][0]["items"][0]["evidence"][0]["source_id"] = "src_bbb"
    merged = merge_v3_window_reports([early, late])
    assert merged["items"] == 1, merged


def test_merge_removes_empty_activities():
    early = window("2026-11-07T03:40:00+08:00", item(action="买一包烟"))
    late = window("2026-11-07T03:50:00+08:00", item(action="买包烟"))
    late["confirmed_result"]["activities"].append(
        {"activity_id": "act-2", "category": "生活·日常", "title": "只有重复项",
         "items": [item(action="买一包烟")]})
    merged = merge_v3_window_reports([early, late])
    # 后窗（act-2）的同键条目覆盖前窗 → act-1 变空被移除；只剩承载存活条目的那个活动
    assert len(merged["activities"]) == 1, merged["activities"]
    remaining = merged["activities"][0]
    assert len(remaining["items"]) == 1
    assert remaining["activity_id"] == "act-2", remaining["activity_id"]


def test_merge_does_not_mutate_inputs():
    early = window("2026-11-07T03:40:00+08:00", item(action="买一包烟"))
    snapshot = copy.deepcopy(early)
    merge_v3_window_reports([early])
    assert early == snapshot


def test_merge_keeps_distinct_fact_types_for_same_quote():
    """同一句里的 todo 与 other 是两件不同的事，不能互相吞掉。"""
    early = window("2026-11-07T03:40:00+08:00",
                   item(fact_type="todo", action="下楼丢垃圾", smoke=None),
                   item(action="买一包烟"))
    merged = merge_v3_window_reports([early])
    assert sorted(i["fact_type"] for a in merged["activities"] for i in a["items"]) == ["other", "todo"]


# ---------------------------------------------------------------- 3. 真实 11-07 03:40 + 03:50 回归

@pytest.mark.skipif(not SHADOW.is_dir(), reason="本机没有 2026-11-07 的 v3 影子产物")
def test_real_1107_0340_0350_sample():
    wanted = {"2026-11-07T03:40:00+08:00", "2026-11-07T03:50:00+08:00"}
    reports = []
    for path in sorted(SHADOW.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("status") == "valid" and doc.get("cutoff") in wanted:
            reports.append(doc)
    assert len(reports) == 2, [r.get("cutoff") for r in reports]

    def same_sentence(payload):
        found = []
        for activity in (payload.get("activities") or []):
            for entry in activity.get("items") or []:
                quotes = " ".join(e.get("quote") or "" for e in entry.get("evidence") or [])
                if "下楼丢个垃圾" in quotes:
                    found.append(entry)
        return found

    # 合并前：两个窗口各有一轮（03:40 三条、03:50 两条），其中 smoke 各 1 条
    before = [i for r in reports for i in same_sentence(r["confirmed_result"])]
    assert len(before) == 5, len(before)
    assert sum(1 for i in before if i.get("fact_type") == "smoke") == 2

    merged = merge_v3_window_reports(reports)
    after = same_sentence(merged)
    assert len(after) == 2, [(i.get("fact_type"), i.get("action")) for i in after]
    assert sum(1 for i in after if i.get("fact_type") == "smoke") == 0
    assert merged["merge_rule"]["smoke_purchase_demoted"] is True


# ---------------------------------------------------------------- 4. Q1045 活动级合并

def _act(aid, items, *, title="T", category="生活·日常", lifecycle="live", **kw):
    base = {"activity_id": aid, "category": category, "lifecycle": lifecycle,
            "title": title, "items": list(items)}
    base.update(kw)
    return base


def _rep(cutoff, *activities, status="valid"):
    return {"cutoff": cutoff, "status": status,
            "confirmed_result": {"schema_version": 3, "activities": list(activities)}}


def _item(action, quote, fact_type="other", source_id="src_1"):
    return {"action": action, "fact_type": fact_type,
            "evidence": [{"source_id": source_id, "quote": quote}]}


def test_same_cutoff_runs_are_folded_to_the_last_one():
    """同一 cutoff 重跑多次（同输入）→ 只取最后一次，活动数不被灌水。"""
    early = _rep("2026-11-07T03:40:00+08:00",
                 _act("act-a", [_item("旧的一次", "这是一句足够长的旧原话内容", "other", "src_old")],
                      title="旧标题"))
    late = _rep("2026-11-07T03:40:00+08:00",
                _act("act-a", [_item("新的一次", "这是一句足够长的新原话内容", "other", "src_new")],
                     title="新标题"))
    merged = merge_v3_window_reports([early, late])
    assert merged["windows"] == 1
    assert merged["folded_runs"] == 1
    assert len(merged["activities"]) == 1
    assert merged["activities"][0]["title"] == "新标题"
    assert [i["action"] for i in merged["activities"][0]["items"]] == ["新的一次"]


def test_same_activity_id_merges_items_and_takes_later_metadata():
    """规则 A：同 activity_id 跨窗口合并条目，元信息取**后窗**。"""
    first = _rep("2026-11-07T05:00:00+08:00",
                 _act("act-same", [_item("列清单", "把你的文档任务清单都列齐一下吧", "other", "s1")],
                      title="旧标题", status="ongoing"))
    second = _rep("2026-11-07T05:30:00+08:00",
                  _act("act-same", [_item("持续派单", "你的任务派单任务能持续派就行", "idea", "s2")],
                       title="新标题", status="completed"))
    merged = merge_v3_window_reports([first, second])
    assert len(merged["activities"]) == 1
    activity = merged["activities"][0]
    assert activity["title"] == "新标题"          # 后窗元信息
    assert activity["status"] == "completed"
    assert len(activity["items"]) == 2            # 两条都保留（不同原话）
    assert activity["merged_activity_ids"] == ["act-same"]
    assert len(activity["merged_windows"]) == 2
    assert activity["merged_titles"] == ["旧标题", "新标题"]


@pytest.mark.parametrize("category", ["生活·日常", "家务·琐事"])
def test_cross_id_activities_link_on_substantive_quote_and_live_unknown(category):
    """规则 B：跨 id 但同分类、共享 ≥8 字原话、live↔unknown → 合并成一个活动。"""
    early = _rep("2026-11-07T03:40:00+08:00",
                 _act("act-early", [
                     _item("下楼丢垃圾", "下楼丢个垃圾，然后买包烟，然后回来睡觉。", "todo", "s1"),
                     _item("买一包烟", "下楼丢个垃圾，然后买包烟，然后回来睡觉。", "smoke", "s1"),
                 ], title="下楼丢垃圾、买烟再睡觉", category=category, lifecycle="live"))
    late = _rep("2026-11-07T03:50:00+08:00",
                _act("act-late", [
                    _item("下楼丢垃圾后回来睡觉", "下楼丢个垃圾，然后买包烟，然后回来睡觉。", "todo", "s2"),
                    _item("买包烟", "下楼丢个垃圾，然后买包烟，然后回来睡觉。", "smoke", "s2"),
                ], title="计划下楼丢垃圾买烟睡觉", category=category, lifecycle="unknown"))
    merged = merge_v3_window_reports([early, late])
    assert len(merged["activities"]) == 1, merged["activities"]
    activity = merged["activities"][0]
    assert activity["merged_activity_ids"] == ["act-early", "act-late"]
    assert len(activity["items"]) == 2, activity["items"]        # todo + other(买烟降级)
    assert [i["fact_type"] for i in activity["items"]].count("smoke") == 0
    assert activity["title"] == "计划下楼丢垃圾买烟睡觉"          # 后窗元信息


def test_short_quote_never_links_activities():
    """「嗯」这种 1 字原话不许接链（实测会把乒乓球比分和玄幻剧情硬接成一条）。"""
    early = _rep("2026-11-06T13:30:00+08:00",
                 _act("act-bf", [_item("播报乒乓球比分", "嗯", "other", "s1")],
                      title="乒乓球", category="媒体内容", lifecycle="media"))
    late = _rep("2026-11-06T15:00:00+08:00",
                _act("act-filler", [_item("连续嗯填充音", "嗯", "other", "s2")],
                     title="填充音", category="媒体内容", lifecycle="media"))
    merged = merge_v3_window_reports([early, late])
    assert len(merged["activities"]) == 2, merged["activities"]


def test_different_category_never_links_activities():
    quote = "嗯定时任务也在运行吧我现在去忙别的地方去了我要去午睡一下"
    early = _rep("2026-11-07T06:20:00+08:00",
                 _act("act-nap", [_item("计划午睡", quote, "other", "s1")],
                      title="准备午睡", category="健康·作息", lifecycle="live"))
    late = _rep("2026-11-07T07:00:00+08:00",
                _act("act-work", [_item("要求定时任务不停", quote, "other", "s2")],
                     title="交代定时任务", category="工作·项目", lifecycle="live"))
    merged = merge_v3_window_reports([early, late])
    assert len(merged["activities"]) == 2, merged["activities"]


def test_media_category_never_links_across_ids():
    quote = "召集他们那批黑胡子匪徒实在不行立即撤退撤离撤离把车门关上来一个猛虎捉鳖"
    early = _rep("2026-11-07T08:00:00+08:00",
                 _act("act-wuxia", [_item("播放武侠剧情对白", quote, "other", "s1")],
                      title="武侠剧", category="媒体内容", lifecycle="media"))
    late = _rep("2026-11-07T08:40:00+08:00",
                _act("act-game", [_item("播放游戏解说", quote, "other", "s2")],
                     title="游戏解说", category="媒体内容", lifecycle="media"))
    merged = merge_v3_window_reports([early, late])
    assert len(merged["activities"]) == 2, merged["activities"]
    # 两个不同活动各自保留自己的条目：规则 C **不做**跨窗口的全局抢占，
    # 否则前一个活动的唯一 item 会被抢走、整个活动变空消失（实测反例）。
    sources = [i["evidence"][0]["source_id"] for a in merged["activities"] for i in a["items"]]
    assert sources == ["s1", "s2"], sources


def test_real_1107_0340_0350_link_into_one_activity():
    """真实 11-07 03:40 / 03:50 两个不同 activity_id → 合并成 1 个活动、2 条 item、smoke=0。"""
    if not SHADOW.is_dir():
        pytest.skip("本机没有 2026-11-07 的 v3 影子产物")
    wanted = {"2026-11-07T03:40:00+08:00", "2026-11-07T03:50:00+08:00"}
    reports = [doc for doc in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(SHADOW.glob("*.json")))
               if isinstance(doc, dict) and doc.get("status") == "valid" and doc.get("cutoff") in wanted]
    if len(reports) < 2:
        pytest.skip("本机缺 03:40 / 03:50 任一窗口")

    merged = merge_v3_window_reports(reports)
    home = [a for a in merged["activities"]
            if any("下楼丢个垃圾" in (e.get("quote") or "")
                   for i in a.get("items") or [] for e in i.get("evidence") or [])]
    assert len(home) == 1, [(a.get("activity_id"), len(a.get("items") or [])) for a in home]
    activity = home[0]
    assert len(activity["merged_activity_ids"]) == 2, activity["merged_activity_ids"]
    assert len(activity["items"]) == 2, [(i.get("fact_type"), i.get("action")) for i in activity["items"]]
    assert all(i.get("fact_type") != "smoke" for i in activity["items"])


# ---------------------------------------------------------------- 5. live 增量合并（Q1051）

def _runtime_for_live(tmp_path):
    from life_logger.activity_timeline_runtime import ActivityTimelineRuntime

    config = tmp_path / "local-config.json"
    config.write_text(json.dumps({
        "activity_timeline": {"enabled": True, "cadence_minutes": 10,
                              "context_minutes": 30, "route": "jev"}}), encoding="utf-8")
    return ActivityTimelineRuntime(tmp_path / "output", config)


def _apply(runtime, payload, window, index=1):
    import datetime as _dt

    start = _dt.datetime(2026, 11, 7, 3, 40, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    cutoff = start + _dt.timedelta(minutes=10 * (index - 1))
    runtime._apply_extraction_to_day_payload(
        payload, window, cutoff, cutoff - _dt.timedelta(minutes=30), records=[], window_index=index)


def _empty_payload():
    return {"activities": [], "unassociated_items": [], "conflicts": [],
            "window_history": [], "coverage": {}}


def _v3_activity(aid, items, *, title, category="家务·琐事", lifecycle="live", **kw):
    activity = {"activity_id": aid, "title": title, "category": category, "lifecycle": lifecycle,
                "status": "planned", "fact_schema_version": 3, "items": items}
    activity.update(kw)
    return activity


def _v3_item(action, quote, fact_type, source_id="src_1", spoken_at="2026-11-07T03:35:00+08:00"):
    return {"item_id": f"it-{action[:2]}", "action": action, "fact_type": fact_type,
            "fact_schema_version": 3, "spoken_at": spoken_at, "occurred_at": None,
            "evidence": [{"source_id": source_id, "quote": quote,
                          "source_label": "self", "segment_id": "seg_1"}]}


def test_live_apply_merges_cross_id_v3_activities(tmp_path):
    """03:40 + 03:50 两个**不同 activity_id** 依次落盘 → 只剩 1 个活动、2 条 item、smoke=0。"""
    runtime = _runtime_for_live(tmp_path)
    payload = _empty_payload()
    first = {"activities": [_v3_activity(
        "act_throw_trash_buy_cigs_sleep_01",
        [_v3_item("下楼丢垃圾", QUOTE, "todo", "s1"),
         _v3_item("买一包烟", QUOTE, "smoke", "s1"),
         _v3_item("回来睡觉", QUOTE, "other", "s1")],
        title="下楼丢垃圾、买烟再睡觉")], "unassociated_items": [], "conflicts": []}
    second = {"activities": [_v3_activity(
        "act_plan_trash_cigarette_sleep_01",
        [_v3_item("下楼丢垃圾后回来睡觉", QUOTE, "todo", "s2"),
         _v3_item("买包烟", QUOTE, "smoke", "s2")],
        title="计划下楼丢垃圾买烟睡觉", lifecycle="unknown")], "unassociated_items": [], "conflicts": []}

    _apply(runtime, payload, first, 1)
    _apply(runtime, payload, second, 2)

    home = [a for a in payload["activities"]
            if any("下楼丢个垃圾" in (e.get("quote") or "")
                   for i in a.get("items") or [] for e in i.get("evidence") or [])]
    assert len(home) == 1, [(a.get("activity_id"), len(a.get("items") or [])) for a in home]
    activity = home[0]
    assert len(activity["items"]) == 2, [(i.get("fact_type"), i.get("action")) for i in activity["items"]]
    assert all(i.get("fact_type") != "smoke" for i in activity["items"])
    assert activity["title"] == "计划下楼丢垃圾买烟睡觉"          # 后窗元信息
    assert activity["merged_activity_ids"] == [
        "act_plan_trash_cigarette_sleep_01", "act_throw_trash_buy_cigs_sleep_01"]
    # 字段不许丢
    for field in ("category", "lifecycle", "status", "fact_schema_version", "items"):
        assert field in activity, field
    # 活动上的 v3 标记与 item 上的水合字段都完好
    assert activity.get("fact_schema_version") == 3
    for item in activity["items"]:
        assert item.get("spoken_at") == "2026-11-07T03:35:00+08:00"
        assert item["evidence"][0].get("source_label") == "self"


def test_live_apply_dedupes_reworded_item_for_same_activity_id(tmp_path):
    """同 activity_id 跨窗更新、item 措辞微调（买一包烟 → 买包烟）→ 不攒成两条。"""
    runtime = _runtime_for_live(tmp_path)
    payload = _empty_payload()
    quote = QUOTE
    _apply(runtime, payload, {"activities": [_v3_activity(
        "act_same", [_v3_item("买一包烟", quote, "smoke", "s1")], title="买烟")],
        "unassociated_items": [], "conflicts": []}, 1)
    _apply(runtime, payload, {"activities": [_v3_activity(
        "act_same", [_v3_item("买包烟", quote, "smoke", "s2")], title="买烟（已计划）")],
        "unassociated_items": [], "conflicts": []}, 2)

    assert len(payload["activities"]) == 1
    items = payload["activities"][0]["items"]
    assert len(items) == 1, items                        # 旧措辞被去重
    assert items[0]["action"] == "买包烟"                 # 保留最新一轮
    assert items[0]["fact_type"] == "other"              # 买烟 → 不是吸烟


def test_live_apply_never_merges_media_across_activity_ids(tmp_path):
    """媒体类（媒体内容/浏览·视频/媒体·科技资讯 或 lifecycle=media）不跨 id 合并。"""
    runtime = _runtime_for_live(tmp_path)
    payload = _empty_payload()
    long_quote = "召集他们那批黑胡子匪徒实在不行立即撤退撤离撤离把车门关上来一个猛虎捉鳖"
    _apply(runtime, payload, {"activities": [_v3_activity(
        "act_wuxia", [_v3_item("播放武侠剧情对白", long_quote, "other", "s1")],
        title="武侠剧", category="媒体内容", lifecycle="media")], "unassociated_items": [], "conflicts": []}, 1)
    _apply(runtime, payload, {"activities": [_v3_activity(
        "act_game", [_v3_item("播放游戏解说", long_quote, "other", "s2")],
        title="游戏解说", category="浏览·视频", lifecycle="live")], "unassociated_items": [], "conflicts": []}, 2)

    assert len(payload["activities"]) == 2, [a.get("activity_id") for a in payload["activities"]]


def test_live_apply_leaves_v2_activities_untouched(tmp_path, monkeypatch):
    """v2 窗口（无 fact_schema_version==3）→ **完全不调用**合并函数，结果与 apply_window_update 一致。"""
    runtime = _runtime_for_live(tmp_path)
    v2_window = {"activities": [
        {"activity_id": "act_v2", "title": "买早餐", "category": "生活·日常",
         "items": [{"item_id": "i1", "action": "买包子", "evidence": [{"quote": "买了两个包子"}]}]}],
        "unassociated_items": [], "conflicts": []}

    import life_logger.activity_prompt_v3 as v3

    def _boom(*_a, **_kw):
        raise AssertionError("v2 窗口不允许调用 v3 合并")

    monkeypatch.setattr(v3, "merge_live_v3_activities", _boom)

    payload = _empty_payload()
    _apply(runtime, payload, v2_window, 1)

    # 与直接跑 apply_window_update 的结果逐字段一致
    from life_logger.activity_window_state import apply_window_update

    import datetime as _dt
    cutoff = _dt.datetime(2026, 11, 7, 3, 50, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    expected = apply_window_update(
        {"schema_version": "activity-window-state-v1", "activities": {},
         "unassociated_items": [], "conflicts": [], "window_history": []},
        v2_window,
        window_meta={"index": 1, "cutoff": cutoff.isoformat(),
                     "lookback": (cutoff - _dt.timedelta(minutes=30)).isoformat(),
                     "record_count": 0, "provider": "deepseek"})
    assert len(payload["activities"]) == len(expected["activities"]) == 1
    assert payload["activities"][0]["activity_id"] == "act_v2"
    assert [i["action"] for i in payload["activities"][0]["items"]] == ["买包子"]


@pytest.mark.skipif(not SHADOW.is_dir(), reason="本机没有 2026-11-07 的 v3 影子产物")
def test_live_apply_with_real_0340_0350_windows(tmp_path):
    """真实影子：project_live → _apply_extraction_to_day_payload 逐窗喂入，结果与合并函数一致。"""
    from life_logger.activity_prompt_v3 import project_live

    wanted = {"2026-11-07T03:40:00+08:00", "2026-11-07T03:50:00+08:00"}
    reports = {}
    for path in sorted(SHADOW.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("status") == "valid" and doc.get("cutoff") in wanted:
            reports[doc["cutoff"]] = doc
    if len(reports) < 2:
        pytest.skip("本机缺 03:40 / 03:50 任一窗口")

    def hydrate(report):
        source_ids = []
        for activity in (report.get("confirmed_result") or {}).get("activities") or []:
            for item in activity.get("items") or []:
                for evidence in item.get("evidence") or []:
                    sid = evidence.get("source_id")
                    if sid and sid not in source_ids:
                        source_ids.append(sid)
        records = [{"source_id": sid, "segment_id": "seg", "row_id": n,
                    "source_label": "self", "timestamp": report["cutoff"]}
                   for n, sid in enumerate(source_ids)]
        return project_live(report, records)

    runtime = _runtime_for_live(tmp_path)
    payload = _empty_payload()
    for n, cutoff in enumerate(sorted(reports), start=1):
        window = hydrate(reports[cutoff])
        _apply(runtime, payload, {"activities": window["activities"],
                                  "unassociated_items": window.get("unassociated_items") or [],
                                  "conflicts": []}, n)

    home = [a for a in payload["activities"]
            if any("下楼丢个垃圾" in (e.get("quote") or "")
                   for i in a.get("items") or [] for e in i.get("evidence") or [])]
    assert len(home) == 1, [(a.get("activity_id"), len(a.get("items") or [])) for a in home]
    activity = home[0]
    assert len(activity["items"]) == 2, [(i.get("fact_type"), i.get("action")) for i in activity["items"]]
    assert all(i.get("fact_type") != "smoke" for i in activity["items"])
    assert activity.get("fact_schema_version") == 3
    for item in activity["items"]:
        assert item.get("spoken_at"), item
        assert item["evidence"][0].get("source_label") in {"self", "non_self", "unknown"}, item


# ---------------------------------------------------------------- 6. 现场买烟识别 + 事后补报合并 + AI排查指令去误报（2026-11-12）

def test_completed_smoke_purchase_with_expense_keeps_smoke_bought():
    """真实买烟（status=completed + 同活动含 expense + smoke.kind=bought, count=None）保留 smoke 子项。"""
    payload = {
        "schema_version": 3,
        "activities": [
            {
                "activity_id": "act_ziyun_20261112_080846",
                "category": "消费·购物",
                "lifecycle": "live",
                "status": "completed",
                "title": "购买一包紫云香烟",
                "items": [
                    {
                        "item_id": "it_exp",
                        "action": "现场购买一包紫云香烟（金额待定）",
                        "fact_type": "expense",
                        "status": "completed",
                        "expense": {"item": "一包紫云香烟", "amount": None, "currency": "CNY", "payment_status": "unknown"},
                        "evidence": [{"source_id": "src_1", "quote": "打包那个紫云"}],
                    },
                    {
                        "item_id": "it_smoke",
                        "action": "购买一包紫云香烟",
                        "fact_type": "smoke",
                        "status": "completed",
                        "smoke": {"kind": "bought", "brand": "紫云", "count": None},
                        "evidence": [{"source_id": "src_1", "quote": "打包那个紫云"}],
                    },
                ],
            }
        ],
    }
    res = demote_smoke_purchases(copy.deepcopy(payload))
    items = res["activities"][0]["items"]
    assert [it["fact_type"] for it in items] == ["expense", "smoke"]
    assert items[1]["smoke"] == {"kind": "bought", "brand": "紫云", "count": None}


def test_ai_inspection_smoke_backreport_is_suppressed_in_apply_app_policy():
    """对总控/AI下达排查指令（「然后再检查一下，今天上午是有买一包烟的。」「你看一下能找到蛛丝马迹吗？」）不当成买烟补报。"""
    payload = {
        "schema_version": 3,
        "activities": [
            {
                "activity_id": "act_work_lifelogger",
                "category": "工作·项目",
                "lifecycle": "live",
                "status": "ongoing",
                "title": "检查Life Logger并反馈问题",
                "items": [
                    {
                        "item_id": "it_work_1",
                        "action": "请求查找上午买烟的录音线索",
                        "fact_type": "other",
                        "status": "ongoing",
                        "evidence": [
                            {"source_id": "src_22", "quote": "然后再检查一下，今天上午是有买一包烟的。"},
                            {"source_id": "src_23", "quote": "你看一下能找到蛛丝马迹吗？"},
                        ],
                    }
                ],
            },
            {
                "activity_id": "act_cigarette_purchase_20261112_morning",
                "category": "消费·购物",
                "lifecycle": "live",
                "status": "completed",
                "title": "补报上午购买一包烟",
                "items": [
                    {
                        "item_id": "it_exp",
                        "action": "补报今天上午买了一包烟",
                        "fact_type": "expense",
                        "status": "completed",
                        "expense": {"item": "一包烟", "amount": None, "currency": "CNY", "payment_status": "unknown"},
                        "evidence": [{"source_id": "src_22", "quote": "然后再检查一下，今天上午是有买一包烟的。"}],
                    },
                    {
                        "item_id": "it_smoke",
                        "action": "记录买烟",
                        "fact_type": "smoke",
                        "status": "completed",
                        "smoke": {"kind": "bought", "brand": None, "count": None},
                        "evidence": [{"source_id": "src_22", "quote": "然后再检查一下，今天上午是有买一包烟的。"}],
                    },
                ],
            },
        ],
    }
    res = apply_app_policy(payload, {})
    assert len(res["activities"]) == 1
    assert res["activities"][0]["activity_id"] == "act_work_lifelogger"
    assert res["activities"][0]["category"] == "工作·项目"


def test_same_day_smoke_backreport_merges_into_earlier_onsite_purchase():
    """同日事后口头补报买烟（「今天上午买了一包烟」）与更早的现场买烟（08:08 紫云）合并成一笔。"""
    from life_logger.activity_prompt_v3 import merge_live_v3_activities

    onsite = {
        "activity_id": "act_ziyun_20261112_080846",
        "title": "购买一包紫云香烟",
        "category": "消费·购物",
        "lifecycle": "live",
        "status": "completed",
        "fact_schema_version": 3,
        "items": [
            {
                "item_id": "it_exp_1",
                "action": "现场购买一包紫云香烟",
                "fact_type": "expense",
                "fact_schema_version": 3,
                "status": "completed",
                "amount": None,
                "expense": {"item": "一包紫云香烟", "amount": None, "currency": "CNY", "payment_status": "unknown"},
                "evidence": [{"source_id": "src_832", "source_label": "self", "quote": "打包那个紫云"}],
            },
            {
                "item_id": "it_smoke_1",
                "action": "购买一包紫云香烟",
                "fact_type": "smoke",
                "fact_schema_version": 3,
                "status": "completed",
                "smoke": {"kind": "bought", "brand": "紫云", "count": None},
                "evidence": [{"source_id": "src_832", "source_label": "self", "quote": "打包那个紫云"}],
            },
        ],
    }
    backreport = {
        "activity_id": "act_smoke_backreport_afternoon",
        "title": "补报上午买一包烟",
        "category": "消费·购物",
        "lifecycle": "live",
        "status": "completed",
        "fact_schema_version": 3,
        "items": [
            {
                "item_id": "it_exp_2",
                "action": "补报今天上午买了一包烟",
                "fact_type": "expense",
                "fact_schema_version": 3,
                "status": "completed",
                "amount": 13,
                "expense": {"item": "一包烟", "amount": 13, "currency": "CNY", "payment_status": "paid"},
                "evidence": [{"source_id": "src_br", "source_label": "self", "quote": "今天上午买了一包烟，十三块。"}],
            },
            {
                "item_id": "it_smoke_2",
                "action": "补报买一包烟",
                "fact_type": "smoke",
                "fact_schema_version": 3,
                "status": "completed",
                "smoke": {"kind": "bought", "brand": None, "count": None},
                "evidence": [{"source_id": "src_br", "source_label": "self", "quote": "今天上午买了一包烟，十三块。"}],
            },
        ],
    }
    merged = merge_live_v3_activities([onsite, backreport], incoming_activities=[backreport])
    assert len(merged) == 1
    act = merged[0]
    assert act["activity_id"] == "act_ziyun_20261112_080846"
    assert sorted(act["merged_activity_ids"]) == ["act_smoke_backreport_afternoon", "act_ziyun_20261112_080846"]
    assert [it["fact_type"] for it in act["items"]] == ["expense", "smoke"]
    assert act["items"][0]["expense"]["item"] == "一包紫云香烟"
    assert act["items"][0]["expense"]["amount"] == 13
    assert act["items"][1]["smoke"]["brand"] == "紫云"
    assert len(act["items"][0]["evidence"]) == 2


def test_autofill_cigarette_prices_confirmed_and_unconfirmed():
    """烟价自动填：紫云（用户确认价13元）、红塔山（历史报过10元）直接填；未确认品牌（贵烟23元）仅标参考价待确认且不计入已确认金额。"""
    from life_logger.activity_prompt_v3 import autofill_cigarette_prices

    acts = [
        {
            "activity_id": "act_ziyun_0808",
            "title": "购买一包紫云香烟",
            "category": "消费·购物",
            "lifecycle": "live",
            "status": "completed",
            "items": [
                {
                    "item_id": "it_zy_exp",
                    "action": "现场购买一包紫云香烟",
                    "fact_type": "expense",
                    "status": "completed",
                    "amount": None,
                    "expense": {"item": "紫云香烟一包", "amount": None, "currency": "CNY", "payment_status": "unknown"},
                    "evidence": [{"source_id": "s1", "source_label": "self", "quote": "打包那个紫云"}],
                },
                {
                    "item_id": "it_zy_smk",
                    "action": "购买一包紫云",
                    "fact_type": "smoke",
                    "status": "completed",
                    "smoke": {"kind": "bought", "brand": "紫云", "count": None},
                    "evidence": [{"source_id": "s1", "source_label": "self", "quote": "打包那个紫云"}],
                },
            ],
        },
        {
            "activity_id": "act_hts_unpriced",
            "title": "购买一包红塔山",
            "category": "消费·购物",
            "lifecycle": "live",
            "status": "completed",
            "items": [
                {
                    "item_id": "it_hts_exp",
                    "action": "现场拿一包红塔山",
                    "fact_type": "expense",
                    "status": "completed",
                    "amount": None,
                    "expense": {"item": "红塔山一包", "amount": None, "currency": "CNY", "payment_status": "unknown"},
                    "evidence": [{"source_id": "s2", "source_label": "self", "quote": "老板拿包红塔山"}],
                },
            ],
        },
        {
            "activity_id": "act_guiyan_ref",
            "title": "购买一包贵烟",
            "category": "消费·购物",
            "lifecycle": "live",
            "status": "completed",
            "items": [
                {
                    "item_id": "it_gy_exp",
                    "action": "现场拿一包贵烟",
                    "fact_type": "expense",
                    "status": "completed",
                    "amount": None,
                    "expense": {"item": "贵烟一包", "amount": None, "currency": "CNY", "payment_status": "unknown"},
                    "evidence": [{"source_id": "s3", "source_label": "self", "quote": "拿包贵烟"}],
                },
            ],
        },
    ]
    out = autofill_cigarette_prices(copy.deepcopy(acts))
    # 1. 紫云 -> 13（用户确认价）
    zy_exp = out[0]["items"][0]["expense"]
    assert zy_exp["amount"] == 13
    assert zy_exp["amount_source"]["source_type"] == "user_confirmed"
    assert zy_exp["amount_source"]["confirmed"] is True
    assert "你确认过的价" in zy_exp["amount_source"]["label"]

    # 2. 红塔山 -> 10（你上次买同款 10 元）
    hts_exp = out[1]["items"][0]["expense"]
    assert hts_exp["amount"] == 10
    assert hts_exp["amount_source"]["source_type"] == "user_reported"
    assert hts_exp["amount_source"]["confirmed"] is True
    assert hts_exp["amount_source"]["label"] == "你上次买同款 10 元"

    # 3. 贵烟 -> 未确认品牌，amount 保持 None，reference_amount=23，带 confirm_amount / edit_amount
    gy_exp = out[2]["items"][0]["expense"]
    assert gy_exp["amount"] is None
    assert gy_exp["payment_status"] == "unknown"
    assert gy_exp["reference_amount"] == 23
    assert gy_exp["amount_source"]["source_type"] == "reference_unconfirmed"
    assert gy_exp["amount_source"]["confirmed"] is False
    assert gy_exp["amount_source"]["label"] == "参考价，待你确认"
    assert out[2]["review_actions"] == ["confirm_amount", "edit_amount"]


