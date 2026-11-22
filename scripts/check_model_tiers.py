#!/usr/bin/env python3
"""
scripts/check_model_tiers.py
模型降级链健康检查工具（三层健康探测）

降级链架构：
- Primary (Tier 1): deepseek.base_url + deepseek.model (经由 magpie 代理，默认 deepseek-v4.1-flash)
- Tier 2: deepseek.base_url + deepseek.tier2_model (经由 magpie 代理，默认 gemini-3.8-flash)
- Tier 3 (Fallback): deepseek.fallback.base_url + deepseek.fallback.model (官方 DeepSeek 平台)

运行模式：
- 默认 --dry-run：只打印配置与三层模型连通性参数，不发起真实网络调用
- 显式 --probe：逐层发送最小心跳探测（max_tokens=8，仅打 "hi"），记录连通状态、延迟与失败原因
- 退出码：0 表示所有启用的层级均健康；非 0 表示存在不可用或死链层级
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "local-config.json"

def load_tier_configs(config_path: Path) -> List[Dict[str, Any]]:
    """从 local-config.json 解析出三层模型调用配置"""
    if not config_path.is_file():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"配置文件 JSON 解析失败: {e}")

    ds = cfg.get("deepseek") or {}
    if not isinstance(ds, dict):
        raise ValueError("local-config.json 中缺少有效的 'deepseek' 对象")

    base_url = str(ds.get("base_url") or "").rstrip("/")
    api_key = str(ds.get("api_key") or "").strip()
    primary_model = str(ds.get("model") or "").strip()
    tier2_model = str(ds.get("tier2_model") or "").strip()
    fallback_cfg = ds.get("fallback") if isinstance(ds.get("fallback"), dict) else {}

    tiers = []

    # 1. Primary (Tier 1)
    t1_valid = bool(base_url and primary_model)
    tiers.append({
        "tier": "tier1",
        "name": "Primary",
        "enabled": bool(ds.get("enabled", True)),
        "base_url": base_url,
        "api_key": api_key,
        "model": primary_model,
        "valid_config": t1_valid,
        "missing_fields": [k for k, v in [("base_url", base_url), ("model", primary_model)] if not v]
    })

    # 2. Tier 2
    t2_valid = bool(base_url and tier2_model)
    tiers.append({
        "tier": "tier2",
        "name": "Tier 2",
        "enabled": bool(ds.get("enabled", True) and tier2_model),
        "base_url": base_url,
        "api_key": api_key,
        "model": tier2_model,
        "valid_config": t2_valid,
        "missing_fields": [k for k, v in [("base_url", base_url), ("tier2_model", tier2_model)] if not v]
    })

    # 3. Fallback (Tier 3)
    fb_enabled = bool(fallback_cfg.get("enabled", False))
    fb_url = str(fallback_cfg.get("base_url") or "").rstrip("/")
    fb_key = str(fallback_cfg.get("api_key") or "").strip()
    fb_model = str(fallback_cfg.get("model") or "").strip()
    t3_valid = bool(fb_url and fb_model and fb_key)
    tiers.append({
        "tier": "tier3",
        "name": "Fallback (Tier 3)",
        "enabled": fb_enabled,
        "base_url": fb_url,
        "api_key": fb_key,
        "model": fb_model,
        "valid_config": t3_valid,
        "missing_fields": [k for k, v in [("base_url", fb_url), ("model", fb_model), ("api_key", fb_key)] if not v]
    })

    return tiers

def probe_model_tier(
    tier_info: Dict[str, Any],
    timeout: float = 8.0,
    opener=None
) -> Tuple[bool, Optional[str], float]:
    """对单层模型执行 8 token 连通性测试"""
    if not tier_info.get("enabled"):
        return False, "层级未启用 (disabled)", 0.0

    if not tier_info.get("valid_config"):
        missing = ", ".join(tier_info.get("missing_fields") or [])
        return False, f"配置缺失必填字段: {missing}", 0.0

    endpoint = f"{tier_info['base_url']}/chat/completions"
    payload = {
        "model": tier_info["model"],
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8
    }
    headers = {
        "Authorization": f"Bearer {tier_info['api_key']}",
        "Content-Type": "application/json",
        "User-Agent": "curl/8.7.1"
    }

    t0 = time.monotonic()
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")

    open_func = opener.open if opener else urllib.request.urlopen

    try:
        with open_func(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            elapsed = round((time.monotonic() - t0) * 1000, 2)
            if "choices" in data and len(data["choices"]) > 0:
                return True, "可用", elapsed
            else:
                return False, f"响应缺少 choices 字段: {str(data)[:100]}", elapsed
    except urllib.error.HTTPError as e:
        elapsed = round((time.monotonic() - t0) * 1000, 2)
        body = ""
        try:
            body = e.read().decode("utf-8")[:120]
        except Exception:
            pass
        return False, f"HTTP {e.code} {e.reason}: {body}", elapsed
    except urllib.error.URLError as e:
        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return False, f"连接失败: {e.reason}", elapsed
    except Exception as e:
        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return False, f"异常: {type(e).__name__}: {e}", elapsed

def run_checks(config_path: Path, probe: bool, timeout: float = 8.0, opener=None) -> Dict[str, Any]:
    tiers = load_tier_configs(config_path)
    report = {
        "config_path": str(config_path),
        "mode": "probe" if probe else "dry-run",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tiers": []
    }

    has_dead_tier = False

    for t in tiers:
        masked_key = (t["api_key"][:3] + "..." + t["api_key"][-3:]) if len(t["api_key"]) > 6 else ("***" if t["api_key"] else "")
        info = {
            "tier": t["tier"],
            "name": t["name"],
            "enabled": t["enabled"],
            "model": t["model"],
            "base_url": t["base_url"],
            "api_key_masked": masked_key,
        }

        if not probe:
            # dry-run 模式：只做静态合法性分析
            if not t["enabled"]:
                info["status"] = "DISABLED"
                info["reason"] = "未启用"
            elif not t["valid_config"]:
                info["status"] = "INVALID_CONFIG"
                info["reason"] = f"缺失字段: {', '.join(t['missing_fields'])}"
                has_dead_tier = True
            else:
                info["status"] = "CONFIG_OK"
                info["reason"] = "配置完备（dry-run 待测）"
            info["latency_ms"] = None
        else:
            # probe 模式：发起真实心跳探测
            ok, reason, latency = probe_model_tier(t, timeout=timeout, opener=opener)
            info["latency_ms"] = latency
            if ok:
                info["status"] = "OK"
                info["reason"] = reason
            else:
                info["status"] = "FAILED"
                info["reason"] = reason
                if t["enabled"]:
                    has_dead_tier = True

        report["tiers"].append(info)

    report["all_healthy"] = not has_dead_tier
    return report

def main():
    parser = argparse.ArgumentParser(description="DeepSeek 模型三层降级链健康检查诊断工具")
    parser.add_argument("--config", "-c", type=Path, default=DEFAULT_CONFIG_PATH, help="local-config.json 路径")
    parser.add_argument("--probe", action="store_true", help="显式执行实际网络探测（每层发送 8 token 最小请求）；默认 dry-run")
    parser.add_argument("--dry-run", action="store_true", help="强制 dry-run 模式（默认）")
    parser.add_argument("--timeout", type=float, default=8.0, help="网络请求超时秒数（默认 8.0s）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结构化数据")

    args = parser.parse_args()
    do_probe = args.probe and not args.dry_run

    try:
        report = run_checks(args.config, probe=do_probe, timeout=args.timeout)
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            print(f"执行失败: {e}", file=sys.stderr)
        sys.exit(3)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"\n=== 模型三层降级链健康检查 ({report['mode'].upper()}) ===")
        print(f"配置文件: {report['config_path']}\n")
        for t in report["tiers"]:
            tag = "✅ [可用]" if t["status"] in ("OK", "CONFIG_OK") else ("⏸️ [未启用]" if t["status"] == "DISABLED" else "❌ [失败/死链]")
            lat_str = f" ({t['latency_ms']}ms)" if t["latency_ms"] is not None else ""
            print(f"{tag} {t['name']}: {t['model']}")
            print(f"    端点: {t['base_url']}")
            print(f"    状态: {t['status']} - {t['reason']}{lat_str}")

        print(f"\n整体健康状态: {'✅ 全部健康' if report['all_healthy'] else '❌ 存在死链或失效层级'}")

    sys.exit(0 if report["all_healthy"] else 1)

if __name__ == "__main__":
    main()
