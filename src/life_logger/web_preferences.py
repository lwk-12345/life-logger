"""从 local_web.py 机械搬出的 web_preferences 部分，行为不变；分组依据见 work/split-prep-localweb-20261104/。"""
from __future__ import annotations

from pathlib import Path
from typing import Any
import ipaddress
import json
import sys
from life_logger.web_common import DEFAULT_PREFERENCES, PROJECT_ROOT, SUPPORTED_ASR_MODELS, SUPPORTED_SPEAKER_MODELS, _atomic_json


def _read_preferences(root: Path) -> dict[str, Any]:
    path = Path(root) / "preferences.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    result = dict(DEFAULT_PREFERENCES)
    if isinstance(payload, dict):
        result.update({key: value for key, value in payload.items()
                       if key not in {"use_jev", "decision_provider", "jev_min_confidence"}})
    if result.get("asr_model") not in SUPPORTED_ASR_MODELS:
        result["asr_model"] = "mlx-whisper-turbo"
    return result


def _normalize_trusted_network_bind(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        address = ipaddress.ip_address(text)
    except ValueError as error:
        raise ValueError("受信私网绑定地址必须是 IPv4 地址") from error
    if address.version != 4:
        raise ValueError("当前受信私网绑定只支持 IPv4")
    allowed_networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("100.64.0.0/10"),
    )
    if not any(address in network for network in allowed_networks):
        raise ValueError("受信私网绑定只允许 RFC1918 或 Tailscale 100.64.0.0/10 地址")
    return str(address)


def _desired_mobile_bind(preferences: dict[str, Any]) -> str:
    if not preferences.get("lan_access_enabled", False):
        return "127.0.0.1"
    trusted = _normalize_trusted_network_bind(
        preferences.get("trusted_network_bind", "")
    )
    return trusted or "0.0.0.0"


def _normalize_preferences(payload: dict[str, Any]) -> dict[str, Any]:
    asr_model = str(payload.get("asr_model") or "mlx-whisper-turbo")
    if asr_model == "fun-asr-nano":
        asr_model = "mlx-whisper-turbo"
    speaker_model = str(payload.get("speaker_model") or "cam++")
    if asr_model not in SUPPORTED_ASR_MODELS:
        raise ValueError("当前主链路暂不支持这个 ASR 模型")
    if speaker_model not in SUPPORTED_SPEAKER_MODELS:
        raise ValueError("当前主链路暂不支持这个声源模型")
    return {
        "onboarding_completed": bool(payload.get("onboarding_completed", False)),
        "asr_model": asr_model,
        "speaker_model": speaker_model,
        "lan_access_enabled": bool(payload.get("lan_access_enabled", False)),
        "trusted_network_bind": _normalize_trusted_network_bind(
            payload.get("trusted_network_bind", "")
        ),
        "mobile_capture_enabled": bool(
            payload.get("mobile_capture_enabled", True)
        ),
        "mobile_capture_auto_start": bool(
            payload.get("mobile_capture_auto_start", False)
        ),
    }


def _write_preferences(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_preferences(payload)
    _atomic_json(Path(root) / "preferences.json", normalized)
    if Path(root).resolve() == (PROJECT_ROOT / "output" / "local-web").resolve():
        config_path = PROJECT_ROOT / "local-config.json"
        if config_path.is_file():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                asr_model = normalized.get("asr_model")
                if asr_model == "moss":
                    cfg["asr_provider"] = "moss"
                else:
                    cfg["asr_provider"] = "local_turbo"
                _atomic_json(config_path, cfg)
            except Exception as error:
                # 偏好写盘失败不能静默吞掉：否则前端会以为保存成功，实际未落盘。
                sys.stderr.write(
                    f"[life-logger] 偏好写盘失败 {config_path}: {type(error).__name__} {error}\n"
                )
                sys.stderr.flush()
    return normalized
