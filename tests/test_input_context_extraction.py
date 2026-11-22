"""输入应用上下文：解析条件、提示词透传、事件证据保留、旧路等价。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.input_context import resolve_input_context  # noqa: E402

REC_START = "2026-10-25T18:06:09+08:00"
REC_END = "2026-10-25T18:06:11+08:00"


def _window(**over):
    base = {"start": "2026-10-25T18:00:00+08:00", "end": "2026-10-25T18:10:00+08:00",
            "target_bundle": "com.example.editor", "provider": "mock", "source": "window-scan",
            "confidence": 0.9}
    base.update(over)
    return base


def test_single_overlapping_window_resolves():
    out = resolve_input_context(record_start=REC_START, record_end=REC_END, windows=[_window()])
    assert out["status"] == "resolved"
    assert out["target_bundle"] == "com.example.editor"
    assert out["meaning"] == "input_target_only_not_delivery", "不等于已发送"


def test_multiple_apps_is_unknown():
    out = resolve_input_context(record_start=REC_START, record_end=REC_END,
                                windows=[_window(), _window(target_bundle="com.other.app")])
    assert out["status"] == "unknown"
    assert out["reason"] == "multiple_targets"


def test_missing_end_is_unknown():
    out = resolve_input_context(record_start=REC_START, record_end=REC_END,
                                windows=[_window(end=None)])
    assert out["status"] == "unknown" and out["reason"] == "missing_end"


def test_no_overlap_is_unknown():
    out = resolve_input_context(record_start=REC_START, record_end=REC_END,
                                windows=[_window(start="2026-10-25T19:00:00+08:00",
                                                 end="2026-10-25T19:10:00+08:00")])
    assert out["status"] == "unknown" and out["reason"] == "no_overlap"


def test_time_conflict_is_unknown():
    out = resolve_input_context(record_start=REC_START, record_end=REC_END,
                                windows=[_window(start="2026-10-25T18:10:00+08:00",
                                                 end="2026-10-25T18:00:00+08:00")])
    assert out["status"] == "unknown" and out["reason"] == "time_conflict"


def test_no_windows_is_old_path_equivalent():
    assert resolve_input_context(record_start=REC_START, record_end=REC_END, windows=None)["reason"] == "no_signal"
    assert resolve_input_context(record_start=REC_START, record_end=REC_END, windows=[])["reason"] == "no_signal"


def test_utc_and_local_timestamps_interoperate():
    out = resolve_input_context(
        record_start="2026-10-25T10:06:09+00:00", record_end="2026-10-25T10:06:11+00:00",
        windows=[_window(start="2026-10-25T18:00:00+08:00", end="2026-10-25T18:10:00+08:00")],
    )
    assert out["status"] == "resolved"


# ---------------------------------------------------------------- 端到端（mock opener，不调真实模型）

def _run_with_mock(records, view=None):
    import json as _json
    from life_logger.contextual_extraction import run_contextual_extraction

    seen = []

    class Response:
        def __init__(self, payload):
            self._body = _json.dumps(payload).encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        body = _json.loads(request.data)
        prompt = body["messages"][0]["content"]
        seen.append(prompt)
        packet = _json.loads(prompt.split("输入记录：\n", 1)[1])
        return Response({
            "choices": [{"message": {"content": _json.dumps(
                {"events": [{"source_id": row["source_id"], "is_event": False, "events": []}
                            for row in packet]}, ensure_ascii=False)}}]
        })

    options = dict(deepseek_api_key="offline", deepseek_opener=opener)
    if view:
        options.update(view=view)
    run_contextual_extraction(records, **options)
    return seen


def _records():
    return [{
        "source_id": "src_ic_1", "segment_id": "000036", "recording_id": "watch_X",
        "source_label": "self", "text": "第一组四十公斤",
        "absolute_started_at": REC_START, "start_offset_seconds": 1.0, "end_offset_seconds": 3.0,
        "input_context": {"schema": "input-context-v1", "status": "resolved",
                          "start": "2026-10-25T18:00:00+08:00", "end": "2026-10-25T18:10:00+08:00",
                          "target_bundle": "com.example.editor", "provider": "mock",
                          "source": "window-scan", "confidence": 0.9},
    }]


def test_prompt_carries_input_context_on_single_extraction_path():
    prompts = _run_with_mock(_records())
    assert prompts, "必须有模型调用"
    for prompt in prompts:
        if "输入记录：" not in prompt:
            continue
        packet = prompt.split("输入记录：\n", 1)[1]
        assert "com.example.editor" in packet, "提取提示词必须带上下文证据"
        assert "input_context" in packet
    assert len(prompts) == 1, "单路上下文只用一次提取调用"


def test_no_input_context_key_when_absent_old_path_equivalent():
    records = _records()
    records[0].pop("input_context")
    prompts = _run_with_mock(records)
    for prompt in prompts:
        if "输入记录：" in prompt:
            assert "input_context" not in prompt.split("输入记录：\n", 1)[1], "无信号时提示词不带该字段"


# ---------------------------------------------------------------- Q425：真实采集器 schema（started_at/ended_at/target_app）

COLLECTOR_FIXTURE = ROOT / "work" / "verify-input-context-collector-20261026" / "test_output_a0.json"


def _collector_windows():
    import json

    if not COLLECTOR_FIXTURE.is_file():
        return None
    return json.loads(COLLECTOR_FIXTURE.read_text())


def test_real_collector_output_does_not_resolve_target_unknown():
    """真实采集窗（target_unknown=true）→ 必须 unknown，不能因别名缺省变 resolved。"""
    data = _collector_windows()
    if data is None:
        import pytest
        pytest.skip("采集器 fixture 不在本机")
    for window in data.get("windows") or []:
        out = resolve_input_context(
            record_start=window.get("started_at"),
            record_end=window.get("ended_at"),
            windows={"windows": [window]},
        )
        assert out["status"] == "unknown", (window, out)
        assert out["reason"] in {"target_unknown", "incomplete_window", "missing_target"}, out


def test_collector_schema_with_real_target_resolves_and_keeps_timezone():
    windows = {"schema_version": "input-context-v1", "windows": [{
        "started_at": "2026-10-25T18:00:00+08:00", "ended_at": "2026-10-25T18:10:00+08:00",
        "target_app": "com.example.editor", "target_unknown": False, "complete": True,
        "front_bundles": ["com.example.editor"], "provider": "mock", "duration_seconds": 600,
    }]}
    out = resolve_input_context(record_start=REC_START, record_end=REC_END, windows=windows)
    assert out["status"] == "resolved"
    assert out["target_bundle"] == "com.example.editor"
    assert out["start"] == "2026-10-25T18:00:00+08:00", "保留原始时区字符串"
    assert out["end"] == "2026-10-25T18:10:00+08:00"
    assert out["schema_source"] == "collector"


def test_collector_complete_false_or_multi_front_is_unknown():
    base = {"started_at": "2026-10-25T18:00:00+08:00", "ended_at": "2026-10-25T18:10:00+08:00",
            "target_app": "com.example.editor", "target_unknown": False, "complete": True,
            "front_bundles": ["com.example.editor"]}
    incomplete = resolve_input_context(record_start=REC_START, record_end=REC_END,
                                       windows=[dict(base, complete=False)])
    assert incomplete["reason"] == "incomplete_window"
    multi = resolve_input_context(record_start=REC_START, record_end=REC_END,
                                  windows=[dict(base, front_bundles=["a", "b"])])
    assert multi["reason"] == "multiple_front_apps"


def test_collector_shape_flows_to_prompt_and_event_evidence():
    """采集器 shape → resolver → packet → event evidence 全链（mock opener）。"""
    windows = [{"started_at": "2026-10-25T18:00:00+08:00", "ended_at": "2026-10-25T18:10:00+08:00",
                "target_app": "com.example.editor", "target_unknown": False, "complete": True,
                "front_bundles": ["com.example.editor"], "provider": "mock"}]
    context = resolve_input_context(record_start=REC_START, record_end=REC_END, windows=windows)
    assert context["status"] == "resolved"
    record = _records()[0]
    record["input_context"] = context
    prompts = _run_with_mock([record])
    joined = "\n".join(prompts)
    assert "com.example.editor" in joined, "采集器 shape 必须能进提示词"
    assert "input_context" in joined


# ---------------------------------------------------------------- Q426：13 窗一次性输入（两阶段）

def test_all_13_real_windows_with_one_good_overlap_resolve():
    """真实 13 窗（含早先不重叠的 target_unknown 窗）+ 1 个正确重叠窗 → 必须 resolved。

    旧实现会在循环头遇 target_unknown 提前 return，被不相关异常窗遮挡。
    """
    data = _collector_windows()
    if data is None:
        import pytest
        pytest.skip("采集器 fixture 不在本机")
    windows = list(data.get("windows") or [])
    good = {
        "started_at": "2026-10-25T18:00:00+08:00", "ended_at": "2026-10-25T18:10:00+08:00",
        "target_app": "com.openai.codex", "target_unknown": False, "complete": True,
        "front_bundles": ["com.openai.codex"], "provider": "mock",
    }
    # 记录时间落在 good 窗内
    out = resolve_input_context(record_start="2026-10-25T18:05:00+08:00",
                                record_end="2026-10-25T18:05:30+08:00",
                                windows={"windows": windows + [good]})
    assert out["status"] == "resolved", out
    assert out["target_bundle"] == "com.openai.codex"


def test_13_real_windows_alone_stay_unknown_without_pollution():
    data = _collector_windows()
    if data is None:
        import pytest
        pytest.skip("采集器 fixture 不在本机")
    out = resolve_input_context(record_start="2026-10-25T18:05:00+08:00",
                                record_end="2026-10-25T18:05:30+08:00",
                                windows={"windows": list(data.get("windows") or [])})
    assert out["status"] == "unknown"
    assert out["reason"] in {"no_overlap", "missing_end", "target_unknown", "incomplete_window"}


def test_truly_overlapping_anomaly_still_blocks():
    """真重叠的 target_unknown 窗仍必须 unknown（不被正确窗掩盖）。"""
    good = {"started_at": "2026-10-25T18:00:00+08:00", "ended_at": "2026-10-25T18:10:00+08:00",
            "target_app": "com.openai.codex", "target_unknown": False, "complete": True,
            "front_bundles": ["com.openai.codex"]}
    bad = dict(good, target_app=None, target_unknown=True)
    out = resolve_input_context(record_start="2026-10-25T18:05:00+08:00",
                                record_end="2026-10-25T18:05:30+08:00",
                                windows=[good, bad])
    assert out["status"] == "unknown" and out["reason"] == "target_unknown"


def test_missing_end_overlapping_window_is_conservative_unknown():
    missing_end = {"started_at": "2026-10-25T18:04:00+08:00", "target_app": "com.openai.codex",
                   "complete": True, "target_unknown": False}
    out = resolve_input_context(record_start="2026-10-25T18:05:00+08:00",
                                record_end="2026-10-25T18:05:30+08:00",
                                windows=[missing_end])
    assert out["status"] == "unknown" and out["reason"] == "missing_end"


def test_missing_end_window_far_before_record_does_not_pollute():
    early = {"started_at": "2026-10-25T09:00:00+08:00", "target_app": "com.openai.codex"}
    good = {"started_at": "2026-10-25T18:00:00+08:00", "ended_at": "2026-10-25T18:10:00+08:00",
            "target_app": "com.openai.codex", "target_unknown": False, "complete": True,
            "front_bundles": ["com.openai.codex"]}
    out = resolve_input_context(record_start="2026-10-25T18:05:00+08:00",
                                record_end="2026-10-25T18:05:30+08:00",
                                windows=[early, good])
    assert out["status"] == "resolved" and out["target_bundle"] == "com.openai.codex"
