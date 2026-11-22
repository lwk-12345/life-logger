import json
from unittest.mock import patch

import pytest

from life_logger import daily_cognitive_session as synthesis


@pytest.mark.parametrize("force", [False, True])
def test_failed_generation_preserves_existing_synthesis(tmp_path, monkeypatch, force):
    target = tmp_path / "synthesis-2000-01-01.json"
    original = json.dumps({"date": "2000-01-01", "cache_fingerprint": "old",
                           "executive_summary": "original summary"}).encode()
    target.write_bytes(original)
    monkeypatch.setenv("LIFE_LOGGER_PAUSE_ON_MODEL_FAILURE", "0")
    with patch.object(synthesis, "_get_deepseek_settings",
                      return_value=("http://unused", "fake", "fake", None, None)), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("stub failure")), \
         patch("time.sleep"):
        result = synthesis.generate_daily_cognitive_synthesis(
            "2000-01-01", [], force=force, cache_dir=tmp_path)
    assert result["fallback"] is True
    assert target.read_bytes() == original
    assert result["preserved_existing"] is True
    assert result["persisted"] is False


def test_fallback_does_not_overwrite_concurrent_success(tmp_path, monkeypatch):
    target = tmp_path / "synthesis-2000-01-01.json"
    actual_link = synthesis.os.link
    def concurrent_success(source, destination):
        target.write_text("concurrent success", encoding="utf-8")
        return actual_link(source, destination)
    monkeypatch.setattr(synthesis.os, "link", concurrent_success)
    monkeypatch.setenv("LIFE_LOGGER_PAUSE_ON_MODEL_FAILURE", "0")
    with patch.object(synthesis, "_get_deepseek_settings",
                      return_value=("http://unused", "fake", "fake", None, None)), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("stub failure")), \
         patch("time.sleep"):
        result = synthesis.generate_daily_cognitive_synthesis(
            "2000-01-01", [], cache_dir=tmp_path)
    assert target.read_text(encoding="utf-8") == "concurrent success"
    assert result["preserved_existing"] is True
    assert sorted(p.name for p in tmp_path.iterdir()) == [target.name]


def test_link_failure_reports_error_without_leaving_partial_report(tmp_path, monkeypatch):
    monkeypatch.setattr(synthesis.os, "link", lambda *args: (_ for _ in ()).throw(OSError("link denied")))
    monkeypatch.setenv("LIFE_LOGGER_PAUSE_ON_MODEL_FAILURE", "0")
    with patch.object(synthesis, "_get_deepseek_settings",
                      return_value=("http://unused", "fake", "fake", None, None)), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("stub failure")), \
         patch("time.sleep"):
        result = synthesis.generate_daily_cognitive_synthesis(
            "2000-01-01", [], cache_dir=tmp_path)
    assert "link denied" in result["persist_error"]
    assert list(tmp_path.iterdir()) == []


def test_concurrent_fallback_processes_publish_once(tmp_path):
    import multiprocessing
    import os
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    output = ctx.Queue()
    def run():
        actual_link = synthesis.os.link
        def synchronized_link(src, dst):
            barrier.wait(timeout=5)
            return actual_link(src, dst)
        os.environ["LIFE_LOGGER_PAUSE_ON_MODEL_FAILURE"] = "0"
        with patch.object(synthesis, "_get_deepseek_settings",
                          return_value=("http://unused", "fake", "fake", None, None)), \
             patch("urllib.request.urlopen", side_effect=RuntimeError("stub failure")), \
             patch("time.sleep"), patch.object(synthesis.os, "link", side_effect=synchronized_link):
            result = synthesis.generate_daily_cognitive_synthesis(
                "2000-01-01", [], cache_dir=tmp_path)
            output.put((bool(result.get("preserved_existing")), result.get("persist_error")))
    children = [ctx.Process(target=run) for _ in range(2)]
    for child in children:
        child.start()
    for child in children:
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()
            child.join(timeout=2)
        assert child.exitcode == 0
    results = [output.get(timeout=2) for _ in children]
    assert sorted(results) == [(False, None), (True, None)]
    target = tmp_path / "synthesis-2000-01-01.json"
    assert json.loads(target.read_text())["date"] == "2000-01-01"
    assert sorted(path.name for path in tmp_path.iterdir()) == [target.name]
