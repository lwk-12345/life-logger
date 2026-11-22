"""v3 影子去重 + 重试路径 400 行上限（走真实方法链，不 mock 被测函数）。"""
from __future__ import annotations

import datetime
import json
from pathlib import Path


from life_logger import activity_prompt_v3 as pv3
from life_logger.activity_timeline_runtime import ActivityTimelineRuntime, BEIJING_TZ

CUTOFF = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=BEIJING_TZ)
LOOKBACK = CUTOFF - datetime.timedelta(minutes=10)


def _records(n=30):
    return [{"source_id": f"s{i}", "segment_id": f"seg{i}", "row_id": i,
             "timestamp": (LOOKBACK + datetime.timedelta(seconds=10 * i)).isoformat(),
             "text": f"第 {i} 句原话，今天在健身房练胸。", "speaker": "本人",
             "source_label": "self"} for i in range(n)]


def _runtime(tmp_path: Path, calls: list):
    rt = object.__new__(ActivityTimelineRuntime)
    rt.output_root = tmp_path
    rt.input_context_root = tmp_path
    rt.config_path = tmp_path / "local-config.json"
    rt.config_path.write_text(json.dumps({"deepseek": {"api_key": "k", "base_url": "http://127.0.0.1:1/v1",
                                                       "model": "m"}}), encoding="utf-8")

    def sender(body):
        calls.append(body)
        return {"activities": [], "items": []}

    rt.v3_extractor = sender
    rt.get_deepseek_max_concurrency = lambda: 1
    return rt


def _fake_run_window(status="valid"):
    """替换掉真正发请求的那一层，只验证「调不调用」。"""
    def fake(runtime, cutoff, lookback, records, activities, *, mode, gate, sender=None, trigger="first"):
        sender({"prompt": "x"})
        report = {"schema": "activity-v3-window-v1", "mode": mode, "date": "2026-11-06",
                  "trigger": trigger, "status": status, "cutoff": cutoff.isoformat(),
                  "lookback": lookback.isoformat(), "records": len(records)}
        folder = runtime.output_root / "activity-shadow-v3" / "2026-11-06"
        folder.mkdir(parents=True, exist_ok=True)
        key = pv3.input_key(lookback, cutoff, records)
        (folder / f"120000-{key}-1.json").write_text(json.dumps(report), encoding="utf-8")
        return report
    return fake


def test_same_input_skipped_second_time(tmp_path, monkeypatch):
    calls = []
    rt = _runtime(tmp_path, calls)
    monkeypatch.setattr(pv3, "run_window", _fake_run_window("valid"))

    first = rt._run_activity_v3(CUTOFF, LOOKBACK, _records(), {"activities": []}, "shadow")
    second = rt._run_activity_v3(CUTOFF, LOOKBACK, _records(), {"activities": []}, "shadow")

    assert first["status"] == "valid"
    assert second["status"] == "skipped" and second["reason"] == "same_input"
    assert len(calls) == 1, "同输入第二次不许再调用模型"
    skips = (tmp_path / "activity-shadow-v3/2026-11-06/_skips.jsonl").read_text().splitlines()
    assert json.loads(skips[0])["trigger"] == "same_input_skipped"


def test_input_change_reruns(tmp_path, monkeypatch):
    calls = []
    rt = _runtime(tmp_path, calls)
    monkeypatch.setattr(pv3, "run_window", _fake_run_window("valid"))

    rt._run_activity_v3(CUTOFF, LOOKBACK, _records(30), {"activities": []}, "shadow")
    changed = _records(30)
    changed[0]["text"] = "换了一句话"
    report = rt._run_activity_v3(CUTOFF, LOOKBACK, changed, {"activities": []}, "shadow")

    assert report["status"] == "valid" and report["trigger"] == "input_changed"
    assert len(calls) == 2


def test_invalid_previous_allows_retry(tmp_path, monkeypatch):
    calls = []
    rt = _runtime(tmp_path, calls)
    monkeypatch.setattr(pv3, "run_window", _fake_run_window("invalid"))

    rt._run_activity_v3(CUTOFF, LOOKBACK, _records(), {"activities": []}, "shadow")
    second = rt._run_activity_v3(CUTOFF, LOOKBACK, _records(), {"activities": []}, "shadow")

    assert second["status"] == "invalid" and second["trigger"] == "v2_retry"
    assert len(calls) == 2, "上次 invalid 允许按重试规则再跑一次"


def test_skip_does_not_touch_v2(tmp_path, monkeypatch):
    """跳过只影响 shadow：live/v2 路径不走这段逻辑。"""
    calls = []
    rt = _runtime(tmp_path, calls)
    monkeypatch.setattr(pv3, "run_window", _fake_run_window("valid"))
    rt._run_activity_v3(CUTOFF, LOOKBACK, _records(), {"activities": []}, "shadow")
    live = rt._run_activity_v3(CUTOFF, LOOKBACK, _records(), {"activities": []}, "live")
    assert live["status"] == "valid"
    assert len(calls) == 2, "live 不受 shadow 去重影响"


def test_retry_path_is_capped_at_400_rows(tmp_path, monkeypatch):
    """失败窗口重试：扫描到 483 行也必须收敛到 WINDOW_MAX_RECORDS=400。"""
    rt = object.__new__(ActivityTimelineRuntime)
    rt.output_root = tmp_path
    rt.input_context_root = tmp_path
    route_dir = tmp_path / "activity-timeline" / "jev"
    route_dir.mkdir(parents=True)
    (route_dir / "2026-11-06.json").write_text(json.dumps({
        "date": "2026-11-06",
        "metadata": {"failed_window": {
            "retry_at": "2026-11-06T11:00:00+08:00",
            "cutoff": CUTOFF.isoformat(), "lookback": LOOKBACK.isoformat(), "attempts": 1}},
        "activities": [],
    }), encoding="utf-8")

    def _many(n=483):
        # 10 分钟窗口内 483 行（约 1.2s 一条），保证时间跨度不超过 cutoff
        return [{"source_id": f"s{i}", "segment_id": f"seg{i}", "row_id": i,
                 "timestamp": (LOOKBACK + datetime.timedelta(seconds=i)).isoformat(),
                 "text": f"第 {i} 句", "speaker": "本人", "source_label": "self"}
                for i in range(n)]
    rt._scan_asr_records_in_window = lambda *a, **kw: _many(483)
    rt._window_records_chars = lambda rows: 0

    out = rt._due_failed_window_unit(CUTOFF + datetime.timedelta(minutes=1), None, 10, "jev")
    assert out is not None
    day, lb, cutoff, records = out
    assert len(records) == 400, f"重试路径没有被 400 行上限约束：{len(records)}"
    assert cutoff <= CUTOFF
