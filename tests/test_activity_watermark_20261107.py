"""方案A：活动层水位与"写哪一天"解耦（2026-11-07）。

死锁现场（`work/activity-lag-20261107`）：11-06.json 的 `coverage.through` 停在
`2026-11-07T00:10`，11-07.json 不存在 ⇒ 旧代码只读"今天"的文件 ⇒ through=None ⇒
cutoff 恒为 00:10；同时 `target_day = 最早有积压的那天` 让 live 结果持续写进 11-06.json，
11-07.json 永远建不出来 ⇒ 11-07 凌晨已转写的行被 `ts > cutoff` 永久丢掉。

本文件只测"读水位 / 定目标日"这两处，不调模型（extractor 是返回空结果的替身）。
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from life_logger.activity_timeline_runtime import (
    BEIJING_TZ,
    SCHEMA,
    ActivityTimelineRuntime,
)


# ---------------------------------------------------------------- 夹具


def _day_file(root: Path, day: str, through: str | None,
              processed: list[str] | None = None) -> Path:
    path = root / "activity-timeline" / "jev" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA,
        "date": day,
        "status": "ready",
        "coverage": {"through": through, "source_count": len(processed or [])},
        "activities": [],
        "unassociated_items": [],
        "conflicts": [],
        "window_history": [],
        "metadata": {"processed_source_ids": sorted(processed or [])},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _segments(root: Path, stamps: list[datetime.datetime], *, tag: str = "t") -> Path:
    seg = (root / "watch-sessions" / f"watch-{tag}" / f"sess-{tag}"
           / "formal-asr" / "segments" / "000001")
    seg.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "segment_id": f"watch_{tag}_{i:06d}",
            "recording_id": f"rec_{tag}",
            "start_offset_seconds": float(i * 5),
            "text": f"测试语句 {i}",
            "absolute_started_at": ts.isoformat(),
            "source": "mock",
            "source_label": "self",
            "speaker": "说话人一",
        }
        for i, ts in enumerate(stamps, 1)
    ]
    path = seg / "state.json"
    path.write_text(json.dumps({"status": "completed", "sequence": 1, "attempt": 1,
                                "error": None, "rows": rows}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def _config(root: Path, *, enabled_at: datetime.datetime) -> Path:
    path = root / "local-config.json"
    path.write_text(json.dumps({
        "activity_timeline": {
            "enabled": True,
            "cadence_minutes": 10,
            "context_minutes": 30,
            "route": "jev",
            "enabled_at": enabled_at.isoformat(),
        }
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _runtime(root: Path, config: Path) -> ActivityTimelineRuntime:
    def extractor(prompt: str) -> dict:
        # 替身 provider：不联网、不调模型，返回空抽取结果（本轮只验证调度）。
        return {"activities": [], "unassociated_items": [], "conflicts": [], "updates": []}

    return ActivityTimelineRuntime(output_root=root, config_path=config, extractor=extractor)


def _payload(root: Path, day: str) -> dict:
    return json.loads((root / "activity-timeline" / "jev" / f"{day}.json")
                      .read_text(encoding="utf-8"))


@pytest.fixture
def freeze_now(monkeypatch):
    """把模块里的 `datetime.datetime.now()` 钉死（沿用 test_activity_timeline_runtime 的写法）。"""
    import life_logger.activity_timeline_runtime as runtime_mod

    def _freeze(fixed: datetime.datetime):
        class FixedDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz is not None else fixed

        monkeypatch.setattr(runtime_mod.datetime, "datetime", FixedDatetime)

    return _freeze


# ---------------------------------------------------------------- a) 死锁复现


def test_deadlock_watermark_advances_and_writes_cutoff_day(tmp_path: Path, freeze_now):
    """11-06 还有积压行、11-07.json 不存在时，live 轮必须写进 11-07.json 并推进到 00:20。

    改前行为（红）：through 只读"今天"的 11-07.json（不存在）→ cutoff 恒为 00:10，
    live 结果仍写进 11-06.json，11-07.json 永远建不出来。
    """
    now = datetime.datetime(2026, 11, 7, 6, 40, tzinfo=BEIJING_TZ)
    freeze_now(now)

    backlog_ts = datetime.datetime(2026, 11, 6, 23, 55, tzinfo=BEIJING_TZ)
    late_ts = datetime.datetime(2026, 11, 7, 3, 39, tzinfo=BEIJING_TZ)
    _segments(tmp_path, [backlog_ts, late_ts])

    # 11-06 的水位已经推到 11-07T00:10（就是死锁现场那个值），11-07.json 故意不存在。
    _day_file(tmp_path, "2026-11-06", "2026-11-07T00:10:00+08:00", processed=["old_1"])
    assert not (tmp_path / "activity-timeline" / "jev" / "2026-11-07.json").exists()

    config = _config(tmp_path, enabled_at=datetime.datetime(2026, 11, 6, 10, 0, tzinfo=BEIJING_TZ))
    rt = _runtime(tmp_path, config)

    result = rt.tick()
    assert result["status"] == "success", result
    assert result["kind"] == "live", result
    # 关键：cutoff 推进到 00:20（改前恒为 00:10）
    assert result["cutoff"] == "2026-11-07T00:20:00+08:00", result

    day7 = tmp_path / "activity-timeline" / "jev" / "2026-11-07.json"
    assert day7.exists(), "改后 live 轮必须建出 11-07.json"
    assert _payload(tmp_path, "2026-11-07")["coverage"]["through"] == "2026-11-07T00:20:00+08:00"

    # 11-06 那一行确实进了本轮 prompt → 两边都记"已消费"，下一轮不会当积压重做
    # 11-06 23:55 那一行确实进了本轮 prompt（records 非空 → 证明不是"空转推进"）
    assert result.get("records", 1) != 0
    ids6 = set(_payload(tmp_path, "2026-11-06")["metadata"]["processed_source_ids"])
    assert len(ids6) == 2, (ids6, "行归属日必须记录已消费（否则下一轮会当积压重做）")

    # 03:39 的行还没到 cutoff，本轮不应处理（推进是逐窗口的），但也不能丢
    ids7 = set(_payload(tmp_path, "2026-11-07")["metadata"]["processed_source_ids"])
    assert len(ids7) == 1

    # 下一轮：水位继续推进到 00:30（证明死锁已解，不再钉死在 00:10）
    result2 = rt.tick()
    assert result2["status"] in {"success", "advanced_empty", "empty"}, result2
    assert result2.get("cutoff") == "2026-11-07T00:30:00+08:00", result2


def test_watermark_ignores_corrupt_day_file(tmp_path: Path, freeze_now):
    """某个日文件坏了 → 当 None 跳过，不能因此停止推进。"""
    now = datetime.datetime(2026, 11, 7, 6, 40, tzinfo=BEIJING_TZ)
    freeze_now(now)
    _day_file(tmp_path, "2026-11-06", "2026-11-07T02:00:00+08:00")
    bad = tmp_path / "activity-timeline" / "jev" / "2026-11-07.json"
    bad.write_text("{ not json", encoding="utf-8")
    config = _config(tmp_path, enabled_at=datetime.datetime(2026, 11, 6, 10, 0, tzinfo=BEIJING_TZ))
    rt = _runtime(tmp_path, config)
    # 水位取"最近 3 个文件里最大的 through"：坏掉的今天文件被跳过，昨天那份仍然生效
    assert rt.latest_through("jev", "2026-11-07") == "2026-11-07T02:00:00+08:00"


def test_watermark_returns_none_when_no_day_file(tmp_path: Path, freeze_now):
    freeze_now(datetime.datetime(2026, 11, 7, 6, 40, tzinfo=BEIJING_TZ))
    config = _config(tmp_path, enabled_at=datetime.datetime(2026, 11, 6, 10, 0, tzinfo=BEIJING_TZ))
    rt = _runtime(tmp_path, config)
    assert rt.latest_through("jev", "2026-11-07") is None


# ---------------------------------------------------------------- b) 跨零点不重复


def test_cross_midnight_window_lands_on_cutoff_day_without_duplicates(tmp_path: Path, freeze_now):
    """23:50 → 00:00 的窗口：活动写进 00:00 那天，且同一批行不会被第二轮重做。"""
    now = datetime.datetime(2026, 11, 8, 0, 5, tzinfo=BEIJING_TZ)
    freeze_now(now)
    rows = [
        datetime.datetime(2026, 11, 7, 23, 40, tzinfo=BEIJING_TZ),
        datetime.datetime(2026, 11, 7, 23, 58, tzinfo=BEIJING_TZ),
    ]
    _segments(tmp_path, rows)
    _day_file(tmp_path, "2026-11-07", "2026-11-07T23:50:00+08:00")

    config = _config(tmp_path, enabled_at=datetime.datetime(2026, 11, 5, 0, 0, tzinfo=BEIJING_TZ))
    rt = _runtime(tmp_path, config)

    result = rt.tick()
    assert result["kind"] == "live", result
    assert result["cutoff"] == "2026-11-08T00:00:00+08:00", result

    day8 = tmp_path / "activity-timeline" / "jev" / "2026-11-08.json"
    assert day8.exists(), "跨零点窗口归 cutoff 所在日"
    doc8 = _payload(tmp_path, "2026-11-08")
    assert doc8["coverage"]["through"] == "2026-11-08T00:00:00+08:00"
    assert doc8["window_history"], "本窗口必须有记录"
    win_count = len(doc8["window_history"])

    # 行归属日（11-07）只记消费、不改水位
    doc7 = _payload(tmp_path, "2026-11-07")
    ids7 = set(doc7["metadata"]["processed_source_ids"])
    assert len(ids7) == 2, (ids7, "两行都必须在行归属日记为已消费")
    assert doc7["coverage"]["through"] == "2026-11-07T23:50:00+08:00", "行归属日的水位不能被跨零点轮推进"

    # 第二轮：同一批行已经记过 → 不得再进 prompt（不重复）。
    # now=00:05，水位已推到 00:00 ⇒ 下一轮 cutoff=00:10 还没到点，必然是 idle。
    result2 = rt.tick()
    assert result2["status"] == "idle", result2
    assert result2.get("reason") == "awaiting_next_cutoff", result2
    doc8b = _payload(tmp_path, "2026-11-08")
    assert len(doc8b["window_history"]) == win_count, "不得重复处理同一批行"
    # 更硬：11-07 的行归属日 id 集合没变（没有第二轮再消费一次）
    assert set(_payload(tmp_path, "2026-11-07")["metadata"]["processed_source_ids"]) == ids7


# ---------------------------------------------------------------- c) 只有 backfill 时水位不变


def test_backfill_only_does_not_advance_through(tmp_path: Path, freeze_now):
    now = datetime.datetime(2026, 11, 7, 6, 40, tzinfo=BEIJING_TZ)
    freeze_now(now)
    # 全部行都早于 lookback → 必然是 backfill
    old = [datetime.datetime(2026, 11, 6, 8, 5 * i, tzinfo=BEIJING_TZ) for i in range(1, 8)]
    _segments(tmp_path, old)
    _day_file(tmp_path, "2026-11-06", "2026-11-06T23:00:00+08:00")
    config = _config(tmp_path, enabled_at=datetime.datetime(2026, 11, 6, 6, 0, tzinfo=BEIJING_TZ))
    rt = _runtime(tmp_path, config)

    result = rt.tick()
    assert result["kind"] == "backfill", result
    assert _payload(tmp_path, "2026-11-06")["coverage"]["through"] == "2026-11-06T23:00:00+08:00"


# ---------------------------------------------------------------- d) 正常单日推进不变


def test_normal_single_day_advance_unchanged(tmp_path: Path, freeze_now):
    """没有积压、当天有行：行为与改前一致（写当天、through 推进到 cutoff）。"""
    now = datetime.datetime(2026, 11, 7, 14, 5, tzinfo=BEIJING_TZ)
    freeze_now(now)
    rows = [datetime.datetime(2026, 11, 7, 13, 52, tzinfo=BEIJING_TZ),
            datetime.datetime(2026, 11, 7, 13, 58, tzinfo=BEIJING_TZ)]
    _segments(tmp_path, rows)
    _day_file(tmp_path, "2026-11-07", "2026-11-07T13:50:00+08:00")
    config = _config(tmp_path, enabled_at=datetime.datetime(2026, 11, 5, 0, 0, tzinfo=BEIJING_TZ))
    rt = _runtime(tmp_path, config)

    result = rt.tick()
    assert result["kind"] == "live", result
    assert result["cutoff"] == "2026-11-07T14:00:00+08:00", result
    doc = _payload(tmp_path, "2026-11-07")
    assert doc["coverage"]["through"] == "2026-11-07T14:00:00+08:00"
    assert len(doc["metadata"]["processed_source_ids"]) == 2
    # 只应存在 11-07 一个日文件（不能凭空建出别的日期）
    days = sorted(p.stem for p in (tmp_path / "activity-timeline" / "jev").glob("2026-*.json"))
    assert days == ["2026-11-07"], days
