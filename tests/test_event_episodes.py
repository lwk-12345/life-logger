"""event_episodes 纯函数回归测试（R1–R5）。

两组：
* 合成夹具：逐条锁住 R1/R2/R3/R4 与 R5 红线，不依赖真实产物；
* 真实样本：Watch 会话 AAC382B0（18 条事件），验证 18 → 约 9 条、指定组合并/不合并。
  样本文件不存在时跳过（该目录不进版本库）。

约束：模块本身不写文件、不改事件；这里额外断言「输入未被修改」与「幂等」。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from life_logger.event_episodes import build_episodes

REPO = Path(__file__).resolve().parents[1]

# 固定夹具（2026-10-24 冻结）。**不再读 output/ 生产目录**：
# 上游会周期性重跑聚合、重写 events.json，id 和条数都会漂移，
# 之前直接读生产文件导致 6 条 sample 测试随上游重生成集体失败。
# 冻结依据与更新流程见 tests/fixtures/event-episodes-aac382b0/README.md。
SAMPLE = REPO / "tests" / "fixtures" / "event-episodes-aac382b0" / "events.json"

# 夹具冻结时的事实；夹具一改，这些数字必须同步改（防静默漂移）
FROZEN_SHA256 = "afd0a957f7758f55d9497ce97817841a44096e61969504f933e53084787c577c"
FROZEN_EVENTS = 19
FROZEN_EPISODES = 12
FROZEN_DROPPED = 1


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _event(
    eid: str,
    *,
    quote: str,
    segment: str | None = None,
    activity: str = "work_discussion",
    categories: list[str] | None = None,
    absolute: str | None = None,
    reported_at: str | None = None,
    title: str = "",
    source_label: str = "self",
    data: dict | None = None,
    recording: str = "watch_TEST",
) -> dict:
    evidence: dict = {
        "quote": quote,
        "recording_id": recording,
        "source_id": f"src_{eid}",
        "source_label": source_label,
        "reported_at": reported_at,
    }
    if segment:
        evidence["segment_id"] = segment
    return {
        "event_id": eid,
        "title": title or quote[:20],
        "activity_type": activity,
        "categories": categories if categories is not None else ["work_communication"],
        "confidence": 0.9,
        "data": data or {},
        "evidence": evidence,
        "time": {
            "absolute": absolute,
            "precision": "exact" if absolute else "unknown",
            "relative_seconds": None,
        },
    }


def _ids(episodes: list[dict]) -> list[set[str]]:
    return [set(episode["event_ids"]) for episode in episodes]


def _episode_of(result: dict, event_id: str) -> set[str]:
    alias = result["alias"][event_id]
    for episode in result["episodes"]:
        if episode["episode_id"] == alias:
            return set(episode["event_ids"])
    return set()


# ---------------------------------------------------------------------------
# R1：同段相邻同族 ≤60s
# ---------------------------------------------------------------------------
def test_r1_same_segment_merges():
    events = [
        _event("e1", quote="其实很多计算工作都给了前端代码", segment="watch_000220_000001",
               absolute="2026-10-22T22:04:00+08:00"),
        _event("e2", quote="而本来应该给后端的写到前端上面", segment="watch_000220_000002",
               absolute="2026-10-22T22:04:10+08:00"),
    ]
    result = build_episodes(events)
    assert _ids(result["episodes"]) == [{"e1", "e2"}]
    assert "R1:same_segment" in result["episodes"][0]["reasons"]


def test_r1_gap_over_window_does_not_merge():
    events = [
        _event("e1", quote="前半句内容在这里", segment="watch_000300_000001",
               absolute="2026-10-22T23:00:00+08:00"),
        _event("e2", quote="后半句内容在这里", segment="watch_000300_000001",
               absolute="2026-10-22T23:05:00+08:00"),
    ]
    result = build_episodes(events)
    assert len(result["episodes"]) == 2


# ---------------------------------------------------------------------------
# R2：跨段续句 ≤120s 且 Jaccard ≥0.5
# ---------------------------------------------------------------------------
def test_r2_adjacent_continuation_merges():
    events = [
        _event("e1", quote="我在一个窗口里面命令他去监控我终端里的六个agent",
               segment="watch_000308_000001", absolute="2026-10-22T23:32:00+08:00"),
        _event("e2", quote="我在一个窗口里面命令他去监控我终端里的六个agent的",
               segment="watch_000309_000001", absolute="2026-10-22T23:33:00+08:00"),
    ]
    result = build_episodes(events)
    assert _ids(result["episodes"]) == [{"e1", "e2"}]
    assert "R2:adjacent_continuation" in result["episodes"][0]["reasons"]


def test_r2_low_similarity_does_not_merge():
    events = [
        _event("e1", quote="我在一个窗口里面命令他去监控终端", segment="watch_000308_000001",
               absolute="2026-10-22T23:32:00+08:00"),
        _event("e2", quote="今天午饭吃的是汉堡和薯条", segment="watch_000309_000001",
               absolute="2026-10-22T23:33:00+08:00", activity="meal", categories=["food"]),
    ]
    result = build_episodes(events)
    assert len(result["episodes"]) == 2


# ---------------------------------------------------------------------------
# R3：session 级 vs ctx 级去重
# ---------------------------------------------------------------------------
def test_r3_session_duplicate_dropped_but_kept_as_alias():
    events = [
        _event("evt_session_aaa", quote="六个窗口六个agent谁完成都要做一次汇报检查一下",
               absolute="2025-01-01T10:27:00+08:00", categories=["work_communication", "todo"]),
        _event("evt_ctx_bbb", quote="还有这六个窗口六个agent他谁完成你都要做一次汇报检查一下然后给我汇报",
               segment="watch_000270_000001", absolute="2026-10-22T22:54:00+08:00",
               activity="task", categories=["todo"]),
    ]
    result = build_episodes(events)
    assert [item["event_id"] for item in result["dropped"]] == ["evt_session_aaa"]
    assert result["dropped"][0]["duplicate_of"] == "evt_ctx_bbb"
    # 被丢弃的事件仍有 alias，且 alias 值必须是 episode_id（不是 event_id）
    episode_ids = {episode["episode_id"] for episode in result["episodes"]}
    assert result["alias"]["evt_session_aaa"] in episode_ids
    assert result["alias"]["evt_session_aaa"] == result["dropped"][0]["duplicate_of_episode_id"]


def test_r3_different_object_is_not_treated_as_duplicate():
    events = [
        _event("evt_session_aaa", quote="老板还有肉夹馍吗帮我放辣椒",
               activity="general", categories=["food", "expense"],
               data={"food": {"items": [{"name": "肉夹馍"}]}}),
        _event("evt_ctx_bbb", quote="然后这个汉堡的壳就风味比较足",
               segment="watch_000252_000005", activity="meal", categories=["food"],
               data={"food": {"item": "汉堡"}}),
    ]
    result = build_episodes(events)
    assert result["dropped"] == []
    assert len(result["episodes"]) == 2


# ---------------------------------------------------------------------------
# R4：短碎片吸收
# ---------------------------------------------------------------------------
def test_r4_fragment_absorbed_into_same_segment():
    events = [
        _event("e1", quote="而且之前还出现了某一个终端任务做完了汇报了",
               segment="watch_000309_000001", activity="work_issue"),
        _event("e2", quote="然后他给忘了", segment="watch_000309_000003", activity="work_issue"),
    ]
    result = build_episodes(events)
    assert _ids(result["episodes"]) == [{"e1", "e2"}]
    assert "R4:fragment_same_segment" in result["episodes"][0]["reasons"]


# ---------------------------------------------------------------------------
# R5：红线
# ---------------------------------------------------------------------------
def test_r5_amount_never_merges():
    events = [
        _event("e1", quote="买了肉夹馍十二元", segment="watch_000100_000001",
               categories=["food", "expense"], data={"expense": {"amount": 12.0}}),
        _event("e2", quote="又买了一个肉夹馍", segment="watch_000100_000002",
               categories=["food", "expense"], data={"expense": {"amount": 12.0}}),
    ]
    result = build_episodes(events)
    assert len(result["episodes"]) == 2


def test_r5_plan_never_merges_with_fact():
    """计划 vs 事实：两条同段、同族、时间相邻 —— 结构上允许合并，
    必须靠 R5 红线拦住（否则这个测试只是在验证「没有 segment」）。"""
    from life_logger.event_episodes import explain_merge

    events = [
        _event("e1", quote="计划凌晨去健身房做慢爬坡一小时", segment="watch_000044_000003",
               activity="workout", categories=["exercise", "todo"], absolute="2026-10-22T19:08:20+08:00"),
        _event("e2", quote="左右各十公斤", segment="watch_000044_000004",
               activity="strength_training", categories=["exercise"], absolute="2026-10-22T19:08:31+08:00"),
    ]
    verdict = explain_merge(events[0], events[1])
    assert verdict["can_merge"] is False
    assert verdict["reason"] == "red_line:plan_or_todo", verdict

    result = build_episodes(events)
    assert len(result["episodes"]) == 2
    assert _episode_of(result, "e1") != _episode_of(result, "e2")


def test_r5_cross_family_never_merges():
    events = [
        _event("e1", quote="这段内容讲的是工作架构", segment="watch_000050_000001"),
        _event("e2", quote="这段内容讲的是健身训练", segment="watch_000050_000002",
               activity="workout", categories=["exercise"]),
    ]
    result = build_episodes(events)
    assert len(result["episodes"]) == 2


def test_r5_different_speaker_never_merges():
    events = [
        _event("e1", quote="同一段话的内容", segment="watch_000060_000001", source_label="self"),
        _event("e2", quote="同一段话的内容", segment="watch_000060_000002", source_label="other_person"),
    ]
    result = build_episodes(events)
    assert len(result["episodes"]) == 2


# ---------------------------------------------------------------------------
# 不伪造时间 / 不改入参 / 幂等 / provenance
# ---------------------------------------------------------------------------
def test_never_uses_placeholder_date_for_gap():
    """2025-01-01 占位值不得参与时间差；只能用同段结构关系。"""
    events = [
        _event("e1", quote="第一个片段", segment="watch_000400_000001",
               absolute="2025-01-01T10:00:00+08:00"),
        _event("e2", quote="第二个片段", segment="watch_000400_000002",
               absolute="2025-01-01T10:00:30+08:00"),
    ]
    result = build_episodes(events)
    episode = result["episodes"][0]
    assert episode["time_unknown"] is True
    assert "R1:same_segment" in episode["reasons"]


def test_does_not_mutate_input_events():
    events = [
        _event("e1", quote="同一段内容的前半", segment="watch_000500_000001"),
        _event("e2", quote="同一段内容的后半", segment="watch_000500_000002"),
    ]
    snapshot = copy.deepcopy(events)
    build_episodes(events)
    assert events == snapshot


def test_idempotent_on_same_input():
    events = [
        _event("e1", quote="同一段内容的前半", segment="watch_000500_000001"),
        _event("e2", quote="同一段内容的后半", segment="watch_000500_000002"),
    ]
    first = build_episodes(events)
    second = build_episodes(events)
    assert first == second


def test_provenance_is_preserved_for_every_event():
    events = [
        _event("e1", quote="同一段内容的前半", segment="watch_000500_000001"),
        _event("e2", quote="同一段内容的后半", segment="watch_000500_000002"),
    ]
    result = build_episodes(events)
    provenance_ids = {
        item["event_id"]
        for episode in result["episodes"]
        for item in episode["provenance"]
    }
    provenance_ids |= {item["event_id"] for item in result["dropped"]}
    assert provenance_ids == {"e1", "e2"}
    for episode in result["episodes"]:
        for item in episode["provenance"]:
            assert item["recording_id"] == "watch_TEST"
            assert item["source_id"].startswith("src_")


# ---------------------------------------------------------------------------
# 固定夹具：AAC382B0（冻结 19 条 → 12 episodes）
# ---------------------------------------------------------------------------
pytestmark_sample = pytest.mark.skipif(
    not SAMPLE.is_file(), reason="固定夹具缺失：tests/fixtures/event-episodes-aac382b0/events.json"
)


def _sample_events() -> list[dict]:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def test_sample_fixture_is_frozen_and_self_contained():
    """夹具必须是仓库内文件、内容与冻结哈希一致。

    这条同时守住两件事：
    1. 不再回退成"读 output/ 实时产物"；
    2. 谁改了夹具，必须先改 README 与这里的常量，漂移无法静默通过。
    """
    import hashlib

    assert "output/" not in SAMPLE.as_posix(), "夹具不得指向 output/ 生产目录"
    assert SAMPLE.is_relative_to(REPO), "夹具必须在仓库内"
    digest = hashlib.sha256(SAMPLE.read_bytes()).hexdigest()
    assert digest == FROZEN_SHA256, f"夹具内容已变（{digest}），请按 README 同步冻结常量"
    assert len(_sample_events()) == FROZEN_EVENTS


@pytestmark_sample
def test_sample_events_collapse_to_frozen_episode_count():
    result = build_episodes(_sample_events())
    assert result["stats"]["input_events"] == FROZEN_EVENTS
    assert result["stats"]["episodes"] == FROZEN_EPISODES, result["stats"]
    assert result["stats"]["dropped"] == FROZEN_DROPPED, result["stats"]
    # 19 条里确实发生了合并（不是因为一条都不合才凑出 12）
    assert result["stats"]["kept_events"] < FROZEN_EVENTS


@pytestmark_sample
def test_sample_seven_eight_merged():
    result = build_episodes(_sample_events())
    assert _episode_of(result, "evt_ctx_03a48a06d1c303ad") == {
        "evt_ctx_03a48a06d1c303ad",
        "evt_ctx_f63cb5ea434f262d",
    }


@pytestmark_sample
def test_sample_thirteen_to_sixteen_merged():
    merged = _episode_of(build_episodes(_sample_events()), "evt_ctx_c7dee95a2e17b676")
    assert merged == {
        "evt_ctx_c7dee95a2e17b676",
        "evt_ctx_3235e0080781c1f9",
        "evt_ctx_a84162861548cf3d",
        "evt_ctx_1dca94a611b34ed6",
    }


@pytestmark_sample
def test_sample_session_duplicate_of_ctx_is_dropped():
    """会话级事件被 ctx 级重复内容吸收时，必须进 dropped 并留档，而不是直接消失。"""
    result = build_episodes(_sample_events())
    dropped = {item["event_id"]: item for item in result["dropped"]}
    assert set(dropped) == {"evt_session_03f6e58eb580329c"}, set(dropped)
    item = dropped["evt_session_03f6e58eb580329c"]
    assert item["duplicate_of_episode_id"].startswith("ep_")
    assert item["quote"], "被丢弃的事件必须保留原文，便于追溯"


@pytestmark_sample
@pytest.mark.parametrize(
    "left,right",
    [
        # 跨家族：力量训练事实 vs 前端计算
        ("evt_ctx_43590e81479c6810", "evt_ctx_03a48a06d1c303ad"),
        # 计划 vs 事实：计划慢爬坡 vs 已做力量训练第一组
        ("evt_session_04c804e28588fe83", "evt_ctx_43590e81479c6810"),
        # 金额：肉夹馍（expense） vs 汉堡
        ("evt_session_1e4721339beec056", "evt_ctx_aaa2e7a2cc13cfc8"),
    ],
)
def test_sample_red_line_pairs_stay_separate(left, right):
    result = build_episodes(_sample_events())
    assert _episode_of(result, left) != _episode_of(result, right)


@pytestmark_sample
def test_sample_red_lines_have_expected_reasons():
    """不只断言"没合并"，还锁住**为什么**没合并（否则红线退化也测不出来）。"""
    from life_logger.event_episodes import explain_merge

    events = {event["event_id"]: event for event in _sample_events()}
    cases = {
        ("evt_ctx_43590e81479c6810", "evt_ctx_03a48a06d1c303ad"): "red_line:cross_family",
        ("evt_session_04c804e28588fe83", "evt_ctx_43590e81479c6810"): "red_line:plan_or_todo",
        ("evt_session_1e4721339beec056", "evt_ctx_aaa2e7a2cc13cfc8"): "red_line:amount",
    }
    for (left, right), expected in cases.items():
        verdict = explain_merge(events[left], events[right])
        assert verdict["can_merge"] is False, (left, right, verdict)
        assert verdict["reason"] == expected, (left, right, verdict)


# ---------------------------------------------------------------------------
# API 契约：alias 只允许 event_id -> episode_id；dropped 必须留档
# ---------------------------------------------------------------------------
def test_alias_values_are_always_episode_ids():
    events = [
        _event("e1", quote="同一段内容的前半", segment="watch_000500_000001"),
        _event("e2", quote="同一段内容的后半", segment="watch_000500_000002"),
        _event("evt_session_x", quote="同一段内容的前半", categories=["work_communication"]),
    ]
    result = build_episodes(events)
    episode_ids = {episode["episode_id"] for episode in result["episodes"]}
    assert result["alias_schema"] == "event_id -> episode_id"
    assert set(result["alias"]) == {"e1", "e2", "evt_session_x"}
    for event_id, value in result["alias"].items():
        assert value in episode_ids, f"alias[{event_id}] = {value} 不是 episode_id"


def test_dropped_entry_preserves_detail_and_targets_episode():
    original_quote = "六个窗口，六个agent，他谁完成了你都要做一次汇报，检查一下，然后给我汇报。不要等他六个全部结束工作了你再向我汇报"
    events = [
        _event("evt_session_aaa", quote=original_quote, title="远程监控多个Agent任务",
               categories=["work_communication", "todo"]),
        # ctx 引文必须是 session 引文的子串（真实数据里它带了"还有"前缀，
        # 这里取子串以稳定触发 R3 的包含判定，避免依赖 Jaccard 抖动）
        _event("evt_ctx_bbb", quote="六个窗口，六个agent，他谁完成了你都要做一次汇报",
               segment="watch_000270_000001", activity="task", categories=["todo"]),
    ]
    result = build_episodes(events)

    dropped = result["dropped"][0]
    assert dropped["event_id"] == "evt_session_aaa"
    # 「不要等六个结束」这句只在 session 级出现，去重后必须还在
    assert "不要等他六个全部结束" in dropped["quote"]
    assert dropped["title"]
    assert dropped["activity_type"]
    assert dropped["categories"] == ["work_communication", "todo"]
    assert dropped["provenance"]["event_id"] == "evt_session_aaa"
    assert dropped["provenance"]["source_id"] == "src_evt_session_aaa"
    # 指向的是 episode，不是 event_id
    assert dropped["duplicate_of_episode_id"] in {
        episode["episode_id"] for episode in result["episodes"]
    }
    assert result["alias"]["evt_session_aaa"] == dropped["duplicate_of_episode_id"]


@pytestmark_sample
def test_sample_alias_contract_and_dropped_provenance():
    result = build_episodes(_sample_events())

    episode_ids = {episode["episode_id"] for episode in result["episodes"]}
    assert len(result["alias"]) == FROZEN_EVENTS
    assert all(value in episode_ids for value in result["alias"].values())

    dropped = {item["event_id"]: item for item in result["dropped"]}
    assert set(dropped) == {"evt_session_03f6e58eb580329c"}, set(dropped)
    for item in dropped.values():
        assert item["duplicate_of_episode_id"] in episode_ids
        assert item["provenance"]["event_id"] == item["event_id"]
        assert item["provenance"]["recording_id"]
        assert item["quote"]


@pytestmark_sample
def test_sample_plan_vs_fact_red_line_is_the_blocker():
    from life_logger.event_episodes import explain_merge

    events = {event["event_id"]: event for event in _sample_events()}
    plan = events["evt_session_04c804e28588fe83"]        # 计划：慢爬坡一小时（含 todo）
    fact = events["evt_ctx_43590e81479c6810"]            # 事实：力量训练第一组，已发生

    verdict = explain_merge(plan, fact)
    assert verdict["can_merge"] is False
    assert verdict["reason"] == "red_line:plan_or_todo", verdict

    result = build_episodes(list(events.values()))
    assert _episode_of(result, plan["event_id"]) != _episode_of(result, fact["event_id"])
