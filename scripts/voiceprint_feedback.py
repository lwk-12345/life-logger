#!/usr/bin/env python3
"""声纹答案回流：把飞轮里「是我说的 / 不是我说的」评审判定，影子比对本人声纹池。

为什么默认 dry-run：答错一次就把非本人的声音写进本人模板，之后所有同源误判都会
被池子「自证」。所以本脚本只做两件事——**算清楚会不会采纳**，以及**把理由写出来**；
真正写池必须显式 `--apply`，且写之前先备份、可用 `--rollback` 回滚。

数据来源
  答案：output/local-web/flywheel/answers/*.json，筛 purpose == "voiceprint"
        （operation == "personal" 表示用户回答「是我说的」；其它值一律当反例）
  片段：segment_id（如 rec_20ffebd97a0a_seg_001378）
        → 在 output/local-web/jobs/*/files/*/asr/checkpoint.json 里定位
        → start_offset_seconds / end_offset_seconds / acoustic_proximity / proximity_ratio_db
        → 音频取同目录 recordings.json 的 source_file

采纳门槛（全部满足才可能采纳；缺一即拒，理由写进报告）
  1. 片段存在、音频文件存在
  2. 时长 ≥ policy.min_window_seconds（CAM++ 0.8s / 48 维 0.2s）
  3. 近场：acoustic_proximity == "near" 且 proximity_ratio_db ≥ 3.0（与线上 maybe_update 同条件）
  4. embedding 维度 == 池维度（池里混进 48 维向量会静默污染 192 维模板）
  5. 相似度 ≥ 生效门槛（pool_min_update_similarity 叠加近场软增益，与 maybe_update 同公式）

用法
  .venv/bin/python scripts/voiceprint_feedback.py                 # 只看，不写
  .venv/bin/python scripts/voiceprint_feedback.py --json          # 机器可读
  .venv/bin/python scripts/voiceprint_feedback.py --apply         # 真写（本单禁止在真实数据上跑）
  .venv/bin/python scripts/voiceprint_feedback.py --rollback <备份文件>

退出码：0 正常（含「全部拒绝」）；1 参数/环境错误；2 有答案被判为「可采纳」但没加 --apply 时仍返回 0，
        只有 --apply 且写入失败才返回 1。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ANSWERS = ROOT / "output" / "local-web" / "flywheel" / "answers"
DEFAULT_JOBS = ROOT / "output" / "local-web" / "jobs"
DEFAULT_POOL = ROOT / "output" / "local-web" / "watch-sessions" / "voiceprints"
DEFAULT_BACKUPS = ROOT / "output" / "local-web" / "watch-sessions" / "voiceprint-backups"

#: 与 source_attribution.CAMPPLUS_VOICEPRINT_POLICY 对齐；不 import 是为了让本脚本
#: 在没装 funasr 的 .venv 里也能跑（policy 对象本身不依赖任何三方包）。
DEFAULT_MIN_UPDATE_SIMILARITY = 0.70
DEFAULT_MIN_WINDOW_SECONDS = 0.8
NEAR_FIELD_SOFT_GAIN_START_DB = 3.0
NEAR_FIELD_SOFT_GAIN_SPAN_DB = 7.0
NEAR_FIELD_SOFT_GAIN_MAX = 0.12

#: operation 里哪些算「不是我说的」
NEGATIVE_OPERATIONS = {
    "non_personal", "not_personal", "not_me", "other", "media", "third_party", "negative",
}


# --------------------------------------------------------------------------- 数据结构


@dataclass
class Answer:
    path: Path
    op_id: str
    operation: str
    segment_id: str
    source_id: str
    quote: str
    created_at: float | None = None

    @property
    def positive(self) -> bool:
        return self.operation.strip().lower() == "personal"


@dataclass
class SegmentRef:
    segment_id: str
    checkpoint: Path
    recording_id: str
    start_seconds: float
    end_seconds: float
    audio_path: Path | None
    proximity: str
    proximity_ratio_db: float | None
    text: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, float(self.end_seconds) - float(self.start_seconds))


@dataclass
class PoolSnapshot:
    path: Path
    embedding_dim: int | None
    backend: str
    current_template: list[float]
    entries: list[list[float]]

    @property
    def usable(self) -> bool:
        return bool(self.current_template)


@dataclass
class Decision:
    answer: Answer
    kind: str            # accept / record_negative / reject
    reason: str          # 机器可读的原因码
    detail: str = ""     # 中文解释，直接进报告
    similarity: float | None = None
    threshold: float | None = None
    duration: float | None = None
    proximity: str | None = None
    embedding: list[float] | None = field(default=None, repr=False)

    def to_public(self) -> dict[str, Any]:
        return {
            "op_id": self.answer.op_id,
            "segment_id": self.answer.segment_id,
            "operation": self.answer.operation,
            "decision": self.kind,
            "reason": self.reason,
            "detail": self.detail,
            "similarity": None if self.similarity is None else round(self.similarity, 4),
            "threshold": None if self.threshold is None else round(self.threshold, 4),
            "duration_s": None if self.duration is None else round(self.duration, 2),
            "proximity": self.proximity,
        }


# --------------------------------------------------------------------------- 读取答案


def read_answers(answers_dir: Path, *, purpose: str = "voiceprint") -> list[Answer]:
    out: list[Answer] = []
    if not answers_dir.is_dir():
        return out
    for path in sorted(answers_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(raw.get("purpose") or "") != purpose:
            continue
        segment = str(raw.get("segment_id") or "")
        if not segment:
            continue
        out.append(
            Answer(
                path=path,
                op_id=str(raw.get("op_id") or path.stem),
                operation=str(raw.get("operation") or ""),
                segment_id=segment,
                source_id=str(raw.get("source_id") or ""),
                quote=str(raw.get("quote") or ""),
                created_at=raw.get("created_at"),
            )
        )
    return out


# --------------------------------------------------------------------------- 定位片段


def index_segments(jobs_root: Path) -> dict[str, SegmentRef]:
    """扫 jobs/*/files/*/asr/checkpoint.json，建 segment_id → 片段引用。"""
    index: dict[str, SegmentRef] = {}
    if not jobs_root.is_dir():
        return index
    for checkpoint in jobs_root.glob("*/files/*/asr/checkpoint.json"):
        try:
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        audio = _audio_for(checkpoint.parent)
        for item in payload.get("transcripts") or []:
            sid = str(item.get("segment_id") or "")
            if not sid or sid in index:
                continue
            try:
                start = float(item.get("start_offset_seconds"))
                end = float(item.get("end_offset_seconds"))
            except (TypeError, ValueError):
                continue
            index[sid] = SegmentRef(
                segment_id=sid,
                checkpoint=checkpoint,
                recording_id=str(item.get("recording_id") or ""),
                start_seconds=start,
                end_seconds=end,
                audio_path=audio.get(str(item.get("recording_id") or "")),
                proximity=str(item.get("acoustic_proximity") or "uncertain"),
                proximity_ratio_db=_as_float(item.get("proximity_ratio_db")),
                text=str(item.get("text") or ""),
            )
    return index


def _audio_for(asr_dir: Path) -> dict[str, Path]:
    try:
        rows = json.loads((asr_dir / "recordings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, Path] = {}
    for row in rows if isinstance(rows, list) else []:
        rid = str(row.get("recording_id") or "")
        src = row.get("source_file")
        if rid and src:
            out[rid] = Path(str(src))
    return out


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


# --------------------------------------------------------------------------- 池与相似度


def load_pool(path: Path) -> PoolSnapshot:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return PoolSnapshot(path=path, embedding_dim=None, backend="", current_template=[], entries=[])
    template = [float(v) for v in (raw.get("current_template") or []) if isinstance(v, (int, float))]
    entries = [
        [float(v) for v in (e.get("vector") or [])]
        for e in (raw.get("entries") or [])
        if isinstance(e, Mapping) and e.get("vector")
    ]
    return PoolSnapshot(
        path=path,
        embedding_dim=raw.get("embedding_dim"),
        backend=str(raw.get("backend") or ""),
        current_template=template,
        entries=entries,
    )


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def pool_score(vector: Sequence[float], snap: PoolSnapshot) -> float:
    """与 RollingVoiceprintPool.score_candidate 同义：模板与历史取最大值。"""
    best = cosine(snap.current_template, vector) if snap.current_template else 0.0
    for entry in snap.entries:
        if len(entry) == len(vector):
            best = max(best, cosine(entry, vector))
    return best


def effective_threshold(ratio_db: float | None, base: float) -> float:
    """复刻 maybe_update 的近场软增益：近场越清晰，门槛可略降（下限 0.70）。"""
    if ratio_db is None or not math.isfinite(ratio_db):
        return base
    if ratio_db >= NEAR_FIELD_SOFT_GAIN_START_DB:
        gain = max(0.0, min(1.0, (ratio_db - NEAR_FIELD_SOFT_GAIN_START_DB) / NEAR_FIELD_SOFT_GAIN_SPAN_DB))
        return max(0.70, base - NEAR_FIELD_SOFT_GAIN_MAX * gain)
    if ratio_db < 0.0:
        return min(0.92, base + 0.05)
    return base


# --------------------------------------------------------------------------- 判定


def decide(
    answer: Answer,
    segment: SegmentRef | None,
    embedding: Sequence[float] | None,
    snapshot: PoolSnapshot,
    *,
    base_threshold: float = DEFAULT_MIN_UPDATE_SIMILARITY,
    min_window_seconds: float = DEFAULT_MIN_WINDOW_SECONDS,
) -> Decision:
    """单个答案 → 判定。纯函数，测试可直接喂假 embedding/池。"""
    if not answer.positive:
        return Decision(
            answer=answer, kind="record_negative", reason="negative_answer",
            detail="用户回答「不是我说的」：只记反例，永不进本人池",
        )
    if segment is None:
        return Decision(answer=answer, kind="reject", reason="segment_missing",
                        detail="找不到这个 segment（checkpoint.json 里没有）")
    if segment.audio_path is None or not Path(segment.audio_path).is_file():
        return Decision(answer=answer, kind="reject", reason="audio_missing",
                        detail="片段存在但音频文件不在，无法重算声纹", duration=segment.duration)
    if segment.duration < min_window_seconds:
        return Decision(answer=answer, kind="reject", reason="too_short",
                        detail=f"片段只有 {segment.duration:.2f}s < {min_window_seconds:.1f}s，短句声纹不稳",
                        duration=segment.duration)
    ratio = segment.proximity_ratio_db
    if segment.proximity != "near" or ratio is None or ratio < NEAR_FIELD_SOFT_GAIN_START_DB:
        return Decision(answer=answer, kind="reject", reason="far_field",
                        detail=f"不是近场录音（proximity={segment.proximity}, ratio={ratio}），本人声纹只在近场采纳",
                        duration=segment.duration, proximity=segment.proximity)
    if embedding is None:
        return Decision(answer=answer, kind="reject", reason="no_embedding",
                        detail="声纹模型不可用（CAM++/48 维都算不出来）",
                        duration=segment.duration, proximity=segment.proximity)
    if snapshot.embedding_dim and len(embedding) != int(snapshot.embedding_dim):
        return Decision(answer=answer, kind="reject", reason="dim_mismatch",
                        detail=f"embedding {len(embedding)} 维 ≠ 池 {snapshot.embedding_dim} 维，混入会污染模板",
                        duration=segment.duration, proximity=segment.proximity)
    threshold = effective_threshold(ratio, base_threshold)
    similarity = pool_score(embedding, snapshot)
    common = dict(duration=segment.duration, proximity=segment.proximity,
                  similarity=similarity, threshold=threshold, embedding=list(embedding))
    if similarity < threshold:
        return Decision(answer=answer, kind="reject", reason="below_threshold",
                        detail=f"相似度 {similarity:.3f} < 门槛 {threshold:.3f}", **common)
    return Decision(answer=answer, kind="accept", reason="accepted",
                    detail=f"相似度 {similarity:.3f} ≥ 门槛 {threshold:.3f}，满足全部采纳条件", **common)


# --------------------------------------------------------------------------- 音频与 embedding


def decode_window(audio_path: Path, start: float, duration: float, *, sample_rate: int = 16000) -> Any:
    """用 ffmpeg 截取片段并返回 16k 单声道 float32 数组（无三方音频库依赖）。"""
    import subprocess

    import numpy as np

    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-ss", f"{max(0.0, start):.3f}", "-t", f"{max(0.0, duration):.3f}",
        "-i", str(audio_path), "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 解码失败：{proc.stderr[:200].decode('utf-8', 'replace')}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def compute_embedding(segment: SegmentRef, *, backend: str = "auto") -> tuple[str, list[float] | None]:
    """返回 (实际后端, 向量)。CAM++ 不可用时**不**偷偷退回 48 维去比 192 维池。"""
    samples = decode_window(segment.audio_path, segment.start_seconds, segment.duration)
    if backend in {"auto", "campplus"}:
        try:
            from life_logger.campplus_voiceprint import load_campplus_embedder

            embedder = load_campplus_embedder()
            vector = embedder.embed(samples)
            if vector:
                return "campplus", [float(v) for v in vector]
        except Exception as error:  # 模型/依赖缺失都只影响这次判定，不影响脚本
            if backend == "campplus":
                print(f"[voiceprint-feedback] CAM++ 不可用：{type(error).__name__}: {error}", file=sys.stderr)
    if backend in {"auto", "numpy48"}:
        from life_logger.moss_transcribe import _extract_numpy_voiceprint

        vector = _extract_numpy_voiceprint(samples)
        if vector:
            return "numpy48", [float(v) for v in vector]
    return backend, None


# --------------------------------------------------------------------------- 备份 / 写入 / 回滚


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_pool(pool_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = backup_dir / f"{pool_path.name}.bak-{stamp}"
    shutil.copy2(pool_path, target)
    (backup_dir / f"{target.name}.sha256").write_text(sha256_of(target) + "\n", encoding="utf-8")
    return target


def apply_accepted(
    pool_path: Path,
    accepted: Sequence[Decision],
    *,
    backup_dir: Path,
    min_update_similarity: float = DEFAULT_MIN_UPDATE_SIMILARITY,
) -> dict[str, Any]:
    """把 accept 的向量写进池：先备份，再走生产同一条 store.persist 路径。"""
    if not accepted:
        return {"status": "noop", "reason": "no_accepted", "written": 0}
    if not pool_path.is_file():
        return {"status": "error", "reason": "pool_missing", "written": 0}

    from life_logger.source_attribution import RollingVoiceprintPool
    from life_logger.voiceprint_pool_store import VoiceprintPoolStore

    backup = backup_pool(pool_path, backup_dir)
    snapshot = load_pool(pool_path)
    template = list(snapshot.current_template)
    pool = RollingVoiceprintPool(
        initial_template=list(template),
        current_template=list(template),
        history=[list(template)] if template else [],
        min_update_similarity=min_update_similarity,
    )
    store = VoiceprintPoolStore(path=pool_path)
    store.hydrate(pool)

    vectors: list[list[float]] = []
    for decision in accepted:
        if decision.embedding is None:
            continue
        # decide() 已经把近场/时长/维度都过了一遍，这里按基础门槛再确认一次。
        if pool.maybe_update(decision.embedding, float(decision.similarity or 0.0)):
            vectors.append(list(decision.embedding))
    if not vectors:
        return {"status": "noop", "reason": "maybe_update_rejected_all", "written": 0,
                "backup": str(backup)}
    result = store.persist(current_template=pool.current_template, history=vectors,
                           embedding_dim=len(vectors[0]))
    return {"status": "written", "written": len(vectors), "backup": str(backup),
            "store": {k: result.get(k) for k in ("status", "added", "merged", "reason") if k in result}}


def rollback(backup_path: Path, pool_path: Path) -> dict[str, Any]:
    """用备份覆盖池文件。先校验备份的 sha256 与同名 .sha256 一致。"""
    if not backup_path.is_file():
        return {"status": "error", "reason": "backup_missing"}
    digest_file = backup_path.with_suffix(backup_path.suffix + ".sha256")
    if digest_file.is_file():
        expected = digest_file.read_text(encoding="utf-8").strip()
        actual = sha256_of(backup_path)
        if expected and expected != actual:
            return {"status": "error", "reason": "backup_corrupt", "expected": expected, "actual": actual}
    shutil.copy2(backup_path, pool_path)
    return {"status": "restored", "sha256": sha256_of(pool_path), "from": str(backup_path)}


def write_negative(decision: Decision, negatives_dir: Path) -> Path:
    negatives_dir.mkdir(parents=True, exist_ok=True)
    target = negatives_dir / f"voiceprint-{decision.answer.op_id}.json"
    payload = {
        "schema_version": "voiceprint-negative-v1",
        "op_id": decision.answer.op_id,
        "segment_id": decision.answer.segment_id,
        "source_id": decision.answer.source_id,
        "quote": decision.answer.quote,
        "recorded_at": time.time(),
        "note": "用户回答「不是我说的」；本文件只作反例留存，任何代码都不得据此更新本人池",
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return target


# --------------------------------------------------------------------------- CLI


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    answers_dir = Path(args.answers_dir)
    jobs_root = Path(args.jobs_root)
    pool_path = Path(args.pool)

    answers = read_answers(answers_dir)
    if args.limit:
        answers = answers[: int(args.limit)]
    index = index_segments(jobs_root)
    snapshot = load_pool(pool_path)

    decisions: list[Decision] = []
    for answer in answers:
        segment = index.get(answer.segment_id)
        embedding = None
        if answer.positive and segment is not None and args.embedder != "none":
            if segment.audio_path and Path(segment.audio_path).is_file():
                try:
                    _backend, embedding = compute_embedding(segment, backend=args.embedder)
                except Exception as error:
                    print(f"[voiceprint-feedback] {answer.segment_id} embedding 失败：{error}", file=sys.stderr)
        decisions.append(
            decide(answer, segment, embedding, snapshot,
                   base_threshold=args.min_similarity,
                   min_window_seconds=args.min_window_seconds)
        )

    accepted = [d for d in decisions if d.kind == "accept"]
    negatives = [d for d in decisions if d.kind == "record_negative"]
    result: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "pool": str(pool_path),
        "pool_sha256_before": sha256_of(pool_path) if pool_path.is_file() else None,
        "pool_embedding_dim": snapshot.embedding_dim,
        "answers": len(decisions),
        "accepted": len(accepted),
        "would_adopt": [d.to_public() for d in accepted],
        "negatives": len(negatives),
        "decisions": [d.to_public() for d in decisions],
    }

    if args.apply:
        written = apply_accepted(pool_path, accepted, backup_dir=Path(args.backup_dir),
                                 min_update_similarity=args.min_similarity)
        result["write"] = written
        result["negatives_written"] = [str(write_negative(d, Path(args.negatives_dir))) for d in negatives]
    result["pool_sha256_after"] = sha256_of(pool_path) if pool_path.is_file() else None
    return 0, result


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="声纹答案回流（默认 dry-run，只算不写）")
    ap.add_argument("--answers-dir", default=str(DEFAULT_ANSWERS))
    ap.add_argument("--jobs-root", default=str(DEFAULT_JOBS))
    ap.add_argument("--pool", default=str(DEFAULT_POOL / "pool-watch.json"),
                    help="池文件；目录给错会直接报错，不会猜")
    ap.add_argument("--apply", action="store_true", help="真的写池（默认只读）")
    ap.add_argument("--rollback", default="", help="用指定备份恢复池文件后退出")
    ap.add_argument("--backup-dir", default=str(DEFAULT_BACKUPS))
    ap.add_argument("--negatives-dir", default=str(ROOT / "output" / "local-web" / "flywheel" / "negatives"))
    ap.add_argument("--embedder", choices=("auto", "campplus", "numpy48", "none"), default="auto")
    ap.add_argument("--min-similarity", type=float, default=DEFAULT_MIN_UPDATE_SIMILARITY)
    ap.add_argument("--min-window-seconds", type=float, default=DEFAULT_MIN_WINDOW_SECONDS)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)

    pool_path = Path(args.pool)
    if pool_path.is_dir():
        candidates = sorted(pool_path.glob("pool-*.json"))
        if len(candidates) == 1:
            pool_path = candidates[0]
        else:
            print(f"--pool 指向目录且不唯一：{pool_path}", file=sys.stderr)
            return 1
    if not pool_path.is_file():
        print(f"池文件不存在：{pool_path}", file=sys.stderr)
        return 1
    args.pool = str(pool_path)

    if args.rollback:
        result = rollback(Path(args.rollback), pool_path)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0 if result.get("status") == "restored" else 1

    code, result = run(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(f"模式：{result['mode']}  池：{result['pool']}（{result['pool_embedding_dim']} 维）")
        print(f"voiceprint 答案 {result['answers']} 条 → 会采纳 {result['accepted']} 条，反例 {result['negatives']} 条\n")
        for item in result["decisions"]:
            sim = "-" if item["similarity"] is None else f"{item['similarity']:.3f}"
            print(f"  [{item['decision']:<15}] {item['reason']:<18} 相似度 {sim:<6} {item['detail']}")
        if result.get("write"):
            print(f"\n写入结果：{json.dumps(result['write'], ensure_ascii=False)}")
        print(f"\n池 sha256：{result['pool_sha256_before']} → {result['pool_sha256_after']}")
    return code


if __name__ == "__main__":
    sys.exit(main())
