# -*- coding: utf-8 -*-
"""scripts/dispatch_engine.py 的分类器单测。

只测**判定逻辑本身**，不碰真看板、不投递、不起服务。每条断言对应一条
DESIGN.md 里的派单规则或今晚踩过的坑，删掉被测实现体应当至少一条变红。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("dispatch_engine", REPO / "scripts" / "dispatch_engine.py")


@pytest.fixture(scope="module")
def de():
    mod = importlib.util.module_from_spec(SPEC)
    sys.modules["dispatch_engine"] = mod
    SPEC.loader.exec_module(mod)
    return mod


def T(**kw):
    base = {"id": "X", "state": "todo", "owner": "1", "write_scope": [], "dependencies": []}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# 路径与 scope 归一
# --------------------------------------------------------------------------- #

def test_write_scope_prefers_new_field_over_legacy_scope(de):
    """新字段优先。真实台账 16 单用的是 legacy `scope`，必须仍能读。"""
    assert de.write_scope({"write_scope": ["a"], "scope": ["b"]}) == ["a"]
    assert de.write_scope({"scope": ["b"]}) == ["b"]
    assert de.write_scope({}) == []


def test_strnum_only_takes_ascii_digits(de):
    """`str.isdigit()` 对带圈数字 ① 也返回 True —— 只认 ASCII 才不会被骗。"""
    assert de.StrNum("1号") == "1"
    assert de.StrNum("③3号") == "3"
    assert de.StrNum("3") == "3"
    assert de.StrNum("①") == ""      # 纯圈号取不出阿拉伯数字，就是失败


def test_norm_paths_strips_dot_slash_and_dedupes(de):
    assert de.norm_paths(["./a/b.py", "a/b.py", "", None, " a/b.py "]) == ["a/b.py"]


def test_effective_scope_unions_declared_and_observed(de):
    """只用声明等于相信撒谎的人自证清白 —— 实际改动必须并进来。"""
    t = {"write_scope": ["a.py"], "observed_write": ["b.py", "a.py"]}
    assert de.effective_scope(t) == ["a.py", "b.py"]


def test_scope_overlap_exact_name(de):
    assert de.scope_overlap(["a/b.py"], ["a/b.py"]) == ["a/b.py"]
    assert de.scope_overlap(["a/b.py"], ["a/c.py"]) == []


def test_scope_overlap_directory_prefix(de):
    """`work/x/` 与 `work/x/f.md` 必须算相交，否则目录级声明形同虚设。"""
    assert de.scope_overlap(["work/x/"], ["work/x/f.md"]) == ["work/x/*"]
    assert de.scope_overlap(["work/x/f.md"], ["work/x/"]) == ["work/x/*"]
    # 前缀相同但不是子路径，不能误判
    assert de.scope_overlap(["work/x/"], ["work/xy/f.md"]) == []


# --------------------------------------------------------------------------- #
# 单写者：两条规则各自独立，不能互相掩盖
# --------------------------------------------------------------------------- #

def _engine(de, tmp_path, live=False):
    board_dir = tmp_path / "board"
    board_dir.mkdir(exist_ok=True)
    (board_dir / "targets.json").write_text(json.dumps({
        "①": {"id": "01a1499e-5603-761b-b85a-8e156b0d6dbe", "label": "1号"},
        "②": {"id": "01a13f92-7aa0-75e5-bc44-d6170f28925a", "label": "2号"},
    }), encoding="utf-8")
    (board_dir / "queue.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
    return de.Engine(
        board=de.Board(board_dir),
        windows=de.WindowTable(board_dir),
        status=de.StatusSource(None, de.WindowTable(board_dir)),
        repo=tmp_path, live=live,
    )


def test_single_writer_rejects_second_task_on_running_scope(de, tmp_path):
    """① running 占着同一文件 → ② 必须 REFUSED 且报出冲突方 id。"""
    eng = _engine(de, tmp_path)
    doc = {"tasks": [
        T(id="A", state="running", owner="9", write_scope=["a.py"]),
        T(id="B", state="todo", owner="1", write_scope=["a.py"]),
    ]}
    d = {x["task_id"]: x for x in eng.plan(doc, {"1": "idle"})}
    # A 自己不是在跑的「别人」，但 A 是 running 所以它占着 a.py；B 撞上
    assert d["B"]["decision"] == "REFUSED"
    assert d["B"]["reason_code"] == "SCOPE_CONFLICT"
    assert d["B"]["conflict_with"] == ["A"]
    assert "a.py" in d["B"]["reason"]


def test_single_writer_uses_observed_write_too(de, tmp_path):
    """声明没重叠、实际重叠 → 仍须拒绝。这是「声明撒谎」的检出路径。"""
    eng = _engine(de, tmp_path)
    doc = {"tasks": [
        T(id="A", state="running", owner="9", write_scope=["a.py"]),
        T(id="B", state="todo", owner="1", write_scope=["b.py"], observed_write=["a.py"]),
    ]}
    d = {x["task_id"]: x for x in eng.plan(doc, {"1": "idle"})}
    assert d["B"]["reason_code"] == "SCOPE_CONFLICT"
    assert d["B"]["conflict_with"] == ["A"]


def test_empty_write_scope_is_refused_not_dispatched(de, tmp_path):
    """范围为空就无法证明单写者 —— 宁可不派。"""
    eng = _engine(de, tmp_path)
    doc = {"tasks": [T(id="A", state="todo", owner="1", write_scope=[])]}
    d = eng.plan(doc, {"1": "idle"})[0]
    assert d["reason_code"] == "NO_WRITE_SCOPE"
    assert d["decision"] == "SKIPPED"


# --------------------------------------------------------------------------- #
# 依赖
# --------------------------------------------------------------------------- #

def test_dependency_unmet_skips(de, tmp_path):
    eng = _engine(de, tmp_path)
    doc = {"tasks": [
        T(id="A", state="todo", owner="1", write_scope=["a.py"]),
        T(id="B", state="todo", owner="2", write_scope=["b.py"], dependencies=["A"]),
    ]}
    d = {x["task_id"]: x for x in eng.plan(doc, {"1": "idle", "2": "idle"})}
    assert d["B"]["reason_code"] == "DEP_UNMET"
    assert "A=todo" in d["B"]["reason"]


def test_dependency_completed_lets_through(de, tmp_path):
    eng = _engine(de, tmp_path)
    doc = {"tasks": [
        T(id="A", state="completed", write_scope=["a.py"]),
        T(id="B", state="todo", owner="2", write_scope=["b.py"], dependencies=["A"]),
    ]}
    d = {x["task_id"]: x for x in eng.plan(doc, {"2": "idle"})}
    assert d["B"]["decision"] == "DISPATCH_DRY_RUN"


def test_dependency_missing_is_not_treated_as_satisfied(de, tmp_path):
    """依赖 id 打错 → 绝不能当成「已完成」（静默放行是最坏的一种）。"""
    eng = _engine(de, tmp_path)
    doc = {"tasks": [T(id="B", state="todo", owner="1", write_scope=["b.py"], dependencies=["NOPE"])]}
    d = eng.plan(doc, {"1": "idle"})[0]
    assert d["reason_code"] == "DEP_MISSING"
    assert "NOPE" in d["reason"]


def test_legacy_done_counts_as_completed(de, tmp_path):
    """旧台账用 done，新 schema 用 completed。两者都得认，否则老单永远等不到。"""
    eng = _engine(de, tmp_path)
    doc = {"tasks": [
        T(id="A", state="done", write_scope=["a.py"]),
        T(id="B", state="todo", owner="2", write_scope=["b.py"], dependencies=["A"]),
    ]}
    d = {x["task_id"]: x for x in eng.plan(doc, {"2": "idle"})}
    assert d["B"]["decision"] == "DISPATCH_DRY_RUN"


# --------------------------------------------------------------------------- #
# blocked_user / 窗口忙闲
# --------------------------------------------------------------------------- #

def test_blocked_user_is_listed_never_dispatched(de, tmp_path):
    eng = _engine(de, tmp_path)
    doc = {"tasks": [T(id="A", state="blocked_user", owner="1", write_scope=["a.py"])]}
    d = eng.plan(doc, {"1": "idle"})[0]
    assert d["reason_code"] == "BLOCKED_USER"
    assert d["decision"] == "LIST_ONLY"


def test_window_not_idle_skips(de, tmp_path):
    eng = _engine(de, tmp_path)
    doc = {"tasks": [T(id="A", state="todo", owner="1", write_scope=["a.py"])]}
    d = eng.plan(doc, {"1": "thinking"})[0]
    assert d["reason_code"] == "WINDOW_NOT_IDLE"
    assert "thinking" in d["reason"]


def test_one_window_gets_at_most_one_task_per_round(de, tmp_path):
    """一窗一单：同轮两单都指向窗 1，第二单必须让开，否则窗口被双重派活。"""
    eng = _engine(de, tmp_path)
    doc = {"tasks": [
        T(id="A", state="todo", owner="1", write_scope=["a.py"]),
        T(id="B", state="todo", owner="1", write_scope=["b.py"]),
    ]}
    d = {x["task_id"]: x for x in eng.plan(doc, {"1": "idle"})}
    assert d["A"]["decision"] == "DISPATCH_DRY_RUN"
    assert d["B"]["reason_code"] == "WINDOW_OCCUPIED"


# --------------------------------------------------------------------------- #
# 状态源：绝不允许「读不到」等于「全空闲」
# --------------------------------------------------------------------------- #

def test_status_empty_stdout_is_failure_not_all_idle(de, tmp_path):
    """bash 调 zsh 脚本的典型症状：stdout 空 + rc=0。必须判失败。"""
    sh = tmp_path / "dispatch.sh"
    sh.write_text("#!/bin/zsh\nexit 0\n", encoding="utf-8")
    sh.chmod(0o755)
    src = de.StatusSource(sh, de.WindowTable(tmp_path))
    statuses, _ = src.fetch()
    assert statuses is None
    assert "空" in src.last_error


def test_status_bash_shebang_is_never_used(de, tmp_path):
    """shebang 含 zsh 且可执行 → 直接执行；否则显式 zsh。永不落到 bash。"""
    sh = tmp_path / "dispatch.sh"
    sh.write_text("#!/bin/zsh\nexit 0\n", encoding="utf-8")
    sh.chmod(0o755)
    src = de.StatusSource(sh, de.WindowTable(tmp_path))
    assert "bash" not in src._argv()
    assert src._argv()[0].endswith("dispatch.sh")


def test_status_unrecognized_sessions_is_failure(de, tmp_path):
    """session id 全对不上 targets.json → 身份表变了，判忙闲必然全错，必须不派。"""
    sh = tmp_path / "dispatch.sh"
    sh.write_text(
        '#!/bin/zsh\nprint -r -- \'{"sessions":[{"id":"ffffffff-0000","status":"idle"}]}\'\n',
        encoding="utf-8",
    )
    sh.chmod(0o755)
    src = de.StatusSource(sh, de.WindowTable(tmp_path))
    statuses, _ = src.fetch()
    assert statuses is None


# --------------------------------------------------------------------------- #
# 双写：事故 vs 噪声必须分开，否则告警淹没
# --------------------------------------------------------------------------- #

def test_two_running_on_same_file_is_concurrent_severity(de):
    doc = {"tasks": [
        {"id": "W1", "state": "running", "write_scope": ["tests/foo.py"]},
        {"id": "W5", "state": "running", "write_scope": ["tests/foo.py"]},
    ]}
    out = de.attribute_observed(doc["tasks"], {"tests/foo.py"})
    assert len(out) == 1
    assert out[0]["severity"] == "concurrent"
    assert out[0]["concurrent_tasks"] == ["W1", "W5"]


def test_two_delivered_on_same_file_is_not_an_incident(de):
    """两个 delivered 声明同一文件只是「先后做过同一处」，不是事故。"""
    doc = {"tasks": [
        {"id": "Q14", "state": "delivered", "write_scope": ["tests/foo.py"]},
        {"id": "Q16", "state": "delivered", "write_scope": ["tests/foo.py"]},
    ]}
    out = de.attribute_observed(doc["tasks"], {"tests/foo.py"})
    assert out[0]["severity"] == "declared"
    assert out[0]["concurrent_tasks"] == []


# --------------------------------------------------------------------------- #
# 收割基线：不许拿派单前的自述当交付
# --------------------------------------------------------------------------- #

def test_missing_baseline_is_reported_as_degraded(de, tmp_path):
    """缺 baseline_offset 只能退回 readall，且必须留下降级痕迹。"""
    board_dir = tmp_path / "board"
    board_dir.mkdir()
    (board_dir / "readall.py").write_text(
        "import sys\nprint('AGENT> 我完成了 a.py')\n", encoding="utf-8"
    )
    (board_dir / "targets.json").write_text(json.dumps({
        "①": {"id": "01a1499e-5603-761b-b85a-8e156b0d6dbe", "label": "1号"},
    }), encoding="utf-8")
    (board_dir / "queue.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
    sh = board_dir / "dispatch.sh"
    sh.write_text("#!/bin/zsh\nexit 0\n", encoding="utf-8")
    sh.chmod(0o755)
    eng = de.Engine(
        board=de.Board(board_dir), windows=de.WindowTable(board_dir),
        status=de.StatusSource(sh, de.WindowTable(board_dir)), repo=tmp_path,
    )
    claim, source = eng.read_report({"id": "T", "window": "1"})
    assert source == "readall_fallback"
    assert eng.notes, "降级必须留下可见痕迹"


def test_delivery_in_queue_wins_over_transcript(de, tmp_path):
    eng = _engine(de, tmp_path)
    claim, source = eng.read_report({"id": "T", "window": "1", "delivery": "回报正文"})
    assert source == "queue.delivery"
    assert claim == "回报正文"


# --------------------------------------------------------------------------- #
# 失败回填
# --------------------------------------------------------------------------- #

def test_append_banner_keeps_only_latest_reason(de):
    """重复失败只保留最近一条理由，但不能把原描述吃掉。"""
    t = {"id": "A", "description": "原始描述"}
    de.append_banner(t, "上次未通过（t1）：原因一")
    de.append_banner(t, "上次未通过（t2）：原因二")
    assert "原始描述" in t["description"]
    assert "原因二" in t["description"]
    assert "原因一" not in t["description"]


def test_requeue_conflict_names_the_current_holder(de):
    """退回 todo 那一刻就该点名占着文件的在跑任务，不能等到下一轮才暴露。"""
    doc = {"tasks": [
        {"id": "A", "state": "todo", "write_scope": ["a.py"]},
        {"id": "B", "state": "running", "write_scope": ["a.py"]},
    ]}
    hit = de.requeue_conflict(doc["tasks"][0], doc)
    assert hit["conflict_with"] == ["B"]
    assert hit["files"] == ["a.py"]


def test_stalled_marks_but_never_redispatches(de):
    """超时只标 stalled —— 自动重派等于两个窗口改同一文件。"""
    tasks = [{"id": "A", "state": "running", "dispatched_at": "2020-01-01T00:00:00+0800"}]
    out = de.mark_stalled(tasks, stall_s=60)
    assert out == ["A"]
    assert tasks[0]["stalled"] is True
    assert tasks[0]["state"] == "running"


def test_stall_ignores_task_that_already_reported(de):
    tasks = [{
        "id": "A", "state": "running", "dispatched_at": "2020-01-01T00:00:00+0800",
        "delivery": "我做完了",
    }]
    assert de.mark_stalled(tasks, stall_s=60) == []
