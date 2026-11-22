"""守 launchd 模板：渲染结果必须与**当前已安装的 plist** 等价（Q861）。

为什么这么写
------------
需求是"换台机器能装"：模板里只能有占位符（`__REPO__` / `__PYTHON__` / `__HOME__`），
不能有真实家目录。但"没有家目录"不等于"装上去还能跑"——所以本测试反过来做：
**用当前仓库路径 + 当前家目录渲染模板，再和 ~/Library/LaunchAgents 里的现有 plist 逐字段比对**，
不一致就失败。这样模板一改坏，测试立刻红。

只读保证
--------
本测试**不写** ~/Library/LaunchAgents、**不调用** launchctl：渲染目标永远是系统临时目录。

比对范围（派单指定）
--------------------
* `ProgramArguments`（逐元素，含 --flag 实参）
* `StartCalendarInterval`（含 list 形式）
* `EnvironmentVariables` 的**键集合**（值另比对，见下）

额外比对（更严，覆盖"键对值错"）
--------------------------------
`EnvironmentVariables` 的值也比对（把 `__HOME__`/`__REPO__` 替换后），
以及 `WorkingDirectory` / `Label` / `StartInterval` / `RunAtLoad` / `KeepAlive` /
`ThrottleInterval` / `ProcessType` / `LimitLoadToSessionType` / 两个日志路径。
理由：这些字段任一处写错，装上去就是"静默不跑"或"日志写错地方"。

`__PYTHON__` 的处理
-------------------
模板里出现 `__PYTHON__` 的项（当前只有 daily-synthesis，用系统 anaconda），
**测试不写死解释器路径**，而是从现有 plist 的 `ProgramArguments[0]` 自动取，
通过 `--python-map label=path` 传给渲染器。这样测试文件里不含任何本机路径。
"""
from __future__ import annotations

import importlib.util
import plistlib
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "deploy" / "launchd"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
RENDERER = ROOT / "scripts" / "render_launchd.py"

#: AirPassport 相关 plist 不在本单范围（派单明确"AirPassport 相关的不管"）
SKIP_LABELS = {"com.life-logger.airpassport-watch"}

#: 本机还没安装、或安装名与模板名不一致的条目（逐条写原因）
#: 空表示"每个模板都必须有对应的已安装 plist"。若将来有不装的 job，在这里逐条写原因。
ALLOW_MISSING: dict[str, str] = {
    # [2026-11-05 晚间对账] 模板已入库，但**故意不安装**：派单明确"不 launchctl load"，
    # 且未确认 21:00 推送的内容口径前不想让它每天自动发消息。装的时候删掉这条即可。
    "com.life-logger.evening-alignment": "模板已就绪但本机未安装（本单不加载 launchd，待人工确认后再 install）",
}


def _load_renderer():
    spec = importlib.util.spec_from_file_location("render_launchd", RENDERER)
    assert spec and spec.loader, f"找不到渲染器：{RENDERER}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _template_files() -> list[Path]:
    files = sorted(TEMPLATES.glob("*.plist.tmpl"))
    assert files, f"{TEMPLATES} 下没有 *.plist.tmpl"
    return files


