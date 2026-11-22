"""薄荷花开放平台：只做「最后一道在线补查」，不改变本地优先级。

设计约束（用户 2026-11-06 决定，2026-11-07 实施）：

* 凭据只从 ``boohee-credentials.json`` 读，**从不记录/打印**；
* access_token 复用 ``boohee-token.json``，过期才换；
* search 与 detail 共用一个每日硬上限（默认 50），按北京时间自然日计数，
  计数写入 ``food-data/boohee-usage.json``；超额直接返回空，不抛错；
* 任何网络/解析失败都退化为「没查到」，查询链不能因此失败；
* 单测通过 ``transport`` 注入假响应，默认不联网。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

BASE_URL = "https://fc.boohee.com"
TOKEN_PATH = "boohee-token.json"
USAGE_PATH = "boohee-usage.json"
DEFAULT_DAILY_LIMIT = 50
UTC8 = dt.timezone(dt.timedelta(hours=8))

#: 详情里各营养素的中英文名（只认精确名，不做模糊包含，避免「脂肪酸」串到「脂肪」）
_ENERGY = {"能量", "热量", "energy", "calories", "kcal"}
_PROTEIN = {"蛋白质", "protein"}
_FAT = {"脂肪", "fat", "total lipid (fat)", "total fat"}
_CARBS = {"碳水化合物", "carbohydrate", "carbohydrates", "carbohydrate, by difference"}
_SODIUM = {"钠", "sodium", "sodium, na"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _norm_name(value: Any) -> str:
    return str(value or "").strip().lower()


class BooheeClient:
    """限额、令牌与解析都在这里；调用方只看到 search()/detail()。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        credentials: Mapping[str, Any] | None = None,
        credentials_path: Path | str | None = None,
        token_path: Path | str | None = None,
        usage_path: Path | str | None = None,
        transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.credentials = dict(credentials) if credentials is not None else None
        self.credentials_path = Path(credentials_path) if credentials_path else None
        if self.credentials is None and self.credentials_path is None:
            self.credentials_path = self.data_dir.parent / "boohee-credentials.json"
        self.token_path = Path(token_path) if token_path else self.data_dir / TOKEN_PATH
        self.usage_path = Path(usage_path) if usage_path else self.data_dir / USAGE_PATH
        self._transport = transport or self._urllib_transport
        self._now = now or (lambda: dt.datetime.now(UTC8))
        self._token_cache: str | None = None

    # -- 凭据/限额 ---------------------------------------------------------------
    def _config(self) -> dict[str, Any]:
        if self.credentials is not None:
            return self.credentials
        try:
            payload = json.loads(Path(self.credentials_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    @property
    def daily_limit(self) -> int:
        raw = self._config().get("daily_limit", DEFAULT_DAILY_LIMIT)
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_DAILY_LIMIT
        return limit if limit > 0 else DEFAULT_DAILY_LIMIT

    def _today(self) -> str:
        return self._now().astimezone(UTC8).date().isoformat()

    def usage(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if not isinstance(payload, Mapping) or payload.get("date") != self._today():
            return {"date": self._today(), "count": 0}
        return {"date": payload["date"], "count": int(payload.get("count") or 0)}

    def _spend(self) -> bool:
        state = self.usage()
        if state["count"] >= self.daily_limit:
            return False
        state["count"] += 1
        try:
            self.usage_path.parent.mkdir(parents=True, exist_ok=True)
            self.usage_path.write_text(json.dumps(state, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError:
            pass
        return True

    # -- 令牌 -------------------------------------------------------------------
    @staticmethod
    def sign(app_id: str, timestamp: int, app_secret: str) -> str:
        params = {"app_id": app_id, "timestamp": timestamp}
        joined = "".join(f"{key}{params[key]}" for key in sorted(params))
        return hashlib.md5(f"{app_secret}{joined}{app_secret}".encode()).hexdigest()

    def _stored_token(self) -> str | None:
        if self._token_cache:
            return self._token_cache
        try:
            payload = json.loads(self.token_path.read_text(encoding="utf-8"))
            token = str(payload.get("access_token") or "")
            expired = str(payload.get("expired_at") or "")
            moment = dt.datetime.fromisoformat(expired.replace("Z", "+00:00"))
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        if token and moment - self._now() > dt.timedelta(seconds=60):
            self._token_cache = token
            return token
        return None

    def _save_token(self, token: str, expired_at: Any) -> None:
        try:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(
                json.dumps({"access_token": token, "expired_at": str(expired_at)}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.chmod(self.token_path, 0o600)
        except OSError:
            pass

    def token(self) -> str | None:
        stored = self._stored_token()
        if stored:
            return stored
        config = self._config()
        app_id, secret = str(config.get("app_id") or ""), str(config.get("app_secret") or "")
        if not app_id or not secret:
            return None
        timestamp = int(time.time())
        body = {"app_id": app_id, "timestamp": timestamp,
                "sign": self.sign(app_id, timestamp, secret)}
        try:
            payload = self._transport({"url": f"{BASE_URL}/api/v2/access_tokens", "method": "POST",
                                       "headers": {"Content-Type": "application/json"},
                                       "body": json.dumps(body)})
            token = str(payload.get("access_token") or "")
            if token:
                self._save_token(token, payload.get("expired_at") or "")
                self._token_cache = token
                return token
        except Exception:  # noqa: BLE001 - 取不到令牌只能是「没查到」
            return None
        return None

    # -- 请求 -------------------------------------------------------------------
    @staticmethod
    def _urllib_transport(request: dict[str, Any]) -> dict[str, Any]:
        data = request.get("body")
        http_request = urllib.request.Request(
            request["url"], data=data.encode() if isinstance(data, str) else data,
            headers=dict(request.get("headers") or {}), method=request.get("method", "GET"),
        )
        with urllib.request.urlopen(http_request, timeout=20) as response:
            return json.load(response)

    def _get(self, url: str) -> dict[str, Any] | None:
        token = self.token()
        if not token:
            return None
        try:
            payload = self._transport({"url": url, "method": "GET",
                                       "headers": {"AccessToken": token}})
        except Exception:  # noqa: BLE001
            return None
        return payload if isinstance(payload, Mapping) else None

    # -- 查询 -------------------------------------------------------------------
    def search(self, query: str) -> list[dict[str, Any]]:
        text = str(query or "").strip()
        if not text or not self._spend():
            return []
        payload = self._get(f"{BASE_URL}/api/v1/foods/search?q={urllib.parse.quote(text)}")
        raw = (payload or {}).get("items") or (payload or {}).get("foods") or []
        out = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, Mapping) or not item.get("code"):
                continue
            out.append({
                "code": str(item.get("code")),
                "name": str(item.get("name") or ""),
                "calory": _number(item.get("calory")),
                "weight": _number(item.get("weight")),
                "is_liquid": bool(item.get("is_liquid")),
            })
        return out

    def detail(self, code: str) -> dict[str, Any] | None:
        if not code or not self._spend():
            return None
        payload = self._get(f"{BASE_URL}/api/v3/foods/{urllib.parse.quote(str(code))}")
        if not payload:
            return None
        food = payload.get("food") if isinstance(payload.get("food"), Mapping) else {}
        return {"name": str(food.get("name") or ""), "per_100g": detail_per_100g(payload)}


def _collect(items: Any, names: set[str], *, unit_scale: float = 1.0) -> float | None:
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        if _norm_name(item.get("name")) not in names:
            continue
        value = _number(item.get("value"))
        if value is None:
            continue
        return value * unit_scale
    return None


def detail_per_100g(payload: Mapping[str, Any]) -> dict[str, float]:
    """把 v3 详情映射成每 100g 营养；没给的键**不出现**（上层按未知处理）。"""
    energy = _collect(payload.get("calory"), _ENERGY)
    if energy is None:
        energy = _collect(payload.get("base_ingredients"), _ENERGY)
    out: dict[str, float] = {}
    if energy is not None:
        out["energy_kcal"] = round(energy, 2)
    for key, names, sections in (
        ("protein_g", _PROTEIN, ("base_ingredients",)),
        ("fat_g", _FAT, ("base_ingredients",)),
        ("carbohydrates_g", _CARBS, ("base_ingredients",)),
    ):
        for section in sections:
            value = _collect(payload.get(section), names)
            if value is not None:
                out[key] = round(value, 2)
                break
    sodium = _collect(payload.get("mineral"), _SODIUM)
    if sodium is None:
        sodium = _collect(payload.get("base_ingredients"), _SODIUM)
    if sodium is not None:
        out["sodium_mg"] = round(sodium, 2)
    return out
