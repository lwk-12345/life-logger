"""Unit tests for Personal Memory RAG and Daily Dashboard Builder."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from life_logger.daily_dashboard import DailyDashboardBuilder
from life_logger.health_store import HealthDataStore, HeartRatePoint
from life_logger.memory_rag import MemoryRAGService
from life_logger.voice_triggers import QuickCaptureStore, scan_voice_triggers


def test_memory_rag_service(tmp_path: Path) -> None:
    job_root = tmp_path / "job_root"
    job_root.mkdir()

    # 准备随手记
    captures = scan_voice_triggers("记一下：周五下午五点去修车", timestamp="2026-10-10T14:30:00")
    cstore = QuickCaptureStore(job_root / "quick_captures.json")
    cstore.append(captures)

    # 准备心率
    hstore = HealthDataStore(job_root / "health")
    hstore.record_heart_rates([
        HeartRatePoint(timestamp="2026-10-10T15:00:00", bpm=112.0),
    ])

    service = MemoryRAGService(job_root)
    citations = service.collect_recent_memory_context(query="修车")
    assert len(citations) >= 1
    assert any("修车" in c.snippet for c in citations)

    # 离线提问测试（无 API Key 走本地可信结构化回答）
    answer = service.ask("周五下午有什么安排？")
    assert answer.confidence > 0
    assert "修车" in answer.answer


def test_daily_dashboard_builder(tmp_path: Path) -> None:
    job_root = tmp_path / "job_root"
    job_root.mkdir()

    hstore = HealthDataStore(job_root / "health")
    hstore.record_heart_rates([
        HeartRatePoint(timestamp="2026-10-11T09:00:00", bpm=75.0),
        HeartRatePoint(timestamp="2026-10-11T14:00:00", bpm=82.0),
    ])
    hstore.record_steps("2026-10-11", 6200)

    cstore = QuickCaptureStore(job_root / "quick_captures.json")
    cstore.append(scan_voice_triggers("记一下：写周报", timestamp="2026-10-11T10:00:00"))
    cstore.append(scan_voice_triggers("午饭花了32块", timestamp="2026-10-11T12:30:00"))

    builder = DailyDashboardBuilder(job_root)
    dash = builder.build_for_date("2026-10-11")
    assert dash.date == "2026-10-11"
    assert dash.health_summary["total_steps"] == 6200
    assert len(dash.captured_todos) == 1
    assert len(dash.captured_expenses) == 1
    assert "写周报" in dash.captured_todos[0]["extracted_content"]
    assert "32.00" in dash.captured_expenses[0]["extracted_content"]
    assert "6200" in dash.ai_daily_highlights


def test_gemini_answer_path_records_timing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 回归：函数内 `import time` 曾让 time.monotonic() 抛 UnboundLocalError，问答静默失败
    import life_logger.memory_rag as rag
    from life_logger import model_usage

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self) -> bytes:
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "周五修车"}]}}]}).encode("utf-8")

    recorded: list[str] = []
    monkeypatch.setattr(rag.urllib.request, "urlopen", lambda req, timeout=0: _Resp())
    monkeypatch.setattr(model_usage, "record", lambda name, **_: recorded.append(name))
    service = MemoryRAGService(tmp_path)
    monkeypatch.setattr(service, "_get_gemini_key", lambda: "test-key")
    monkeypatch.setattr(service, "_get_deepseek_key", lambda: None)

    answer = service.ask("周五干什么？")
    assert answer.answer == "周五修车"
    assert recorded == ["memory_qa_gemini"]
