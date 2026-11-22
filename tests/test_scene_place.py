"""场景上的地点：取出现最多的，没有定位不猜。"""
from life_logger.daily_dashboard import _scene_place


def test_majority_place_inside_scene_window():
    points = [(600, "Home"), (601, "Home"), (602, "Outside"), (900, "Outside")]
    assert _scene_place(points, 600, 660) == "Home"


def test_no_location_means_no_place():
    assert _scene_place([(900, "Home")], 600, 660) is None


def test_stale_or_vague_location_gives_no_place(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from life_logger import ambient_anchors as aa
    from life_logger.daily_dashboard import _fresh_segment_place

    monkeypatch.setattr(aa, "_DEFAULT_CLASSIFIER", aa.AmbientAnchorClassifier(
        geo_anchors=[{"latitude": 22.0, "longitude": 113.0, "location": "Home", "radius_m": 500}],
    ))
    monkeypatch.setattr(aa, "_ensure_default_classifier_configured", lambda: None)
    start = datetime(2026, 11, 5, 19, 0, tzinfo=timezone(timedelta(hours=8)))
    geo = {"latitude": 22.0, "longitude": 113.0, "horizontal_accuracy": 30}
    assert _fresh_segment_place({**geo, "timestamp": "2026-11-05T18:50:00+08:00"}, start) == "Home"
    assert _fresh_segment_place({**geo, "timestamp": "2026-11-05T18:00:00+08:00"}, start) is None
    assert _fresh_segment_place({**geo, "timestamp": "2026-11-05T18:50:00+08:00", "horizontal_accuracy": 500}, start) is None
    assert _fresh_segment_place(geo, start) is None
