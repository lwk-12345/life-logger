"""CAM++（funasr）声纹后端 —— MOSS 主路径的默认声纹。

为什么是它（2026-10-14 三组真实录音实测，脚本 ``scripts/probe_campplus_*.py``）：

* 48 维手工频谱包络把 AI 技术视频判成本人（段 000024：48 维 0.650 判 self，
  CAM++ 给 -0.024）；按 MOSS 说话人合并粒度下两者的分离间隔 0.153 vs **0.440**。
* **同进程可用**：8765 worker 的 venv（``local-config.json`` 的 ``python``）里
  就有 funasr 1.3.30 —— ``moss_transcribe`` 里「MOSS 跑在独立 venv，装不进重依赖」
  那句只对 ``MOSS_PYTHON -m mlx_audio.stt.generate`` **子进程**成立，
  声纹计算本来就在调用方解释器里跑，不需要子进程。
* 成本：模型加载 ~1s（进程内缓存复用）；60 秒音频单次 forward 0.45~0.80s
  （RTF 0.008~0.017）；embedding 192 维。
* **API 形态（实测坑）**：campplus 的 ``generate`` 不接受
  ``{"raw": ndarray, "sample_rate": 16000}`` —— 会在
  ``Kaldi.fbank(au.unsqueeze(0))`` 报 ``'dict' object has no attribute 'unsqueeze'``。
  唯一可行形态是**传文件路径**，所以这里写临时 wav（stdlib ``wave``，不引新依赖），
  用完即删。

一切都可降级：权重/依赖/调用任何一步失败都只返回 ``None`` 并留下 ``load_error``，
绝不抛出去打断 ASR（调用方 ``moss_transcribe`` 会退回 48 维）。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any, Mapping, Sequence

CAMPPLUS_MODEL_REVISION = "master"
CAMPPLUS_EMBEDDING_DIM = 192
# 0.2s @16k：低于这个长度写出来的 wav 对声纹没有意义（与 48 维同门限）
CAMPPLUS_MIN_SAMPLES = 3200
CAMPPLUS_SAMPLE_RATE = 16000
CAMPPLUS_MODEL_ENV = "LIFE_LOGGER_CAMPPLUS_MODEL"

# 本机常见的权重落地位置（按命中顺序）。第一项与 local-config.json 的 campplus 键一致。
DEFAULT_CAMPPLUS_MODEL_CANDIDATES: tuple[Path, ...] = (
    Path.home()
    / ".cache/modelscope/models"
    / "iic--speech_campplus_sv_zh-cn_16k-common/snapshots/master",
    Path.home()
    / ".cache/modelscope/hub/models/iic/speech_campplus_sv_zh-cn_16k-common",
    Path.home() / ".cache/modelscope/hub/iic/speech_campplus_sv_zh-cn_16k-common",
)

_MODEL_LOCK = threading.Lock()
_MODEL_CACHE: dict[tuple[str, str], Any] = {}


class CampplusUnavailable(RuntimeError):
    """CAM++ 运行环境或权重不可用（调用方应降级到 48 维）。"""


def resolve_campplus_model_dir(
    model_dir: str | Path | None = None,
    *,
    config_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Path | None, str]:
    """解析 CAM++ 权重目录：显式参数 > 环境变量 > local-config.json > 本机默认。

    返回 ``(path|None, 来源或失败原因)``。与 ``resolve_template_path`` 同一哲学：
    **显式配置了却不存在的目录不会被悄悄替换**（换成别人的模型更糟），
    只有完全没配置时才去找本机默认位置。
    """

    environ = os.environ if env is None else env

    if model_dir is not None:
        candidate = Path(str(model_dir)).expanduser()
        if candidate.is_dir():
            return candidate, "explicit"
        return None, f"campplus_model_missing:{candidate}"

    raw_env = str(environ.get(CAMPPLUS_MODEL_ENV) or "").strip()
    if raw_env:
        candidate = Path(raw_env).expanduser()
        if candidate.is_dir():
            return candidate, "env"
        return None, f"campplus_model_missing:{candidate}"

    repo_root = Path(__file__).resolve().parents[2]
    cfg_file = (
        Path(str(config_path)).expanduser()
        if config_path is not None
        else repo_root / "local-config.json"
    )
    if cfg_file.is_file():
        configured: Path | None = None
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg = None
        if isinstance(cfg, Mapping):
            for key in ("campplus", "speech_separation_model"):
                raw = str(cfg.get(key) or "").strip()
                if raw and Path(raw).expanduser().is_dir():
                    configured = Path(raw).expanduser()
                    break
            else:
                # 老配置把权重目录写在 speaker 里，也可能是 "cam++"/"local_campplus" 这类别名
                raw = str(cfg.get("speaker") or "").strip()
                if raw and Path(raw).expanduser().is_dir():
                    configured = Path(raw).expanduser()
        if configured is not None:
            return configured, "local-config"

    for candidate in DEFAULT_CAMPPLUS_MODEL_CANDIDATES:
        if candidate.is_dir():
            return candidate, "default"
    return None, f"campplus_model_missing:{DEFAULT_CAMPPLUS_MODEL_CANDIDATES[0]}"


def resolve_campplus_onnx_path(model_dir: Path | str | None = None) -> Path | None:
    """寻找 CAM++ 的 ONNX 导出权重（sherpa-onnx 极速后端用）。"""
    raw_env = os.environ.get("LIFE_LOGGER_CAMPPLUS_ONNX_MODEL", "").strip()
    if raw_env and Path(raw_env).expanduser().is_file():
        return Path(raw_env).expanduser()

    if model_dir is not None:
        p = Path(str(model_dir)).expanduser()
        for name in ("campplus.onnx", "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"):
            candidate = p / name
            if candidate.is_file():
                return candidate
    return None


def _import_automodel(model_dir: Path) -> Any:
    """加载 funasr AutoModel（进程内缓存，加锁）。

    这是本模块唯一的 funasr 接触点：测试与降级路径都替换/捕获它。
    """

    key = (str(model_dir), CAMPPLUS_MODEL_REVISION)
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            from funasr import AutoModel
        except Exception as exc:  # pragma: no cover - 依赖缺失时的降级路径
            raise CampplusUnavailable(
                f"funasr_unavailable:{type(exc).__name__}"
            ) from exc
        try:
            model = AutoModel(
                model=str(model_dir),
                model_revision=CAMPPLUS_MODEL_REVISION,
                disable_update=True,
            )
        except Exception as exc:  # pragma: no cover - 权重损坏时的降级路径
            raise CampplusUnavailable(
                f"campplus_load_failed:{type(exc).__name__}"
            ) from exc
        _MODEL_CACHE[key] = model
        return model


def _write_pcm16_wav(path: Path, samples: Any, sample_rate: int) -> None:
    import numpy as np

    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    pcm = np.clip(arr * 32767.0, -32768.0, 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())


def _normalize_embedding(raw: Any) -> list[float] | None:
    """把模型吐出的 embedding 归一化（cosine 必须与模板同尺度）。"""

    import numpy as np

    try:
        vector = np.asarray(raw, dtype=np.float32).reshape(-1)
    except Exception:
        return None
    if vector.size == 0:
        return None
    norm = float(np.linalg.norm(vector))
    if not norm > 1e-9:
        return None
    return (vector / norm).tolist()


class CampplusEmbedder:
    """CAM++ 说话人嵌入器：``embed(samples) -> 192 维单位向量 | None``。

    一个实例一份模型（模块级缓存跨实例共享）；线程安全只保证加载阶段，
    推理沿用 funasr 自身行为。
    """

    name = "campplus"
    embedding_dim = CAMPPLUS_EMBEDDING_DIM

    def __init__(
        self,
        model_dir: str | Path,
        *,
        sample_rate: int = CAMPPLUS_SAMPLE_RATE,
        min_samples: int = CAMPPLUS_MIN_SAMPLES,
        automodel: Any = None,
    ) -> None:
        self.model_dir = Path(str(model_dir)).expanduser()
        self.sample_rate = int(sample_rate)
        self.min_samples = int(min_samples)
        self.load_error: str | None = None
        self._automodel = automodel
        self._model: Any = None
        self._sherpa_extractor: Any = None

    def _load(self) -> Any:
        if self._automodel is not None:
            return self._automodel
        if self._sherpa_extractor is not None:
            return self._sherpa_extractor
        if self._model is not None:
            return self._model
        if self.load_error is not None:
            return None

        # 优先使用 sherpa-onnx 极速运行时（27MB，RTF 0.01，内存纯享）
        onnx_path = resolve_campplus_onnx_path(self.model_dir)
        if onnx_path is not None:
            try:
                import sherpa_onnx
                cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=str(onnx_path), num_threads=4, debug=False
                )
                self._sherpa_extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
                return self._sherpa_extractor
            except Exception:
                self._sherpa_extractor = None

        try:
            self._model = _import_automodel(self.model_dir)
        except CampplusUnavailable as exc:
            self.load_error = str(exc)
            return None
        except Exception as exc:  # pragma: no cover - 兜底
            self.load_error = f"campplus_load_failed:{type(exc).__name__}"
            return None
        return self._model

    @property
    def available(self) -> bool:
        return self._load() is not None

    def embed(self, samples: Any) -> list[float] | None:
        """算一段音频的 CAM++ embedding；任何失败都返回 None。"""

        import numpy as np

        if not self.available:
            return None
        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        if arr.size < self.min_samples:
            return None

        # 极速路径：sherpa-onnx 内存直接前向（免临时文件写盘，10~25ms 出结果）
        if self._sherpa_extractor is not None:
            try:
                st = self._sherpa_extractor.create_stream()
                st.accept_waveform(self.sample_rate, arr)
                st.input_finished()
                raw_emb = self._sherpa_extractor.compute(st)
                return _normalize_embedding(raw_emb)
            except Exception as exc:
                self.load_error = f"sherpa_campplus_compute_failed:{type(exc).__name__}"
                # 异常时自动降级到下方的文件路径

        # 传文件路径是唯一实测可行的调用形态（dict 形式会炸 Kaldi.fbank）
        tmp = Path(tempfile.mkdtemp(prefix="ll-campplus-"))
        try:
            wav_path = tmp / "window.wav"
            try:
                _write_pcm16_wav(wav_path, arr, self.sample_rate)
            except Exception as exc:
                self.load_error = f"campplus_wav_write_failed:{type(exc).__name__}"
                return None
            model = self._load()
            if model is None:
                return None
            try:
                result = model.generate(input=str(wav_path), extract_embedding=True)
            except Exception as exc:
                self.load_error = f"campplus_generate_failed:{type(exc).__name__}"
                return None
            return _normalize_embedding(_extract_spk_embedding(result))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _extract_spk_embedding(result: Any) -> Any:
    """从 funasr 返回值里取 embedding（形如 ``[{"spk_embedding": ndarray}]``）。"""

    if isinstance(result, Mapping):
        return result.get("spk_embedding")
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes, bytearray)):
        for item in result:
            value = _extract_spk_embedding(item)
            if value is not None:
                return value
    return None


def load_campplus_embedder(
    model_dir: str | Path | None = None,
    *,
    config_path: str | Path | None = None,
    sample_rate: int = CAMPPLUS_SAMPLE_RATE,
    env: Mapping[str, str] | None = None,
) -> CampplusEmbedder:
    """建一个**确认可用**的 CAM++ 嵌入器；不可用则抛 :class:`CampplusUnavailable`。

    确认可用 = 模型真的加载起来了。调用方据此决定走 CAM++ 还是退回 48 维。
    """

    resolved, why = resolve_campplus_model_dir(
        model_dir, config_path=config_path, env=env
    )
    if resolved is None:
        raise CampplusUnavailable(why)
    embedder = CampplusEmbedder(resolved, sample_rate=sample_rate)
    if not embedder.available:
        raise CampplusUnavailable(embedder.load_error or "campplus_unavailable")
    return embedder


__all__ = [
    "CAMPPLUS_EMBEDDING_DIM",
    "CAMPPLUS_MIN_SAMPLES",
    "CAMPPLUS_MODEL_ENV",
    "CAMPPLUS_SAMPLE_RATE",
    "CampplusEmbedder",
    "CampplusUnavailable",
    "DEFAULT_CAMPPLUS_MODEL_CANDIDATES",
    "load_campplus_embedder",
    "resolve_campplus_model_dir",
]
