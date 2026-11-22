"""Dedicated tests for the deterministic user scene-correction hook.

Scope (A0 third-path decision): ONLY explicit user corrections are
applied, by exact interval slicing in DailyDashboardBuilder.
_apply_user_scene_corrections / build_timeline_scenes.

- Revokes only the proven-wrong segment: 2026-10-26 gym 20:51-22:32
  (user explicitly negated it; they had already returned in the
  19:xx hour - approximate wording is never converted to exact times).
- Intersection slicing preserves span parts outside the interval.
- Original input data is never mutated; other scenes/dates untouched.
- Missing correction data => unknown => output unchanged, no guessing.
- No keyword gym matcher exists or is used: _is_gym_presence_event
  must NOT be present on the builder.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from life_logger.daily_dashboard import DailyDashboardBuilder

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "output/local-web"

RULE = {
    "revoke_scene": "gym",
    "start_time": "20:51",
    "end_time": "22:32",
    "reason": "用户明确纠正：19点多已从健身房返回；该时段并非在健身房",
}


def _builder_with_sidecar(tmp_path: Path, payload) -> DailyDashboardBuilder:
    if payload is not None:
        (tmp_path / "scene-corrections.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    return DailyDashboardBuilder(job_root=tmp_path)


def _span(start, end, scene="gym", detail="x"):
    return {
        "start_minute": start,
        "end_minute": end,
        "start_time": f"{start // 60:02d}:{start % 60:02d}",
        "end_time": f"{end // 60:02d}:{end % 60:02d}",
        "duration_minutes": end - start,
        "scene": scene,
        "label": scene,
        "icon": "",
        "color": "",
        "detail": detail,
    }


def test_no_keyword_matcher_on_builder():
    builder = DailyDashboardBuilder(job_root=OUTPUT_ROOT)
    assert not hasattr(builder, "_is_gym_presence_event")


def test_exact_segment_revoked_and_input_not_mutated(tmp_path):
    builder = _builder_with_sidecar(tmp_path, {"2026-10-26": [RULE]})
    scenes = [_span(1251, 1352), _span(1352, 1381, scene="media"), _span(1382, 1440, scene="quiet")]
    snapshot = copy.deepcopy(scenes)
    out = builder._apply_user_scene_corrections(scenes, "2026-10-26")
    assert scenes == snapshot, "原始数据不得被修改"
    assert [s["scene"] for s in out] == ["media", "quiet"]
    assert out == snapshot[1:]


def test_intersection_slicing_preserves_outside_parts(tmp_path):
    builder = _builder_with_sidecar(tmp_path, {"2026-10-26": [RULE]})
    # 19:30-23:30 gym crossing both boundaries of 20:51-22:32
    out = builder._apply_user_scene_corrections([_span(1170, 1410)], "2026-10-26")
    assert len(out) == 2
    assert (out[0]["start_time"], out[0]["end_time"], out[0]["duration_minutes"]) == ("19:30", "20:51", 81)
    assert (out[1]["start_time"], out[1]["end_time"], out[1]["duration_minutes"]) == ("22:32", "23:30", 58)
    assert all(s["scene"] == "gym" for s in out)


def test_missing_or_other_date_data_means_unknown_unchanged(tmp_path):
    scenes = [_span(1251, 1352)]
    no_file = _builder_with_sidecar(tmp_path, None)
    assert no_file._apply_user_scene_corrections(scenes, "2026-10-26") == scenes
    other_date = _builder_with_sidecar(tmp_path, {"2026-10-26": [RULE]})
    assert other_date._apply_user_scene_corrections(scenes, "2026-10-25") == scenes


def test_real_1026_evening_gym_revoked_real_1025_untouched():
    builder = DailyDashboardBuilder(job_root=OUTPUT_ROOT)
    scenes26 = builder.build_timeline_scenes("2026-10-26")
    bad = [
        s for s in scenes26
        if s.get("scene") == "gym"
        and max(int(s["start_minute"]), 1251) < min(int(s["end_minute"]), 1352)
    ]
    assert bad == [], f"20:51-22:32 误标 gym 必须被用户纠正撤销: {bad}"
    # Sidecar is scoped to 2026-10-26 only; 10-25 builds normally.
    payload = json.loads((OUTPUT_ROOT / "scene-corrections.json").read_text(encoding="utf-8"))
    assert list(payload.keys()) == ["2026-10-26"]
    rule = payload["2026-10-26"][0]
    assert (rule["start_time"], rule["end_time"]) == ("20:51", "22:32")
    assert "19点多已从健身房返回" in rule["reason"]
    scenes25 = builder.build_timeline_scenes("2026-10-25")
    assert len(scenes25) > 0
    assert builder._apply_user_scene_corrections(scenes25, "2026-10-25") == scenes25
