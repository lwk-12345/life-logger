"""跟唱口径（2026-11-04 用户决定：跟唱算现场，不算媒体）。

背景：`_LIFECYCLE_MEDIA_PATTERNS` / `_LIFECYCLE_NARRATIVE_LEAD` 里原来含「跟唱」，
于是「跟唱某段旋律」这种**本人行为**会被判成 media 折叠掉。用户 2026-11-04 明确：
跟唱算现场。本次只从这两处移除「跟唱」，念唱 / 演唱 / 歌词**一个不动**。

本文件钉住 5 条口径（外加 1 条"其它词没被动过"的守门用例）：
  1. 无归属证据 + 标题含「跟唱」→ **不判 media**
  2. 有 self 证据 → live
  3. 有归属但 0 self、且无媒体结构证据 → **unknown**（预期，不为跟唱另开规则）
  4. 有归属但 0 self、且 speech_target 是媒体对象 → 仍 media
  5. 标题含「演唱」→ 仍 media
  6. 标题含「念唱」/「歌词」→ 仍 media（证明其它体裁词没被顺手删掉）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.activity_timeline_runtime import activity_lifecycle  # noqa: E402


def _activity(title: str, *, summary: str = "", speech_target: str = "未知",
              evidence_labels: tuple[str, ...] = (), media_reclassified: bool = False):
    """最小活动形状：items[0].evidence 的 source_label 决定「有没有归属」。"""
    identity = {"media_reclassified_only": True} if media_reclassified else None
    item = {
        "item_id": "it_1",
        "action": "测试动作",
        "status": "unknown",
        "evidence": [
            {"quote": "测试原话", "source_label": label} for label in evidence_labels
        ],
    }
    if identity is not None:
        item["identity_safe"] = identity
    return {
        "activity_id": "act_singalong_test",
        "title": title,
        "summary": summary,
        "speech_target": speech_target,
        "items": [item],
    }


def test_singalong_without_attribution_is_not_media() -> None:
    """1) 无归属证据的「跟唱」标题：不再因为体裁词被折叠成媒体。"""
    activity = _activity("跟唱《丁溪分长清愁》人散了西小愁")
    result = activity_lifecycle(activity)
    assert result != "media", "用户已决定跟唱算现场，不得再判 media"
    # 当前落到 Q823 的「无归属 + 无结构证据 → unknown」，保留记录便于回溯
    assert result == "unknown"


def test_singalong_with_self_evidence_is_live() -> None:
    """2) 有 self 证据 → live（跟唱是本人发的声）。"""
    activity = _activity("跟唱某段旋律", evidence_labels=("self", "self"))
    assert activity_lifecycle(activity) == "live"


def test_singalong_with_zero_self_and_no_media_evidence_is_unknown() -> None:
    """3) 有归属但 0 self、无媒体结构证据 → unknown（预期，不另开规则判 live）。"""
    activity = _activity("跟唱某段旋律", evidence_labels=("non_self", "non_self"))
    assert activity_lifecycle(activity) == "unknown"


def test_singalong_with_zero_self_and_media_target_is_media() -> None:
    """4) 0 self 且 speech_target 是媒体对象 → 仍 media（真视频，该折）。"""
    activity = _activity("跟唱某段旋律", speech_target="媒体内容",
                         evidence_labels=("non_self",))
    assert activity_lifecycle(activity) == "media"


def test_yuchang_still_media() -> None:
    """5) 「演唱」仍在媒体体裁词里，口径未动。"""
    assert activity_lifecycle(_activity("演唱歌曲片段")) == "media"


@pytest.mark.parametrize("title", ["念唱含歌词片段", "片段含歌词", "念唱某段"])
def test_other_media_words_untouched(title: str) -> None:
    """6) 守门：念唱 / 歌词等其它体裁词必须保持原样（本次只动「跟唱」）。"""
    assert activity_lifecycle(_activity(title)) == "media"
