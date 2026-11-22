"""日报缓存 events 指纹回归测试。

缺陷：`generate_daily_cognitive_synthesis` 原来只按「日期文件名 + force」判断缓存命中，
事件被纠正/补录后仍返回旧摘要，且调用方（调度器 `_decide`）也会 skip。

修复后必须满足：
1. 同一 events → 指纹稳定（3 次相同；输入顺序无关；键序无关）
2. 语义字段变 → 指纹变；纯元数据（updated_at）变 → 指纹不变
3. 指纹一致 + force=False → 不调用模型
4. 指纹不一致 + force=False → 调用模型 1 次
5. 旧格式缓存（无 events_fingerprint）→ 视为未命中并重建  ← 本次修复的核心
6. force=True → 一定重建
7. 调度器 `_decide` 四种组合行为正确
8. 指纹计算毫秒级，且不引入 daily_dashboard 的目录扫描

全程不联网：模型调用被替换成计数桩。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import daily_cognitive_session as dcs  # noqa: E402

DATE = "2026-10-27"
FP_FIELD = dcs.EVENTS_FINGERPRINT_FIELD


# ── 夹具 ────────────────────────────────────────────────────
def _event(title="晚上在餐厅吃饭买单", hhmm="20:10", cat="food", act="meal", **extra):
    ev = {
        "event_id": "ev_e2e_001",
        "title": title,
        "categories": [cat],
        "activity_type": act,
        "time": {"absolute": f"{DATE}T{hhmm}:00+08:00", "precision": "exact"},
        "evidence": {"quote": title},
    }
    ev.update(extra)
    return ev


def _fake_synthesis(date_str, *, title="fake"):
    """最小合法 synthesis payload（足够让写盘/归一化走通）。"""
    return {
        "date": date_str,
        "executive_summary": title,
        "nutrition_analysis": {"total_calories_kcal": 0, "macronutrients": {},
                               "meals_summary": "", "dietary_quality": "", "dietary_advice": ""},
        "fitness_analysis": {"target_body_parts": [], "workout_highlight": "", "recovery_state": ""},
        "habit_and_smoking_analysis": {"smoke_count": 0, "brand": "", "psychological_conflict": "",
                                       "harm_reduction_guidance": ""},
        "expense_analysis": {"total_amount_cny": 0, "items_summary": "", "spending_context": ""},
        "work_and_cognition_analysis": {"architecture_decisions": [], "cognition_insights": ""},
        "attention_and_energy": {"energy_score": 50, "energy_trajectory": ""},
        "tomorrow_advice": [],
    }


class CallCounter:
    """替换 `_call_model`：只计数并返回固定 payload，不联网。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, url, api_key, body, *, tier):
        self.calls += 1
        return _fake_synthesis(DATE), [{"tier": tier, "ok": True}]


@pytest.fixture()
def patched(monkeypatch):
    counter = CallCounter()
    monkeypatch.setattr(dcs, "_get_deepseek_settings",
                        lambda: ("http://fake.invalid", "fake-key", "m1", "m2", None))
    monkeypatch.setattr(dcs, "_call_model", counter)
    return counter


# ── 1. 稳定性 ──────────────────────────────────────────────
def test_fingerprint_stable_across_calls():
    events = [_event(), _event(title="在健身房练腿", hhmm="22:00", cat="exercise", act="workout")]
    runs = {dcs.events_fingerprint(events) for _ in range(3)}
    assert len(runs) == 1, f"三次计算结果不一致: {runs}"


def test_fingerprint_ignores_input_order():
    a = _event()
    b = _event(title="在健身房练腿", hhmm="22:00", cat="exercise", act="workout")
    assert dcs.events_fingerprint([a, b]) == dcs.events_fingerprint([b, a])


def test_fingerprint_ignores_dict_key_order():
    e1 = {"title": "x", "categories": ["food"], "time": {"absolute": "2026-10-27T20:10:00+08:00"},
          "evidence": {"quote": "x"}}
    e2 = {"evidence": {"quote": "x"}, "time": {"absolute": "2026-10-27T20:10:00+08:00"},
          "categories": ["food"], "title": "x"}
    assert dcs.events_fingerprint([e1]) == dcs.events_fingerprint([e2])


# ── 2. 敏感性 ──────────────────────────────────────────────
def test_fingerprint_changes_on_title():
    assert dcs.events_fingerprint([_event()]) != dcs.events_fingerprint([_event(title="【已纠正】晚上在餐厅吃饭并买单")])


def test_fingerprint_changes_on_added_event():
    assert dcs.events_fingerprint([_event()]) != dcs.events_fingerprint(
        [_event(), _event(title="【补录】20:30 药店买药", hhmm="20:30")])


def test_fingerprint_changes_on_activity_type():
    assert dcs.events_fingerprint([_event(act="meal")]) != dcs.events_fingerprint([_event(act="drink")])


def test_fingerprint_ignores_irrelevant_metadata():
    """updated_at / correction_provenance 不在字段集内 → 不应导致失效。"""
    base = _event()
    noisy = _event(updated_at="2026-10-29T05:00:00+08:00",
                   correction_provenance={"note": "e2e", "updated_at": "2026-10-29T05:00:00+08:00"})
    assert dcs.events_fingerprint([base]) == dcs.events_fingerprint([noisy])


