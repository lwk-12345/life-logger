"""逐句情绪识别默认关闭：正式转写不再为每句话加载/运行 SenseVoice。"""

from __future__ import annotations

import importlib

import life_logger.moss_transcribe as moss


def test_emotion_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("LIFE_LOGGER_MOSS_EMOTION", raising=False)
    assert importlib.reload(moss).EMOTION_ENABLED is False


def test_emotion_can_be_enabled(monkeypatch) -> None:
    monkeypatch.setenv("LIFE_LOGGER_MOSS_EMOTION", "1")
    try:
        assert importlib.reload(moss).EMOTION_ENABLED is True
    finally:
        monkeypatch.delenv("LIFE_LOGGER_MOSS_EMOTION", raising=False)
        importlib.reload(moss)
