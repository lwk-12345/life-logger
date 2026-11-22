from __future__ import annotations

from pathlib import Path

import numpy as np

from life_logger.ambient_anchors import (
    AmbientAnchorClassifier,
    classify_ambient_location,
    extract_ambient_headers,
    get_ambient_metadata,
)
from life_logger.speaker_registry import (
    SCHEMA_VERSION,
    ResidentSpeakerRegistry,
)
from life_logger.watch_stream import WatchSessionStore


def test_classify_ambient_location_empty_and_whitespace() -> None:
    assert classify_ambient_location(None, None) == "Outside"
    assert classify_ambient_location("", "") == "Outside"
    assert classify_ambient_location("   ", "   ") == "Outside"
    assert classify_ambient_location(None, "") == "Outside"


def test_classify_ambient_location_transit_heuristics() -> None:
    assert classify_ambient_location("Subway_Free_WiFi", None) == "Transit"
    assert classify_ambient_location("Airport_Terminal_3", None) == "Transit"
    assert classify_ambient_location("CRH_Passenger_Mesh", None) == "Transit"
    assert classify_ambient_location("Metro_Station_5G", None) == "Transit"
    assert classify_ambient_location("Railway_Onboard", None) == "Transit"


def test_classify_ambient_location_office_heuristics() -> None:
    assert classify_ambient_location("Company_Corp_Secure", None) == "Office"
    assert classify_ambient_location("HQ_Internal_WiFi", None) == "Office"
    assert classify_ambient_location("Google_Workplace_Guest", None) == "Office"
    assert classify_ambient_location("Studio_Lab_Staff", None) == "Office"
    assert classify_ambient_location("Office_Room_402", None) == "Office"


def test_classify_ambient_location_home_heuristics() -> None:
    assert classify_ambient_location("My_Home_Mesh_5G", None) == "Home"
    assert classify_ambient_location("Xiaomi_Router_Living", None) == "Home"
    assert classify_ambient_location("TP-LINK_Family_House", None) == "Home"
    assert classify_ambient_location("Residence_Private", None) == "Home"


def test_classify_ambient_location_outside_heuristics() -> None:
    assert classify_ambient_location("Starbucks_Coffee_Guest", None) == "Outside"
    assert classify_ambient_location("Cafe_Free_WiFi", None) == "Outside"
    assert classify_ambient_location("City_Plaza_Mall", None) == "Outside"
    assert classify_ambient_location("RandomUnknownNetwork", None) == "Outside"


def test_ambient_anchor_classifier_custom_registration() -> None:
    classifier = AmbientAnchorClassifier()
    classifier.register_anchor("aa:bb:cc:dd:ee:ff", "Home")
    classifier.register_anchor("CustomSpecialSSID", "Office")

    # Exact BSSID match with colon and hyphen formats
    assert classifier.classify("RandomSSID", "aa:bb:cc:dd:ee:ff") == "Home"
    assert classifier.classify("RandomSSID", "AA-BB-CC-DD-EE-FF") == "Home"

    # Exact SSID match
    assert classifier.classify("CustomSpecialSSID", None) == "Office"
    assert classifier.classify("customspecialssid", None) == "Office"

    # Remove anchor
    assert classifier.remove_anchor("CustomSpecialSSID") is True
    assert classifier.classify("CustomSpecialSSID", None) == "Outside"


def test_extract_ambient_headers() -> None:
    # Standard headers dict
    headers = {
        "X-LifeLogger-SSID": "Office_WiFi",
        "X-LifeLogger-BSSID": "00:11:22:33:44:55",
    }
    ssid, bssid = extract_ambient_headers(headers)
    assert ssid == "Office_WiFi"
    assert bssid == "00:11:22:33:44:55"

    # Lowercase headers
    headers_lower = {
        "x-lifelogger-ssid": "Home_Net",
        "x-lifelogger-bssid": "66:77:88:99:aa:bb",
    }
    ssid, bssid = extract_ambient_headers(headers_lower)
    assert ssid == "Home_Net"
    assert bssid == "66:77:88:99:aa:bb"

    # None and empty
    assert extract_ambient_headers(None) == (None, None)
    assert extract_ambient_headers({}) == (None, None)


