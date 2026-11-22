"""Q585 回归：补跑（backfill）成功后 `coverage.through` 必须按证据推进。

背景：`through` 原来只在 live 轮推进（`advance_through=(unit_kind == "live" and ...)`）。
整天只走 backfill 的日子（历史日补跑）因此永远 `through=null`，页面水位显示「—」，
与「已经有 N 个活动」自相矛盾（用户 2026-11-02 实测 2026-10-02）。

红线：through 只能**按证据**推进 —— 该日没有任何未处理行时才推，且单调不回退。
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path


from life_logger.activity_timeline_runtime import ActivityTimelineRuntime

BEIJING = datetime.timezone(datetime.timedelta(hours=8))
BASE = datetime.datetime(2026, 10, 2, 8, 0, tzinfo=BEIJING)
DAY = "2026-10-02"


def _write_fixture(root: Path, starts: list[datetime.datetime]) -> Path:
    seg = (
        root / "watch-sessions" / "watch-t" / "sess-t"
        / "formal-asr" / "segments" / "000001"
    )
    seg.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "segment_id": f"watch_000001_{i:06d}",
            "recording_id": "rec_t",
            "start_offset_seconds": float(i * 5),
            "text": f"测试语句 {i}",
            "absolute_started_at": ts.isoformat(),
            "source": "mock",
            "source_label": "self",
            "speaker": "说话人一",
        }
        for i, ts in enumerate(starts, 1)
    ]
    state = {"status": "completed", "sequence": 1, "attempt": 1, "error": None, "rows": rows}
    path = seg / "state.json"
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return path


def _runtime(tmp_path: Path, config: Path) -> ActivityTimelineRuntime:
    calls = {"n": 0}

    def extractor(prompt: str) -> dict:
        calls["n"] += 1
        return {"activities": [], "unassociated_items": [], "conflicts": [], "updates": []}

    rt = ActivityTimelineRuntime(output_root=tmp_path, config_path=config, extractor=extractor)
    rt._test_calls = calls  # type: ignore[attr-defined]
    return rt


def _config(tmp_path: Path, *, enabled_at: datetime.datetime) -> Path:
    cfg = {
        "activity_timeline": {
            "enabled": True,
            "cadence_minutes": 10,
            "context_minutes": 30,
            "route": "jev",
            "enabled_at": enabled_at.isoformat(),
        }
    }
    path = tmp_path / "local-config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def _payload(tmp_path: Path) -> dict:
    path = tmp_path / "activity-timeline" / "jev" / f"{DAY}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_backfill_success_advances_through(tmp_path: Path):
    """全部行在 backfill 桶里一次处理完 → through 应推到本批最后一行。"""
    starts = [BASE + datetime.timedelta(minutes=m) for m in (35, 36, 40)]
    _write_fixture(tmp_path, starts)

    # 让所有行都早于 lookback（cutoff=09:00, lookback=08:30），从而必然走 backfill
    config = _config(tmp_path, enabled_at=BASE - datetime.timedelta(hours=3))
    rt = _runtime(tmp_path, config)

    result = rt.tick()
    assert result["status"] == "success"
    assert result["kind"] == "backfill", result

    payload = _payload(tmp_path)
    through = payload["coverage"]["through"]
    assert through is not None, "补跑成功后 through 不应再是 null"
    assert datetime.datetime.fromisoformat(through) == starts[-1]
    # 事实未丢
    assert len(payload["metadata"]["processed_source_ids"]) == len(starts)


def test_backfill_does_not_advance_through_when_rows_remain(tmp_path: Path):
    """同一天还有未处理行时，through 不得推进（防止 Q566 的「through 说谎」）。"""
    starts = [BASE + datetime.timedelta(minutes=m) for m in range(0, 90, 5)]
    _write_fixture(tmp_path, starts)

    config = _config(tmp_path, enabled_at=BASE - datetime.timedelta(hours=6))
    rt = _runtime(tmp_path, config)

    result = rt.tick()
    assert result["status"] == "success", result
    assert result["kind"] == "backfill", result

    payload = _payload(tmp_path)
    processed = len(payload["metadata"]["processed_source_ids"])
    # 一轮只处理一个 30 分钟桶，必然仍有剩余
    assert processed < len(starts)
    assert payload["coverage"]["through"] is None, (
        "还有未处理行时 through 必须保持 null（不能被补跑轮推进）"
    )
