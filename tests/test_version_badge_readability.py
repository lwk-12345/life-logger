"""版本角标：极简、隐蔽、只显示一行服务状态。

[2026-11-04 用户指令 —— 契约整体改写]
用户连续两次要求这块「更隐蔽」「别那么复杂」：
  ① 「这一块能不能做的更隐蔽一点」→ 从横跨全宽的大黄条收成小胶囊
  ② 「右下角显的信息是不是太复杂了呀」→ 截图里是大灰椭圆套着
     「✓ 网页服务已加载 / 最近加载 03:14（北京时间）/ ▼详情（版本号·代码指纹·git·PID·ISO时间）」

现在的要求是：**一行小字，无边框无背景，只有状态和时间**；指纹/commit/PID 一律不出现，
连悬停提示里也不放。所以本文件从「必须可读到详情」翻转为「必须什么都别显示」。

仍然保留的语义约束：
  * 用文字而非只靠颜色表达状态（无障碍）；
  * 措辞必须是「服务」而不是让人误以为 Watch 装机状态；
  * 有新代码未加载时要能看出来（stale 分支不能被静默吞掉）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import local_web  # noqa: E402

SCRIPT = local_web._VERSION_BADGE_SCRIPT
#: 去掉注释行：注释里会解释"为什么这样改"，不能当实现来断言。
CODE = "\n".join(line for line in SCRIPT.splitlines() if not line.strip().startswith("//"))


def test_states_are_text_not_color_only() -> None:
    """三种状态都必须有文字标签，不能只靠颜色。"""
    for text in ("服务正常", "有更新待加载", "状态未知"):
        assert text in SCRIPT, f"缺少状态文字：{text}"


def test_wording_says_service_not_watch_install() -> None:
    """措辞是「服务」，不会被误读成手表装机状态。"""
    assert "服务" in SCRIPT
    assert "Watch 装机" not in CODE, "可见区不再解释 Watch，改为极简"


def test_visible_line_has_no_technical_detail() -> None:
    """指纹 / commit / PID 一个都不能出现（含悬停提示）。

    注：`deployed_at` 仍会读，但只用于算出「03:23」这个时分，不显示 ISO 原文。
    """
    for token in ("code_fingerprint", "git_commit", "进程 PID"):
        assert token not in SCRIPT, f"极简后不得出现 {token}"


def test_no_details_expander() -> None:
    """▼详情 折叠块已撤销 —— 用户明确说它太复杂。"""
    assert "createElement(\"details\")" not in CODE
    assert "summary" not in CODE


def test_single_line_without_border_or_background() -> None:
    """一行小字：不要边框、不要背景块、不要外框阴影。"""
    assert "border-radius:999px" not in CODE, "不应再做成胶囊"
    assert "border:1px" not in CODE, "不应有边框"
    assert "box-shadow" not in CODE


def test_stale_state_is_still_visible() -> None:
    """有未加载的新代码时必须能看出来，不能被极简掉。"""
    assert "有更新待加载" in SCRIPT
    assert "deploy_state" in SCRIPT


def test_badge_mounts_into_sidebar_foot_not_page_title() -> None:
    """挂到左下角侧栏，而不是页面标题旁（用户要求与左下角合并）。"""
    assert 'getElementById("sidebar-service-status")' in SCRIPT


def test_script_is_valid_javascript() -> None:
    """服务端拼出来的脚本必须是合法 JS（无 node 时跳过）。"""
    import shutil

    if shutil.which("node") is None:
        pytest.skip("node 不可用")
    from life_logger import version

    script = SCRIPT.replace("__BUILD_INFO__", json.dumps(version.build_info()))
    result = subprocess.run(["node", "--check"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr[:400]


def test_badge_can_be_disabled_by_config(tmp_path) -> None:
    config = tmp_path / "local-config.json"
    config.write_text(json.dumps({"web_version_badge_enabled": False}))
    assert local_web._version_badge_enabled(config) is False

    config.write_text(json.dumps({"web_version_badge_enabled": True}))
    assert local_web._version_badge_enabled(config) is True

    config.write_text(json.dumps({}))
    assert local_web._version_badge_enabled(config) is True
