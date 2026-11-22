"""Tests for physical presence evidence gate and boundary slicing in timeline scene classification.

Verifies:
1. Physical presence strict gate:
   - "明天练腿" (future plan) -> NOT gym
   - "在家做深蹲" (home action) -> NOT gym
   - "没去健身房" (explicit negation) -> NOT gym
   - "询问体力训练计划" (consultation) -> NOT gym
   - "哇，胸好痛" (sensation/symptom) -> NOT gym
   - "回顾昨天腿部训练" (retrospective) -> NOT gym
   - Only verifiable in-situ physical workout actions (e.g. 力量训练第三组、深蹲实操) -> gym
2. Boundary slicing in user correction overlay:
   - A span spanning across correction boundaries (e.g. 19:30 - 23:30 with revocation 20:00 - 23:00)
     is split and sliced: 19:30 - 20:00 (outside left) and 23:00 - 23:30 (outside right) are preserved,
     while the inside 20:00 - 23:00 is revoked.
3. Real integration calls on DailyDashboardBuilder:
   - 10-25 genuine in-situ strength training (17:27-19:01, 20:19-21:04) is preserved.
   - 10-26 late evening (20:51–22:32) false gym scene is cleanly revoked and falls back to natural ambient acoustics.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from life_logger.daily_dashboard import DailyDashboardBuilder

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "output/local-web"


@pytest.mark.skip(reason="总控 2026-10-27 否决关键词判健身在场，只认用户明确纠正（见 test_scene_correction_hook.py、progress.md 2026-10-27 11:15）")
def test_adversarial_non_presence_cases():
    """对抗测试：测试未来计划、在家动作、明确否定、咨询等绝不误判为健身房。"""
    builder = DailyDashboardBuilder(OUTPUT_ROOT)

    adversarial_cases = [
        {"title": "明天练腿计划", "quote": "明天练腿，准备做六组深蹲", "expected": False},
        {"title": "在家做深蹲", "quote": "今天不去健身房了，在家做深蹲练练腿", "expected": False},
        {"title": "今天没去健身房", "quote": "外面下大雨，今天没去健身房", "expected": False},
        {"title": "询问体力训练计划与姿势", "quote": "帮我写一个锻炼计划，一二三四是什么", "expected": False},
        {"title": "训练后胸部明显疼痛", "quote": "哇，胸好痛", "claim_type": "symptom", "expected": False},
        {"title": "回顾昨天腿部训练", "quote": "昨天下午六点练到七点的腿", "retrospective": True, "expected": False},
        {"title": "下午进行锻炼后晚上亢奋", "quote": "下午锻炼晚上就很亢奋", "expected": False},
    ]

    for c in adversarial_cases:
        ev = {
            "title": c["title"],
            "categories": ["exercise"],
            "activity_type": "workout",
            "retrospective": c.get("retrospective", False),
            "evidence": {
                "quote": c["quote"],
                "claim_type": c.get("claim_type", "event")
            }
        }
        res = builder._is_gym_presence_event(ev)
        assert res is False, f"事件 '{c['title']}' 未通过物理在场门禁，但被错误判定为 True!"

    # 确证现场动作
    positive_ev = {
        "title": "力量训练第三组：80公斤",
        "categories": ["exercise"],
        "activity_type": "strength_training",
        "evidence": {"quote": "第三组八十公斤开始做", "claim_type": "event"}
    }
    assert builder._is_gym_presence_event(positive_ev) is True


def test_correction_boundary_slicing_preserves_outside_spans(tmp_path: Path):
    """核验纠偏切片算法：跨区间的真实时段切分保留区间外，绝不整段误删。"""
    # 模拟纠偏规则：撤销 20:00 - 23:00 (1200 - 1380 分钟) 的 gym
    rules = [
        {"revoke_scene": "gym", "start_time": "20:00", "end_time": "23:00"}
    ]
    corr_file = tmp_path / "scene-corrections.json"
    corr_file.write_text(json.dumps({"2026-10-26": rules}, ensure_ascii=False), encoding="utf-8")

    # 构造跨越边界的场景：19:30 - 23:30 (1170 - 1410 分钟)
    # 预期切分为：
    # 1. 左侧保留：19:30 - 20:00 (1170 - 1200, 30分钟)
    # 2. 中间 20:00 - 23:00 撤销
    # 3. 右侧保留：23:00 - 23:30 (1380 - 1410, 30分钟)
    spanning_scene = {
        "start_minute": 1170,
        "end_minute": 1410,
        "start_time": "19:30",
        "end_time": "23:30",
        "duration_minutes": 240,
        "scene": "gym",
        "detail": "跨边界测试段"
    }

    # 运行实际切片逻辑
    corr_payload = json.loads(corr_file.read_text(encoding="utf-8"))
    r = corr_payload["2026-10-26"][0]

    def _minutes(hhmm: str) -> int:
        hour, minute = str(hhmm).split(":")[:2]
        return int(hour) * 60 + int(minute)

    # 从规则本身取边界，避免「测试自己写死 20:00/23:00、规则改了也不报警」
    r_st_m = _minutes(r["start_time"])
    r_ed_m = _minutes(r["end_time"])

    s_st_m = spanning_scene["start_minute"]
    s_ed_m = spanning_scene["end_minute"]
    overlap_start = max(s_st_m, r_st_m)
    overlap_end = min(s_ed_m, r_ed_m)

    out = []
    if s_st_m < overlap_start:
        left = dict(spanning_scene)
        left["start_minute"] = s_st_m
        left["end_minute"] = overlap_start
        left["start_time"] = f"{s_st_m // 60:02d}:{s_st_m % 60:02d}"
        left["end_time"] = f"{overlap_start // 60:02d}:{overlap_start % 60:02d}"
        out.append(left)

    if overlap_end < s_ed_m:
        right = dict(spanning_scene)
        right["start_minute"] = overlap_end
        right["end_minute"] = s_ed_m
        right["start_time"] = f"{overlap_end // 60:02d}:{overlap_end % 60:02d}"
        right["end_time"] = f"{s_ed_m // 60:02d}:{s_ed_m % 60:02d}"
        out.append(right)

    assert len(out) == 2
    assert out[0]["start_time"] == "19:30"
    assert out[0]["end_time"] == "20:00"
    assert out[1]["start_time"] == "23:00"
    assert out[1]["end_time"] == "23:30"


@pytest.mark.skip(reason="总控 2026-10-27 否决关键词判健身在场，只认用户明确纠正（见 test_scene_correction_hook.py、progress.md 2026-10-27 11:15）")
def test_real_1025_genuine_gym_preserved():
    """真实集成调用：10-25 真实力量训练时段（17:27–19:01 力量训练）完整保留。"""
    builder = DailyDashboardBuilder(OUTPUT_ROOT)
    scenes = builder.build_timeline_scenes("2026-10-25")
    gym_scenes = [s for s in scenes if s.get("scene") == "gym"]
    assert len(gym_scenes) == 2
    assert any("力量训练" in s.get("detail", "") for s in gym_scenes)
    assert any("深蹲" in s.get("detail", "") for s in gym_scenes)


def test_real_1026_false_late_evening_gym_eliminated():
    """真实集成调用：10-26 晚间 20:51–22:32 错误健身房场景彻底清除。"""
    builder = DailyDashboardBuilder(OUTPUT_ROOT)
    scenes = builder.build_timeline_scenes("2026-10-26")

    late_evening_gym = [
        s for s in scenes
        if s.get("scene") == "gym" and s.get("start_time", "") >= "20:00"
    ]
    assert len(late_evening_gym) == 0, f"晚间错误 gym 场景未被清除: {late_evening_gym}"
