"""大模型配置路径的**实例级**覆盖（2026-11-07）。

背景（work/http-smoke-20261107）：临时实例把 `config_path` 指向测试配置（假 key），
但 `daily_cognitive_session`、`contextual_extraction` 等模块直接读
`PROJECT_ROOT/local-config.json`，于是隔离测试仍然调用了真实模型、写了真实 model-usage。

约定：
* 服务实例启动时调用 `set_config_path(self.config_path)`；
* 各模型入口用 `read_config()` / `config_path()` 取配置，**没有覆盖时回退生产默认**（行为不变）；
* 不用环境变量，避免一个进程里多个实例互相污染。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "local-config.json"

_OVERRIDE: Path | None = None


def set_config_path(path: Path | str | None) -> None:
    """设置本进程的实例配置路径；传 None 恢复生产默认。"""
    global _OVERRIDE
    _OVERRIDE = Path(path).expanduser().resolve() if path else None


def get_config_path() -> Path:
    return _OVERRIDE or DEFAULT_CONFIG_PATH


def is_overridden() -> bool:
    return _OVERRIDE is not None


def read_config() -> dict[str, Any]:
    path = get_config_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def data_root() -> Path:
    """配置所属实例的数据根（<config 所在目录>/output/local-web）。"""
    return get_config_path().parent / "output" / "local-web"
