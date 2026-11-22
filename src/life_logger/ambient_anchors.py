"""Ambient network and environment anchor classification for Life Logger."""

from __future__ import annotations

import ipaddress
import math
import re
from typing import Any, Mapping

# Ambient location categories per PROJECT.md interface contract:
# 'Home' | 'Office' | 'Transit' | 'Outside'
AMBIENT_LOCATIONS = ("Home", "Office", "Transit", "Outside")

# Keyword patterns for heuristic classification
TRANSIT_KEYWORDS = (
    "transit",
    "metro",
    "subway",
    "train",
    "bus",
    "flight",
    "airport",
    "rail",
    "railway",
    "crh",
    "highspeed",
    "vehicle",
    "carplay",
    "onboard",
    "station",
    "hotspot",
)

OFFICE_KEYWORDS = (
    "office",
    "corp",
    "enterprise",
    "work",
    "company",
    "internal",
    "hq",
    "staff",
    "studio",
    "lab",
    "workplace",
    "cowork",
    "business",
)

HOME_KEYWORDS = (
    "home",
    "family",
    "house",
    "residence",
    "living",
    "mesh",
    "xiaomi",
    "tplink",
    "tp-link",
    "huawei",
    "asus",
    "netgear",
)

OUTSIDE_KEYWORDS = (
    "starbucks",
    "cafe",
    "coffee",
    "mall",
    "plaza",
    "restaurant",
    "hotel",
    "public",
    "free-wifi",
    "guest",
    "city",
    "street",
)

MAC_ADDRESS_REGEX = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$")

