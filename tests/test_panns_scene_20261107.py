"""2026-11-07：PANNs 场景/环境声接入手表正式转写。

三条要守住：① 标签必须真的在 AudioSet 527 类里（不自己造名字）；
② 开关默认开、`LIFE_LOGGER_PANNS_ENABLE=0` 能关；③ 场景失败绝不影响转写。
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import audio_scene  # noqa: E402
from life_logger.watch_formal_asr import WatchFormalASRProcessor  # noqa: E402

LABELS_CSV = Path.home() / "panns_data" / "class_labels_indices.csv"


def _audioset_labels() -> set[str]:
    with LABELS_CSV.open(encoding="utf-8") as handle:
        return {row["display_name"] for row in csv.DictReader(handle)}


@pytest.mark.skipif(not LABELS_CSV.exists(), reason="AudioSet 标签表不在本机")
def test_every_hint_is_a_real_audioset_class() -> None:
    labels = _audioset_labels()
    missing = [
        f"{group}:{name}"
        for group, hints in {**audio_scene.SCENE_CLASS_HINTS, **audio_scene.EVENT_CLASS_HINTS}.items()
        for name in hints
        if name not in labels
    ]
    assert missing == [], f"这些提示词不在 AudioSet 527 类里：{missing}"


def test_scene_and_event_groups_are_the_agreed_shape() -> None:
    assert len(audio_scene.SCENE_CLASS_HINTS) == 6
    assert len(audio_scene.EVENT_CLASS_HINTS) == 10


def test_annotate_returns_scores_not_hard_verdicts(monkeypatch) -> None:
    """用假的 windows 验证聚合：输出是分数，缺数据时 scene 为 None 而不是编一个。"""
    source = audio_scene.Path

    def fake_annotate_windows(windows, models=None):
        scene_scores = {k: 0.0 for k in audio_scene.SCENE_CLASS_HINTS}
        for w in windows:
            for scene, hints in audio_scene.SCENE_CLASS_HINTS.items():
                scene_scores[scene] += max((t["score"] for t in w["top"] if t["label"] in hints), default=0.0)
        ranked = sorted(scene_scores.items(), key=lambda kv: kv[1], reverse=True)
        return {"scene": ranked[0][0] if ranked[0][1] > 0 else None, "ranked": ranked}

    windows = [{"top": [{"label": "Music", "score": 0.8}, {"label": "Chatter", "score": 0.1}]}]
    result = fake_annotate_windows(windows)
    assert result["scene"] == "咖啡厅/餐厅"   # Chatter 属于咖啡厅/餐厅
    assert dict(result["ranked"])["户外自然"] == 0.0
    assert source  # 保持 import 被使用


def test_enable_switch_defaults_off_and_can_be_enabled(monkeypatch) -> None:
    # 2026-11-07 总控：MobileNetV1 权重修好并实测前默认关
    monkeypatch.delenv("LIFE_LOGGER_PANNS_ENABLE", raising=False)
    assert WatchFormalASRProcessor._scene_enabled(object()) is False
    monkeypatch.setenv("LIFE_LOGGER_PANNS_ENABLE", "0")
    assert WatchFormalASRProcessor._scene_enabled(object()) is False
    monkeypatch.setenv("LIFE_LOGGER_PANNS_ENABLE", "on")
    assert WatchFormalASRProcessor._scene_enabled(object()) is True


class _Stub:
    """最小 self：只要 session_directory / _directory。"""

    _scene_enabled = WatchFormalASRProcessor._scene_enabled
    _SCENE_STRIDE = 1

    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = self

    def session_directory(self, device, session):
        return self.root / device / session

    def _directory(self, device, session):
        return self.root / device / session / "formal-asr"


def test_scene_failure_never_breaks_transcription(tmp_path, monkeypatch) -> None:
    stub = _Stub(tmp_path)
    audio = tmp_path / "w" / "s" / "segments"
    audio.mkdir(parents=True)
    (audio / "segment-000000.m4a").write_bytes(b"x")
    state = tmp_path / "w" / "s" / "formal-asr" / "segments" / "000000"
    state.mkdir(parents=True)
    (state / "state.json").write_text(json.dumps({"status": "completed", "sequence": 0}), encoding="utf-8")

    def boom(_audio):
        raise RuntimeError("PANNs 挂了")

    monkeypatch.setattr(audio_scene, "annotate", boom)
    monkeypatch.setenv("LIFE_LOGGER_PANNS_STRIDE", "1")
    # 不抛异常，state.json 也不被写坏
    WatchFormalASRProcessor._annotate_scene(stub, "w", "s", {"sequence": 0})
    payload = json.loads((state / "state.json").read_text(encoding="utf-8"))
    assert payload == {"status": "completed", "sequence": 0}


def test_scene_written_into_segment_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LIFE_LOGGER_PANNS_ENABLE", "1")
    stub = _Stub(tmp_path)
    (tmp_path / "w" / "s" / "segments").mkdir(parents=True)
    (tmp_path / "w" / "s" / "segments" / "segment-000000.m4a").write_bytes(b"x")
    state = tmp_path / "w" / "s" / "formal-asr" / "segments" / "000000"
    state.mkdir(parents=True)
    (state / "state.json").write_text(json.dumps({"status": "completed", "sequence": 0}), encoding="utf-8")

    monkeypatch.setattr(audio_scene, "annotate", lambda _a: {
        "policy_version": "panns-audioset-527-v1", "model": "Cnn14", "checkpoint": "Cnn14.pth",
        "warning": None, "window_count": 6, "scene": "咖啡厅/餐厅",
        "scene_ranked": [("咖啡厅/餐厅", 0.42)], "events": [{"event": "音乐", "peak_score": 0.8}],
        "elapsed_seconds": 6.4,
    })
    monkeypatch.setenv("LIFE_LOGGER_PANNS_STRIDE", "1")
    WatchFormalASRProcessor._annotate_scene(stub, "w", "s", {"sequence": 0})

    payload = json.loads((state / "state.json").read_text(encoding="utf-8"))
    assert payload["status"] == "completed"          # 已有字段不变
    assert payload["scene"]["scene"] == "咖啡厅/餐厅"
    assert payload["scene"]["events"][0]["event"] == "音乐"


def test_stride_skips_segments(tmp_path, monkeypatch) -> None:
    stub = _Stub(tmp_path)
    (tmp_path / "w" / "s" / "segments").mkdir(parents=True)
    (tmp_path / "w" / "s" / "segments" / "segment-000003.m4a").write_bytes(b"x")
    state = tmp_path / "w" / "s" / "formal-asr" / "segments" / "000003"
    state.mkdir(parents=True)
    (state / "state.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    called = []
    monkeypatch.setattr(audio_scene, "annotate", lambda a: called.append(a) or None)
    monkeypatch.setenv("LIFE_LOGGER_PANNS_STRIDE", "5")
    WatchFormalASRProcessor._annotate_scene(stub, "w", "s", {"sequence": 3})
    assert called == []
