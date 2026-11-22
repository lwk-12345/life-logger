"""`scene_fields()`：给产物用的 PANNs 字段 —— 必须**永不抛异常**（fail-open）。

不加载真模型：把 `annotate()` 换成假返回，只验证映射与失败路径。
真正的离线验证在 work/panns-scene-20261107/（跑真模型，不进 CI）。
"""
from __future__ import annotations

import life_logger.audio_scene as audio_scene


FAKE_RESULT = {
    "policy_version": "panns-audioset-527-v1",
    "window_seconds": 2.0, "hop_seconds": 1.0, "window_count": 30,
    "scene": "咖啡厅/餐厅",
    "scene_ranked": [("咖啡厅/餐厅", 0.4312), ("家里/室内", 0.2101), ("办公室", 0.1004)],
    "events": [
        {"event": "笑声", "mean_score": 0.33, "peak_score": 0.81, "windows": 12},
        {"event": "音乐", "mean_score": 0.21, "peak_score": 0.55, "windows": 9},
        {"event": "关门/门", "mean_score": 0.05, "peak_score": 0.2, "windows": 2},
    ],
    "windows": [{"start": 0.0, "end": 2.0, "top": [{"label": "Chatter", "score": 0.9}]}],
    "elapsed_seconds": 12.5,
}


def test_scene_fields_ok_maps_to_compact_artifact_fields(tmp_path, monkeypatch):
    audio = tmp_path / "seg.m4a"
    audio.write_bytes(b"x")
    monkeypatch.setattr(audio_scene, "panns_available", lambda: (True, "ok"))
    monkeypatch.setattr(audio_scene, "annotate", lambda path, timeout=1800: dict(FAKE_RESULT))

    fields = audio_scene.scene_fields(audio)

    assert fields["scene_status"] == "ok"
    assert fields["scene"] == "咖啡厅/餐厅"
    assert fields["scene_confidence"] == 0.4312
    assert [item["scene"] for item in fields["scene_top"]] == ["咖啡厅/餐厅", "家里/室内", "办公室"]
    assert fields["scene_events"][0] == {"event": "笑声", "mean_score": 0.33,
                                         "peak_score": 0.81, "windows": 12}
    assert len(fields["scene_events"]) <= 5
    assert fields["scene_windows"] == 30
    # 默认**不带**逐窗口明细：产物会被撑大几十倍
    assert "scene_windows_detail" not in fields
    assert all(key == "scene" or key.startswith("scene_") for key in fields), \
        "字段必须统一 scene/scene_ 前缀，避免和既有字段（speech/music/noise）冲突"


def test_scene_fields_keeps_windows_only_when_asked(tmp_path, monkeypatch):
    audio = tmp_path / "seg.m4a"
    audio.write_bytes(b"x")
    monkeypatch.setattr(audio_scene, "panns_available", lambda: (True, "ok"))
    monkeypatch.setattr(audio_scene, "annotate", lambda path, timeout=1800: dict(FAKE_RESULT))
    assert audio_scene.scene_fields(audio, include_windows=True)["scene_windows_detail"]


def test_scene_fields_is_fail_open_when_panns_missing(tmp_path, monkeypatch):
    audio = tmp_path / "seg.m4a"
    audio.write_bytes(b"x")
    monkeypatch.setattr(audio_scene, "panns_available", lambda: (False, "权重不存在: /nope.pth"))
    fields = audio_scene.scene_fields(audio)
    assert fields["scene_status"] == "unavailable"
    assert "权重不存在" in fields["scene_error"]
    assert fields["scene_policy"] == audio_scene.SCENE_POLICY_VERSION


def test_scene_fields_is_fail_open_when_annotate_raises(tmp_path, monkeypatch):
    audio = tmp_path / "seg.m4a"
    audio.write_bytes(b"x")
    monkeypatch.setattr(audio_scene, "panns_available", lambda: (True, "ok"))

    def boom(path, timeout=1800):
        raise RuntimeError("PANNs 未产出 (exit=1)")

    monkeypatch.setattr(audio_scene, "annotate", boom)
    fields = audio_scene.scene_fields(audio)          # 不抛
    assert fields["scene_status"] == "error"
    assert "RuntimeError" in fields["scene_error"]
    assert len(fields["scene_error"]) <= 200
