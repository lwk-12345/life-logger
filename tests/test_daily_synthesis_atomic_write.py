"""F7-S1：日报临时产物唯一化 + 原子发布的并发/失败回归。

三条要守的行为：
  1) 同一目标文件的并发写入**不互相覆盖**（临时名唯一，最终文件总是某个完整版本）；
  2) `os.replace` 抛错时，**旧正式文件不损坏**，且自己的临时文件被清理；
  3) 写入前 `flush + fsync`（断电/崩溃后不留下半截内容）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from life_logger.synthesis_pause import _atomic_json, write_text_atomic  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 唯一临时名
def test_temp_name_is_unique_and_in_same_dir(tmp_path: Path, monkeypatch):
    seen: list[Path] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(Path(src))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    target = tmp_path / "synthesis-2026-10-24.json"
    write_text_atomic(target, '{"v": 1}')
    write_text_atomic(target, '{"v": 2}')

    assert len(seen) == 2
    assert seen[0] != seen[1], "两次写入必须用不同的临时文件"
    for src in seen:
        assert src.parent == target.parent, "临时文件必须与目标同目录（os.replace 才是原子的）"
        assert src.name != f"{target.name}.tmp", "不得再使用固定的 .tmp 名字"
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}
    assert not list(tmp_path.glob("*.tmp")), "不得留下临时文件"


def test_concurrent_writers_never_produce_a_mixed_file(tmp_path: Path):
    """40 个线程并发写同一目标：最终内容必须是**某一个**完整版本，不能是拼接/截断。"""
    target = tmp_path / "synthesis-2026-10-24.json"
    payloads = [{"writer": i, "blob": "x" * (200 + i)} for i in range(40)]
    errors: list[BaseException] = []

    def run(payload):
        try:
            write_text_atomic(target, json.dumps(payload, ensure_ascii=False))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed in payloads, "最终文件必须是某个写入者的完整内容"
    assert parsed["blob"] == "x" * (200 + parsed["writer"]), "内容与 writer 必须自洽（没被拼接）"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != target.name]
    assert not leftovers, f"不得留下临时文件：{leftovers}"


def test_two_processes_do_not_clobber_each_other(tmp_path: Path):
    """跨进程版本：两个独立 python 进程同时写同一目标。"""
    target = tmp_path / "synthesis-2026-10-24.json"
    code = (
        "import json,sys;sys.path.insert(0,{root!r});"
        "from life_logger.synthesis_pause import write_text_atomic;"
        "from pathlib import Path;"
        "write_text_atomic(Path({target!r}), json.dumps({{'pid': {pid}, 'pad': 'y'*{pad}}}))"
    )
    procs = []
    for i, pad in enumerate((300, 600)):
        script = code.format(root=str(ROOT / "src"), target=str(target), pid=i, pad=pad)
        procs.append(subprocess.Popen([sys.executable, "-c", script],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err.decode("utf-8", "ignore")

    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed["pid"] in (0, 1), parsed
    assert parsed["pad"] == "y" * (300 if parsed["pid"] == 0 else 600)
    assert not [p for p in tmp_path.iterdir() if p.name != target.name]


# ---------------------------------------------------------------- 失败不损坏旧文件
def test_replace_failure_keeps_old_file_intact(tmp_path: Path):
    target = tmp_path / "synthesis-2026-10-24.json"
    target.write_text('{"old": true}', encoding="utf-8")

    with patch("os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            write_text_atomic(target, '{"new": true}')

    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}, "旧正式文件必须完好"
    assert not [p for p in tmp_path.iterdir() if p.name != target.name], "失败后必须清理临时文件"


def test_write_failure_before_replace_keeps_old_file_intact(tmp_path: Path):
    target = tmp_path / "synthesis-2026-10-24.json"
    target.write_text('{"old": true}', encoding="utf-8")

    with patch("os.fsync", side_effect=OSError("io error")):
        with pytest.raises(OSError):
            write_text_atomic(target, '{"new": true}')

    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert not [p for p in tmp_path.iterdir() if p.name != target.name]


def test_fsync_is_called_before_replace(tmp_path: Path, monkeypatch):
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])

    write_text_atomic(tmp_path / "x.json", "{}")
    assert order == ["fsync", "replace"], order


# ---------------------------------------------------------------- _atomic_json 同步受益
def test_atomic_json_uses_unique_temp(tmp_path: Path):
    target = tmp_path / "pending-2026-10-24.json"
    _atomic_json(target, {"a": 1})
    _atomic_json(target, {"a": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    assert not [p for p in tmp_path.iterdir() if p.name != target.name]


# ---------------------------------------------------------------- 日报真实调用点接线
def test_daily_synthesis_writes_via_atomic_helper(tmp_path: Path):
    """走真实 fallback 落盘分支（模型全挂 + 关闭暂停）：必须经 write_text_atomic，且不留 .tmp。"""
    from life_logger import daily_cognitive_session as dcs

    events = [{
        "event_id": "e1", "title": "测试事件", "activity_type": "work_discussion",
        "categories": ["work_communication"], "confidence": 0.9,
        "time": {"clock": "10:00"}, "evidence": {"quote": "测试"},
    }]
    calls: list[Path] = []

    def spy(path, text, **kwargs):
        calls.append(Path(path))
        return write_text_atomic(path, text, **kwargs)   # 用本模块导入的真实实现，避免递归

    with patch.object(dcs, "_get_deepseek_settings",
                      return_value=("https://api.deepseek.com", "fake_key", "deepseek-chat", None, None)), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("model down")), \
         patch.object(dcs, "_pause_on_model_failure", return_value=False), \
         patch("life_logger.synthesis_pause.write_text_atomic", side_effect=spy):
        result = dcs.generate_daily_cognitive_synthesis(
            "2026-10-24", events, force=True, cache_dir=tmp_path)

    assert calls, "fallback 落盘必须经过 write_text_atomic"
    target = tmp_path / "synthesis-2026-10-24.json"
    assert target.is_file(), f"未落盘；目录={[p.name for p in tmp_path.iterdir()]}"
    assert json.loads(target.read_text(encoding="utf-8"))["date"] == "2026-10-24"
    leftover = [p.name for p in tmp_path.iterdir() if p.name != target.name]
    assert not leftover, f"不得留临时文件：{leftover}"
    assert result.get("date") == "2026-10-24"
