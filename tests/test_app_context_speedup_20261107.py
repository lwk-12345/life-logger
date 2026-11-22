"""`app_context` 分钟桶索引加速的等价性测试（2026-11-07）。

背景：`read_day("2026-11-06", for_display=True)` 曾耗时 37 秒，其中 99% 在
`attach_to_payload()` —— 每个 item 都把当天 27,275 个窗口线性扫一遍并重新解析 ISO 时间戳
（1,131 × 27,275 ≈ 3,080 万次）。

本文件守两件事：

1. **零语义变化**：同一份 `windows` 走「索引路径」与「线性路径」必须逐字段相等，
   包括 None、异常类型、以及命中窗口的先后顺序（`_nearest_front` 的 `gap < best` 先到先得）。
   构造了覆盖 mic 命中 / nearest_front 回退 / front_sample / 空闲超时 / 乱序 / 非法元素 /
   缺字段 / naive 时间戳 / 自定义阈值 的窗口组合。
2. **真的变快**：真实 `2026-11-06` 上 `attach_to_payload` < 2.5 秒（原 37 秒），
   且 1,131 个 item 的 `input_context` 与活动级汇总与原实现逐项一致。
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import app_context as ac  # noqa: E402

OUTPUT = ROOT / "output" / "local-web"
DAY = "2026-11-06"


# ── 工具：两条路径各跑一次（异常也要比）───────────────────────────────────────
def _run(fn):
    try:
        return ("value", fn())
    except Exception as error:  # noqa: BLE001 - 就是为了比较异常类型
        return ("error", type(error).__name__)


def _both_context_at(moment, windows, **kwargs):
    linear = _run(lambda: ac.context_at(moment, windows, **kwargs))
    indexed = _run(lambda: ac.context_at(
        moment, windows, index=ac.build_window_index(windows), **kwargs))
    return linear, indexed


def _mic(start: str, end: str, target: str | None = "com.app.mic", **extra):
    window = {"started_at": start, "ended_at": end,
              "provider": {"source": "mic_event"}, "complete": True}
    if target:
        window["target_app"] = target
    window.update(extra)
    return window


def _front(start: str, end: str, target: str | None = "com.app.front", **extra):
    window = {"started_at": start, "ended_at": end,
              "provider": {"source": "hid_event"}, "complete": True}
    if target:
        window["target_app"] = target
    window.update(extra)
    return window


# ── 1. 合成窗口：逐场景等价 ─────────────────────────────────────────────────
@pytest.mark.parametrize("name,moment,windows", [
    ("mic 命中（有目标）",
     "2026-11-06T10:00:05+08:00",
     [_mic("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00")]),
    ("front 命中",
     "2026-11-06T10:00:05+08:00",
     [_front("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00")]),
    ("mic 无目标 → nearest_front 回退",
     "2026-11-06T10:00:05+08:00",
     [_mic("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", target=None),
      _front("2026-11-06T09:59:58+08:00", "2026-11-06T10:00:01+08:00"),
      _front("2026-11-06T10:00:12+08:00", "2026-11-06T10:00:20+08:00")]),
    ("mic 无目标且附近没有 front → None",
     "2026-11-06T10:00:05+08:00",
     [_mic("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", target=None)]),
    ("空闲超时 → 跳过",
     "2026-11-06T10:00:05+08:00",
     [_front("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", idle_seconds=600)]),
    ("空闲未知 → confidence low",
     "2026-11-06T10:00:05+08:00",
     [_front("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", idle_seconds=None)]),
    ("窗口不完整 → confidence low",
     "2026-11-06T10:00:05+08:00",
     [_front("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", complete=False)]),
    ("乱序窗口：必须按原下标先到先得",
     "2026-11-06T10:00:05+08:00",
     [_front("2026-11-06T10:00:03+08:00", "2026-11-06T10:00:09+08:00", target="com.b"),
      _front("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", target="com.a")]),
    ("多个 mic 命中：按原顺序取第一个有目标的",
     "2026-11-06T10:00:05+08:00",
     [_mic("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", target="com.first"),
      _mic("2026-11-06T10:00:01+08:00", "2026-11-06T10:00:09+08:00", target="com.second")]),
    ("缺字段 / 非法时间戳 / naive 时间戳都被跳过",
     "2026-11-06T10:00:05+08:00",
     [{}, {"started_at": None}, {"started_at": "not-a-time"},
      {"started_at": "2026-11-06T10:00:00"},  # naive → _parse 返回 None
      _front("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", target="com.ok")]),
    ("跨时区：窗口是 -08:00，时刻是 +08:00",
     "2026-11-06T10:00:05+08:00",
     [_front("2026-11-05T18:00:00-08:00", "2026-11-05T18:00:10-08:00", target="com.utc")]),
    ("零时长窗口（ended_at 缺失 → end=start）",
     "2026-11-06T10:00:05+08:00",
     [{"started_at": "2026-11-06T10:00:05+08:00",
       "provider": {"source": "hid_event"}, "target_app": "com.point"}]),
    ("空窗口列表", "2026-11-06T10:00:05+08:00", []),
    ("时刻本身非法", "not-a-time",
     [_front("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00")]),
])
def test_indexed_matches_linear_on_synthetic_windows(name, moment, windows):
    linear, indexed = _both_context_at(moment, windows)
    assert indexed == linear, name


@pytest.mark.parametrize("kwargs", [
    {},
    {"max_idle_seconds": 10.0},
    {"max_idle_seconds": 1000.0},
    {"nearest_grace_seconds": 0.0},
    {"nearest_grace_seconds": 300.0},
    {"max_idle_seconds": 10.0, "nearest_grace_seconds": 60.0},
])
def test_custom_thresholds_are_equivalent(kwargs):
    windows = [
        _mic("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", target=None,
             idle_seconds=50),
        _front("2026-11-06T10:00:12+08:00", "2026-11-06T10:00:20+08:00", idle_seconds=50),
        _front("2026-11-06T09:58:00+08:00", "2026-11-06T09:58:10+08:00", target="com.far"),
    ]
    linear, indexed = _both_context_at("2026-11-06T10:00:05+08:00", windows, **kwargs)
    assert indexed == linear, kwargs


def test_non_mapping_element_raises_same_error_on_both_paths():
    """列表里混进非 Mapping：nearest_front 回退时旧实现会抛 AttributeError，
    索引路径必须**同样抛**（不能悄悄吞掉变成 None）。"""
    windows = [
        _mic("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", target=None),
        "not-a-window",
    ]
    linear, indexed = _both_context_at("2026-11-06T10:00:05+08:00", windows)
    assert indexed == linear


def test_index_is_bound_to_the_same_list():
    """索引只对同一份 windows 有效：换成另一个列表要退回线性路径，不能串用。"""
    windows = [_front("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", target="com.a")]
    index = ac.build_window_index(windows)
    other = [_front("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", target="com.b")]
    assert ac.context_at("2026-11-06T10:00:05+08:00", other, index=index) == \
           ac.context_at("2026-11-06T10:00:05+08:00", other)


# ── 2. attach_to_payload：合成 payload 也要等价 ─────────────────────────────
def test_attach_to_payload_equivalent_on_synthetic_payload(monkeypatch):
    windows = [
        _mic("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", target="com.mic"),
        _front("2026-11-06T11:00:00+08:00", "2026-11-06T11:00:10+08:00", target="com.front"),
        _front("2026-11-06T11:00:05+08:00", "2026-11-06T11:00:20+08:00", target="com.front2"),
    ]
    monkeypatch.setattr(ac, "load_windows", lambda **_kwargs: windows)
    payload = {"activities": [
        {"activity_id": "a1", "items": [
            {"item_id": "i1", "spoken_at": "2026-11-06T10:00:05+08:00"},
            {"item_id": "i2", "spoken_at": "2026-11-06T11:00:07+08:00"},
            {"item_id": "i3", "spoken_at": "2026-11-06T12:00:00+08:00"},
        ]},
        {"activity_id": "a2", "items": [{"item_id": "i4"}]},
        "not-a-dict",
    ]}
    new = ac.attach_to_payload(copy.deepcopy(payload), date=DAY, root=ROOT / "unused")

    original_build = ac.build_window_index

    class _Unusable:
        complete = False

        def matches(self, _windows):
            return False

        def hits(self, _at):
            raise AssertionError("索引不应被使用")

    ac.build_window_index = lambda _w: _Unusable()
    try:
        old = ac.attach_to_payload(copy.deepcopy(payload), date=DAY, root=ROOT / "unused")
    finally:
        ac.build_window_index = original_build
    assert json.dumps(new, sort_keys=True, ensure_ascii=False) == \
           json.dumps(old, sort_keys=True, ensure_ascii=False)


# ── 3. 真实 2026-11-06：<2.5s 且逐字节一致 ─────────────────────────────────
@pytest.mark.skipif(
    not (OUTPUT / "activity-timeline" / "jev" / f"{DAY}.json").is_file()
    or not (OUTPUT / "watch-sessions" / "input-context" / "days" / f"{DAY}.jsonl").is_file(),
    reason="本机没有 2026-11-06 的产物",
)
def test_real_day_is_fast_and_identical():
    day_path = OUTPUT / "activity-timeline" / "jev" / f"{DAY}.json"
    if not day_path.is_file():
        pytest.skip("没有当天活动文件")
    payload = json.loads(day_path.read_text(encoding="utf-8"))
    items = sum(len(a.get("items") or []) for a in payload.get("activities") or []
                if isinstance(a, dict))
    if not items:
        pytest.skip("当天没有 items")

    t0 = time.perf_counter()
    new = ac.attach_to_payload(copy.deepcopy(payload), date=DAY, root=OUTPUT / "watch-sessions")
    new_seconds = time.perf_counter() - t0

    original_build = ac.build_window_index

    class _Unusable:
        complete = False

        def matches(self, _windows):
            return False

        def hits(self, _at):
            raise AssertionError("索引不应被使用")

    ac.build_window_index = lambda _w: _Unusable()
    try:
        old = ac.attach_to_payload(copy.deepcopy(payload), date=DAY, root=OUTPUT / "watch-sessions")
    finally:
        ac.build_window_index = original_build

    assert json.dumps(new, sort_keys=True, ensure_ascii=False) == \
           json.dumps(old, sort_keys=True, ensure_ascii=False), "索引路径与原实现不一致"
    # 原实现 37 s；索引路径实测 ~0.5 s。阈值留足机器波动余量。
    assert new_seconds < 2.5, f"attach_to_payload 仍要 {new_seconds:.2f}s（目标 <2.5s）"


# ── 4. openclaw_task_context / work_apps 接入索引后必须等价 ──────────────────
class _UnusableIndex:
    """强制走线性路径的占位索引（`attach_to_payload` 测试里也用它）。"""

    complete = False

    def matches(self, _windows):
        return False

    def hits(self, _at):
        raise AssertionError("索引不应被使用")


@pytest.fixture()
def clear_module_caches():
    from life_logger import openclaw_task_context, work_apps

    openclaw_task_context._WINDOWS_CACHE.clear()
    openclaw_task_context._WINDOW_INDEX_CACHE.clear()
    work_apps._WINDOWS_CACHE.clear()
    yield
    openclaw_task_context._WINDOWS_CACHE.clear()
    openclaw_task_context._WINDOW_INDEX_CACHE.clear()
    work_apps._WINDOWS_CACHE.clear()


def _windows_for_contexts():
    return [
        _mic("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", target="com.mic"),
        _front("2026-11-06T11:00:00+08:00", "2026-11-06T11:00:10+08:00", target="com.front"),
        _front("2026-11-06T12:00:00+08:00", "2026-11-06T12:00:10+08:00",
               target="com.work", idle_seconds=500),
    ]


def _tasks():
    return [
        {"task_id": "t1", "source": {"event_id": "e-mic"}},
        {"task_id": "t2", "source": {"event_id": "e-front"}},
        {"task_id": "t3", "source": {"event_id": "e-idle"}},
        {"task_id": "t4", "source": {"event_id": "e-missing"}},
        {"task_id": "t5"},
        "not-a-mapping",
    ]


def _events():
    def event(moment):
        return {"event_id": "x", "evidence": {"reported_at": moment}}

    return {
        "e-mic": event("2026-11-06T10:00:05+08:00"),
        "e-front": event("2026-11-06T11:00:05+08:00"),
        "e-idle": event("2026-11-06T12:00:05+08:00"),
    }


def test_openclaw_attach_input_context_equivalent_with_index(monkeypatch, clear_module_caches):
    from life_logger import app_context, openclaw_task_context as otc

    monkeypatch.setattr(app_context, "load_windows", lambda **_kw: _windows_for_contexts())
    monkeypatch.setattr(otc, "event_index", lambda _root: _events())

    with_index = otc.attach_input_context(_tasks(), root=ROOT / "unused")

    real_build = app_context.build_window_index
    app_context.build_window_index = lambda _w: _UnusableIndex()
    otc._WINDOWS_CACHE.clear()
    otc._WINDOW_INDEX_CACHE.clear()
    try:
        linear = otc.attach_input_context(_tasks(), root=ROOT / "unused")
    finally:
        app_context.build_window_index = real_build

    assert json.dumps(with_index, sort_keys=True, ensure_ascii=False) == \
           json.dumps(linear, sort_keys=True, ensure_ascii=False)
    # 结果本身也要符合契约：mic → dictation、前台 → front_app、空闲超时 → None
    by_id = {row["task_id"]: row for row in with_index if isinstance(row, dict)}
    assert by_id["t1"]["input_context"]["kind"] == "dictation"
    assert by_id["t2"]["input_context"]["kind"] == "front_app"
    assert by_id["t3"]["input_context"] is None
    assert by_id["t4"]["input_context"] is None
    assert by_id["t5"]["input_context"] is None
    assert with_index[-1] == "not-a-mapping"      # 非 dict 原样透传


def test_openclaw_falls_back_when_context_at_rejects_index(monkeypatch, clear_module_caches):
    """测试替身只接受 (moment, windows)：接入索引后仍要能跑（TypeError → 退回不带 index）。"""
    from life_logger import app_context, openclaw_task_context as otc

    monkeypatch.setattr(app_context, "load_windows", lambda **_kw: _windows_for_contexts())
    monkeypatch.setattr(otc, "event_index", lambda _root: _events())
    calls = []

    def fake_context_at(moment, windows):
        calls.append(moment)
        return {"app_label": "Fake App", "kind": "dictation", "confidence": "high"}

    monkeypatch.setattr(app_context, "context_at", fake_context_at)
    out = otc.attach_input_context([{"task_id": "t1", "source": {"event_id": "e-mic"}}],
                                   root=ROOT / "unused")
    assert out[0]["input_context"] == {
        "app_label": "Fake App", "kind": "dictation", "confidence": "high"}
    assert calls == ["2026-11-06T10:00:05+08:00"]


def test_work_apps_front_app_at_equivalent_with_index(monkeypatch, clear_module_caches):
    from life_logger import app_context, work_apps

    monkeypatch.setattr(app_context, "load_windows", lambda **_kw: _windows_for_contexts())
    moments = ["2026-11-06T10:00:05+08:00", "2026-11-06T11:00:05+08:00",
               "2026-11-06T12:00:05+08:00", "2026-11-06T13:00:00+08:00", "not-a-time"]
    with_index = [work_apps.front_app_at(m, root=ROOT / "unused") for m in moments]

    real_build = app_context.build_window_index
    app_context.build_window_index = lambda _w: _UnusableIndex()
    work_apps._WINDOWS_CACHE.clear()
    try:
        linear = [work_apps.front_app_at(m, root=ROOT / "unused") for m in moments]
    finally:
        app_context.build_window_index = real_build

    assert json.dumps(with_index, sort_keys=True, ensure_ascii=False) == \
           json.dumps(linear, sort_keys=True, ensure_ascii=False)
    assert with_index[0]["kind"] == "dictation"
    assert with_index[1]["kind"] == "front_app"
    assert with_index[2] is None            # 空闲 500s > 120s → 未知
    assert with_index[3] is None            # 没有覆盖该时刻的窗口
    assert with_index[4] is None            # 时刻非法


def test_work_apps_falls_back_when_context_at_rejects_index(monkeypatch, clear_module_caches):
    from life_logger import app_context, work_apps

    monkeypatch.setattr(app_context, "load_windows", lambda **_kw: _windows_for_contexts())
    monkeypatch.setattr(
        app_context, "context_at",
        lambda moment, windows: {"app_label": "Fake", "target_bundle": "com.fake",
                                 "kind": "front_app"},
    )
    out = work_apps.front_app_at("2026-11-06T11:00:05+08:00", root=ROOT / "unused")
    assert out == {"app_label": "Fake", "target_bundle": "com.fake", "kind": "front_app"}


def test_window_index_cache_invalidates_with_fingerprint(tmp_path, monkeypatch, clear_module_caches):
    """窗口文件一变，索引必须重建（不能拿旧索引匹配新窗口列表）。"""
    from life_logger import app_context, work_apps

    state = {"windows": _windows_for_contexts()}
    monkeypatch.setattr(app_context, "load_windows", lambda **_kw: state["windows"])
    watch = tmp_path / "watch-sessions"
    (watch / "input-context" / "days").mkdir(parents=True)
    day_file = watch / "input-context" / "days" / "2026-11-06.jsonl"
    day_file.write_text("{}", encoding="utf-8")

    first_windows, first_index = work_apps._windows_and_index(watch, "2026-11-06")
    assert first_index.matches(first_windows)

    state["windows"] = [_front("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00")]
    day_file.write_text("{}\n", encoding="utf-8")          # 指纹变化
    second_windows, second_index = work_apps._windows_and_index(watch, "2026-11-06")
    assert second_windows is state["windows"]
    assert second_index.matches(second_windows)
    assert second_windows is not first_windows


# ── v3 `prepare_records` 接入索引（2026-11-07）───────────────────────────────
def _v3_records():
    """三条记录：有 mic 命中、有 front 命中、有"空闲超时 → None"。"""
    return [
        {"source_id": "s1", "timestamp": "2026-11-06T10:00:05+08:00", "text": "对着麦克风说话"},
        {"source_id": "s2", "timestamp": "2026-11-06T10:00:22+08:00", "text": "前台窗口期间"},
        {"source_id": "s3", "timestamp": "2026-11-06T10:05:00+08:00", "text": "早已空闲"},
        # 已有 input_context 的行必须原样跳过（不查窗口）
        {"source_id": "s4", "timestamp": "2026-11-06T10:00:05+08:00",
         "input_context": {"app_label": "已有", "kind": "dictation", "confidence": "high"}},
        # 没有时间戳的行也必须原样保留
        {"source_id": "s5", "text": "没有时间戳"},
    ]


def _v3_windows():
    return [
        _mic("2026-11-06T10:00:00+08:00", "2026-11-06T10:00:10+08:00", target="com.mic.app"),
        _front("2026-11-06T10:00:15+08:00", "2026-11-06T10:00:30+08:00", target="com.front.app"),
    ]


def test_prepare_records_matches_linear_context_at(monkeypatch):
    """索引路径必须与"逐条线性 context_at"逐字段一致（含 None 与已有 input_context 的行）。"""
    from life_logger import activity_prompt_v3 as v3
    from life_logger import app_context

    monkeypatch.setattr(app_context, "load_windows", lambda **_kw: _v3_windows())
    expected = []
    windows = _v3_windows()
    for row in _v3_records():
        row = dict(row)
        if row.get("input_context") is None and str(row.get("timestamp") or "")[:10]:
            context = app_context.context_at(row["timestamp"], windows)     # 线性路径
            if context:
                row["input_context"] = context
        expected.append(row)

    got = v3.prepare_records(_v3_records(), ROOT / "unused")
    assert json.dumps(got, sort_keys=True, ensure_ascii=False) == \
           json.dumps(expected, sort_keys=True, ensure_ascii=False)
    by_id = {r["source_id"]: r for r in got}
    assert by_id["s1"]["input_context"]["kind"] == "dictation"
    assert by_id["s2"]["input_context"]["kind"] == "front_app"
    assert "input_context" not in by_id["s3"]        # 空闲超时 → 不写字段（不编）
    assert by_id["s4"]["input_context"]["app_label"] == "已有"
    assert "input_context" not in by_id["s5"]


def test_prepare_records_builds_index_once_per_day(monkeypatch):
    """同一天的多条记录只能建一次索引（否则退化成"每行建一次"）。"""
    from life_logger import activity_prompt_v3 as v3
    from life_logger import app_context

    monkeypatch.setattr(app_context, "load_windows", lambda **_kw: _v3_windows())
    calls = []
    real_build = app_context.build_window_index
    monkeypatch.setattr(app_context, "build_window_index",
                        lambda windows: calls.append(len(windows)) or real_build(windows))

    v3.prepare_records(_v3_records(), ROOT / "unused")
    assert calls == [2], calls                       # 一天一次，参数是那天的窗口数


def test_prepare_records_survives_two_arg_context_at(monkeypatch):
    """替身 `context_at` 只收 (moment, windows) → 必须回退，不抛 TypeError。"""
    from life_logger import activity_prompt_v3 as v3
    from life_logger import app_context

    monkeypatch.setattr(app_context, "load_windows", lambda **_kw: _v3_windows())
    calls = []

    def fake_context_at(moment, windows):            # 老签名：没有 index 关键字
        calls.append(moment)
        return {"app_label": "Fake App", "kind": "dictation", "confidence": "high"}

    monkeypatch.setattr(app_context, "context_at", fake_context_at)
    out = v3.prepare_records(_v3_records(), ROOT / "unused")

    assert [r.get("input_context", {}).get("app_label") for r in out[:3]] == \
           ["Fake App", "Fake App", "Fake App"]
    assert out[3]["input_context"]["app_label"] == "已有"      # 已有序不被覆盖
    assert calls == ["2026-11-06T10:00:05+08:00", "2026-11-06T10:00:22+08:00",
                     "2026-11-06T10:05:00+08:00"]


def test_prepare_records_falls_back_when_index_build_fails(monkeypatch):
    """建索引抛异常 → 退回线性扫描，结果仍与线性路径一致（不能因为加速而丢数据）。"""
    from life_logger import activity_prompt_v3 as v3
    from life_logger import app_context

    monkeypatch.setattr(app_context, "load_windows", lambda **_kw: _v3_windows())

    def boom(_windows):
        raise RuntimeError("index_unavailable")

    monkeypatch.setattr(app_context, "build_window_index", boom)
    got = v3.prepare_records(_v3_records(), ROOT / "unused")
    assert got[0]["input_context"]["kind"] == "dictation"
    assert got[1]["input_context"]["kind"] == "front_app"


def test_prepare_records_real_day_equivalent_and_faster():
    """真实 2026-11-06：索引路径与线性路径逐字段一致，且明显更快。"""
    from life_logger import activity_prompt_v3 as v3
    from life_logger import app_context

    day_file = OUTPUT / "activity-timeline" / "jev" / f"{DAY}.json"
    if not day_file.is_file():
        pytest.skip("本机没有 2026-11-06 的活动文件")
    activities = json.loads(day_file.read_text(encoding="utf-8")).get("activities") or []
    stamps = [i.get("spoken_at") or i.get("occurred_at")
              for a in activities for i in a.get("items") or []]
    records = [{"source_id": f"r{n}", "timestamp": ts, "text": "x"}
               for n, ts in enumerate(stamps[:200]) if ts]
    if not records:
        pytest.skip("这一天没有可用时间戳")

    windows = app_context.load_windows(root=OUTPUT / "watch-sessions", day=DAY)
    if not windows:
        pytest.skip("这一天没有归档窗口")

    linear = [app_context.context_at(r["timestamp"], windows) for r in records]
    start = time.perf_counter()
    got = v3.prepare_records(records, OUTPUT)
    elapsed = time.perf_counter() - start

    for row, expected in zip(got, linear):
        assert row.get("input_context") == expected, row.get("timestamp")
    print(f"\nprepare_records {len(records)} 条 / {len(windows)} 窗口: {elapsed*1000:.0f} ms")
    assert elapsed < 5.0, elapsed
