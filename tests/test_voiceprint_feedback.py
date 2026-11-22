"""声纹答案回流脚本的护栏测试（全部离线、夹具数据、不碰真实池）。

对应 scripts/voiceprint_feedback.py。四条硬要求：
  1. 门槛以下拒绝
  2. 回答「不是我说的」永不进本人池
  3. dry-run 不写任何文件
  4. --apply 之后能用备份回滚
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location("voiceprint_feedback", ROOT / "scripts" / "voiceprint_feedback.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["voiceprint_feedback"] = module
    spec.loader.exec_module(module)
    return module


vf = _load_script()


# --------------------------------------------------------------------------- 夹具


def unit_vector(index: int, dim: int = 192) -> list[float]:
    """确定性的基向量：第 index 维为 1，其余 0。两条基向量余弦 = 0。"""
    vec = [0.0] * dim
    vec[index % dim] = 1.0
    return vec


def near_vector(base: list[float], epsilon: float = 0.05) -> list[float]:
    """与 base 余弦 ≈ 0.9987 的向量（足够过 0.70 门槛）。"""
    vec = list(base)
    vec[0] += epsilon
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_wav(path: Path, seconds: float = 2.0, rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(b"\x00\x00" * int(seconds * rate))


@pytest.fixture()
def sandbox(tmp_path: Path):
    """造一套：池 + 片段索引 + 音频 + 两条答案（一条本人、一条非本人）。"""
    pool_path = tmp_path / "pool-watch-test.json"
    template = unit_vector(1)
    pool_path.write_text(json.dumps({
        "schema": "voiceprint-pool-v2", "backend": "campplus", "embedding_dim": 192,
        "current_template": template, "entries": [{"vector": unit_vector(3), "hits": 1}],
    }), encoding="utf-8")

    audio = tmp_path / "audio" / "rec_test.m4a"
    write_wav(audio.with_suffix(".wav"))
    asr_dir = tmp_path / "jobs" / "job-1" / "files" / "hash1" / "asr"
    asr_dir.mkdir(parents=True)
    (asr_dir / "checkpoint.json").write_text(json.dumps({"transcripts": [{
        "segment_id": "rec_test_seg_000001", "recording_id": "rec_test",
        "start_offset_seconds": 10.0, "end_offset_seconds": 12.0,
        "acoustic_proximity": "near", "proximity_ratio_db": 8.0, "text": "测试片段",
    }]}), encoding="utf-8")
    (asr_dir / "recordings.json").write_text(json.dumps([
        {"recording_id": "rec_test", "source_file": str(audio.with_suffix(".wav"))}
    ]), encoding="utf-8")

    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "op_1.json").write_text(json.dumps({
        "op_id": "op_1", "purpose": "voiceprint", "operation": "personal",
        "segment_id": "rec_test_seg_000001", "source_id": "src_a", "quote": "睡醒了。",
    }), encoding="utf-8")
    (answers / "op_2.json").write_text(json.dumps({
        "op_id": "op_2", "purpose": "voiceprint", "operation": "non_personal",
        "segment_id": "rec_test_seg_000001", "source_id": "src_b", "quote": "电视里的人说话",
    }), encoding="utf-8")
    (answers / "op_other.json").write_text(json.dumps({
        "op_id": "op_other", "purpose": "scene", "operation": "media",
        "segment_id": "rec_test_seg_000001",
    }), encoding="utf-8")

    return {
        "pool": pool_path, "answers": answers, "jobs": tmp_path / "jobs",
        "backup": tmp_path / "backups", "negatives": tmp_path / "negatives",
        "template": template, "segment_index": vf.index_segments(tmp_path / "jobs"),
    }


def run_main(sandbox, *extra: str):
    argv = [
        "--answers-dir", str(sandbox["answers"]),
        "--jobs-root", str(sandbox["jobs"]),
        "--pool", str(sandbox["pool"]),
        "--backup-dir", str(sandbox["backup"]),
        "--negatives-dir", str(sandbox["negatives"]),
        "--json", *extra,
    ]
    return vf.main(argv)


# --------------------------------------------------------------------------- 1. 门槛


def _answer(sandbox, op: str = "op_1"):
    return [a for a in vf.read_answers(sandbox["answers"]) if a.op_id == op][0]


def _segment(sandbox):
    return sandbox["segment_index"]["rec_test_seg_000001"]


def test_only_voiceprint_answers_are_read(sandbox):
    answers = vf.read_answers(sandbox["answers"])
    assert {a.op_id for a in answers} == {"op_1", "op_2"}, "scene/money 类答案不该被读进来"


def test_below_threshold_is_rejected(sandbox):
    snap = vf.load_pool(sandbox["pool"])
    decision = vf.decide(_answer(sandbox), _segment(sandbox), unit_vector(77), snap)
    assert decision.kind == "reject"
    assert decision.reason == "below_threshold"
    assert decision.similarity is not None and decision.similarity < decision.threshold


def test_above_threshold_is_accepted(sandbox):
    snap = vf.load_pool(sandbox["pool"])
    decision = vf.decide(_answer(sandbox), _segment(sandbox), near_vector(sandbox["template"]), snap)
    assert decision.kind == "accept", decision.detail


def test_near_field_soft_gain_lowers_threshold_but_never_below_floor():
    assert vf.effective_threshold(3.0, 0.70) == pytest.approx(0.70)
    assert vf.effective_threshold(10.0, 0.85) == pytest.approx(0.73)
    assert vf.effective_threshold(-1.0, 0.70) == pytest.approx(0.75)
    assert vf.effective_threshold(None, 0.70) == 0.70


def test_dimension_mismatch_is_rejected(sandbox):
    snap = vf.load_pool(sandbox["pool"])
    decision = vf.decide(_answer(sandbox), _segment(sandbox), unit_vector(1, dim=48), snap)
    assert decision.kind == "reject"
    assert decision.reason == "dim_mismatch"


def test_short_segment_is_rejected(sandbox):
    segment = _segment(sandbox)
    short = vf.SegmentRef(**{**segment.__dict__, "end_seconds": segment.start_seconds + 0.3})
    snap = vf.load_pool(sandbox["pool"])
    decision = vf.decide(_answer(sandbox), short, near_vector(sandbox["template"]), snap)
    assert decision.reason == "too_short"


def test_far_field_is_rejected(sandbox):
    segment = _segment(sandbox)
    far = vf.SegmentRef(**{**segment.__dict__, "proximity": "far", "proximity_ratio_db": -2.0})
    snap = vf.load_pool(sandbox["pool"])
    decision = vf.decide(_answer(sandbox), far, near_vector(sandbox["template"]), snap)
    assert decision.reason == "far_field"


def test_pool_score_matches_production_pool(sandbox):
    """自写 pool_score 必须与线上 RollingVoiceprintPool.score_candidate 同值。"""
    from life_logger.source_attribution import RollingVoiceprintPool

    snap = vf.load_pool(sandbox["pool"])
    candidate = near_vector(sandbox["template"], 0.3)
    pool = RollingVoiceprintPool(
        initial_template=list(sandbox["template"]),
        current_template=list(sandbox["template"]),
        history=[list(sandbox["template"]), unit_vector(3)],
    )
    assert vf.pool_score(candidate, snap) == pytest.approx(pool.score_candidate(candidate), abs=1e-9)


# --------------------------------------------------------------------------- 2. 反例


def test_negative_answer_never_enters_pool(sandbox, monkeypatch, tmp_path):
    """只放一条「不是我说的」答案：--apply 之后池必须逐字节不变，且不产生备份。"""
    only_neg = tmp_path / "answers-neg"
    only_neg.mkdir()
    src = sandbox["answers"] / "op_2.json"
    (only_neg / "op_2.json").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    before = sha256(sandbox["pool"])
    monkeypatch.setattr(vf, "compute_embedding", lambda seg, backend="auto": ("fake", near_vector(sandbox["template"])))

    argv = ["--answers-dir", str(only_neg), "--jobs-root", str(sandbox["jobs"]), "--pool", str(sandbox["pool"]),
            "--backup-dir", str(sandbox["backup"]), "--negatives-dir", str(sandbox["negatives"]),
            "--json", "--apply"]
    assert vf.main(argv) == 0
    assert sha256(sandbox["pool"]) == before, "反例绝不允许进本人池"
    assert not sandbox["backup"].exists(), "没有采纳就不该产生备份"
    written = list(sandbox["negatives"].glob("voiceprint-op_2.json"))
    assert written, "反例应写进 negatives 目录"
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["segment_id"] == "rec_test_seg_000001"
    assert "不得据此更新本人池" in payload["note"]


def test_negative_decision_shape(sandbox):
    decision = vf.decide(_answer(sandbox, "op_2"), _segment(sandbox), None, vf.load_pool(sandbox["pool"]))
    assert decision.kind == "record_negative"
    assert decision.embedding is None


# --------------------------------------------------------------------------- 3. dry-run


def test_dry_run_does_not_write_anything(sandbox, monkeypatch):
    before_pool = sha256(sandbox["pool"])
    mtimes = {p: p.stat().st_mtime_ns for p in sandbox["pool"].parent.iterdir()}
    monkeypatch.setattr(vf, "compute_embedding", lambda seg, backend="auto": ("fake", near_vector(sandbox["template"])))

    code = run_main(sandbox)
    assert code == 0
    assert sha256(sandbox["pool"]) == before_pool, "dry-run 不许改池"
    assert not sandbox["backup"].exists(), "dry-run 不许建备份"
    assert not sandbox["negatives"].exists(), "dry-run 不许写反例"
    assert {p: p.stat().st_mtime_ns for p in sandbox["pool"].parent.iterdir()} == mtimes


def test_dry_run_reports_would_adopt(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(vf, "compute_embedding", lambda seg, backend="auto": ("fake", near_vector(sandbox["template"])))
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = run_main(sandbox)
    assert code == 0, f"dry-run 应正常退出，实际 rc={code}"
    out = json.loads(buf.getvalue())
    assert out["mode"] == "dry-run"
    assert out["accepted"] >= 1, out
    assert out["pool_sha256_before"] == out["pool_sha256_after"]


# --------------------------------------------------------------------------- 4. apply + 回滚


def test_apply_writes_backup_and_rollback_restores(sandbox, monkeypatch):
    original_sha = sha256(sandbox["pool"])
    monkeypatch.setattr(vf, "compute_embedding", lambda seg, backend="auto": ("fake", near_vector(sandbox["template"])))

    code = run_main(sandbox, "--apply")
    assert code == 0
    assert sha256(sandbox["pool"]) != original_sha, "apply 后池应变化"
    backups = sorted(sandbox["backup"].glob("pool-watch-test.json.bak-*"))
    assert backups, "apply 前必须备份"
    assert sha256(backups[0]) == original_sha, "备份内容必须等于写前内容"

    restored = vf.rollback(backups[0], sandbox["pool"])
    assert restored["status"] == "restored"
    assert sha256(sandbox["pool"]) == original_sha, "回滚后必须逐字节等于原文件"


def test_rollback_refuses_corrupted_backup(sandbox):
    backup = sandbox["backup"] / "broken.bak"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text("{}", encoding="utf-8")
    backup.with_suffix(".bak.sha256").write_text("deadbeef\n", encoding="utf-8")
    result = vf.rollback(backup, sandbox["pool"])
    assert result["status"] == "error" and result["reason"] == "backup_corrupt"
