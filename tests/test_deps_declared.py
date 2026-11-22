"""依赖声明闸门：代码里 import 的三方包必须在 pyproject 里声明。

对应 scripts/check_deps.py。测试本身不装包、不联网、不改任何文件。
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_check_deps():
    spec = importlib.util.spec_from_file_location("check_deps", ROOT / "scripts" / "check_deps.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_deps"] = module
    spec.loader.exec_module(module)
    return module


check_deps = _load_check_deps()


def test_all_third_party_imports_are_declared():
    """真实仓库：src/ 与 scripts/ 里的三方 import 全部能在 pyproject 找到。"""
    core, extras = check_deps.declared_distributions(ROOT / "pyproject.toml")
    imports = check_deps.collect_imports(("src", "scripts"))
    result = check_deps.evaluate(imports, set(core) | set(extras))
    assert not result["undeclared"], [
        (m, d, sorted(g["hard"])[:2] + sorted(g["soft"])[:2]) for m, d, g in result["undeclared"]
    ]


def test_main_exits_zero():
    """命令行入口在真实仓库上退出码为 0（这就是 CI 闸门）。"""
    assert check_deps.main(["--quiet"]) == 0


def test_checker_is_not_vacuous():
    """反向用例：临时目录里放一个没人声明过的 import，必须被判定为未声明。

    这一条防的是「扫描器悄悄坏了、永远返回空」——空跑也会让上面的测试变绿。
    """
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "pkgx").mkdir()
        (base / "pkgx" / "sample.py").write_text(
            "import totally_fake_pkg_xyz\n"
            "def f():\n"
            "    import another_fake_pkg_abc\n"
            "    return 1\n",
            encoding="utf-8",
        )
        imports = check_deps.collect_imports(("pkgx",), base=base)
        result = check_deps.evaluate(imports, declared=set())
        flagged = {m for m, _dist, _g in result["undeclared"]}
        assert flagged == {"totally_fake_pkg_xyz", "another_fake_pkg_abc"}, flagged
        groups = imports["totally_fake_pkg_xyz"]
        assert len(groups["hard"]) == 1, "模块级 import 应算硬依赖"
        assert len(imports["another_fake_pkg_abc"]["soft"]) == 1, "函数内 import 应算软依赖"


def test_core_dependencies_match_hard_imports():
    """core 里必须包含 src/ 的模块级三方 import（当前只有 numpy）。"""
    core, _extras = check_deps.declared_distributions(ROOT / "pyproject.toml")
    imports = check_deps.collect_imports(("src",))
    hard = {check_deps.normalize(check_deps.IMPORT_TO_DIST.get(m, m)) for m, g in imports.items() if g["hard"]}
    missing = hard - set(core)
    assert not missing, f"src 模块级 import 未进 core：{sorted(missing)}"


def test_lock_file_pins_every_runtime_package():
    """requirements.lock.txt 必须给每个声明过的运行时包钉住版本（工具类除外）。"""
    core, extras = check_deps.declared_distributions(ROOT / "pyproject.toml")
    lock = check_deps.lock_versions(ROOT / "requirements.lock.txt")
    declared = (set(core) | set(extras)) - check_deps.TOOLING_ONLY - check_deps.UNPINNED_OK
    missing = sorted(declared - set(lock))
    assert not missing, f"锁文件缺这些包：{missing}"


def test_pyproject_parses_and_mentions_python_floor():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["requires-python"].startswith(">=3.11")
    assert "numpy>=1.26" in data["project"]["dependencies"]
