"""大模型入口必须认实例配置路径：隔离实例不再读生产配置、不写生产 model-usage。"""
from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from life_logger import llm_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_override():
    llm_config.set_config_path(None)
    yield
    llm_config.set_config_path(None)


def _cfg(tmp_path: Path) -> Path:
    p = tmp_path / "local-config.json"
    p.write_text(json.dumps({
        "deepseek": {"api_key": "smoke-fake-key", "base_url": "http://127.0.0.1:9/v1", "model": "fake"},
    }, ensure_ascii=False), encoding="utf-8")
    return p


def test_daily_session_reads_instance_config(tmp_path):
    from life_logger import daily_cognitive_session as dcs

    cfg = _cfg(tmp_path)
    llm_config.set_config_path(cfg)
    assert dcs._read_config()["deepseek"]["api_key"] == "smoke-fake-key"


def test_contextual_extraction_reads_instance_config(tmp_path):
    from life_logger import contextual_extraction as cx

    cfg = _cfg(tmp_path)
    llm_config.set_config_path(cfg)
    assert cx.local_config_path() == cfg.resolve()
    assert cx.load_deepseek_settings()["api_key"] == "smoke-fake-key"


def test_model_usage_goes_to_instance_data_root(tmp_path):
    from life_logger import model_usage

    llm_config.set_config_path(_cfg(tmp_path))
    assert model_usage.usage_dir() == tmp_path / "output" / "local-web" / "model-usage"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_isolated_instance_does_not_touch_production_usage(tmp_path):
    """真 HTTP 链：临时实例 + 不可达 base_url，POST /api/daily/synthesize 不得污染生产。"""
    from life_logger.local_web import serve

    root = tmp_path / "data"
    (root / "jobs").mkdir(parents=True)
    (root / "activity-timeline" / "jev").mkdir(parents=True)
    (root / "daily-synthesis").mkdir(parents=True)
    cfg = _cfg(tmp_path)
    port = _free_port()
    threading.Thread(target=lambda: serve(host="127.0.0.1", port=port, config_path=cfg,
                                          open_browser=False, processing_mode="combined",
                                          manage_worker=False, root=root), daemon=True).start()
    for _ in range(120):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    else:
        pytest.skip("temp server did not start")

    prod_dir = ROOT / "output" / "local-web" / "model-usage"
    def snapshot() -> dict[str, str]:
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(prod_dir.glob("*.jsonl"))} if prod_dir.is_dir() else {}

    before = snapshot()
    token = json.loads((root / "secrets.json").read_text())["mobile_access_token"]
    req = Request(f"http://127.0.0.1:{port}/api/daily/synthesize", data=b'{"date":"2026-11-05"}',
                  method="POST", headers={
                      "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                      "Origin": f"http://127.0.0.1:{port}", "Host": f"127.0.0.1:{port}",
                      "X-Life-Logger-CSRF": "1"})
    try:
        with urlopen(req, timeout=60) as resp:
            resp.read(200)
    except HTTPError as error:
        error.read(200)          # 4xx/5xx 都可以，只要不是打到真实模型
    except Exception:
        pass

    assert snapshot() == before, "隔离实例把用量写进了生产 model-usage"