# ── 3. 命中：指纹一致 → 不调模型 ───────────────────────────
def test_cache_hit_does_not_call_model(tmp_path, patched):
    events = [_event()]
    first = dcs.generate_daily_cognitive_synthesis(DATE, events, cache_dir=tmp_path)
    assert patched.calls == 1
    payload = json.loads((tmp_path / f"synthesis-{DATE}.json").read_text(encoding="utf-8"))
    assert payload[FP_FIELD] == dcs.events_fingerprint(events)

    second = dcs.generate_daily_cognitive_synthesis(DATE, events, cache_dir=tmp_path)
    assert patched.calls == 1, "指纹一致时不应重新调用模型"
    assert second is not None and first is not None


# ── 4. 失效：指纹不一致 → 调模型 1 次 ─────────────────────
def test_cache_miss_when_events_changed(tmp_path, patched):
    dcs.generate_daily_cognitive_synthesis(DATE, [_event()], cache_dir=tmp_path)
    assert patched.calls == 1

    changed = [_event(title="【已纠正】晚上在餐厅吃饭并买单", act="drink")]
    dcs.generate_daily_cognitive_synthesis(DATE, changed, cache_dir=tmp_path)
    assert patched.calls == 2, "事件改变后必须重建"

    payload = json.loads((tmp_path / f"synthesis-{DATE}.json").read_text(encoding="utf-8"))
    assert payload[FP_FIELD] == dcs.events_fingerprint(changed)


# ── 5. 核心：旧格式无指纹 → 未命中 ─────────────────────────
def test_legacy_cache_without_fingerprint_is_miss(tmp_path, patched):
    path = tmp_path / f"synthesis-{DATE}.json"
    legacy = _fake_synthesis(DATE, title="旧格式摘要")
    legacy.pop(FP_FIELD, None)
    assert FP_FIELD not in legacy
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    dcs.generate_daily_cognitive_synthesis(DATE, [_event()], cache_dir=tmp_path)
    assert patched.calls == 1, "旧格式缓存必须视为未命中并重建"

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload[FP_FIELD] == dcs.events_fingerprint([_event()])


# ── 6. force=True 一定重建 ────────────────────────────────
def test_force_true_always_rebuilds(tmp_path, patched):
    events = [_event()]
    dcs.generate_daily_cognitive_synthesis(DATE, events, cache_dir=tmp_path)
    assert patched.calls == 1
    dcs.generate_daily_cognitive_synthesis(DATE, events, cache_dir=tmp_path, force=True)
    assert patched.calls == 2

    # 指纹不一致 + force=True 同样重建
    dcs.generate_daily_cognitive_synthesis(DATE, [_event(act="drink")], cache_dir=tmp_path, force=True)
    assert patched.calls == 3


# ── 7. 调度器 _decide 四种组合 ────────────────────────────
def _load_scheduler(monkeypatch, out_dir: Path):
    """把 scripts/daily_synthesis_scheduler.py 作为模块加载，OUT_DIR 指向 tmp。"""
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src") + ":" + str(ROOT / "scripts"))
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "daily_synthesis_scheduler_under_test", ROOT / "scripts" / "daily_synthesis_scheduler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.OUT_DIR = out_dir
    return mod


def test_scheduler_decide_matrix(tmp_path, monkeypatch, patched):
    mod = _load_scheduler(monkeypatch, tmp_path)
    events = [_event()]

    # (a) 文件不存在 → run
    assert mod._decide(DATE, False, events)[0] == "run"

    # 写一份带正确指纹的缓存
    dcs.generate_daily_cognitive_synthesis(DATE, events, cache_dir=tmp_path)

    # (b) 文件在 + 指纹同 + 非 force → skip-exists
    decision, reason = mod._decide(DATE, False, events)
    assert decision == "skip-exists", reason
    assert "未变化" in reason

    # (c) 文件在 + 指纹不同 + 非 force → run
    decision, reason = mod._decide(DATE, False, [_event(title="改了标题")])
    assert decision == "run", reason
    assert "已变更" in reason

    # (d) 文件在 + force → run
    assert mod._decide(DATE, True, events)[0] == "run"

    # (e) 旧格式无指纹 → run 且理由可读
    payload = json.loads((tmp_path / f"synthesis-{DATE}.json").read_text(encoding="utf-8"))
    payload.pop(FP_FIELD, None)
    (tmp_path / f"synthesis-{DATE}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    decision, reason = mod._decide(DATE, False, events)
    assert decision == "run" and "旧格式" in reason

    # (f) 文件不存在 + 无事件 → skip-empty
    assert mod._decide("2026-01-01", False, [])[0] == "skip-empty"


# ── 8. 性能与依赖面 ───────────────────────────────────────
def test_fingerprint_is_fast():
    events = [_event(title=f"事件{i}", hhmm=f"{i % 24:02d}:00") for i in range(500)]
    t = time.monotonic()
    for _ in range(20):
        dcs.events_fingerprint(events)
    per_call_ms = (time.monotonic() - t) / 20 * 1000
    assert per_call_ms < 50, f"500 事件单次指纹 {per_call_ms:.2f}ms，超出毫秒级预算"


def test_fingerprint_does_not_import_daily_dashboard():
    """指纹只依赖 json/hashlib，不得引入 daily_dashboard 的目录扫描。"""
    src = (ROOT / "src/life_logger/daily_cognitive_session.py").read_text(encoding="utf-8")
    fn_start = src.index("def events_fingerprint(")
    fn_end = src.index("def generate_daily_cognitive_synthesis(")
    body = src[fn_start:fn_end]
    for forbidden in ("daily_dashboard", "_watch_content_fingerprint", "glob(", "rglob(", "iterdir("):
        assert forbidden not in body, f"指纹函数体内出现 {forbidden}"