def test_get_ambient_metadata() -> None:
    headers = {
        "X-LifeLogger-SSID": "Airport_WiFi",
        "X-LifeLogger-BSSID": "11:22:33:44:55:66",
    }
    meta = get_ambient_metadata(headers=headers)
    assert meta["ssid"] == "Airport_WiFi"
    assert meta["bssid"] == "11:22:33:44:55:66"
    assert meta["ambient_location"] == "Transit"

    # Explicit override
    meta_override = get_ambient_metadata(headers=headers, ssid="Home_Private")
    assert meta_override["ssid"] == "Home_Private"
    assert meta_override["ambient_location"] == "Home"


def test_resident_speaker_registry_tiered_identification() -> None:
    registry = ResidentSpeakerRegistry(
        primary_threshold=0.60,
        contact_threshold=0.50,
        cluster_threshold=0.40,
    )

    # Unit basis vectors in 3D for exact dot products
    vec_primary = [1.0, 0.0, 0.0]
    vec_contact = [0.0, 1.0, 0.0]
    vec_cluster = [0.0, 0.0, 1.0]

    registry.register_primary_speaker("speaker-primary", vec_primary, name="Self")
    registry.register_contact(
        "speaker-alice", vec_contact, name="Alice", relation="colleague"
    )
    registry.register_cluster("cluster-bob", vec_cluster, name="Bob")

    # 1. Match primary speaker (similarity 1.0 > 0.60)
    res_primary = registry.identify_speaker([0.9, 0.1, 0.0])
    assert res_primary["speaker_id"] == "speaker-primary"
    assert res_primary["label"] == "primary"
    assert res_primary["name"] == "Self"
    assert res_primary["confidence"] >= 0.90

    # 2. Match contact speaker (similarity 1.0 > 0.50)
    res_contact = registry.identify_speaker([0.1, 0.95, 0.0])
    assert res_contact["speaker_id"] == "speaker-alice"
    assert res_contact["label"] == "contact"
    assert res_contact["name"] == "Alice"
    assert res_contact["confidence"] >= 0.90

    # 3. Match cluster (similarity 1.0 > 0.40)
    res_cluster = registry.identify_speaker([0.0, 0.1, 0.95])
    assert res_cluster["speaker_id"] == "cluster-bob"
    assert res_cluster["label"] == "cluster"
    assert res_cluster["name"] == "Bob"

    # 4. Unknown vector with auto_cluster=True creates new cluster
    # Make a vector orthogonal to all or far away in higher dimension
    vec_new = [0.0, 0.0, 0.0, 1.0]
    res_new = registry.identify_speaker(vec_new, auto_cluster=True)
    assert res_new["label"] == "cluster"
    assert res_new["speaker_id"].startswith("cluster-")
    assert res_new["confidence"] == 1.0


def test_resident_speaker_registry_higher_contact_similarity_wins() -> None:
    # Test case where both primary and contact exceed thresholds,
    # but contact has much higher similarity
    registry = ResidentSpeakerRegistry(
        primary_threshold=0.50,
        contact_threshold=0.50,
    )
    # Primary at angle 0 deg
    primary_vec = [1.0, 0.0]
    # Contact at angle 45 deg: [cos(45), sin(45)] ~= [0.7071, 0.7071]
    contact_vec = [0.7071, 0.7071]

    registry.register_primary_speaker("self", primary_vec, name="Self")
    registry.register_contact("charlie", contact_vec, name="Charlie")

    # Target vector very close to contact (e.g. angle 40 deg: [0.766, 0.6428])
    # sim to contact ~= 0.99, sim to primary ~= 0.76
    target = [0.766, 0.6428]
    res = registry.identify_speaker(target)
    assert res["label"] == "contact"
    assert res["speaker_id"] == "charlie"
    assert res["name"] == "Charlie"


def test_resident_speaker_registry_numpy_inputs() -> None:
    registry = ResidentSpeakerRegistry()
    emb = np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float32)
    registry.register_primary_speaker("p1", emb)

    # 1D numpy array
    res1 = registry.identify_speaker(emb)
    assert res1["label"] == "primary"

    # 2D numpy array with shape (1, 4)
    res2 = registry.identify_speaker(emb.reshape(1, 4))
    assert res2["label"] == "primary"


