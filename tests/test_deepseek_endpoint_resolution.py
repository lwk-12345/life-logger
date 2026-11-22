"""回归测试：DeepSeek 运行时端点解析必须走 local-config.json，不得落到官网兜底。

【背景 · 2026-10-14 夜】
症状：job 侧 `context-extraction.json` 长期 `status=failed`、`endpoint=https://api.deepseek.com`，
导致消费金额全 0、事件提取降级为 review_fallback。

根因（两层）：
1. `contextual_extraction.py` 的 `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` 是**模块级常量**，
   在 import 那一刻固化。旧代码在 `attribute_funasr_transcript.py` 里写
   `os.environ["DEEPSEEK_BASE_URL"] = ...` 试图修正，只要模块已被 import 就完全无效。
2. `local_pipeline.build_attribution_command` 硬编码 `--deepseek-model deepseek-flash`，
   且从不传 base_url → 只能落到模块默认的官网。

修法：把值当**参数**传给 `run_contextual_extraction`（内部会 `configure_deepseek`），
并让 `configure_deepseek` 在参数缺失时兜底读 `local-config.json` 的 `deepseek` 节。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# [2026-11-04 夹具入库] 这条资源无法入库（体积过大 / 含真实录音 / 含本机配置），
# 缺失时 skip 而不是 fail —— 新克隆不该因为缺少本机数据而红。
import pytest as _pytest  # noqa: E402

if not ((ROOT / "local-config.json").is_file()):
    _pytest.skip(
        "缺少不入库的夹具：local-config.json 含本机密钥与绝对路径，不入库",
        allow_module_level=True,
    )
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


def _load_attribute_script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_aft_under_test", ROOT / "scripts" / "attribute_funasr_transcript.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_deepseek_runtime_prefers_local_config(tmp_path: Path, monkeypatch) -> None:
    """local-config.json 的 deepseek 节必须被读出来（env 为空时以配置为准）。"""
    module = _load_attribute_script()

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    cfg = tmp_path / "local-config.json"
    cfg.write_text(
        json.dumps(
            {
                "deepseek": {
                    "api_key": "key-from-config",
                    "base_url": "https://api.commandcode.ai/provider/v1",
                    "model": "deepseek/deepseek-v4-flash",
                }
            }
        ),
        encoding="utf-8",
    )

    # 让 helper 认为仓库根就是 tmp_path（helper 用 Path(__file__).parents[1] 定位配置）
    monkeypatch.setattr(module, "__file__", str(tmp_path / "scripts" / "x.py"))

    api_key, base_url, model = module._resolve_deepseek_runtime()

    assert api_key == "key-from-config"
    assert base_url == "https://api.commandcode.ai/provider/v1"
    assert model == "deepseek/deepseek-v4-flash"


def test_resolve_deepseek_runtime_falls_back_to_env(monkeypatch) -> None:
    """环境变量优先于配置文件（显式覆盖语义）。"""
    module = _load_attribute_script()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "key-from-env")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("DEEPSEEK_MODEL", "env-model")

    api_key, base_url, model = module._resolve_deepseek_runtime()

    assert api_key == "key-from-env"
    assert base_url == "https://env.example/v1"
    assert model == "env-model"


def test_configure_deepseek_falls_back_to_local_config(tmp_path: Path, monkeypatch) -> None:
    """参数缺失时 configure_deepseek 必须读 local-config.json，而不是静默保留官网默认。"""
    from life_logger import contextual_extraction as ce

    cfg = tmp_path / "local-config.json"
    cfg.write_text(
        json.dumps(
            {
                "deepseek": {
                    "base_url": "https://api.commandcode.ai/provider/v1",
                    "model": "deepseek/deepseek-v4-flash",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(ce.LOCAL_CONFIG_ENV, str(cfg))

    original_url, original_model = ce.DEEPSEEK_BASE_URL, ce.DEEPSEEK_MODEL
    try:
        ce.configure_deepseek(base_url=None, model=None)
        assert ce.DEEPSEEK_BASE_URL == "https://api.commandcode.ai/provider/v1"
        assert ce.DEEPSEEK_MODEL == "deepseek/deepseek-v4-flash"
    finally:
        ce.DEEPSEEK_BASE_URL = original_url
        ce.DEEPSEEK_MODEL = original_model


def test_configure_deepseek_explicit_args_win(tmp_path: Path, monkeypatch) -> None:
    """显式参数必须压过配置文件。"""
    from life_logger import contextual_extraction as ce

    cfg = tmp_path / "local-config.json"
    cfg.write_text(
        json.dumps({"deepseek": {"base_url": "https://from-config/v1", "model": "config-model"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(ce.LOCAL_CONFIG_ENV, str(cfg))

    original_url, original_model = ce.DEEPSEEK_BASE_URL, ce.DEEPSEEK_MODEL
    try:
        ce.configure_deepseek(base_url="https://explicit/v1", model="explicit-model")
        assert ce.DEEPSEEK_BASE_URL == "https://explicit/v1"
        assert ce.DEEPSEEK_MODEL == "explicit-model"
    finally:
        ce.DEEPSEEK_BASE_URL = original_url
        ce.DEEPSEEK_MODEL = original_model


def test_load_deepseek_settings_never_raises_on_bad_config(tmp_path: Path) -> None:
    """配置损坏时返回空 dict，不抛异常（不能因配置坏了就挂掉整条提取）。"""
    from life_logger import contextual_extraction as ce

    bad = tmp_path / "local-config.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    assert ce.load_deepseek_settings(bad) == {}

    missing = tmp_path / "nope.json"
    assert ce.load_deepseek_settings(missing) == {}


def test_build_attribution_command_passes_configured_endpoint() -> None:
    """归属命令必须带上配置里的 model 与 base_url，不得硬编码老模型名或省略端点。"""
    from life_logger.local_pipeline import build_attribution_command

    # 基于仓库真实配置构造（避免漏字段），只替换 deepseek 节
    base_config: dict[str, Any] = json.loads(
        (ROOT / "local-config.json").read_text(encoding="utf-8")
    )
    base_config["deepseek"] = {
        "enabled": True,
        "api_key": "x",
        "base_url": "https://api.commandcode.ai/provider/v1",
        "model": "deepseek/deepseek-v4-flash",
    }

    command = build_attribution_command(
        audio=Path("/tmp/x.m4a"),
        transcript=Path("/tmp/t.json"),
        recordings=Path("/tmp/r.json"),
        output_dir=Path("/tmp/out"),
        config=base_config,
        timezone_name="Asia/Shanghai",
    )
    joined = " ".join(str(part) for part in command)

    assert "--deepseek-base-url" in joined, joined
    assert "https://api.commandcode.ai/provider/v1" in joined, joined
    assert "deepseek/deepseek-v4-flash" in joined, joined
    assert "--deepseek-model deepseek-flash" not in joined, joined
    # 绝不能再落到官网端点（红线：不走 DeepSeek 官网）
    assert "api.deepseek.com" not in joined, joined


def test_memory_rag_resolves_endpoint_from_config_not_official_site(tmp_path: Path) -> None:
    """回归：memory_rag 的 DeepSeek 备用链路必须读 local-config.json，不得落官网。

    【2026-10-14 实测事故】它原来只有
    `os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"`
    —— 而 8765 由 launchd 启动、环境里没有该变量 → 备用链路**每次都打官网**，
    拿 Command Code 的 key 换 401。且请求头缺 `User-Agent`，就算端点对了
    也会被 Command Code 前置的 Cloudflare 用 403 error:1010 秒拒。
    两处已一起修（新增 `_get_deepseek_endpoint()` + 补 User-Agent）。
    """
    from life_logger.memory_rag import MemoryRAGService

    job_root = tmp_path / "local-web"
    job_root.mkdir(parents=True)
    cfg = tmp_path / "local-config.json"
    cfg.write_text(
        json.dumps(
            {
                "deepseek": {
                    "api_key": "k",
                    "base_url": "https://api.commandcode.ai/provider/v1",
                    "model": "deepseek/deepseek-v4-flash",
                }
            }
        ),
        encoding="utf-8",
    )

    service = MemoryRAGService(job_root, cfg)
    base_url, model = service._get_deepseek_endpoint()

    assert base_url == "https://api.commandcode.ai/provider/v1"
    assert model == "deepseek/deepseek-v4-flash"
    assert "api.deepseek.com" not in base_url


def test_memory_rag_falls_back_to_env_when_config_missing(tmp_path: Path, monkeypatch) -> None:
    """配置缺失时才用环境变量（显式覆盖语义）。"""
    from life_logger.memory_rag import MemoryRAGService

    job_root = tmp_path / "local-web"
    job_root.mkdir(parents=True)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("DEEPSEEK_MODEL", "env-model")

    service = MemoryRAGService(job_root, tmp_path / "does-not-exist.json")
    base_url, model = service._get_deepseek_endpoint()

    assert base_url == "https://env.example/v1"
    assert model == "env-model"