# 【2026-10-15 新增】默认地理围栏半径（米）。
# 为什么加这个：手表上报的只有经纬度，而 SSID 这条路是死的 ——
# 手表端读 SSID 需要 com.apple.developer.networking.wifi-info（须 Apple 审核），
# Mac 端读 SSID 被 macOS 14+ 隐私保护 redact（8765 是 launchd 的 Python 脚本、
# 非 App bundle，拿不到位置权限）。而 CLLocationManager 的坐标是标准能力、已在采，
# 于是「坐标落在哪个围栏」成为 SSID 的可行替代。
DEFAULT_GEO_ANCHOR_RADIUS_M = 500.0


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点间大圆距离（米）。用于判断坐标是否落在围栏半径内。"""
    earth_radius_m = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * earth_radius_m * math.asin(min(1.0, math.sqrt(a)))


def _matches_keyword(text: str, keyword: str) -> bool:
    if keyword == "work":
        # Match 'work', 'workplace', 'workspace', 'atwork', but not 'network'
        return bool(re.search(r"(?<!net)work", text))
    return keyword in text


class AmbientAnchorClassifier:
    """Classifies ambient environment from Wi-Fi SSID, BSSID, and network anchors."""

    def __init__(
        self,
        anchors: dict[str, str] | None = None,
        default_location: str = "Outside",
        geo_anchors: list[dict[str, Any]] | None = None,
        ip_anchors: dict[str, str] | None = None,
    ) -> None:
        # anchors maps exact BSSID or exact SSID (case-insensitive key) to location
        self._anchors: dict[str, str] = {}
        # 【2026-10-15】地理围栏：(lat, lon, radius_m, location)，按注册顺序匹配
        self._geo_anchors: list[tuple[float, float, float, str]] = []
        # 【2026-10-15】IP 网段：(cidr, location)，粗粒度场景（热点/家用网）
        self._ip_anchors: list[tuple[str, str]] = []
        self.default_location = (
            default_location if default_location in AMBIENT_LOCATIONS else "Outside"
        )
        if anchors:
            for identifier, location in anchors.items():
                self.register_anchor(identifier, location)
        if geo_anchors:
            for entry in geo_anchors:
                self.register_geo_anchor(
                    latitude=entry.get("latitude"),
                    longitude=entry.get("longitude"),
                    location=entry.get("location", "Outside"),
                    radius_m=entry.get("radius_m", DEFAULT_GEO_ANCHOR_RADIUS_M),
                    name=entry.get("name"),
                )
        if ip_anchors:
            for cidr, location in ip_anchors.items():
                self.register_ip_anchor(cidr, location)

    @staticmethod
    def _normalize_location(location: str) -> str:
        """把任意写法归一到 AMBIENT_LOCATIONS 里的四个值之一。"""
        loc = str(location).strip().capitalize()
        if loc in AMBIENT_LOCATIONS:
            return loc
        loc_lower = str(location).strip().lower()
        if "home" in loc_lower:
            return "Home"
        if "office" in loc_lower or "work" in loc_lower:
            return "Office"
        if "transit" in loc_lower or "hotspot" in loc_lower:
            return "Transit"
        return "Outside"

    def register_anchor(self, identifier: str, location: str) -> None:
        """Register an exact SSID or BSSID anchor mapping."""
        if not identifier or not str(identifier).strip():
            return
        norm_id = str(identifier).strip().lower()
        # 【2026-10-15】同样跳过 JSON 注释键，避免 "ssid" 段里的 _note 变成一条假锚点。
        if norm_id.startswith("_"):
            return
        loc = self._normalize_location(location)
        self._anchors[norm_id] = loc

    def register_geo_anchor(
        self,
        latitude: Any,
        longitude: Any,
        location: str,
        radius_m: Any = DEFAULT_GEO_ANCHOR_RADIUS_M,
        name: str | None = None,
    ) -> None:
        """【2026-10-15 新增】注册一个地理围栏（经纬度 + 半径 → 地点）。

        用于替代 SSID：手表能采到坐标，但采不到 WiFi 名称。坐标为 None 时静默忽略，
        好让配置里留空占位而不炸。
        """
        try:
            lat = float(latitude)
            lon = float(longitude)
            radius = float(radius_m)
        except (TypeError, ValueError):
            return
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or radius <= 0:
            return
        loc = self._normalize_location(location)
        self._geo_anchors.append((lat, lon, radius, loc))

    def register_ip_anchor(self, cidr: str, location: str) -> None:
        """【2026-10-15 新增】注册一个 IP 网段 → 地点的粗粒度锚点。

        典型用法：`192.168.43.0/24 → Transit`（安卓热点）、`172.20.10.0/24 → Transit`
        （iPhone 热点）。只作辅助，精度远低于坐标与 SSID。
        """
        if not cidr or not str(cidr).strip():
            return
        key = str(cidr).strip()
        # 【2026-10-15】跳过 JSON 里的注释键（_note / _comment 等）。
        # 配置文件是人类可读的，注释键很自然会被写进去；不跳过的话它会被当成
        # 一条无效的 CIDR 锚点（实测把 "_note" 注册成了 Outside，污染匹配表）。
        if key.startswith("_"):
            return
        self._ip_anchors.append((key, self._normalize_location(location)))

    def remove_anchor(self, identifier: str) -> bool:
        """Remove a registered anchor."""
        norm_id = identifier.strip().lower()
        if norm_id in self._anchors:
            del self._anchors[norm_id]
            return True
        return False

    def list_anchors(self) -> dict[str, str]:
        """Return a copy of registered anchors."""
        return dict(self._anchors)

    def classify(
        self,
        ssid: str | None = None,
        bssid: str | None = None,
        geo: Mapping[str, Any] | None = None,
        client_ip: str | None = None,
    ) -> str:
        """Classify ambient location from SSID/BSSID, geo coordinates, and/or client IP.

        【2026-10-15 扩展】新增 `geo` 与 `client_ip` 两个**可选**数据源，
        调用方不传时行为与旧版完全一致。

        判定优先级：BSSID 精确 → SSID 精确 → 地理围栏 → IP 网段 → SSID 关键词 → 默认。
        为什么坐标排在 IP 之前：坐标是「设备实际在哪」，IP 段只能说明「走的是哪条网」，
        前者能区分「在家」与「在公司」，后者不行。
        """
        clean_bssid = (
            str(bssid).strip().lower() if bssid and str(bssid).strip() else None
        )
        clean_ssid = str(ssid).strip() if ssid and str(ssid).strip() else None

        # 1. Check exact BSSID registration (supporting colon and hyphen formats)
        if clean_bssid:
            for bssid_variant in (
                clean_bssid,
                clean_bssid.replace("-", ":"),
                clean_bssid.replace(":", "-"),
            ):
                if bssid_variant in self._anchors:
                    return self._anchors[bssid_variant]

        # 2. Check exact SSID registration
        if clean_ssid and clean_ssid.lower() in self._anchors:
            return self._anchors[clean_ssid.lower()]

        # 3. 【2026-10-15 新增】地理围栏：坐标落在已注册围栏内即命中。
        #    SSID 拿不到时这是最可靠的场景信号（手表能采坐标）。
        if isinstance(geo, Mapping):
            try:
                lat = float(geo.get("latitude"))
                lon = float(geo.get("longitude"))
            except (TypeError, ValueError):
                lat = lon = None  # type: ignore[assignment]
            if lat is not None and lon is not None:
                for anchor_lat, anchor_lon, radius_m, loc in self._geo_anchors:
                    if _haversine_meters(lat, lon, anchor_lat, anchor_lon) <= radius_m:
                        return loc

        # 4. 【2026-10-15 新增】客户端 IP 网段（粗粒度，仅作辅助）。
        if client_ip and self._ip_anchors:
            try:
                addr = ipaddress.ip_address(str(client_ip).strip())
            except ValueError:
                addr = None  # type: ignore[assignment]
            if addr is not None:
                for cidr, loc in self._ip_anchors:
                    try:
                        if addr in ipaddress.ip_network(cidr, strict=False):
                            return loc
                    except ValueError:
                        continue

        # 5. If neither SSID nor BSSID is present (or both are empty / disconnected)
        if not clean_ssid and not clean_bssid:
            return "Outside"

        # 4. Keyword heuristics on SSID
        if clean_ssid:
            ssid_lower = clean_ssid.lower()

            # Check transit first (subway/airport/rail have distinctive networks)
            if any(
                _matches_keyword(ssid_lower, keyword) for keyword in TRANSIT_KEYWORDS
            ):
                return "Transit"

            # Check office next
            if any(
                _matches_keyword(ssid_lower, keyword) for keyword in OFFICE_KEYWORDS
            ):
                return "Office"

            # Check home keywords
            if any(_matches_keyword(ssid_lower, keyword) for keyword in HOME_KEYWORDS):
                return "Home"

            # Check outside / public hotspot keywords
            if any(
                _matches_keyword(ssid_lower, keyword) for keyword in OUTSIDE_KEYWORDS
            ):
                return "Outside"

        # Default fallback
        return self.default_location


# Default global classifier instance。
# 【2026-10-15 修】原来是裸的 `AmbientAnchorClassifier()` —— 空实例、永不命中任何 anchor，
# 于是 `classify_ambient_location()` 实际只能靠 SSID 关键词猜，而 SSID 又拿不到，
# 结果整条场景链路恒返回 Outside。现在改为可从 local-config.json 的
# `ambient_anchors` 段加载；配置不存在时退回空实例（与旧行为一致，不炸）。
_DEFAULT_CLASSIFIER = AmbientAnchorClassifier()
_DEFAULT_CLASSIFIER_CONFIGURED = False


def configure_default_classifier(config: Mapping[str, Any] | None) -> None:
    """用配置重建全局默认分类器。传入 None 或非法内容时保持原样。

    可接受的形状（键都可缺省）::

        {
          "geo": [{"name": "家", "latitude": 22.69, "longitude": 113.80,
                   "location": "Home", "radius_m": 500}],
          "ip":  {"192.168.43.0/24": "Transit"}
        }
    """
    global _DEFAULT_CLASSIFIER, _DEFAULT_CLASSIFIER_CONFIGURED
    if not isinstance(config, Mapping):
        return
    geo = config.get("geo")
    ip = config.get("ip")
    anchors = config.get("ssid")
    rebuilt = AmbientAnchorClassifier(
        anchors=anchors if isinstance(anchors, Mapping) else None,
        geo_anchors=geo if isinstance(geo, list) else None,
        ip_anchors=ip if isinstance(ip, Mapping) else None,
    )
    _DEFAULT_CLASSIFIER = rebuilt
    _DEFAULT_CLASSIFIER_CONFIGURED = True


def _ensure_default_classifier_configured() -> None:
    """懒加载：首次用到默认分类器时，尝试从 local-config.json 读一次配置。

    只在成功读到 `ambient_anchors` 段时才标记已配置，避免「读失败后永久不再尝试」。
    """
    global _DEFAULT_CLASSIFIER_CONFIGURED
    if _DEFAULT_CLASSIFIER_CONFIGURED:
        return
    try:
        import json
        from pathlib import Path as _Path

        candidate = _Path.cwd() / "local-config.json"
        if candidate.is_file():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            section = data.get("ambient_anchors") if isinstance(data, Mapping) else None
            if isinstance(section, Mapping) and section:
                configure_default_classifier(section)
    except Exception:
        # 配置读取失败不能影响主流程；退回空实例即可（旧行为）。
        pass


def classify_ambient_location(
    ssid: str | None = None,
    bssid: str | None = None,
    geo: Mapping[str, Any] | None = None,
    client_ip: str | None = None,
) -> str:
    """Returns 'Home' | 'Office' | 'Transit' | 'Outside'.

    【2026-10-15 扩展】`geo`（坐标）与 `client_ip` 为可选；不传时与旧签名等价。
    """
    _ensure_default_classifier_configured()
    return _DEFAULT_CLASSIFIER.classify(ssid, bssid, geo=geo, client_ip=client_ip)


def place_from_geo(geo: Any) -> str | None:
    """按坐标判断地点：Home/Office 等围栏命中返回地点，围栏外返回 Outside，没有坐标返回 None。"""
    if not isinstance(geo, Mapping) or geo.get("latitude") is None or geo.get("longitude") is None:
        return None
    accuracy = geo.get("horizontal_accuracy")
    if accuracy is not None and float(accuracy) > 100:
        return None
    return classify_ambient_location(geo=geo)


def extract_ambient_headers(headers: Any) -> tuple[str | None, str | None]:
    """Extract Wi-Fi SSID and BSSID from HTTP ingest headers.

    Supports dict, Mapping, or any object with a case-insensitive get() method.
    Reads 'X-LifeLogger-SSID' and 'X-LifeLogger-BSSID'.
    """
    if headers is None:
        return None, None

    ssid: str | None = None
    bssid: str | None = None

    if isinstance(headers, Mapping) or hasattr(headers, "get"):
        # Try direct and case-insensitive lookup
        for key in ("X-LifeLogger-SSID", "x-lifelogger-ssid", "X-Lifelogger-Ssid"):
            val = headers.get(key)
            if val is not None:
                ssid = str(val).strip() or None
                break

        for key in ("X-LifeLogger-BSSID", "x-lifelogger-bssid", "X-Lifelogger-Bssid"):
            val = headers.get(key)
            if val is not None:
                bssid = str(val).strip() or None
                break

    return ssid, bssid


def get_ambient_metadata(
    headers: Any = None,
    ssid: str | None = None,
    bssid: str | None = None,
    classifier: AmbientAnchorClassifier | None = None,
) -> dict[str, Any]:
    """Resolve ambient location metadata from explicit parameters or ingest headers."""
    header_ssid, header_bssid = extract_ambient_headers(headers)
    effective_ssid = ssid if ssid is not None else header_ssid
    effective_bssid = bssid if bssid is not None else header_bssid

    if classifier is None:
        _ensure_default_classifier_configured()
    active_classifier = classifier or _DEFAULT_CLASSIFIER
    location = active_classifier.classify(effective_ssid, effective_bssid)

    return {
        "ssid": effective_ssid,
        "bssid": effective_bssid,
        "ambient_location": location,
    }
