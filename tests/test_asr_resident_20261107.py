"""常驻 ASR worker：协议、模型只加载一次、崩溃重启、超时、开关、重复截断。"""
from __future__ import annotations

import io
import json

import pytest

from life_logger import asr_resident as ar


# --------------------------------------------------------------- worker


def test_models_loaded_once_across_requests():
    loads = {"moss": 0, "campplus": 0}

    def moss():
        loads["moss"] += 1
        return "MOSS"

    def camp():
        loads["campplus"] += 1
        return "CAMP"

    worker = ar.ResidentWorker(
        moss_loader=moss, campplus_loader=camp,
        transcribe_fn=lambda **kw: {"text": "正常一句话", "segment_id": "s1"},
    )
    for i in range(3):
        out = worker.handle({"id": str(i), "wav": "/tmp/a.wav", "outdir": "/tmp/o"})
        assert out["ok"] is True and out["result"]["text"] == "正常一句话"
    assert loads == {"moss": 1, "campplus": 1}, loads


def test_stdio_protocol_roundtrip():
    worker = ar.ResidentWorker(transcribe_fn=lambda **kw: {"text": "你好"})
    stdin = io.StringIO(json.dumps({"id": "1", "wav": "/tmp/a.wav", "outdir": "/tmp/o"}) + "\n")
    stdout = io.StringIO()
    worker.serve(stdin=stdin, stdout=stdout)
    reply = json.loads(stdout.getvalue().strip())
    assert reply["ok"] is True and reply["result"]["text"] == "你好"


def test_bad_json_does_not_kill_worker():
    worker = ar.ResidentWorker(transcribe_fn=lambda **kw: {"text": "x"})
    stdin = io.StringIO("{not json}\n" + json.dumps({"id": "2"}) + "\n")
    stdout = io.StringIO()
    worker.serve(stdin=stdin, stdout=stdout)
    lines = [json.loads(l) for l in stdout.getvalue().strip().splitlines()]
    assert lines[0]["ok"] is False and lines[0]["error"] == "bad_json"
    assert lines[1]["ok"] is True


def test_single_segment_error_is_returned_not_raised():
    def boom(**kw):
        raise RuntimeError("模型炸了")

    out = ar.ResidentWorker(transcribe_fn=boom).handle({"id": "1"})
    assert out["ok"] is False and "模型炸了" in out["error"]


# --------------------------------------------------------------- 重复截断


def test_repetition_is_truncated_and_flagged():
    text = "今天天气不错" + "好的" * 40
    cleaned, warnings = ar.collapse_repetition(text, max_repeat=6, min_unit_chars=2)
    assert cleaned == "今天天气不错" + "好的" * 6
    assert warnings and warnings[0].startswith("repetition_truncated")


def test_normal_speech_untouched():
    text = "先吃饭，然后去健身房，卧推三组，每组十二个，重量六十公斤。"
    cleaned, warnings = ar.collapse_repetition(text)
    assert cleaned == text and warnings == []


def test_mild_repetition_below_threshold_untouched():
    text = "好的" * 6                       # 正好等于阈值，不算失控
    assert ar.collapse_repetition(text, max_repeat=6, min_unit_chars=2)[0] == text


def test_worker_applies_truncation_to_result():
    long_text = "喂" * 200
    out = ar.ResidentWorker(transcribe_fn=lambda **kw: {"text": long_text}).handle({"id": "1"})
    assert out["ok"] is True
    assert len(out["result"]["text"]) < len(long_text)
    assert out["result"]["warnings"]


# --------------------------------------------------------------- client


class _FakeProc:
    def __init__(self, replies, *, die_after=None):
        self.replies = list(replies)
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(self.replies))
        self.dead = False
        self.killed = 0

    def poll(self):
        return None if not self.dead else 0

    def kill(self):
        self.dead = True
        self.killed += 1

    def wait(self, timeout=None):
        return 0


def test_client_returns_result():
    proc = _FakeProc([json.dumps({"id": "1", "ok": True, "result": {"text": "好"}}) + "\n"])
    client = ar.ResidentClient(popen=lambda *a, **kw: proc)
    out = client.transcribe(wav=ar.Path("/tmp/a.wav"), outdir=ar.Path("/tmp/o"))
    assert out["text"] == "好"


def test_client_restarts_once_after_crash():
    calls = {"n": 0}
    procs = []

    def popen(*a, **kw):
        calls["n"] += 1
        # 第一次给一个「空回复」的进程（模拟崩溃），重启后正常
        proc = _FakeProc([] if calls["n"] == 1 else
                         [json.dumps({"id": "1", "ok": True, "result": {"text": "恢复"}}) + "\n"])
        procs.append(proc)
        return proc

    client = ar.ResidentClient(popen=popen, timeout_seconds=0.2)
    out = client.transcribe(wav=ar.Path("/tmp/a.wav"), outdir=ar.Path("/tmp/o"))
    assert out["text"] == "恢复"
    assert calls["n"] == 2, "崩溃后应重启一次"


def test_client_gives_up_after_two_failures():
    def popen(*a, **kw):
        return _FakeProc([])

    client = ar.ResidentClient(popen=popen, timeout_seconds=0.1)
    with pytest.raises(ar.ResidentUnavailable):
        client.transcribe(wav=ar.Path("/tmp/a.wav"), outdir=ar.Path("/tmp/o"))


def test_timeout_raises_unavailable():
    class _Silent(_FakeProc):
        def __init__(self):
            super().__init__([])
            self.stdout = type("S", (), {"readline": lambda self: ""})()

    def popen(*a, **kw):
        return _Silent()

    client = ar.ResidentClient(popen=popen, timeout_seconds=0.05)
    with pytest.raises(ar.ResidentUnavailable):
        client.transcribe(wav=ar.Path("/tmp/a.wav"), outdir=ar.Path("/tmp/o"))


# --------------------------------------------------------------- 开关


@pytest.mark.parametrize("value,expected", [
    (None, True), ("1", True), ("0", False), ("off", False), ("false", False), ("YES", True),
])
def test_resident_switch(value, expected):
    env = {} if value is None else {"LIFE_LOGGER_ASR_RESIDENT": value}
    assert ar.resident_enabled(env) is expected
