"""正式转写常驻 worker：MOSS / CAM++ 只加载一次。

为什么需要：现在每一段 60 秒录音都会冷启动两层子进程
（`run_cloud_asr_pipeline.py --provider moss` → `python -m mlx_audio.stt.generate`），
实测模型加载 2s（内存紧张时 21s），吞吐刚好卡在 58 段/小时 = 录音速度，积压降不下来。

设计：
* **协议**：stdin/stdout 每行一个 JSON（与 `vip_fast_track.py` 的常驻子进程同风格），
  请求 ``{"id": "...", "wav": "/path", "outdir": "/path", "hotwords": null}``，
  响应 ``{"id": "...", "ok": true, "result": {...}}`` 或 ``{"id": "...", "ok": false, "error": "..."}``。
* **模型只加载一次**：``load_moss()`` / ``load_campplus()`` 由调用方注入；生产实现
  ``_load_moss_inprocess()`` 用 mlx_audio 的进程内 API，测试注入假实现。
* **健壮性**：客户端在 worker 崩溃/超时后自动重启一次，仍失败就抛
  ``ResidentUnavailable``，调用方据此**退回旧的每段子进程路径**（不改任何产物格式）。
* **开关**：``LIFE_LOGGER_ASR_RESIDENT``（默认 "1"）；关掉即完全不进这条路径。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

RESIDENT_ENV = "LIFE_LOGGER_ASR_RESIDENT"
#: 单段转写超时（与 watch_formal_asr 的 process_timeout_seconds 一致）
DEFAULT_TIMEOUT_SECONDS = 900.0

#: 重复生成判定：同一短语连续重复超过这个次数就截断。
#: 依据：60 秒中文正常语音里，同一 4~12 字短语连续重复通常 ≤3 次（口头禅/强调）；
#: 实测 MOSS 失控时会把同一句或单字（如「我」）重复几十上百次直到 max-tokens=4096。
DEFAULT_MAX_REPEAT = 6
DEFAULT_MIN_UNIT_CHARS = 4
SHORT_SLICE_SECONDS = 20.0
_TAG_ONLY_RE = re.compile(r"\[S\d+\]|\[\d+(?:\.\d+)?\]|\[(?:S\d*|\d+(?:\.\d*)?)?\s*$")
_LEADING_SPEAKER_RE = re.compile(r"^\s*(\[S\d+\])\s*")
_NORM_PUNCT_RE = re.compile(r"[\s，。！？、,.!?；;：:…—\-~～\"'“”‘’（）()]+")
_RAW_SPAN_RE = re.compile(
    r"(?:\[\d+(?:\.\d+)?\])*\[S\d+\][^\[S]*?(?:\[\d+(?:\.\d+)?\])+"
)


def resident_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    value = str(source.get(RESIDENT_ENV, "1")).strip().lower()
    return value not in {"0", "false", "no", "off"}


class ResidentUnavailable(RuntimeError):
    """常驻 worker 不可用（崩溃/超时/未启动）。调用方据此回退旧路径。"""


# --------------------------------------------------------------------- 重复截断


def _norm_core_text(text: str) -> str:
    stripped = _TAG_ONLY_RE.sub(" ", str(text or ""))
    return _NORM_PUNCT_RE.sub("", stripped).strip().lower()


def collapse_repetition(
    text: str,
    *,
    max_repeat: int = DEFAULT_MAX_REPEAT,
    min_unit_chars: int = DEFAULT_MIN_UNIT_CHARS,
) -> tuple[str, list[str]]:
    """把「同一短语或单字连续重复过多」的尾巴截掉，返回 (文本, 警告列表)。

    只处理**连续**重复；正常段（重复 ≤ max_repeat）原样返回，一个字符都不改。
    同时识别 MOSS 原始时间戳串（如 `[5.14][S02]我[5.64][5.64][S02]我[6.14]...` 或空文本时间戳死循环）。
    """
    raw = str(text or "")
    if not raw or max_repeat < 2:
        return raw, []
    warnings: list[str] = []

    # 1) 检查带时间戳/说话人标签的原始 MOSS 串（时间戳递增导致纯字符比较看不出重复）
    spans = list(_RAW_SPAN_RE.finditer(raw))
    if len(spans) > max_repeat:
        run_norm: str | None = None
        run_count = 0
        cut_pos: int | None = None
        empty_count = 0
        for m in spans:
            norm = _norm_core_text(m.group(0))
            if not norm:
                empty_count += 1
                if empty_count > max_repeat and cut_pos is None:
                    cut_pos = m.start()
                    warnings.append(f"empty_timestamp_loop_truncated:x{len(spans)}")
                continue
            if norm == run_norm:
                run_count += 1
                if run_count == max_repeat + 1 and cut_pos is None:
                    cut_pos = m.start()
            else:
                if run_count > max_repeat and run_norm is not None:
                    warnings.append(f"repetition_truncated:{run_norm[:12]}x{run_count}")
                    break
                run_norm = norm
                run_count = 1
        if run_count > max_repeat and run_norm is not None and not any(
            w.startswith("repetition_truncated:") for w in warnings
        ):
            warnings.append(f"repetition_truncated:{run_norm[:12]}x{run_count}")
        if cut_pos is not None:
            return raw[:cut_pos].rstrip(), warnings

        # 1b) 多句循环复读（如 2~6 句固定台词循环重复 ≥3 轮）
        non_empty_spans = [
            (m, _norm_core_text(m.group(0))) for m in spans
        ]
        non_empty_spans = [(m, n) for m, n in non_empty_spans if n]
        for k in range(2, 7):
            min_cycles = max(3, (max_repeat + k - 1) // k)
            if len(non_empty_spans) < k * min_cycles:
                continue
            for idx in range(len(non_empty_spans) - k * min_cycles + 1):
                pattern = [n for _, n in non_empty_spans[idx : idx + k]]
                if len(set(pattern)) < 2:
                    continue
                cycles = 1
                while idx + (cycles + 1) * k <= len(non_empty_spans):
                    nxt = [
                        n
                        for _, n in non_empty_spans[
                            idx + cycles * k : idx + (cycles + 1) * k
                        ]
                    ]
                    if nxt == pattern:
                        cycles += 1
                    else:
                        break
                if cycles >= min_cycles:
                    cycle_cut = non_empty_spans[idx + k][0].start()
                    warnings.append(
                        f"multi_span_repetition_truncated:{pattern[0][:10]}_k{k}x{cycles}"
                    )
                    return raw[:cycle_cut].rstrip(), warnings

    # 2) 常规连续子串重复（先按调用方 min_unit_chars 扫，再补扫 1..min_unit_chars-1 单字/短词复读）
    unit_orders = list(range(max(1, min_unit_chars), 41)) + list(
        range(1, max(1, min_unit_chars))
    )
    for unit in unit_orders:
        if len(raw) < unit * (max_repeat + 1):
            continue
        index = 0
        while index + unit * (max_repeat + 1) <= len(raw):
            piece = raw[index : index + unit]
            count = 1
            while raw[index + count * unit : index + (count + 1) * unit] == piece:
                count += 1
            if count > max_repeat:
                cut = index + max_repeat * unit
                warnings.append(f"repetition_truncated:{piece[:12]}x{count}")
                return raw[:cut].rstrip(), warnings
            index += 1
    return raw, warnings


def collapse_segment_repetitions(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_repeat: int = DEFAULT_MAX_REPEAT,
) -> tuple[list[dict[str, Any]], list[str]]:
    """对 MOSS 输出的 segments/sentences 列表做跨段复读与空时间戳死循环截断（保留原时间戳）。"""
    if not rows:
        return [], []
    cleaned_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    run_norm: str | None = None
    run_count = 0
    empty_dropped = 0

    for item in rows:
        if not isinstance(item, Mapping):
            continue
        row = dict(item)
        raw_text = str(row.get("text") or "")
        lead_match = _LEADING_SPEAKER_RE.match(raw_text)
        lead_spk = lead_match.group(1) if lead_match else ""
        core = _TAG_ONLY_RE.sub(" ", raw_text)
        core = re.sub(r"\s+", " ", core).strip()
        if not core:
            empty_dropped += 1
            continue
        from life_logger.moss_transcribe import _MOSS_META_HALLUCINATION_RE
        if _MOSS_META_HALLUCINATION_RE.search(core):
            warnings.append(f"prompt_leak_hallucination_dropped:{core[:18]}")
            continue
        core_cleaned, inner_warns = collapse_repetition(
            core, max_repeat=max_repeat, min_unit_chars=1
        )
        if inner_warns:
            warnings.extend(inner_warns)
            row["text"] = f"{lead_spk} {core_cleaned}".strip() if lead_spk else core_cleaned
        norm = _norm_core_text(core_cleaned)
        if not norm:
            empty_dropped += 1
            continue
        # 多字短语（≥3字，如「那个地方」）连续重复超过 3 次即视为复读；单字/双字（如「我」）按 max_repeat（6 次）
        limit = min(max_repeat, 3) if len(norm) >= 3 else max_repeat
        if norm == run_norm:
            run_count += 1
            if run_count <= limit:
                cleaned_rows.append(row)
        else:
            if run_norm is not None and run_count > (
                min(max_repeat, 3) if len(run_norm) >= 3 else max_repeat
            ):
                warnings.append(f"segment_repetition_truncated:{run_norm[:12]}x{run_count}")
            run_norm = norm
            run_count = 1
            cleaned_rows.append(row)

    if run_norm is not None and run_count > (
        min(max_repeat, 3) if len(run_norm) >= 3 else max_repeat
    ):
        warnings.append(f"segment_repetition_truncated:{run_norm[:12]}x{run_count}")
    if empty_dropped:
        warnings.append(f"empty_timestamp_segments_dropped:x{empty_dropped}")

    # 多句循环复读截断（如 2~6 句固定台词跨段循环重复 ≥3 轮，仅保留首轮）
    norms = [_norm_core_text(str(r.get("text") or "")) for r in cleaned_rows]
    for k in range(2, 7):
        min_cycles = 3
        if len(cleaned_rows) < k * min_cycles:
            continue
        truncated = False
        for idx in range(len(cleaned_rows) - k * min_cycles + 1):
            pattern = norms[idx : idx + k]
            if not all(pattern) or len(set(pattern)) < 2:
                continue
            cycles = 1
            while idx + (cycles + 1) * k <= len(cleaned_rows):
                if norms[idx + cycles * k : idx + (cycles + 1) * k] == pattern:
                    cycles += 1
                else:
                    break
            if cycles >= min_cycles:
                cleaned_rows = cleaned_rows[: idx + k] + cleaned_rows[idx + cycles * k :]
                warnings.append(
                    f"multi_segment_cycle_truncated:{pattern[0][:10]}_k{k}x{cycles}"
                )
                truncated = True
                break
        if truncated:
            norms = [_norm_core_text(str(r.get("text") or "")) for r in cleaned_rows]

    return cleaned_rows, list(dict.fromkeys(warnings))


def collapse_payload_repetition(
    payload: Mapping[str, Any],
    *,
    max_repeat: int = DEFAULT_MAX_REPEAT,
) -> tuple[dict[str, Any], list[str]]:
    """同时清洗 payload 的 text 与 segments/sentences，并汇总 warnings。"""
    out = dict(payload or {})
    warnings: list[str] = list(out.get("warnings") or [])
    text = str(out.get("text") or "")
    cleaned_text, text_warns = collapse_repetition(text, max_repeat=max_repeat)
    if text_warns:
        out["text"] = cleaned_text
        warnings.extend(text_warns)
    for key in ("segments", "sentences"):
        rows = out.get(key)
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
            cleaned_rows, seg_warns = collapse_segment_repetitions(
                rows, max_repeat=max_repeat
            )
            if seg_warns:
                out[key] = cleaned_rows
                warnings.extend(seg_warns)
    dedup_warns = list(dict.fromkeys(warnings))
    if dedup_warns:
        out["warnings"] = dedup_warns
    return out, dedup_warns


# --------------------------------------------------------------------- worker（服务端）


@dataclass
class ResidentWorker:
    """常驻进程主体。模型由 `moss_loader` / `campplus_loader` 注入（测试用假实现）。"""

    moss_loader: Callable[[], Any] | None = None
    campplus_loader: Callable[[], Any] | None = None
    transcribe_fn: Callable[..., dict[str, Any]] | None = None
    _moss: Any = field(default=None, repr=False)
    _campplus: Any = field(default=None, repr=False)
    loaded: bool = False

    def ensure_loaded(self) -> None:
        if self.loaded:
            return
        if self.moss_loader is not None:
            self._moss = self.moss_loader()
        if self.campplus_loader is not None:
            self._campplus = self.campplus_loader()
        self.loaded = True

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        rid = str(request.get("id") or "")
        try:
            self.ensure_loaded()
            fn = self.transcribe_fn or _default_transcribe
            result = fn(
                wav=Path(str(request.get("wav") or "")),
                outdir=Path(str(request.get("outdir") or "")),
                hotwords=request.get("hotwords"),
                moss=self._moss,
                campplus=self._campplus,
            )
            cleaned_result, warnings = collapse_payload_repetition(result or {})
            if warnings:
                result = cleaned_result
            return {"id": rid, "ok": True, "result": result}
        except Exception as error:  # noqa: BLE001 - 单段失败不能打死常驻 worker
            return {"id": rid, "ok": False, "error": f"{type(error).__name__}: {error}"}

    def serve(self, stdin: Any = None, stdout: Any = None) -> None:
        src = stdin or sys.stdin
        dst = stdout or sys.stdout
        for line in src:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                dst.write(json.dumps({"ok": False, "error": "bad_json"}) + "\n")
                dst.flush()
                continue
            if str(request.get("cmd") or "") == "shutdown":
                dst.write(json.dumps({"ok": True, "bye": True}) + "\n")
                dst.flush()
                return
            dst.write(json.dumps(self.handle(request), ensure_ascii=False) + "\n")
            dst.flush()


#: 与旧 CLI 路径一致的硬上限（防重复生成烧 GPU；见 moss_transcribe._run_moss 注释）。
MAX_NEW_TOKENS = 4096


def _output_payload(output: Any) -> dict[str, Any]:
    """把 mlx_audio 的 STTOutput 序列化成旧 CLI `--format json` 的同一形状。

    旧进程产物是 ``{"text": ..., "sentences": [...]}``（带说话人）或
    ``{"text": ..., "segments": [...]}``；下游 `moss_transcribe` 只读
    ``segments``/``sentences`` 里的 text/start/end/speaker_id。
    """
    if hasattr(output, "sentences"):
        sentences = []
        for sentence in output.sentences:
            row = {"text": str(getattr(sentence, "text", "")),
                   "start": float(getattr(sentence, "start", 0.0) or 0.0),
                   "end": float(getattr(sentence, "end", 0.0) or 0.0)}
            speaker = getattr(sentence, "speaker_id", None)
            if speaker is not None:
                row["speaker_id"] = speaker
            sentences.append(row)
        return {"text": str(getattr(output, "text", "")), "sentences": sentences}
    rows = []
    for segment in getattr(output, "segments", []) or []:
        row = {"text": str(segment.get("text") or ""),
               "start": float(segment.get("start") or 0.0),
               "end": float(segment.get("end") or 0.0),
               "duration": float(segment.get("end") or 0.0) - float(segment.get("start") or 0.0)}
        if segment.get("speaker_id") is not None:
            row["speaker_id"] = segment["speaker_id"]
        rows.append(row)
    return {"text": str(getattr(output, "text", "")), "segments": rows}


def _wav_duration_seconds(wav: Path) -> float:
    try:
        with wave.open(str(wav), "rb") as wf:
            rate = wf.getframerate()
            return (wf.getnframes() / float(rate)) if rate > 0 else 0.0
    except Exception:
        return 0.0


def _slice_wav_file(wav: Path, slice_seconds: float = SHORT_SLICE_SECONDS) -> tuple[Path, list[tuple[Path, float]]]:
    tmpdir = Path(tempfile.mkdtemp(prefix="ll-moss-slice-"))
    slices: list[tuple[Path, float]] = []
    with wave.open(str(wav), "rb") as wf:
        params = wf.getparams()
        rate = wf.getframerate()
        total_frames = wf.getnframes()
        frames_per_slice = int(slice_seconds * rate)
        offset_frames = 0
        idx = 0
        min_tail_frames = int(1.0 * rate)
        while offset_frames < total_frames:
            if idx > 0 and (total_frames - offset_frames) < min_tail_frames:
                break
            wf.setpos(offset_frames)
            chunk_bytes = wf.readframes(min(frames_per_slice, total_frames - offset_frames))
            if not chunk_bytes:
                break
            sp = tmpdir / f"slice-{idx:03d}.wav"
            with wave.open(str(sp), "wb") as out_wf:
                out_wf.setparams(params)
                out_wf.writeframes(chunk_bytes)
            slices.append((sp, round(offset_frames / float(rate), 3)))
            offset_frames += frames_per_slice
            idx += 1
    return tmpdir, slices


def _needs_slice_retranscribe(warnings: Sequence[str]) -> bool:
    """判断复读告警是否需要切 20s 短片重转写。

    纯静音/环境音段常见的单字语气词重复（如 `segment_repetition_truncated:嗯x6`）
    已在段级折叠为最多保留 3 条，不需要切 20s 重跑；只有真正的词句死循环、字符级
    死循环或大量空时间戳段才切 20s 重转写。
    """
    for w in warnings or []:
        if w.startswith(("char_run_truncated:", "phrase_repeat_truncated:")):
            return True
        if w.startswith("empty_timestamp_segments_dropped:x"):
            try:
                if int(w.rsplit("x", 1)[1]) >= 6:
                    return True
            except ValueError:
                return True
        if w.startswith("segment_repetition_truncated:"):
            body = w.split(":", 1)[1]
            phrase, _, cnt_s = body.rpartition("x")
            try:
                cnt = int(cnt_s)
            except ValueError:
                cnt = 99
            if len(phrase) >= 2 or cnt >= 10:
                return True
    return False


def _generate_moss_with_penalty(moss: Any, wav_path: Path, prompt: str | None) -> Any:
    return moss.generate(str(wav_path), max_tokens=MAX_NEW_TOKENS, prompt=prompt)


def _default_transcribe(*, wav: Path, outdir: Path, hotwords: Any, moss: Any, campplus: Any) -> dict[str, Any]:
    """进程内调用已加载的 MOSS；命中复读死循环时自动切 20s 短片重转写并保底截断。"""
    if moss is None:
        raise ResidentUnavailable("MOSS model is not loaded")
    from life_logger.moss_transcribe import moss_prompt
    import shutil

    prompt = moss_prompt(str(hotwords) if hotwords else None)
    output = moss.generate(str(wav), max_tokens=MAX_NEW_TOKENS, prompt=prompt)
    payload = _output_payload(output)
    cleaned, warnings = collapse_payload_repetition(payload)
    if not warnings:
        return payload
    if not _needs_slice_retranscribe(warnings):
        return cleaned

    dur = _wav_duration_seconds(wav)
    if dur > SHORT_SLICE_SECONDS + 5.0 and wav.exists():
        tmpdir: Path | None = None
        try:
            tmpdir, slices = _slice_wav_file(wav, SHORT_SLICE_SECONDS)
            merged_segments: list[dict[str, Any]] = []
            merged_texts: list[str] = []
            slice_warnings: list[str] = list(warnings) + ["repetition_retranscribed_20s"]
            key = "sentences" if "sentences" in payload else "segments"
            for slice_path, offset_sec in slices:
                sub_out = _generate_moss_with_penalty(moss, slice_path, None)
                sub_payload, sub_warns = collapse_payload_repetition(_output_payload(sub_out))
                slice_warnings.extend(sub_warns)
                if sub_payload.get("text"):
                    merged_texts.append(str(sub_payload["text"]))
                for seg in sub_payload.get("segments") or sub_payload.get("sentences") or []:
                    if not isinstance(seg, Mapping):
                        continue
                    st = round(float(seg.get("start") or 0.0) + offset_sec, 3)
                    en = round(float(seg.get("end") or 0.0) + offset_sec, 3)
                    shifted = {
                        **seg,
                        "start": st,
                        "end": en,
                        "duration": round(max(0.0, en - st), 3),
                    }
                    merged_segments.append(shifted)
            rebuilt = {
                "text": "".join(merged_texts),
                key: merged_segments,
                "warnings": list(dict.fromkeys(slice_warnings)),
            }
            final_payload, _ = collapse_payload_repetition(rebuilt)
            return final_payload
        except Exception as exc:
            cleaned["warnings"] = list(
                dict.fromkeys(list(warnings) + [f"retranscribe_20s_failed:{type(exc).__name__}"])
            )
            return cleaned
        finally:
            if tmpdir is not None:
                shutil.rmtree(tmpdir, ignore_errors=True)

    return cleaned


def load_moss_model() -> Any:
    """只在 MOSS venv 里 import mlx_audio；BF16 权重按原 dtype 加载，不做量化。"""
    import importlib

    # 动态导入：主服务 venv 没有 mlx_audio，模块顶层/静态 AST 里也不能出现它。
    stt = importlib.import_module("mlx_audio.stt")
    from life_logger.moss_transcribe import MOSS_MODEL

    return stt.load(MOSS_MODEL)


# --------------------------------------------------------------------- client（调用方）


class ResidentClient:
    """管理常驻子进程：自动启动、超时重启一次、失败抛 ResidentUnavailable。"""

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        popen: Callable[..., Any] = subprocess.Popen,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._env = dict(env) if env is not None else None
        if command is None:
            from life_logger.moss_transcribe import MOSS_PYTHON

            python = MOSS_PYTHON if Path(MOSS_PYTHON).exists() else sys.executable
            command = [python, "-m", "life_logger.asr_resident", "--moss"]
        self._command = list(command)
        self._timeout = float(timeout_seconds)
        self._popen = popen
        self._proc: Any = None
        self._lock = threading.Lock()

    def _ensure(self) -> Any:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        env = self._env
        if env is None:
            repo_src = str(Path(__file__).resolve().parents[1])
            env = dict(os.environ, PYTHONPATH=repo_src + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
                       HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        self._proc = self._popen(
            self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
        )
        return self._proc

    def transcribe(self, *, wav: Path, outdir: Path, hotwords: str | None = None,
                   request_id: str = "seg") -> dict[str, Any]:
        payload = json.dumps({"id": request_id, "wav": str(wav), "outdir": str(outdir),
                              "hotwords": hotwords}, ensure_ascii=False)
        with self._lock:
            for attempt in (1, 2):
                try:
                    proc = self._ensure()
                    proc.stdin.write(payload + "\n")
                    proc.stdin.flush()
                    line = _readline_with_timeout(proc, self._timeout)
                    if not line:
                        raise TimeoutError("resident worker 无响应")
                    reply = json.loads(line)
                    if not reply.get("ok"):
                        raise ResidentUnavailable(str(reply.get("error") or "worker_error"))
                    return dict(reply.get("result") or {})
                except (TimeoutError, ResidentUnavailable, OSError, ValueError):
                    self.close()
                    if attempt == 2:
                        raise ResidentUnavailable("resident worker 崩溃或超时（已重启一次）") from None
        raise ResidentUnavailable("unreachable")

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _readline_with_timeout(proc: Any, timeout: float) -> str:
    """带超时的 readline（POSIX 用 select；不可用时退化为阻塞读）。"""
    import select

    try:
        proc.stdout.fileno()
    except (AttributeError, OSError, ValueError, Exception):  # 非真实管道（测试替身）：直接读
        return proc.stdout.readline()
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise TimeoutError("timeout")
    return proc.stdout.readline()


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--moss", action="store_true", help="启动时加载 MOSS（进程内，BF16）")
    args = parser.parse_args(argv)
    if args.moss:
        moss = load_moss_model()
        ResidentWorker(moss_loader=lambda: moss).serve()
    else:
        ResidentWorker().serve()
    return 0


if __name__ == "__main__":  # pragma: no cover - 常驻进程入口
    raise SystemExit(main())