def test_resident_speaker_registry_serialization(tmp_path: Path) -> None:
    registry = ResidentSpeakerRegistry()
    registry.register_primary_speaker("user-1", [1.0, 0.0], name="Device Owner")
    registry.register_contact("contact-1", [0.0, 1.0], name="Mom", relation="family")
    registry.register_cluster("c-1", [0.5, 0.5])

    data = registry.to_dict()
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["primary_speaker"]["name"] == "Device Owner"
    assert len(data["contacts"]) == 1
    assert len(data["clusters"]) == 1

    file_path = tmp_path / "speakers.json"
    registry.save_to_file(file_path)

    loaded = ResidentSpeakerRegistry.load_from_file(file_path)
    assert loaded.primary_speaker is not None
    assert loaded.primary_speaker.name == "Device Owner"
    assert "contact-1" in loaded.contacts
    assert loaded.contacts["contact-1"].relation == "family"
    assert "c-1" in loaded.clusters


def test_watch_stream_network_headers_capture(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path / "sessions")

    # Start session with network headers
    start_res = store.start_session(
        device_id="watch-test",
        session_id="session-net-1",
        started_at="2026-09-27T10:00:00+08:00",
        headers={
            "X-LifeLogger-SSID": "Home_WiFi_Mesh",
            "X-LifeLogger-BSSID": "11:22:33:44:55:66",
        },
    )
    manifest = start_res["session"]
    assert "network_snapshot" in manifest
    assert manifest["network_snapshot"]["ssid"] == "Home_WiFi_Mesh"
    assert manifest["network_snapshot"]["ambient_location"] == "Home"

    # Receive segment with office network headers
    import hashlib

    body = b"dummy-audio-bytes"
    checksum = "sha256:" + hashlib.sha256(body).hexdigest()

    store.receive_segment(
        device_id="watch-test",
        session_id="session-net-1",
        sequence=0,
        start_offset_ms=0,
        duration_ms=60000,
        checksum=checksum,
        body=body,
        headers={
            "X-LifeLogger-SSID": "Company_HQ_Internal",
            "X-LifeLogger-BSSID": "aa:bb:cc:dd:ee:ff",
        },
    )

    loaded = store.load_session("watch-test", "session-net-1")
    seg = loaded["segments"][0]
    assert "network_snapshot" in seg
    assert seg["network_snapshot"]["ssid"] == "Company_HQ_Internal"
    assert seg["network_snapshot"]["bssid"] == "aa:bb:cc:dd:ee:ff"
    assert seg["network_snapshot"]["ambient_location"] == "Office"

    # Verify list_coverage_intervals carries ambient_location
    intervals = store.list_coverage_intervals()
    assert len(intervals) == 1
    assert intervals[0]["ambient_location"] == "Office"
    assert intervals[0]["network_snapshot"]["ssid"] == "Company_HQ_Internal"


# ── 【2026-10-15 新增】地理围栏（坐标圈）与 IP 网段两个替代数据源 ──
# 背景：SSID 这条路走不通（手表需 Apple 审核的 entitlement、Mac 被 macOS 14+ redact），
# 而手表坐标与客户端 IP 都拿得到，所以用它们替代。


def test_geo_anchor_hits_inside_radius():
    c = AmbientAnchorClassifier(geo_anchors=[
        {"latitude": 22.6904, "longitude": 113.8010, "location": "Home", "radius_m": 500},
    ])
    assert c.classify(geo={"latitude": 22.6904, "longitude": 113.8010}) == "Home"
    # 约 290 米处仍应命中
    assert c.classify(geo={"latitude": 22.6930, "longitude": 113.8010}) == "Home"


def test_geo_anchor_misses_outside_radius():
    c = AmbientAnchorClassifier(geo_anchors=[
        {"latitude": 22.6904, "longitude": 113.8010, "location": "Home", "radius_m": 500},
    ])
    assert c.classify(geo={"latitude": 22.7080, "longitude": 113.8010}) == "Outside"


def test_geo_anchor_office_and_priority_after_exact_ssid():
    c = AmbientAnchorClassifier(
        anchors={"KnownOffice": "Office"},
        geo_anchors=[{"latitude": 22.6904, "longitude": 113.8010, "location": "Home"}],
    )
    # SSID 精确匹配优先于坐标
    assert c.classify("KnownOffice", None, geo={"latitude": 22.6904, "longitude": 113.8010}) == "Office"
    # 只有坐标时走围栏
    assert c.classify(None, None, geo={"latitude": 22.6904, "longitude": 113.8010}) == "Home"


