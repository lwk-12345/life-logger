"""模型调用用量记录（Q921）的回归测试。

全部用**假响应**：不调真模型、不联网、不写生产目录（用 `LL_MODEL_USAGE_DIR` 指向 tmp_path）。

守的不变量：
1. OpenAI / Gemini 两种 usage 形状都能抽出来（prompt/completion/cached/reasoning）；
2. 落盘字段齐全，且**绝不包含**提示词、原话、URL、密钥；
3. 日期按 UTC+8 归日（跨 UTC 零点时仍是本地日）；
4. 记录失败（目录不可写、序列化异常）**不影响主流程**，`record()` 返回 None 而不抛；
5. 并发追加不丢行、不写坏行（走 atomic.append_json_line 的 flock）；
6. `LL_MODEL_USAGE_LOG=0` 可整体关闭；
7. 汇总脚本能按天/按步骤聚合，坏行计数不静默。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import model_usage  # noqa: E402

UTC8 = dt.timezone(dt.timedelta(hours=8))

OPENAI_ENVELOPE = {
    "choices": [{"message": {"content": "{\"ok\":true}"}}],
    "usage": {
        "prompt_tokens": 1200,
        "completion_tokens": 300,
        "total_tokens": 1500,
        "prompt_tokens_details": {"cached_tokens": 800},
        "completion_tokens_details": {"reasoning_tokens": 210},
    },
    "model": "deepseek-v4.1-flash",
}

GEMINI_ENVELOPE = {
    "candidates": [{"content": {"parts": [{"text": "答案"}]}}],
    "usageMetadata": {
        "promptTokenCount": 900,
        "candidatesTokenCount": 120,
        "totalTokenCount": 1020,
        "cachedContentTokenCount": 500,
        "thoughtsTokenCount": 40,
    },
}


@pytest.fixture()
def usage_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LL_MODEL_USAGE_DIR", str(tmp_path / "model-usage"))
    monkeypatch.delenv("LL_MODEL_USAGE_LOG", raising=False)
    return tmp_path / "model-usage"


def _lines(directory: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


# --------------------------------------------------------------------------- 抽取
def test_extract_usage_openai_shape():
    usage = model_usage.extract_usage(OPENAI_ENVELOPE)
    assert usage == {
        "prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500,
        "cached_tokens": 800, "reasoning_tokens": 210,
    }


def test_extract_usage_gemini_shape():
    usage = model_usage.extract_usage(GEMINI_ENVELOPE)
    assert usage["prompt_tokens"] == 900
    assert usage["completion_tokens"] == 120
    assert usage["cached_tokens"] == 500
    assert usage["reasoning_tokens"] == 40


def test_extract_usage_handles_garbage():
    for payload in (None, "not-json", {"usage": "x"}, {"usage": {"prompt_tokens": "abc"}}, [1, 2]):
        assert model_usage.extract_usage(payload) == {}


# --------------------------------------------------------------------------- 落盘
def test_record_writes_one_line_with_all_fields(usage_dir):
    written = model_usage.record(
        "activity_timeline", payload=OPENAI_ENVELOPE, model="deepseek-v4.1-flash",
        elapsed_ms=1234.56, endpoint_kind="chat.completions", extra={"tier": "primary"},
    )
    assert written is not None
    lines = _lines(usage_dir)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["schema"] == "model-usage-v1"
    assert rec["step"] == "activity_timeline"
    assert rec["model"] == "deepseek-v4.1-flash"
    assert rec["prompt_tokens"] == 1200 and rec["completion_tokens"] == 300
    assert rec["cached_tokens"] == 800 and rec["reasoning_tokens"] == 210
    assert rec["elapsed_ms"] == 1234.6
    assert rec["ok"] is True
    assert rec["endpoint_kind"] == "chat.completions"
    assert rec["tier"] == "primary"
    # 日期是 UTC+8 的今天
    assert rec["date"] == dt.datetime.now(tz=UTC8).date().isoformat()


def test_record_never_writes_prompt_or_secrets(usage_dir):
    payload = {
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        "choices": [{"message": {"content": "用户原话：我在楼下买了包烟"}}],
    }
    model_usage.record(
        "natural_correction", payload=payload, model="m",
        extra={"prompt": "秘密提示词", "url": "https://api.example.com/v1/chat?key=SECRET",
               "api_key": "sk-SECRET", "content": "原话"},
    )
    raw = "".join(p.read_text(encoding="utf-8") for p in usage_dir.glob("*.jsonl"))
    for banned in ("我在楼下买了包烟", "秘密提示词", "api.example.com", "sk-SECRET", "原话"):
        assert banned not in raw, f"日志里出现了不该记录的内容：{banned}"


def test_date_uses_utc8_across_utc_midnight(usage_dir):
    # UTC 2026-11-05 17:30 == UTC+8 2026-11-06 01:30 → 必须归到 11-06
    when = dt.datetime(2026, 11, 5, 17, 30, tzinfo=dt.timezone.utc)
    model_usage.record("vip_fast_track", payload=OPENAI_ENVELOPE, now=when)
    assert (usage_dir / "2026-11-06.jsonl").is_file()
    assert not (usage_dir / "2026-11-05.jsonl").exists()


def test_record_failure_is_swallowed(tmp_path, monkeypatch):
    # 指向一个不可能创建的路径（父级是文件），record 必须返回 None 而不抛
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("LL_MODEL_USAGE_DIR", str(blocker / "nested"))
    monkeypatch.delenv("LL_MODEL_USAGE_LOG", raising=False)
    assert model_usage.record("activity_timeline", payload=OPENAI_ENVELOPE) is None


def test_record_can_be_disabled(usage_dir, monkeypatch):
    monkeypatch.setenv("LL_MODEL_USAGE_LOG", "0")
    assert model_usage.record("activity_timeline", payload=OPENAI_ENVELOPE) is None
    assert not usage_dir.exists() or not any(usage_dir.glob("*.jsonl"))


def test_concurrent_appends_do_not_lose_or_corrupt_lines(usage_dir):
    def worker(index: int) -> None:
        model_usage.record("memory_qa_gemini", payload=GEMINI_ENVELOPE, model=f"m{index}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = _lines(usage_dir)          # 任何坏行都会让 json.loads 抛错
    assert len(lines) == 12
    assert {r["model"] for r in lines} == {f"m{i}" for i in range(12)}


# --------------------------------------------------------------------------- 调用点接线（假响应）
def test_activity_digest_call_model_records_usage(usage_dir, monkeypatch):
    """接线验证：用假 urllib 响应跑 activity_digest.call_model，必须写出 usage。"""
    import urllib.request

    from life_logger import activity_digest

    class FakeResponse:
        def read(self):
            return json.dumps(OPENAI_ENVELOPE).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(activity_digest.cognitive, "_get_deepseek_settings",
                        lambda: ("http://127.0.0.1:3425/v1", "fake-key", "fake-model", None, None))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse())

    prepared = [{"activity_id": "act_1", "title": "测试", "items": [], "lifecycle": "live",
                 "reported_at": None}]
    activity_digest.call_model(prepared)

    steps = [r["step"] for r in _lines(usage_dir)]
    assert "activity_digest_summary" in steps
    record = next(r for r in _lines(usage_dir) if r["step"] == "activity_digest_summary")
    assert record["prompt_tokens"] == 1200
    assert record["model"] == "fake-model"


def test_vip_fast_track_records_primary_and_tier2(usage_dir, monkeypatch):
    import urllib.request

    from life_logger import vip_fast_track

    calls = {"n": 0}

    class FakeResponse:
        def read(self):
            return json.dumps(OPENAI_ENVELOPE).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeConfig:
        pass

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("primary down")
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    cfg = {"deepseek": {"base_url": "http://127.0.0.1:3425/v1", "api_key": "magpie",
                        "model": "primary-model", "tier2_model": "tier2-model"}}
    path = ROOT / "local-config.json"
    original = path.read_text(encoding="utf-8") if path.is_file() else None
    try:
        path.write_text(json.dumps(cfg), encoding="utf-8")
        vip_fast_track.call_magpie_deepseek("hello")
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(original, encoding="utf-8")

    records = [r for r in _lines(usage_dir) if r["step"] == "vip_fast_track"]
    assert len(records) == 1, f"应只记 tier2 这一次成功调用，实际 {len(records)}"
    assert records[0]["model"] == "tier2-model"
    assert records[0]["tier"] == "tier2"


# --------------------------------------------------------------------------- 汇总脚本
def _load_report_module():
    spec = importlib.util.spec_from_file_location(
        "model_usage_report", ROOT / "scripts" / "model_usage_report.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_report_aggregates_by_day_and_step(usage_dir, capsys):
    model_usage.record("activity_timeline", payload=OPENAI_ENVELOPE, model="m1", elapsed_ms=100)
    model_usage.record("activity_timeline", payload=OPENAI_ENVELOPE, model="m1", elapsed_ms=300)
    model_usage.record("memory_qa_gemini", payload=GEMINI_ENVELOPE, model="gemini",
                       elapsed_ms=50, ok=False, error_kind="HTTP 429")
    report = _load_report_module()
    rc = report.main(["--dir", str(usage_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "活动整理" in out and "问答 · Gemini" in out
    assert "2,400" in out           # activity_timeline: 1200 × 2
    assert "合计" in out


def test_report_counts_bad_lines(tmp_path, capsys):
    directory = tmp_path / "usage"
    directory.mkdir()
    (directory / "2026-11-06.jsonl").write_text(
        json.dumps({"step": "activity_timeline", "date": "2026-11-06", "ok": True,
                    "prompt_tokens": 10}) + "\n{broken json\n", encoding="utf-8")
    report = _load_report_module()
    assert report.main(["--dir", str(directory)]) == 0
    assert "坏行 1 条" in capsys.readouterr().out


def test_no_writes_under_pytest_without_explicit_dir(tmp_path, monkeypatch):
    """跑测试时若没显式指定目录，**不得**写生产目录（否则假响应会污染真实成本账）。

    背景：本功能刚接上时，既有用例（test_activity_digest 等）用假响应调用
    call_model/call_categories，一次全量 pytest 就往 output/local-web/model-usage/
    写了 24 条假记录。
    """
    monkeypatch.delenv("LL_MODEL_USAGE_DIR", raising=False)
    monkeypatch.delenv("LL_MODEL_USAGE_LOG", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_model_usage.py::fake (call)")
    # 线上服务会往生产目录写真实记录，所以比对调用前后，不要求目录为空
    def snapshot():
        if not model_usage.DEFAULT_DIR.exists():
            return {}
        return {p.name: p.stat().st_size for p in model_usage.DEFAULT_DIR.glob("*.jsonl")}

    before = snapshot()
    assert model_usage.record("activity_timeline", payload=OPENAI_ENVELOPE) is None
    assert snapshot() == before
