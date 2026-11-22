# -*- coding: utf-8 -*-
"""前台应用**连续采样**归因：front_windows 的生成 → 读取 → 落到 ASR 行。

背景（2026-11-04 实测）
----------------------
`input-context/latest.json` 里只有 **mic 启停**产生的窗口（provider.source=mic_event，
时长 0.004–9.8s，30 分钟保留）。手表录音时 Mac 麦克风并没有被占用，所以：

  * 2163 条 ASR 行里 **0 条** 带 input_context；
  * 180 条活动里 **0 条** 能回答"我在跟哪个应用说话"。

而采集器其实**每 0.5s 就在采样前台应用**（`collector.front_samples`），只是从来没被导出成窗口。
本文件的测试锁定三件事：

  1. 连续采样会被合并成区间（同一 bundle 连续 → 一段），并以 `front_windows` 导出；
  2. `windows` 字段**不被改动**（既有契约/测试不变）；
  3. 读取端会把 front_windows 一起喂给 `resolve_input_context`，于是
     「某个时刻前台是什么应用」真的能解析出来，且**解析不到时绝不编造**。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.input_context_runtime import (  # noqa: E402
    InputContextRuntimeManager,
    SHANGHAI_TZ,
    _iso,
)
from life_logger.watch_formal_asr import (  # noqa: E402
    attach_watch_input_context,
    load_input_context_windows,
)


class _Sample:
    def __init__(self, at, bundle, pid="468"):
        self.at = at
        self.bundle = bundle
        self.pid = pid
        self.source = "lsappinfo"


class _FakeCollector:
    """最小假采集器：只提供 runtime 需要的四个属性。"""

    def __init__(self, samples):
        self._samples = list(samples)
        self.front_samples = []
        self.windows = []
        self.open_window = None
        self._i = 0

    def sample_once(self, *, include_front=True, at=None):
        # 一次采样喂 4 条（模拟 0.5s 轮询下的批量追赶）
        for _ in range(4):
            if self._i >= len(self._samples):
                return
            self.front_samples.append(self._samples[self._i])
            self._i += 1


def _mk_runtime(tmp_path, samples):
    rt = InputContextRuntimeManager(tmp_path, poll_interval_seconds=0.5)
    rt.collector = _FakeCollector(samples)
    rt._external_collector = True
    return rt


def test_front_samples_become_merged_spans(tmp_path: Path):
    """连续相同 bundle → 合并成一段；bundle 变化 → 新段。"""
    t0 = datetime.now(SHANGHAI_TZ) - timedelta(seconds=60)
    samples = []
    for i in range(10):                       # 0–9s  : codex
        samples.append(_Sample(t0 + timedelta(seconds=i), "com.openai.codex"))
    for i in range(10, 20):                   # 10–19s: alma
        samples.append(_Sample(t0 + timedelta(seconds=i), "com.yetone.alma"))

    rt = _mk_runtime(tmp_path, samples)
    for _ in range(6):                        # 每次推进 4 条
        rt.step_once(at=datetime.now(SHANGHAI_TZ))

    payload = json.loads((tmp_path / "input-context/latest.json").read_text(encoding="utf-8"))
    front = payload.get("front_windows") or []
    apps = [w.get("target_app") for w in front]
    assert apps == ["com.openai.codex", "com.yetone.alma"], apps
    assert front[0]["provider"]["source"] == "front_sample"
    assert front[0]["duration_seconds"] >= 8
    assert front[0]["meaning"] == "front_app_at_speech_time_not_delivery"
    # 隐私边界：不出现标题/剪贴板/键盘
    assert all("title" not in json.dumps(w).lower() for w in front)


def test_windows_field_untouched(tmp_path: Path):
    """`windows` 语义不变（只有 mic 窗口），front 数据单独放 front_windows。"""
    t0 = datetime.now(SHANGHAI_TZ) - timedelta(seconds=30)
    rt = _mk_runtime(tmp_path, [_Sample(t0 + timedelta(seconds=i), "com.openai.codex") for i in range(8)])
    rt.step_once(at=datetime.now(SHANGHAI_TZ))
    payload = json.loads((tmp_path / "input-context/latest.json").read_text(encoding="utf-8"))
    assert payload["windows"] == []           # 没有 mic 事件 → 旧的 windows 仍为空
    assert payload["front_window_count"] >= 1


def test_reader_merges_front_windows_and_resolves_row(tmp_path: Path):
    """端到端：front_windows → load → resolve → ASR 行带上 input_context。"""
    t0 = datetime.now(SHANGHAI_TZ) - timedelta(seconds=40)
    samples = [_Sample(t0 + timedelta(seconds=i), "com.yetone.alma") for i in range(8)]
    rt = _mk_runtime(tmp_path, samples)
    for _ in range(3):                    # 多推几步，让 span 覆盖到 7s
        rt.step_once(at=datetime.now(SHANGHAI_TZ))

    windows = load_input_context_windows(tmp_path)
    assert any(w.get("provider", {}).get("source") == "front_sample" for w in windows)

    row = {
        "source_id": "src_1", "segment_id": "seg_1", "recording_id": "rec_1",
        "text": "这个焦点这个东西，在跟哪个应用说话",
        "source_label": "self",
        "absolute_started_at": _iso(t0 + timedelta(seconds=2)),
        "absolute_ended_at": _iso(t0 + timedelta(seconds=3)),
    }
    stats = attach_watch_input_context([row], windows)
    assert stats["resolved"] >= 1
    assert row["input_context"]["target_bundle"] == "com.yetone.alma"
    assert row["input_context"]["meaning"] == "input_target_only_not_delivery"


def test_no_window_means_no_field_never_null(tmp_path: Path):
    """解析不到时**不写字段**（绝不写一个永远为 null 的假归因）。"""
    t0 = datetime.now(SHANGHAI_TZ) - timedelta(seconds=40)
    rt = _mk_runtime(tmp_path, [_Sample(t0, "com.yetone.alma")])
    rt.step_once(at=datetime.now(SHANGHAI_TZ))

    windows = load_input_context_windows(tmp_path)
    far_away = _iso(t0 - timedelta(hours=3))
    row = {
        "source_id": "src_2", "segment_id": "seg_2", "recording_id": "rec_2",
        "text": "三小时前的一句话", "source_label": "self",
        "absolute_started_at": far_away,
        "absolute_ended_at": far_away,
    }
    attach_watch_input_context([row], windows)
    assert "input_context" not in row


def test_single_sample_span_is_not_trusted(tmp_path: Path):
    """只有一个采样点的区间不可信 → complete=False → 解析端必须拒绝（不硬猜）。"""
    t0 = datetime.now(SHANGHAI_TZ) - timedelta(seconds=5)
    rt = _mk_runtime(tmp_path, [_Sample(t0, "com.openai.codex")])
    rt.step_once(at=datetime.now(SHANGHAI_TZ))
    payload = json.loads((tmp_path / "input-context/latest.json").read_text(encoding="utf-8"))
    front = payload.get("front_windows") or []
    assert front and front[0]["complete"] is False
    assert front[0]["reason"] == "single_sample_span"


def test_span_pruning_respects_retention(tmp_path: Path):
    """超过 retention 的 span 不进快照（产物有界）。"""
    old = datetime.now(SHANGHAI_TZ) - timedelta(hours=2)
    rt = _mk_runtime(tmp_path, [_Sample(old, "com.openai.codex")])
    rt.step_once(at=datetime.now(SHANGHAI_TZ))
    payload = json.loads((tmp_path / "input-context/latest.json").read_text(encoding="utf-8"))
    assert payload.get("front_window_count", 0) == 0
