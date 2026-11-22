"""check_release_tree.py 的隔离夹具测试（不依赖真实仓库状态）。

每个用例都在 tmp_path 里造一棵最小树，用 `--root` + `--files` 直接喂给门禁，
断言退出码与 JSON 里的关键字段——不碰真实暂存区、不跑 git commit。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_release_tree.py"

HARD = [
    "src/life_logger/runtime_queue.py",
    "src/life_logger/day_views.py",
    "src/life_logger/processing_runtime.py",
    "src/life_logger/processing_actions.py",
    "src/life_logger/runtime_launch.py",
    "scripts/processing_worker.py",
]


def _run(root: Path, files: list[str], *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--files", *files, "--json", *extra],
        capture_output=True, text=True, timeout=120,
    )


def _write(root: Path, rel: str, content: str = "x = 1\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _report(proc: subprocess.CompletedProcess) -> dict:
    return json.loads(proc.stdout)


# ── 安全样本 ────────────────────────────────────────────────────────────────
def test_clean_source_files_pass(tmp_path: Path):
    _write(tmp_path, "src/life_logger/expense_policy.py")
    _write(tmp_path, "tests/test_something.py")
    proc = _run(tmp_path, ["src/life_logger/expense_policy.py", "tests/test_something.py"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rep = _report(proc)
    assert rep["verdict"] == "PASS"
    assert rep["checked_files"] == 2


def test_full_hard_dependency_cluster_passes(tmp_path: Path):
    for rel in HARD:
        _write(tmp_path, rel)
    proc = _run(tmp_path, HARD)
    assert proc.returncode == 0, proc.stdout
    rep = _report(proc)
    assert len(rep["hard_dependency_present"]) == len(HARD)


def test_acceptance_markdown_is_allowlisted(tmp_path: Path):
    _write(tmp_path, "agent-board/gate-runner-acceptance.md", "# 验收\n通过\n")
    _write(tmp_path, "work/automation-20261023/DESIGN.md", "# 设计\n")
    proc = _run(tmp_path, ["agent-board/gate-runner-acceptance.md", "work/automation-20261023/DESIGN.md"])
    assert proc.returncode == 0, proc.stdout
    assert _report(proc)["blocking"] == []


# 夹具里的"敏感值"一律在运行时拼出来：
# 一是避免仓库里真的躺着手机号/密钥，二是防止这行文字本身被扫描器当成泄漏。
FAKE_PHONE = "1" + "39" + "0" * 8          # 11 位，号段合法但显然是假号
FAKE_EMAIL = "someone" + "@" + "example" + ".com"
FAKE_SECRET_VALUE = "z" * 20                # 够长、不含 test/dummy 等占位词


def test_test_fixture_token_is_not_flagged(tmp_path: Path):
    """`test-xxxx` 这种占位 token 不该被判成密钥泄漏（真实碰到的误报）。"""
    _write(tmp_path, "tests/test_external_web.py",
           "def test_x():\n    access_token = 'test-" + "a" * 12 + "'\n")
    proc = _run(tmp_path, ["tests/test_external_web.py"])
    assert proc.returncode == 0, proc.stdout
    assert _report(proc)["pii"] == []


# ── 危险样本 ────────────────────────────────────────────────────────────────
def test_secret_file_is_blocked(tmp_path: Path):
    _write(tmp_path, "config/secrets.json", '{"a": 1}\n')
    proc = _run(tmp_path, ["config/secrets.json"])
    assert proc.returncode == 1
    rep = _report(proc)
    assert any(b["kind"] == "forbidden_path" for b in rep["blocking"])


def test_runtime_output_is_blocked(tmp_path: Path):
    _write(tmp_path, "output/local-web/reminders.json", "{}\n")
    proc = _run(tmp_path, ["output/local-web/reminders.json"])
    assert proc.returncode == 1
    assert any(b["kind"] == "forbidden_path" for b in _report(proc)["blocking"])


def test_log_and_jsonl_are_blocked(tmp_path: Path):
    _write(tmp_path, "docs/run.log", "line\n")
    _write(tmp_path, "docs/run.jsonl", "{}\n")
    proc = _run(tmp_path, ["docs/run.log", "docs/run.jsonl"])
    assert proc.returncode == 1
    kinds = {b["path"] for b in _report(proc)["blocking"]}
    assert {"docs/run.log", "docs/run.jsonl"} <= kinds


def test_pem_and_env_are_blocked(tmp_path: Path):
    _write(tmp_path, "certs/server.pem", "x\n")
    _write(tmp_path, ".env", "A=1\n")
    proc = _run(tmp_path, ["certs/server.pem", ".env"])
    assert proc.returncode == 1


def test_real_looking_secret_value_is_blocked(tmp_path: Path):
    # 12+ 位、不含 test/dummy 等占位词，命中「疑似密钥赋值」
    _write(tmp_path, "src/life_logger/net.py",
           '_DEFAULT = {"api_key": "' + FAKE_SECRET_VALUE + '"}\n')
    proc = _run(tmp_path, ["src/life_logger/_probe_net.py"])  # 文件不存在 → 不查
    assert proc.returncode == 0
    proc2 = _run(tmp_path, ["src/life_logger/net.py"])
    assert proc2.returncode == 1, proc2.stdout
    rep = _report(proc2)
    assert rep["pii_blocking_count"] >= 1


def test_phone_like_pii_in_new_file_is_blocked(tmp_path: Path):
    _write(tmp_path, "docs/note.md", "联系我 " + FAKE_PHONE + " 谢谢\n")
    proc = _run(tmp_path, ["docs/note.md"])
    assert proc.returncode == 1
    rep = _report(proc)
    assert any("手机号" in p["categories"] for p in rep["pii"])


def test_partial_hard_dependency_is_blocked(tmp_path: Path):
    """只提交 8 个硬依赖中的 3 个 → 必须 FAIL（clone 后会 ImportError）。"""
    subset = HARD[:3]
    for rel in subset:
        _write(tmp_path, rel)
    proc = _run(tmp_path, subset)
    assert proc.returncode == 1, proc.stdout
    rep = _report(proc)
    hard = [b for b in rep["blocking"] if b["kind"] == "hard_dependency_partial"]
    assert hard, rep
    assert len(hard[0]["missing"]) == len(HARD) - 3


def test_oversize_file_is_blocked(tmp_path: Path):
    big = _write(tmp_path, "src/life_logger/blob.bin", "")
    big.write_bytes(b"\0" * (6 * 1024 * 1024))
    proc = _run(tmp_path, ["src/life_logger/blob.bin"])
    assert proc.returncode == 1
    assert any(b["kind"] == "oversize_file" for b in _report(proc)["blocking"])


def test_pii_report_never_echoes_content(tmp_path: Path):
    _write(tmp_path, "docs/contact.md", "电话 " + FAKE_PHONE + "，邮箱 " + FAKE_EMAIL + "\n")
    proc = _run(tmp_path, ["docs/contact.md"])
    out = proc.stdout
    assert FAKE_PHONE not in out, "PII 原文不得出现在输出里"
    assert FAKE_EMAIL not in out
    rep = _report(proc)
    cats = set().union(*(p["categories"] for p in rep["pii"])) if rep["pii"] else set()
    assert "手机号" in cats and "邮箱" in cats


# ── JSON 契约与边界 ────────────────────────────────────────────────────────
def test_json_schema_and_exit_code_consistency(tmp_path: Path):
    _write(tmp_path, "docs/run.log", "x\n")
    proc = _run(tmp_path, ["docs/run.log"])
    rep = _report(proc)
    assert rep["schema_version"] == "release-tree-guard-v1"
    assert rep["exit_code"] == 1 == proc.returncode
    for key in ("checked_files", "hard_dependency_cluster", "blocking", "warnings",
                "pii", "verdict", "exit_code"):
        assert key in rep


def test_empty_file_set_passes(tmp_path: Path):
    proc = _run(tmp_path, [])
    assert proc.returncode == 0
    rep = _report(proc)
    assert rep["checked_files"] == 0 and rep["verdict"] == "PASS"


def test_no_pii_flag_skips_scan(tmp_path: Path):
    _write(tmp_path, "docs/note.md", "联系 " + FAKE_PHONE + "\n")
    proc = _run(tmp_path, ["docs/note.md"], "--no-pii")
    assert proc.returncode == 0, proc.stdout
    assert _report(proc)["pii"] == []