def test_geo_anchor_ignores_invalid_input():
    c = AmbientAnchorClassifier(geo_anchors=[
        {"latitude": "abc", "longitude": None, "location": "Home"},          # 非法，应被忽略
        {"latitude": 200.0, "longitude": 113.8, "location": "Home"},         # 越界纬度
        {"latitude": 22.6904, "longitude": 113.8010, "location": "Home", "radius_m": 0},  # 半径非正
    ])
    assert c._geo_anchors == []
    assert c.classify(geo={"latitude": "abc", "longitude": None}) == "Outside"


def test_ip_anchor_maps_hotspot_to_transit():
    c = AmbientAnchorClassifier(ip_anchors={
        "192.168.43.0/24": "hotspot",
        "172.20.10.0/24": "hotspot",
        "192.168.1.0/24": "home",
    })
    assert c.classify(client_ip="192.168.1.100") == "Transit"
    assert c.classify(client_ip="172.20.10.3") == "Transit"
    assert c.classify(client_ip="192.168.1.50") == "Home"
    assert c.classify(client_ip="8.8.8.8") == "Outside"
    assert c.classify(client_ip="not-an-ip") == "Outside"


def test_priority_exact_ssid_then_geo_then_ip():
    c = AmbientAnchorClassifier(
        anchors={"KnownOffice": "Office"},
        geo_anchors=[{"latitude": 22.6904, "longitude": 113.8010, "location": "Home"}],
        ip_anchors={"192.168.43.0/24": "Transit"},
    )
    both = dict(geo={"latitude": 22.6904, "longitude": 113.8010}, client_ip="192.168.43.5")
    assert c.classify("KnownOffice", None, **both) == "Office"   # SSID 精确最优先
    assert c.classify(None, None, **both) == "Home"              # 其次坐标
    assert c.classify(None, None, client_ip="192.168.43.5") == "Transit"  # 再次 IP


def test_backward_compatible_defaults_unchanged():
    """不传新参数时，行为必须与旧版完全一致。"""
    c = AmbientAnchorClassifier(anchors={"MyHome": "Home"})
    assert c.classify("MyHome", None) == "Home"
    assert c.classify(None, None) == "Outside"
    assert c.classify("SomeCafeWifi", None) == "Outside"   # 关键词命中咖啡馆


def test_configure_default_classifier_from_mapping():
    import life_logger.ambient_anchors as aa
    aa.configure_default_classifier({
        "geo": [{"latitude": 1.0, "longitude": 2.0, "location": "Office", "radius_m": 100}],
        "ip": {"10.0.0.0/8": "Transit"},
    })
    assert aa.classify_ambient_location(geo={"latitude": 1.0, "longitude": 2.0}) == "Office"
    assert aa.classify_ambient_location(client_ip="10.1.2.3") == "Transit"


def test_comment_keys_are_skipped():
    """JSON 配置文件里的 _note/_comment 等注释键不能被当成锚点。"""
    c = AmbientAnchorClassifier(
        anchors={"_note": "Home"},
        ip_anchors={"_note": "Transit", "192.168.43.0/24": "Transit"},
    )
    assert c.list_anchors() == {}
    assert len(c._ip_anchors) == 1
    assert c.classify(client_ip="192.168.43.5") == "Transit"


def test_place_from_geo_uses_geofence_and_never_guesses(monkeypatch):
    from life_logger import ambient_anchors as aa

    monkeypatch.setattr(aa, "_DEFAULT_CLASSIFIER", aa.AmbientAnchorClassifier(
        geo_anchors=[{"latitude": 22.0, "longitude": 113.0, "location": "Home", "radius_m": 500}],
    ))
    monkeypatch.setattr(aa, "_ensure_default_classifier_configured", lambda: None)
    assert aa.place_from_geo({"latitude": 22.0001, "longitude": 113.0001}) == "Home"
    assert aa.place_from_geo({"latitude": 30.0, "longitude": 120.0}) == "Outside"
    assert aa.place_from_geo(None) is None
    assert aa.place_from_geo({"latitude": None, "longitude": 113.0}) is None
