"""启动保护：与处理模式无关，防止重复启动，且清理脚本只碰自己登记的资源。"""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import pytest
from life_logger.local_web import serve
from life_logger.runtime_queue import DispatchConflict, WorkerLease


def test_serve_guard_rejects_second_combined_instance_on_non_default_root(tmp_path):
    root = tmp_path / "drill-state"
    root.mkdir()
    config = tmp_path / "config.json"
    config.write_text("{}")
    with WorkerLease(root):
        with pytest.raises(DispatchConflict):
            serve(root=root, config_path=config, open_browser=False, processing_mode="combined")


def test_serve_guard_default_root_uses_configured_default(tmp_path, monkeypatch):
    root = tmp_path / "custom-default"
    root.mkdir()
    config = tmp_path / "config.json"
    config.write_text("{}")
    monkeypatch.setattr("life_logger.local_web.DEFAULT_JOB_ROOT", root)
    with WorkerLease(root):
        with pytest.raises(DispatchConflict):
            serve(config_path=config, open_browser=False, processing_mode="combined")


def test_serve_passes_actual_root_to_runtime(tmp_path, monkeypatch):
    root = tmp_path / "drill-state"
    root.mkdir()
    config = tmp_path / "config.json"
    config.write_text("{}")
    calls = []
    monkeypatch.setattr("life_logger.local_web._serve_runtime", lambda **kwargs: calls.append(kwargs))
    serve(root=root, config_path=config, open_browser=False, processing_mode="combined")
    assert calls and calls[0]["root"] == root


def test_drill_cleanup_only_kills_registered_marked_process_and_dir():
    drill_path = Path(__file__).resolve().parents[1] / "work" / "external-mode-20261023" / "drill.py"
    spec = importlib.util.spec_from_file_location("external_drill_cleanup", drill_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workdir = Path(tempfile.mkdtemp(prefix="external-drill-")).resolve()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(workdir)])
    module.register_drill_process(process, workdir)
    module.cleanup_drill_resources()
    assert process.poll() is not None
    assert not workdir.exists()


def test_drill_cleanup_rejects_unmarked_or_unregistered_resources(tmp_path):
    drill_path = Path(__file__).resolve().parents[1] / "work" / "external-mode-20261023" / "drill.py"
    spec = importlib.util.spec_from_file_location("external_drill_cleanup2", drill_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(ValueError):
            module.register_drill_process(process, tmp_path / "not-a-drill-dir")
        module.cleanup_drill_resources()
        assert process.poll() is None, "unknown process must not be killed"
    finally:
        process.terminate()
        process.wait(timeout=5)

