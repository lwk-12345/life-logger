"""手表睡眠请求头：合法才收，坏数据不影响上传。"""
import json

import pytest

from life_logger.local_web import _parse_sleep_header


def test_valid_sleep_header():
    raw = json.dumps({"date": "2026-11-05", "total_minutes": 400, "deep_minutes": 60, "start_time": "a", "end_time": "b"})
    out = _parse_sleep_header(raw)
    assert out["date"] == "2026-11-05" and out["total_minutes"] == 400 and out["deep_minutes"] == 60


@pytest.mark.parametrize("raw", [None, "", "not json", "{}", '{"date":"x","total_minutes":10}',
                                 '{"date":"2026-11-05","total_minutes":0}', '{"date":"2026-11-05","total_minutes":5000}'])
def test_bad_sleep_header_ignored(raw):
    assert _parse_sleep_header(raw) is None


def test_watch_sleep_only_grows_for_same_day(tmp_path):
    from life_logger.health_store import HealthDataStore

    store = HealthDataStore(tmp_path)
    night = {"date": "2026-11-05", "start_time": "a", "end_time": "b", "total_minutes": 480,
             "deep_minutes": 0, "core_minutes": 480, "rem_minutes": 0, "awake_minutes": 0, "source": "apple-watch"}
    store.record_sleep(night)
    store.record_sleep({**night, "total_minutes": 37})
    assert store.get_daily_summary("2026-11-05").sleep_minutes == 480
    store.record_sleep({**night, "total_minutes": 500})
    assert store.get_daily_summary("2026-11-05").sleep_minutes == 500


def test_vip_command_blocks_audio_path_from_network():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src/life_logger/web_routes_watch.py").read_text(encoding="utf-8")
    block = source[source.index("def _post_watch_vip_command"):]
    assert block.index("audio_path 只允许本机调用") < block.index("transcribe_vip_snippet(audio_path)")


@pytest.mark.parametrize("raw", ['{"date":"2026-99-99","total_minutes":10}', '{"date":"2026-11-05","total_minutes":1e999}'])
def test_sleep_header_rejects_bad_date_and_overflow(raw):
    assert _parse_sleep_header(raw) is None