def _label_of(template: Path) -> str:
    """`com.life-logger.local-web.plist.tmpl` → `com.life-logger.local-web`"""
    name = template.name
    for suffix in (".plist.tmpl", ".tmpl"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return template.stem


def _installed() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not LAUNCH_AGENTS.is_dir():
        return out
    for path in LAUNCH_AGENTS.glob("com.life-logger.*.plist"):
        if path.name.endswith(".bak"):
            continue
        try:
            data = plistlib.loads(path.read_bytes())
        except Exception:  # noqa: BLE001 - 坏 plist 直接跳过，交给下面的"缺失"断言报出来
            continue
        label = str(data.get("Label") or path.stem)
        if label in SKIP_LABELS:
            continue
        out[label] = data
    return out


def _python_map(installed: dict[str, dict]) -> dict[str, str]:
    """`__PYTHON__` 用哪个解释器：从现有 plist 的 ProgramArguments[0] 自动判断。"""
    mapping: dict[str, str] = {}
    for label, data in installed.items():
        prog = data.get("ProgramArguments") or []
        if prog and "python" in str(prog[0]):
            mapping[label] = str(prog[0])
    return mapping


# --------------------------------------------------------------------------- 模板卫生
@pytest.mark.parametrize("template", _template_files(), ids=lambda p: p.name)
def test_template_has_no_home_or_secret(template: Path) -> None:
    """模板里不得出现真实家目录、仓库绝对路径、令牌。"""
    text = template.read_text(encoding="utf-8")
    home = str(Path.home())
    assert home not in text, f"{template.name} 里出现了真实家目录 {home}"
    assert str(ROOT) not in text, f"{template.name} 里出现了仓库绝对路径"
    assert "/Users/" not in text, f"{template.name} 里出现了 /Users/ 绝对路径"
    for needle in ("mobile_access_token", "Bearer ", "api_key", "API_KEY", "token="):
        assert needle not in text, f"{template.name} 疑似含敏感串：{needle}"


def test_every_template_declares_placeholders_it_uses() -> None:
    """模板用到的占位符必须被渲染器支持（防止拼错 `__REPO_` 之类静默留下）。"""
    renderer = _load_renderer()
    known = set(getattr(renderer, "PLACEHOLDERS", ()))
    assert known == {"__REPO__", "__PYTHON__", "__HOME__"}, f"渲染器占位符集合异常：{known}"
    import re

    for template in _template_files():
        text = template.read_text(encoding="utf-8")
        # 先把**合法**占位符整体挖掉，剩下的任何 `__XXX` 都是拼错/未闭合的
        stripped = text
        for token in known:
            stripped = stripped.replace(token, "")
        stray = re.findall(r"__[A-Za-z][A-Za-z0-9_]*", stripped)
        assert not stray, f"{template.name} 里有未闭合/拼错的占位符：{sorted(set(stray))}"


# --------------------------------------------------------------------------- 渲染等价
def test_rendered_matches_installed_plists() -> None:
    """核心断言：渲染结果 == 现有 plist（ProgramArguments / 日历 / 环境变量键值 …）。"""
    renderer = _load_renderer()
    installed = _installed()
    assert installed, "没有读到任何 com.life-logger.*.plist，无法比对"

    py_map = _python_map(installed)
    with tempfile.TemporaryDirectory(prefix="launchd-render-") as tmp:
        out = Path(tmp)
        written = renderer.render_all(
            out_dir=out,
            repo=ROOT,
            home=Path.home(),
            python=None,
            python_map=py_map,
        )
        assert written, "渲染器没有产出任何文件"

        problems: list[str] = []
        compared = 0
        for path in written:
            label = path.stem
            if label in SKIP_LABELS:
                continue
            if label not in installed:
                if label in ALLOW_MISSING:
                    continue
                problems.append(f"{label}: 本机没有对应 plist，且未在 ALLOW_MISSING 说明")
                continue
            rendered = plistlib.loads(path.read_bytes())
            existing = installed[label]
            compared += 1

            # ① 派单指定：ProgramArguments
            r_prog = [str(x) for x in (rendered.get("ProgramArguments") or [])]
            e_prog = [str(x) for x in (existing.get("ProgramArguments") or [])]
            if r_prog != e_prog:
                problems.append(f"{label}.ProgramArguments: 渲染={r_prog} 现有={e_prog}")

            # ② 派单指定：StartCalendarInterval（可能是 dict 或 list）
            if rendered.get("StartCalendarInterval") != existing.get("StartCalendarInterval"):
                problems.append(
                    f"{label}.StartCalendarInterval: 渲染={rendered.get('StartCalendarInterval')} "
                    f"现有={existing.get('StartCalendarInterval')}"
                )

            # ③ 派单指定：环境变量键集合
            r_env = rendered.get("EnvironmentVariables") or {}
            e_env = existing.get("EnvironmentVariables") or {}
            if set(r_env) != set(e_env):
                problems.append(f"{label}.EnvironmentVariables 键: 渲染={sorted(r_env)} 现有={sorted(e_env)}")
            # ④ 更严：环境变量的值也要一致
            for key in sorted(set(r_env) & set(e_env)):
                if str(r_env[key]) != str(e_env[key]):
                    problems.append(f"{label}.EnvironmentVariables[{key}]: 渲染={r_env[key]!r} 现有={e_env[key]!r}")

            # ⑤ 其余影响"能不能跑/日志写哪"的字段
            for key in ("Label", "WorkingDirectory", "StartInterval", "RunAtLoad", "KeepAlive",
                        "ThrottleInterval", "ProcessType", "LimitLoadToSessionType",
                        "StandardOutPath", "StandardErrorPath"):
                if rendered.get(key) != existing.get(key):
                    problems.append(f"{label}.{key}: 渲染={rendered.get(key)!r} 现有={existing.get(key)!r}")

        assert compared >= 8, f"只比对了 {compared} 个 plist，覆盖面异常"
        assert problems == [], "模板与现有 plist 不一致：\n  " + "\n  ".join(problems)


# --------------------------------------------------------------------------- 安全护栏
def test_renderer_refuses_launch_agents_target() -> None:
    """绝不写 ~/Library/LaunchAgents（派单硬要求）。"""
    renderer = _load_renderer()
    with pytest.raises(Exception) as exc:
        renderer.render_all(
            out_dir=LAUNCH_AGENTS,
            repo=ROOT,
            home=Path.home(),
            python=None,
            python_map={},
        )
    assert "LaunchAgents" in str(exc.value) or "拒绝" in str(exc.value), (
        f"拒绝理由不明确：{exc.value}"
    )


def test_renderer_does_not_shell_out_to_launchctl() -> None:
    """渲染器源码里不得出现 launchctl（只生成文件，不加载）。"""
    text = RENDERER.read_text(encoding="utf-8")
    assert "launchctl" not in text, "渲染器里出现了 launchctl —— 本单要求只渲染、不加载"
