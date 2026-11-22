"""按处理阶段配置模型与降级链管理模块 (Stage Model Settings).

支持阶段 (stages):
- extract: 10分钟窗口活动抽取 / 上下文抽取
- daily: 宏观认知日报生成 (Daily Cognitive Synthesis)
- classify: 意图 / 类别 / 音频分类

每阶段支持三级有序降级链 (primary -> tier2 -> fallback)。
若未配置特定阶段或字段，平滑回退到全局 local-config.json 的 deepseek 段，保证 100% 向后兼容。
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "local-config.json"
VALID_STAGES = ("extract", "daily", "classify")


def load_raw_config(config_path: Path | str | None = None) -> dict[str, Any]:
    """读取原始 local-config.json 配置字典。"""
    p = Path(config_path or CONFIG_PATH)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_raw_config(config: Mapping[str, Any], config_path: Path | str | None = None) -> None:
    """原子写回 local-config.json。"""
    p = Path(config_path or CONFIG_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(dict(config), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def get_global_deepseek_tiers(cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """提取全局 deepseek 段的 primary -> tier2 -> fallback 降级链。"""
    ds = cfg.get("deepseek") if isinstance(cfg.get("deepseek"), Mapping) else {}
    base_url = str(ds.get("base_url") or "").rstrip("/")
    api_key = str(ds.get("api_key") or "").strip()
    model = str(ds.get("model") or "").strip()
    tier2_model = str(ds.get("tier2_model") or "").strip()
    fallback = ds.get("fallback") if isinstance(ds.get("fallback"), Mapping) else {}

    tiers: list[dict[str, Any]] = []

    if base_url and api_key and model:
        tiers.append({
            "name": "primary",
            "tier": "primary",
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
        })
    if base_url and api_key and tier2_model:
        tiers.append({
            "name": "tier2",
            "tier": "tier2",
            "base_url": base_url,
            "api_key": api_key,
            "model": tier2_model,
        })
    if fallback.get("enabled", True) is not False:
        fb_url = str(fallback.get("base_url") or base_url or "").rstrip("/")
        fb_key = str(fallback.get("api_key") or api_key or "").strip()
        fb_model = str(fallback.get("model") or "").strip()
        if fb_url and fb_key and fb_model:
            tiers.append({
                "name": "fallback",
                "tier": "fallback",
                "base_url": fb_url,
                "api_key": fb_key,
                "model": fb_model,
            })
    return tiers


def resolve_stage_tiers(stage: str, config_path: Path | str | None = None) -> list[dict[str, Any]]:
    """为指定阶段返回有序降级链 (list of dicts)。

    各条目结构：
    {"name": str, "tier": str, "base_url": str, "api_key": str, "model": str}

    优先级：stages[stage][name] -> 全局 deepseek 回退。
    """
    if stage not in VALID_STAGES:
        raise ValueError(f"Invalid stage: {stage}. Must be one of {VALID_STAGES}")

    cfg = load_raw_config(config_path)
    global_tiers = get_global_deepseek_tiers(cfg)
    global_map = {t["name"]: t for t in global_tiers}

    stages_conf = cfg.get("stages") if isinstance(cfg.get("stages"), Mapping) else {}
    stage_conf = stages_conf.get(stage) if isinstance(stages_conf.get(stage), Mapping) else {}

    if not stage_conf:
        # 完全未配置该阶段 -> 100% 沿用全局配置
        return global_tiers

    resolved: list[dict[str, Any]] = []
    for tier_name in ("primary", "tier2", "fallback"):
        t_conf = stage_conf.get(tier_name)
        base = global_map.get(tier_name, {})
        if isinstance(t_conf, Mapping):
            if t_conf.get("enabled", True) is False:
                continue
            b_url = str(t_conf.get("base_url") or base.get("base_url") or "").rstrip("/")
            k_val = str(t_conf.get("api_key") or base.get("api_key") or "").strip()
            m_val = str(t_conf.get("model") or base.get("model") or "").strip()
            if b_url and k_val and m_val:
                resolved.append({
                    "name": tier_name,
                    "tier": tier_name,
                    "base_url": b_url,
                    "api_key": k_val,
                    "model": m_val,
                })
        elif tier_name in global_map:
            # 该 tier 未单独覆盖 -> 继承全局该 tier
            resolved.append(dict(global_map[tier_name]))

    return resolved


def update_stage_config(stage: str, updates: Mapping[str, Any], config_path: Path | str | None = None) -> dict[str, Any]:
    """更新并校验某阶段的模型配置并落盘。"""
    if stage not in VALID_STAGES:
        raise ValueError(f"Invalid stage: {stage}. Must be one of {VALID_STAGES}")

    cfg = load_raw_config(config_path)
    stages = cfg.setdefault("stages", {})
    if not isinstance(stages, dict):
        stages = {}
        cfg["stages"] = stages
    stage_dict = stages.setdefault(stage, {})
    if not isinstance(stage_dict, dict):
        stage_dict = {}
        stages[stage] = stage_dict

    for tier in ("primary", "tier2", "fallback"):
        if tier in updates:
            val = updates[tier]
            if val is None or val is False:
                stage_dict.pop(tier, None)
            elif isinstance(val, Mapping):
                stage_dict[tier] = {
                    "model": str(val.get("model") or "").strip(),
                    "base_url": str(val.get("base_url") or "").strip(),
                    "api_key": str(val.get("api_key") or "").strip(),
                    "enabled": bool(val.get("enabled", True)),
                }

    save_raw_config(cfg, config_path)
    return cfg


def check_model_connectivity(base_url: str, api_key: str, model: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """轻量测试指定模型端点的连通性 (只发空列表/极小测试探测，绝不调用昂贵大推理)。"""
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "curl/8.7.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {
                "ok": True,
                "status": resp.status,
                "model_reported": data.get("model") or model,
                "message": "连通性测试通过",
            }
    except urllib.error.HTTPError as err:
        return {
            "ok": False,
            "status": err.code,
            "error": f"HTTP {err.code}: {err.reason}",
            "message": "模型服务报错",
        }
    except Exception as err:
        return {
            "ok": False,
            "status": None,
            "error": f"{type(err).__name__}: {err}",
            "message": "连接失败或超时",
        }
