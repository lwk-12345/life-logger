"""从 _Handler 机械提取的路由，分支体逐字不变。local_web 的名字在调用时才导入，避免循环依赖。"""
from __future__ import annotations

from http import HTTPStatus
from life_logger.model_settings import VALID_STAGES
from life_logger.model_settings import check_model_connectivity
from life_logger.model_settings import load_raw_config
from life_logger.model_settings import resolve_stage_tiers
from life_logger.web_preferences import _desired_mobile_bind
from life_logger.web_preferences import _normalize_trusted_network_bind
from life_logger.web_preferences import _read_preferences
from life_logger.web_preferences import _write_preferences
import json
import os


def mask_secret(value: str) -> str:
    """只回显“前 4 + 后 2”，中间固定星号；短值整体掩码。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) < 8:
        return "*" * len(text)
    return f"{text[:4]}{'*' * 8}{text[-2:]}"


def mobile_access_public(app) -> dict:
    token = str(getattr(app, "access_token", "") or "")
    return {
        # 兼容旧前端字段名：默认只给掩码，完整值走 /api/settings/mobile-token（需认证）。
        "token": mask_secret(token),
        "token_masked": mask_secret(token),
        "token_length": len(token),
        "token_configured": bool(token),
        "token_reveal_endpoint": "/api/settings/mobile-token",
    }


class _SettingsRoutesMixin:
    def _get_settings(self, parsed):
        from life_logger.local_web import (
            API_FEATURES,
            SERVER_BUILD_ID,
            _mobile_capture_config,
            _model_catalog,
            _read_secret_settings,
        )

        try:
            config = json.loads(
                self.app.config_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            config = {}
        local_secrets = _read_secret_settings(self.app.store.root)
        preferences = _read_preferences(self.app.store.root)
        if not (self.app.store.root / "preferences.json").exists():
            preferences["onboarding_completed"] = bool(self.app.store.list())
        from life_logger.version import build_info as _build_info

        self._send_json(
            {
                "local_first": True,
                "server_build_id": SERVER_BUILD_ID,
                "server_started_at": self.app.started_at,
                # 【2026-10-19】版本标识：一眼看出「现在跑的是哪一版、是否落后于磁盘」
                "version": _build_info(process_started_at=self.app.started_at),
                "api_features": list(API_FEATURES),
                "deepseek": {
                    "enabled": bool(local_secrets.get("deepseek_enabled")),
                    "model": str((config.get("deepseek") or {}).get("model") or "deepseek-flash"),
                    "base_url": str((config.get("deepseek") or {}).get("base_url") or "https://api.deepseek.com"),
                    "key_configured": bool(local_secrets.get("deepseek_api_key")),
                },
                "preferences": preferences,
                "mobile_access": {
                    **mobile_access_public(self.app),
                    "lan_access_enabled": bool(
                        preferences.get("lan_access_enabled", False)
                    ),
                    "trusted_network_bind": str(
                        preferences.get("trusted_network_bind") or ""
                    ),
                    "bind_host": str(self.app.server_address[0]),
                    "port": int(self.app.server_address[1]),
                    "watch_direct_access_enabled": (
                        self.app.watch_local_access_enabled
                    ),
                    "desired_bind_host": _desired_mobile_bind(preferences),
                    "restart_required": str(self.app.server_address[0])
                    != _desired_mobile_bind(preferences),
                },
                "mobile_capture": _mobile_capture_config(preferences),
                "models": _model_catalog(config),
            }
        )
        return

    def _get_settings_mobile_token(self, parsed):
        """完整移动令牌：只给已认证用户，且只在用户主动点击复制/配对时请求。"""
        token = str(getattr(self.app, "access_token", "") or "")
        self._send_json({"token": token, "token_length": len(token), "configured": bool(token)})
        return

    def _get_settings_stage_models(self, parsed):
        raw_cfg = load_raw_config(self.app.config_path)
        stages_conf = raw_cfg.get("stages") if isinstance(raw_cfg.get("stages"), dict) else {}
        global_deepseek = raw_cfg.get("deepseek") if isinstance(raw_cfg.get("deepseek"), dict) else {}

        stages_resp = {}
        for stg in VALID_STAGES:
            stage_conf = stages_conf.get(stg) if isinstance(stages_conf.get(stg), dict) else {}
            resolved_list = resolve_stage_tiers(stg, self.app.config_path)
            resolved_map = {t["tier"]: t for t in resolved_list}

            config_dict = {}
            for t_name in ("primary", "tier2", "fallback"):
                explicit_t = stage_conf.get(t_name) if isinstance(stage_conf.get(t_name), dict) else None
                res_t = resolved_map.get(t_name) or {}
                if explicit_t:
                    config_dict[t_name] = {
                        "model": str(explicit_t.get("model") or ""),
                        "base_url": str(explicit_t.get("base_url") or ""),
                        "enabled": bool(explicit_t.get("enabled", True)),
                    }
                elif res_t:
                    config_dict[t_name] = {
                        "model": str(res_t.get("model") or ""),
                        "base_url": str(res_t.get("base_url") or ""),
                        "enabled": True,
                    }
                else:
                    config_dict[t_name] = {}
            stages_resp[stg] = {"config": config_dict}

        clean_global = dict(global_deepseek)
        clean_global.pop("api_key", None)
        if isinstance(clean_global.get("fallback"), dict):
            fb = dict(clean_global["fallback"])
            fb.pop("api_key", None)
            clean_global["fallback"] = fb

        self._send_json({
            "ok": True,
            "stages": stages_resp,
            "global_deepseek": clean_global,
        })
        return

    def _post_settings_deepseek(self, parsed):
        from life_logger.local_web import (
            _read_secret_settings,
            _write_secret_settings,
        )

        payload = self._body_json()
        raw_key = payload.get("api_key")
        if raw_key is not None and not isinstance(raw_key, str):
            raise ValueError("API Key 必须是文本")
        api_key = str(raw_key or "").strip()
        if len(api_key) > 4096:
            raise ValueError("API Key 长度异常")
        if not isinstance(payload.get("enabled"), bool):
            raise ValueError("enabled 必须是布尔值")
        local_secrets = _read_secret_settings(self.app.store.root)
        if api_key:
            local_secrets["deepseek_api_key"] = api_key
        if payload["enabled"] and not local_secrets.get("deepseek_api_key"):
            raise ValueError("请先填写 DeepSeek API Key")
        local_secrets["deepseek_enabled"] = payload["enabled"]
        _write_secret_settings(self.app.store.root, local_secrets)
        _ds_cfg: dict = {}
        try:
            _ds_cfg = json.loads(self.app.config_path.read_text(encoding="utf-8")).get("deepseek") or {}
        except (OSError, json.JSONDecodeError, AttributeError):
            _ds_cfg = {}
        self._send_json({"saved": True, "deepseek": {
            "enabled": payload["enabled"],
            "model": str(_ds_cfg.get("model") or "deepseek-flash"),
            "base_url": str(_ds_cfg.get("base_url") or "https://api.deepseek.com"),
            "key_configured": bool(local_secrets.get("deepseek_api_key")),
        }})
        return

    def _post_settings_stage_models_check(self, parsed):
        from life_logger.local_web import (
            _read_secret_settings,
        )

        payload = self._body_json()
        stage = str(payload.get("stage") or "").strip()
        tier = str(payload.get("tier") or "").strip()
        model = str(payload.get("model") or "").strip()
        base_url = str(payload.get("base_url") or "").strip()

        if not model or not base_url:
            self._send_json({"ok": False, "error": "model 与 base_url 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        if stage and stage not in VALID_STAGES:
            self._send_json({"ok": False, "error": f"非法 stage: {stage}"}, HTTPStatus.BAD_REQUEST)
            return
        if tier and tier not in ("primary", "tier2", "fallback"):
            self._send_json({"ok": False, "error": f"非法 tier: {tier}"}, HTTPStatus.BAD_REQUEST)
            return

        # API Key 检索优先级
        api_key = ""
        raw_cfg = load_raw_config(self.app.config_path)
        stages_conf = raw_cfg.get("stages") if isinstance(raw_cfg.get("stages"), dict) else {}
        if stage and tier:
            t_conf = stages_conf.get(stage, {}).get(tier, {})
            if isinstance(t_conf, dict) and t_conf.get("api_key"):
                api_key = str(t_conf["api_key"]).strip()
        if not api_key:
            ds = raw_cfg.get("deepseek") if isinstance(raw_cfg.get("deepseek"), dict) else {}
            if tier == "fallback" and isinstance(ds.get("fallback"), dict) and ds["fallback"].get("api_key"):
                api_key = str(ds["fallback"]["api_key"]).strip()
            elif ds.get("api_key"):
                api_key = str(ds["api_key"]).strip()
        if not api_key:
            local_secrets = _read_secret_settings(self.app.store.root)
            if local_secrets.get("deepseek_api_key"):
                api_key = str(local_secrets["deepseek_api_key"]).strip()
        if not api_key:
            api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()

        if not api_key:
            self._send_json({
                "ok": False,
                "error": "未检测到可用的 API Key，请先配置全局或本地 DeepSeek API Key",
                "message": "缺少 API Key",
            })
            return

        res = check_model_connectivity(base_url, api_key, model, timeout=5.0)
        clean_res = {
            "ok": bool(res.get("ok")),
            "model_reported": str(res.get("model_reported") or model),
            "message": str(res.get("message") or ""),
        }
        if not clean_res["ok"]:
            clean_res["error"] = str(res.get("error") or res.get("message") or "探测失败")
        self._send_json(clean_res)
        return

    def _post_mobile_access(self, parsed):
        payload = self._body_json()
        current = _read_preferences(self.app.store.root)
        enabled = bool(payload.get("enabled"))
        trusted_bind = _normalize_trusted_network_bind(
            payload.get("trusted_network_bind", "")
        )
        current["lan_access_enabled"] = enabled
        current["trusted_network_bind"] = trusted_bind
        saved = _write_preferences(self.app.store.root, current)
        current_host = str(self.app.server_address[0])
        desired_host = _desired_mobile_bind(saved)
        self._send_json(
            {
                "mobile_access": {
                    **mobile_access_public(self.app),
                    "lan_access_enabled": saved["lan_access_enabled"],
                    "trusted_network_bind": saved["trusted_network_bind"],
                    "bind_host": current_host,
                    "desired_bind_host": desired_host,
                    "port": int(self.app.server_address[1]),
                    "watch_direct_access_enabled": (
                        self.app.watch_local_access_enabled
                    ),
                    "restart_required": current_host != desired_host,
                }
            }
        )
        return
