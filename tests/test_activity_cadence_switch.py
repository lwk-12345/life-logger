from life_logger.watch_formal_asr import WatchFormalASRProcessor


def test_ten_minute_mode_does_not_spawn_legacy_per_segment_extraction(monkeypatch):
    import life_logger.watch_formal_asr as formal

    def forbidden_thread(*args, **kwargs):
        raise AssertionError("Legacy per-segment extraction must not be scheduled")

    monkeypatch.setattr(formal.threading, "Thread", forbidden_thread)
    processor = object.__new__(WatchFormalASRProcessor)
    processor._schedule_live_events_sync("watch", "session", {"activity_timeline": {"enabled": True}})
