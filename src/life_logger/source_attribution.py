"""Conservative speaker-source decisions for downstream Life Logger extraction."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from math import isfinite, log10, sqrt
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

SourceLabel = Literal["self", "non_self", "unknown"]
ProximityLabel = Literal["near", "far", "uncertain"]
ReviewPriority = Literal["high", "medium", "low"]
ReviewLayer = Literal["focus", "standard", "background"]

IMPORTANT_EVENT_MARKERS = (
    "买了",
    "刚买",
    "已经买",
    "付款",
    "支付",
    "花了",
    "花费",
    "消费",
    "提醒我",
    "记得",
    "待办",
    "身体不舒服",
    "身体",
    "不舒服",
    "胃部",
    "想吐",
    "恶心",
    "血糖",
    "头痛",
    "头晕",
    "疼",
    "发烧",
    "过敏",
    "情绪",
    "健身",
    "锻炼",
    "训练",
    "运动",
    "深蹲",
    "卧推",
    "硬拉",
    "练腿",
    "杠铃",
    "哑铃",
    "器械",
    "千克",
    "公斤",
    "斤",
    "一组",
    "爬坡",
    "跑步",
    "骑行",
    "健身房",
    "吃饭",
    "吃了",
    "早餐",
    "午餐",
    "晚餐",
    "喝了",
    "饮食",
    "作息",
    "睡觉",
    "睡眠",
    "减肥",
    "体重",
    "戒烟",
    "抽烟",
    "工作",
    "上班",
    "项目",
    "开会",
    "工资",
    "发票",
    "花钱",
    "消费记录",
    "计划",
)
# 兼容旧名：多处代码仍按私有名引用
_IMPORTANT_UNKNOWN_MARKERS = IMPORTANT_EVENT_MARKERS

# --- 近场/远场 → 声纹相似度的调整（transcribe.py 与 moss_transcribe.py 共用）---
#
# 远场**不是**「不是本人」的证据：手表戴在手腕上，嘴到手腕 30~50cm，
# 用户自己正常说话测出来就是 far。旧行为把远场段的相似度封顶 0.18
# （实测 far 段 100% 判 non_self），等于「听起来远 → 一票否决是我说的」。
# 现在只按声学退化降权 20%，并且把该段标成「需复核」交给人工。
FAR_FIELD_SIMILARITY_DAMPING = 0.8
# 备用远场判据：近讲低频比低于该值说明没有近讲效应，同样按远场处理。
FAR_FIELD_PROXIMITY_RATIO_DB = -1.5
# 近场加分（把 raw 抬到 FLOOR，只在 raw 已具备最低可信度时生效）。
# 它不是身份证据，而是**可信性前提**：声纹模板是近场录的，只有近场录音的
# 分数才与模板条件可比。低于 MIN_RAW 或低频比不达标时一律不加分。
NEAR_FIELD_SIMILARITY_FLOOR = 0.55
NEAR_FIELD_FLOOR_MIN_RAW_SIMILARITY = 0.30
NEAR_FIELD_FLOOR_MIN_PROXIMITY_RATIO_DB = 4.0


def proximity_adjusted_similarity(
    raw_similarity: float,
    *,
    prox_label: str | None,
    proximity_ratio_db: float | None,
    far_field_damping: float = FAR_FIELD_SIMILARITY_DAMPING,
    near_field_floor: float = NEAR_FIELD_SIMILARITY_FLOOR,
    near_field_floor_min_raw: float = NEAR_FIELD_FLOOR_MIN_RAW_SIMILARITY,
    near_field_floor_min_ratio_db: float = NEAR_FIELD_FLOOR_MIN_PROXIMITY_RATIO_DB,
) -> tuple[float, bool]:
    """按近场/远场证据调整声纹相似度，返回 ``(相似度, 是否强制人工复核)``。

    三条分支（两条路径上的同一段逻辑，必须保持一致）：

    1. **远场**（``far`` 或低频比 < :data:`FAR_FIELD_PROXIMITY_RATIO_DB`）：
       只乘 ``far_field_damping``（默认 0.8）做声学退化降权，
       **不再封顶**；但因为远场下分数不可比，必须标 ``review_required``。
    2. **近场 + 低频强**：``max(raw, near_field_floor)`` —— 信道
       补偿（模板条件可比），不是身份证据。``near_field_floor <= 0`` 时
       该分支关闭（CAM++ 这类已经学过信道的后端不需要补偿）。
    3. **其余**（含 ``uncertain``）：裸值，交给阈值分类。

    身份结论永远由 ``classify_similarity`` 的阈值做，本函数只负责把「录远了」
    与「不像本人」分开，避免远场一票否决。可调常数由调用方按声纹后端传入
    （见 :class:`VoiceprintPolicy`）—— 换后端必须换整套常数，不能只换阈值。
    """

    raw = float(raw_similarity)
    if not isfinite(raw):
        raise ValueError("raw_similarity must be finite")
    label = str(prox_label or "uncertain")
    ratio: float | None = None
    if proximity_ratio_db is not None and isfinite(float(proximity_ratio_db)):
        ratio = float(proximity_ratio_db)

    if label == "far" or (ratio is not None and ratio < FAR_FIELD_PROXIMITY_RATIO_DB):
        return raw * float(far_field_damping), True
    if (
        float(near_field_floor) > 0.0
        and label == "near"
        and ratio is not None
        and ratio >= float(near_field_floor_min_ratio_db)
        and raw >= float(near_field_floor_min_raw)
    ):
        return max(raw, float(near_field_floor)), False
    return raw, False


def has_important_signal(text: str) -> bool:
    """文本里是否含「不能静默丢弃」的个人生活主题信号。

    dictation 剔除时用它做保险：即使一句话落在「对着电脑说话」的时间窗里，
    只要它涉及重要生活主题，也保留下来送人工复核，而不是直接删掉。
    用户明确要求过「没买没去那些东西也很重要，为什么要去过滤掉」。
    """
    if not text:
        return False
    return any(marker in text for marker in IMPORTANT_EVENT_MARKERS)

# --- 生活关键词豁免（Q961 之后的 ⑥ 单）--------------------------------------
#
# 问题：声纹判 unknown 的句子，如果只是「没被确认是本人」，下游没有任何理由把它
# 当成媒体/他人内容折叠掉。用户的诉求是「先别丢，放到'需要我判断'里」。
#
# 判据只用**已有的**声纹结论，不新增/不改阈值（那属于 far_field_damping 调研）：
#   * `self`                → 不算豁免场景（本来就保留）；
#   * `unknown`             → 一律豁免：unknown 的定义就是「没结论」，不能当否证用；
#   * `non_self` + 需复核    → 豁免：复核带里的判定本来就不确定；
#   * `non_self` + 明确他人  → **不**豁免（例如 strong_personal_negative_match，
#                              或分数低于 unknown_threshold 且无需复核）—— 这句
#                              确实不是本人说的，含「花了」也只能作为他人内容待确认。
#
# 「明确他人」的阈值直接复用 `AttributionConfig.unknown_threshold`（默认 0.25）：
# 该常量本身就是「低于此分 → 判 non_self」的分界线，不另造数字。

#: 带这些决策理由的 non_self 属于「还不确定是谁」，不是「明确是他人」。
UNCERTAIN_OTHER_DECISION_REASONS = frozenset({
    "similarity_in_review_band",
    "short_segment_low_similarity_review",
    "multi_evidence_far_low_similarity",
    "context_supported_self",
})


def is_confirmed_other(
    label: str,
    *,
    similarity: float | None = None,
    review_required: bool | None = None,
    decision_reason: str | None = None,
    config: "AttributionConfig | None" = None,
) -> bool:
    """这条记录是否**明确**是他人说的（可以按他人内容处理）。

    只读已有字段，不计算新阈值：``config`` 缺省用 ``AttributionConfig()`` 的
    ``unknown_threshold``（默认 0.25），与 `attribute_transcript` 判 non_self 的口径一致。
    """
    if label != "non_self":
        return False
    if review_required:
        return False
    reason = str(decision_reason or "").strip()
    if reason in UNCERTAIN_OTHER_DECISION_REASONS:
        return False
    if reason == "strong_personal_negative_match":
        return True
    threshold = (config or AttributionConfig()).unknown_threshold
    if similarity is not None:
        try:
            return float(similarity) < float(threshold)
        except (TypeError, ValueError):
            return False
    # 没有分数、亦无「明确他人」的决策理由 → 不能声称明确，按不确定处理
    return False


def important_signal_survives(
    label: str,
    text: str,
    *,
    similarity: float | None = None,
    review_required: bool | None = None,
    decision_reason: str | None = None,
    config: "AttributionConfig | None" = None,
) -> bool:
    """含生活关键词的句子，是否**不得**因声源标签被当媒体/他人内容丢掉。

    返回 True 的含义：按 ``unknown`` 继续送抽取，产出的事实进「需要我判断」，
    不直接入账、不折叠成媒体。
    """
    if not has_important_signal(text):
        return False
    if label == "self":
        return True
    if label not in {"unknown", "non_self"}:
        return False
    return not is_confirmed_other(
        label,
        similarity=similarity,
        review_required=review_required,
        decision_reason=decision_reason,
        config=config,
    )


@dataclass(frozen=True)
class AttributionConfig:
    """Thresholds for turning a voice-template similarity into a source label."""

    self_threshold: float = 0.50
    unknown_threshold: float = 0.25

    def __post_init__(self) -> None:
        if not 0.0 <= self.unknown_threshold < self.self_threshold <= 1.0:
            raise ValueError(
                "thresholds must satisfy 0 <= unknown_threshold < self_threshold <= 1"
            )


@dataclass(frozen=True)
class VoiceprintPolicy:
    """声纹后端的**成套**判据：阈值 + 所有按该后端标定过的门控常数。

    为什么必须成套切换：48 维手工频谱包络与 CAM++（192 维、判别式说话人模型）的
    余弦分布完全不同 —— 实测同一批真实手表录音（2026-10-14）：

    * 48 维逐 span：本人中位 0.653 / 视频中位 0.500（分离间隔只有 0.153）；
    * CAM++ 按说话人合并：本人中位 0.635 / 视频中位 0.195（间隔 0.440）。

    把 48 维用惯的 0.45/0.25 套到 CAM++ 上，或反过来，都会把大量非本人判成 self。
    近场抬升（``near_field_floor``）与多证据加分（``near_clear_similarity_bonus``）
    同样是按 48 维的信道敏感度标定的：CAM++ 已经把信道因素学进模型，再抬一次
    会把 0.31 的非本人抬过 0.40 → 直接判 self（假阳性）。
    """

    name: str
    attribution: AttributionConfig
    far_field_damping: float = FAR_FIELD_SIMILARITY_DAMPING
    # <= 0 表示不做近场抬升（该后端不需要信道补偿）
    near_field_floor: float = NEAR_FIELD_SIMILARITY_FLOOR
    near_field_floor_min_raw: float = NEAR_FIELD_FLOOR_MIN_RAW_SIMILARITY
    near_field_floor_min_ratio_db: float = NEAR_FIELD_FLOOR_MIN_PROXIMITY_RATIO_DB
    # 滚动模板池的更新门（必须高于自阈值，避免非本人污染模板）
    pool_min_update_similarity: float = 0.85
    # 多证据融合里近场清晰度最多能给相似度加多少分（0 = 不参与身份判定）
    near_clear_similarity_bonus: float = 0.20
    # 低于该时长不做声纹判定（短句 embedding 不稳定）
    min_window_seconds: float = 0.2
    embedding_dim: int | None = None


NUMPY48_VOICEPRINT_POLICY = VoiceprintPolicy(
    name="numpy48",
    attribution=AttributionConfig(),
    embedding_dim=48,
)

CAMPPLUS_VOICEPRINT_POLICY = VoiceprintPolicy(
    name="campplus",
    attribution=AttributionConfig(self_threshold=0.40, unknown_threshold=0.30),
    # 乘法降权与量纲无关：本人 0.635*0.8 = 0.508 仍在 0.40 之上
    far_field_damping=FAR_FIELD_SIMILARITY_DAMPING,
    near_field_floor=0.0,
    near_field_floor_min_raw=0.0,
    near_field_floor_min_ratio_db=0.0,
    pool_min_update_similarity=0.70,
    near_clear_similarity_bonus=0.0,
    min_window_seconds=0.8,
    embedding_dim=192,
)

VOICEPRINT_POLICIES: dict[str, VoiceprintPolicy] = {
    NUMPY48_VOICEPRINT_POLICY.name: NUMPY48_VOICEPRINT_POLICY,
    CAMPPLUS_VOICEPRINT_POLICY.name: CAMPPLUS_VOICEPRINT_POLICY,
}


def voiceprint_policy(name: str) -> VoiceprintPolicy:
    """按名字取后端判据；未知后端直接报错，不允许悄悄退回默认量纲。"""

    key = str(name).strip().lower()
    try:
        return VOICEPRINT_POLICIES[key]
    except KeyError as error:
        raise ValueError(f"unknown voiceprint backend: {name!r}") from error


def build_voiceprint_policy(
    name: str,
    *,
    self_threshold: float | None = None,
    unknown_threshold: float | None = None,
) -> VoiceprintPolicy:
    """取某后端的判据，可选覆盖两个阈值（其余常数保持该后端的标定值）。"""

    base = voiceprint_policy(name)
    if self_threshold is None and unknown_threshold is None:
        return base
    attribution = AttributionConfig(
        self_threshold=(
            base.attribution.self_threshold if self_threshold is None else float(self_threshold)
        ),
        unknown_threshold=(
            base.attribution.unknown_threshold
            if unknown_threshold is None
            else float(unknown_threshold)
        ),
    )
    return replace(base, attribution=attribution)


@dataclass(frozen=True)
class AttributionDecision:
    """A source label plus the score and whether a human must review it."""

    label: SourceLabel
    score: float | None
    is_review_required: bool
    reason: str


# ═══ 命名联系人（可选，2026-11-04 Q816 接线）════════════════════════════════════
# 用途：非 self 的段如果命中已注册联系人池，回答"这是谁"。
# 硬约束：**不改变 source_label 的 self / non_self / unknown 三值契约，不改任何阈值，
#        无联系人池时行为与接入前逐字节一致**（只在命中时多两个字段）。
#: 只有这两种标签才查联系人；判成 self 的段不需要"这是谁"。
NAMED_CONTACT_LABELS = ("non_self", "unknown")


def named_contact_fields(
    embedding: Sequence[float] | None,
    *,
    pool_path: Any = None,
    threshold: float | None = None,
    backend: str | None = None,
) -> dict[str, Any]:
    """匹配命名联系人，返回可并入归属字典的**可选字段**（未命中/无池 ⇒ ``{}``）。

    · 池路径缺省走 ``voiceprint_pool_store.env_pool_path()``（与声纹池同一文件）；
    · 阈值缺省取 ``voiceprint_pool_store.CONTACT_MATCH_DEFAULT_THRESHOLD``（不在此另立标准）；
    · 任何异常（文件坏、维度不符、numpy 缺失…）一律返回 ``{}`` ——
      联系人是**增强信息**，绝不允许把整条 ASR 链路带崩。
    """
    if embedding is None:
        return {}
    try:
        from life_logger import voiceprint_pool_store as vps

        path = Path(pool_path) if pool_path is not None else vps.env_pool_path()
        if path is None:
            return {}
        try:
            if not Path(path).is_file():
                return {}
        except OSError:
            return {}
        limit = vps.CONTACT_MATCH_DEFAULT_THRESHOLD if threshold is None else float(threshold)
        hit = vps.match_contact(path, embedding, threshold=limit, backend=backend)
    except Exception:  # noqa: BLE001 - 联系人匹配失败绝不影响归属结论
        return {}
    if hit is None:
        return {}
    return {
        "contact_id": hit.contact_id,
        "contact_label": hit.label or hit.contact_id,
        "contact_similarity": round(float(hit.similarity), 6),
    }


def apply_named_contact(
    result: Mapping[str, Any],
    embedding: Sequence[float] | None,
    **kwargs: Any,
) -> dict[str, Any]:
    """就地给归属字典补 ``contact_id`` / ``contact_label``（不命中则不写任何键）。

    ``source_label`` **原样保留**：本函数只追加，不判定、不改标签、不改 review 标记。
    """
    out = dict(result)
    if str(out.get("source_label") or "") not in NAMED_CONTACT_LABELS:
        return out
    fields = named_contact_fields(embedding, **kwargs)
    if fields:
        out.update(fields)
    return out


def cosine_similarity(
    left: Sequence[float],
    right: Sequence[float],
) -> float:
    """Return cosine similarity for two non-zero vectors."""

    if len(left) != len(right) or not left:
        raise ValueError("vectors must have the same non-zero length")
    left_values = [float(value) for value in left]
    right_values = [float(value) for value in right]
    if not all(isfinite(value) for value in (*left_values, *right_values)):
        raise ValueError("vectors must contain only finite values")
    left_norm = sqrt(sum(value * value for value in left_values))
    right_norm = sqrt(sum(value * value for value in right_values))
    if left_norm == 0.0 or right_norm == 0.0:
        raise ValueError("vectors must be non-zero")
    return sum(
        left_value * right_value
        for left_value, right_value in zip(left_values, right_values)
    ) / (left_norm * right_norm)


def normalize_audio_gain(
    samples: Any,
    *,
    target_rms_dbfs: float = -24.0,
    peak_ceiling_dbfs: float = -3.0,
    max_gain_db: float = 18.0,
) -> tuple[Any, dict[str, float | bool]]:
    """Normalize quiet speech without clipping and return measurable gain metadata."""

    values = [float(value) for value in samples]
    if not values:
        return samples, {
            "applied": False,
            "gain_db": 0.0,
            "input_rms_dbfs": -120.0,
            "input_peak_dbfs": -120.0,
        }
    if not all(isfinite(value) for value in values):
        raise ValueError("audio samples must contain only finite values")
    peak = max(abs(value) for value in values)
    if peak == 0.0:
        rms = 0.0
    else:
        rms = peak * sqrt(sum((value / peak) ** 2 for value in values) / len(values))
    input_rms_dbfs = 20.0 * log10(max(rms, 1e-6))
    input_peak_dbfs = 20.0 * log10(max(peak, 1e-6))
    if peak < 1e-5:
        return samples, {
            "applied": False,
            "gain_db": 0.0,
            "input_rms_dbfs": round(input_rms_dbfs, 3),
            "input_peak_dbfs": round(input_peak_dbfs, 3),
        }
    desired_gain_db = target_rms_dbfs - input_rms_dbfs
    peak_limited_gain_db = peak_ceiling_dbfs - input_peak_dbfs
    gain_db = min(max_gain_db, desired_gain_db, peak_limited_gain_db)
    if abs(gain_db) < 0.1:
        return samples, {
            "applied": False,
            "gain_db": 0.0,
            "input_rms_dbfs": round(input_rms_dbfs, 3),
            "input_peak_dbfs": round(input_peak_dbfs, 3),
        }
    gain = 10.0 ** (gain_db / 20.0)
    try:
        normalized = samples * gain
    except TypeError:
        normalized = [value * gain for value in values]
    return normalized, {
        "applied": True,
        "gain_db": round(gain_db, 3),
        "input_rms_dbfs": round(input_rms_dbfs, 3),
        "input_peak_dbfs": round(input_peak_dbfs, 3),
    }


def classify_similarity(
    score: float,
    config: AttributionConfig | None = None,
) -> AttributionDecision:
    """Apply conservative thresholds to a template similarity score."""

    config = config or AttributionConfig()
    score = float(score)
    if not isfinite(score) or not -1.0 <= score <= 1.0:
        raise ValueError("similarity score must be finite and between -1 and 1")
    if score >= config.self_threshold:
        return AttributionDecision("self", score, False, "above_self_threshold")
    if score < config.unknown_threshold:
        return AttributionDecision("non_self", score, False, "below_non_self_threshold")
    return AttributionDecision("unknown", score, True, "similarity_in_review_band")


@dataclass
class RollingVoiceprintPool:
    """Adaptive rolling pool of high-confidence speaker embeddings.

    Prevents voiceprint drift caused by morning hoarseness, fatigue, or
    minor acoustic variations while maintaining a hard gate to avoid noise contamination.
    """

    initial_template: list[float]
    current_template: list[float]
    history: list[list[float]]
    max_history: int = 20
    ema_alpha: float = 0.08
    min_update_similarity: float = 0.85
    #: [Q806] 跨进程持久化（None = 完全退回改造前行为：每段从模板重新开始）
    pool_store: Any = None
    pool_status: dict[str, Any] | None = None
    persist_min_interval_s: float = 0.0
    _last_persist_at: float = field(default=0.0, repr=False)

    @classmethod
    def from_template(
        cls,
        template: Sequence[float],
        max_history: int = 20,
        *,
        min_update_similarity: float | None = None,
        pool_path: str | Path | None = None,
        backend: str | None = None,
        use_env_store: bool = True,
    ) -> RollingVoiceprintPool:
        """用模板建池；``min_update_similarity`` 按声纹后端给（见 VoiceprintPolicy）。

        [Q806] 若给了 ``pool_path``（或环境变量 ``LIFE_LOGGER_VOICEPRINT_POOL``），
        建完池后立即把上次进程落盘的历史灌进来 —— 这是"池子能跨段累积"的唯一开关。
        没给则行为与改造前**逐字节一致**。
        """

        base = [float(v) for v in template]
        pool = cls(
            initial_template=list(base),
            current_template=list(base),
            history=[list(base)],
            max_history=max_history,
        )
        if min_update_similarity is not None:
            pool.min_update_similarity = float(min_update_similarity)
        pool.attach_store(pool_path=pool_path, backend=backend, use_env_store=use_env_store)
        return pool

    # ---------------------------------------------------------------- [Q806]
    def attach_store(
        self,
        *,
        pool_path: str | Path | None = None,
        backend: str | None = None,
        use_env_store: bool = True,
    ) -> dict[str, Any] | None:
        """绑定持久化 store 并把落盘历史灌进池子。任何异常都只记录、不抛。"""
        self.pool_store = None
        self.pool_status = None
        try:
            from life_logger.voiceprint_pool_store import VoiceprintPoolStore, store_from_env

            if pool_path:
                self.pool_store = VoiceprintPoolStore(pool_path, backend=backend)
            elif use_env_store:
                self.pool_store = store_from_env(backend=backend)
        except Exception as exc:  # pragma: no cover - 依赖异常不能拖垮归属
            self.pool_status = {"status": "error",
                               "detail": f"store_init:{type(exc).__name__}: {exc}"}
            return self.pool_status
        if self.pool_store is None:
            return None
        try:
            self.pool_status = self.pool_store.hydrate(self)
        except Exception as exc:  # pragma: no cover
            self.pool_status = {"status": "error",
                               "detail": f"hydrate:{type(exc).__name__}: {exc}"}
        return self.pool_status

    def flush_to_store(self) -> dict[str, Any] | None:
        """把当前模板与历史写回盘（原子 + flock）。返回写入结果；无 store 时返回 None。"""
        if self.pool_store is None:
            return None
        try:
            result = self.pool_store.persist(
                current_template=self.current_template,
                history=self.history,
                embedding_dim=len(self.current_template) or None,
            )
        except Exception as exc:  # pragma: no cover
            result = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
        self.pool_status = result
        return result

    def _maybe_persist(self) -> None:
        """更新成功后节流落盘（默认间隔 0 = 立即写）。"""
        if self.pool_store is None:
            return
        now = time.monotonic()
        if self._last_persist_at and (now - self._last_persist_at) < self.persist_min_interval_s:
            return
        self._last_persist_at = now
        self.flush_to_store()

    def score_candidate(self, candidate: Sequence[float]) -> float:
        """Score candidate against both current template and the rolling pool."""
        cand = [float(v) for v in candidate]
        ema_sim = cosine_similarity(self.current_template, cand)
        pool_sim = max(
            (cosine_similarity(v, cand) for v in self.history), default=ema_sim
        )
        return max(ema_sim, pool_sim)

    def maybe_update(
        self,
        candidate: Sequence[float],
        similarity: float,
        *,
        proximity_ratio_db: float | None = None,
    ) -> bool:
        """Conditionally update rolling template if confidence meets gating criteria.

        Uses CAM++ continuous cosine similarity fused with the +3.0~+10.0 dB
        near-field proximity soft interval.
        """
        effective_min_similarity = self.min_update_similarity
        if proximity_ratio_db is not None and isfinite(float(proximity_ratio_db)):
            prox_val = float(proximity_ratio_db)
            if prox_val >= 3.0:
                soft_gain = max(0.0, min(1.0, (prox_val - 3.0) / 7.0))
                effective_min_similarity = max(
                    0.70, self.min_update_similarity - 0.12 * soft_gain
                )
            elif prox_val < 0.0:
                effective_min_similarity = min(0.92, self.min_update_similarity + 0.05)
        if similarity < effective_min_similarity:
            return False

        cand = [float(v) for v in candidate]
        new_template = [
            (1.0 - self.ema_alpha) * cur + self.ema_alpha * c
            for cur, c in zip(self.current_template, cand)
        ]
        norm = sqrt(sum(v * v for v in new_template))
        if norm > 0:
            self.current_template = [v / norm for v in new_template]

        self.history.append(cand)
        if len(self.history) > self.max_history:
            self.history.pop(1)
        self._maybe_persist()
        return True


def decisions_for_embeddings(
    template: Sequence[float] | RollingVoiceprintPool,
    candidates: Sequence[Sequence[float]],
    config: AttributionConfig | None = None,
    *,
    proximity_ratios_db: Sequence[float | None] | None = None,
) -> list[AttributionDecision]:
    """Score candidate speaker embeddings in their input order, with optional rolling update."""
    results = []
    pool = template if isinstance(template, RollingVoiceprintPool) else None

    for idx, candidate in enumerate(candidates):
        if pool is not None:
            score = pool.score_candidate(candidate)
            prox = (
                proximity_ratios_db[idx]
                if proximity_ratios_db and idx < len(proximity_ratios_db)
                else None
            )
            pool.maybe_update(candidate, score, proximity_ratio_db=prox)
        else:
            score = cosine_similarity(template, candidate)
        results.append(classify_similarity(score, config))

    return results


def unknown_decision(reason: str) -> AttributionDecision:
    """Return a review decision when no valid embedding can be computed."""

    if not reason.strip():
        raise ValueError("unknown decision requires a reason")
    return AttributionDecision("unknown", None, True, reason)


def protect_short_low_similarity(
    decision: AttributionDecision,
    *,
    duration_seconds: float,
    review_seconds: float = 4.0,
    review_score_floor: float = 0.10,
) -> AttributionDecision:
    """Keep ambiguous short speech reviewable instead of discarding it as non-self.

    Short utterances are less stable for speaker embeddings. A low score should only
    become a hard non-self decision when it is also below ``review_score_floor``.
    Longer utterances and already-self/unknown decisions are left unchanged.
    """

    duration_seconds = float(duration_seconds)
    review_seconds = float(review_seconds)
    review_score_floor = float(review_score_floor)
    if duration_seconds < 0:
        raise ValueError("duration_seconds must be non-negative")
    if review_seconds <= 0:
        raise ValueError("review_seconds must be positive")
    if not -1.0 <= review_score_floor <= 1.0:
        raise ValueError("review_score_floor must be between -1 and 1")
    if decision.label != "non_self" or decision.score is None:
        return decision
    if duration_seconds > review_seconds or decision.score < review_score_floor:
        return decision
    return AttributionDecision(
        "unknown",
        decision.score,
        True,
        "short_segment_low_similarity_review",
    )


def reclassify_attribution_record(
    record: Mapping[str, Any],
    config: AttributionConfig | None = None,
    *,
    short_review_seconds: float = 4.0,
    short_review_score_floor: float = 0.10,
) -> dict[str, Any]:
    """Re-apply source policy to a stored CAM++ score without recomputing audio."""

    updated = dict(record)
    score = record.get("voice_similarity")
    if score is None:
        return updated
    start = float(record.get("start_offset_seconds", 0.0))
    end = float(record.get("end_offset_seconds", start))
    decision = protect_short_low_similarity(
        classify_similarity(float(score), config),
        duration_seconds=max(0.0, end - start),
        review_seconds=short_review_seconds,
        review_score_floor=short_review_score_floor,
    )
    updated.update(
        source_label=decision.label,
        voice_similarity=round(decision.score, 6)
        if decision.score is not None
        else None,
        review_required=decision.is_review_required,
        decision_reason=decision.reason,
    )
    return updated


def classify_acoustic_proximity(
    *,
    speech_rms_dbfs: float,
    noise_floor_dbfs: float,
    relative_loudness_db: float,
    proximity_ratio_db: float | None = None,
) -> dict[str, float | str]:
    """Return a highly robust near/far hint combining level, SNR, and Proximity Effect.

    Proximity Effect (Near-field physical chest & mouth bass boost) provides an
    unforgeable acoustic signature for speech spoken within 15cm of the Watch.
    """

    speech = float(speech_rms_dbfs)
    noise = float(noise_floor_dbfs)
    relative = float(relative_loudness_db)
    if not all(isfinite(value) for value in (speech, noise, relative)):
        raise ValueError("acoustic evidence must contain finite values")
    snr = speech - noise

    def scaled(value: float, low: float, high: float) -> float:
        if high <= low:
            raise ValueError("invalid acoustic scaling range")
        return max(0.0, min(1.0, (value - low) / (high - low)))

    absolute_support = scaled(speech, -40.0, -18.0)
    snr_support = scaled(snr, 4.0, 18.0)
    relative_support = scaled(relative, -7.0, 6.0)

    # Incorporate Proximity Effect low-frequency bass resonance if provided
    if proximity_ratio_db is not None and isfinite(float(proximity_ratio_db)):
        proximity_support = scaled(float(proximity_ratio_db), -4.0, 6.0)
        score = (
            0.20 * absolute_support
            + 0.30 * snr_support
            + 0.20 * relative_support
            + 0.30 * proximity_support
        )
    else:
        score = (
            0.30 * absolute_support
            + 0.40 * snr_support
            + 0.30 * relative_support
        )

    if score >= 0.65:
        label: ProximityLabel = "near"
    elif score <= 0.35:
        label = "far"
    else:
        label = "uncertain"
    confidence = min(1.0, abs(score - 0.5) * 2.0)
    return {
        "acoustic_proximity": label,
        "proximity_score": round(score, 6),
        "proximity_confidence": round(confidence, 6),
        "snr_proxy_db": round(snr, 3),
        "proximity_ratio_db": round(float(proximity_ratio_db), 2) if proximity_ratio_db is not None else None,
    }


def apply_multi_evidence_policy(
    records: Sequence[Mapping[str, Any]],
    config: AttributionConfig | None = None,
    *,
    self_context_seconds: float = 12.0,
    near_clear_similarity_bonus: float = 0.20,
) -> list[dict[str, Any]]:
    """Fuse identity, acoustic proximity, and temporal continuity.

    High-confidence self decisions are never created from proximity alone.
    The only automatic downgrade is an already-low-similarity short segment
    that is also acoustically far and isolated from confirmed self speech.

    ``near_clear_similarity_bonus`` 是「近场清晰 + 声纹已有最低可信度」最多能
    加多少分（默认 0.20，按 48 维标定）。CAM++ 这类后端把它设为 0：
    该加分是给信道敏感的手工特征的补偿，套到判别式模型上会把非本人抬过阈值。
    """

    config = config or AttributionConfig()
    if self_context_seconds <= 0:
        raise ValueError("self_context_seconds must be positive")
    near_clear_bonus = float(near_clear_similarity_bonus)
    if not 0.0 <= near_clear_bonus <= 1.0:
        raise ValueError("near_clear_similarity_bonus must be between 0 and 1")

    ordered = [dict(record) for record in records]
    self_spans = [
        (
            float(record.get("start_offset_seconds", 0.0)),
            float(record.get("end_offset_seconds", 0.0)),
        )
        for record in ordered
        if record.get("source_label") == "self"
    ]

    def context_support(record: Mapping[str, Any]) -> float:
        start = float(record.get("start_offset_seconds", 0.0))
        end = float(record.get("end_offset_seconds", start))
        best = 0.0
        for self_start, self_end in self_spans:
            if self_start == start and self_end == end:
                continue
            if self_end <= start:
                gap = start - self_end
            elif self_start >= end:
                gap = self_start - end
            else:
                gap = 0.0
            if gap <= self_context_seconds:
                best = max(best, 1.0 - gap / self_context_seconds)
        return best

    for record in ordered:
        original_label = str(record.get("source_label") or "unknown")
        record.setdefault("identity_source_label", original_label)
        support = context_support(record)
        record["self_context_support"] = round(support, 6)
        record.setdefault("identity_decision_reason", record.get("decision_reason"))

        similarity = record.get("voice_similarity")
        if similarity is None:
            identity_support = 0.18
        else:
            denominator = config.self_threshold - config.unknown_threshold
            identity_support = max(
                0.0,
                min(
                    1.0,
                    (float(similarity) - config.unknown_threshold) / denominator,
                ),
            )

        proximity_score = float(record.get("proximity_score", 0.5))
        duration = max(
            0.0,
            float(record.get("end_offset_seconds", 0.0))
            - float(record.get("start_offset_seconds", 0.0)),
        )
        duration_support = min(1.0, duration / 4.0)
        # Near-field clarity is useful identity evidence only when the voice
        # embedding already has a plausible match. Loudness/proximity alone
        # must never turn an unrelated speaker into the user.
        raw_similarity = None if similarity is None else float(similarity)
        snr = record.get("snr_proxy_db")
        prox_db = record.get("proximity_ratio_db")
        near_clear_support = 0.0
        if (
            raw_similarity is not None
            and raw_similarity >= config.unknown_threshold
            and (snr is not None or prox_db is not None)
            and duration >= 0.8
            and record.get("acoustic_proximity") in {"near", "uncertain"}
        ):
            snr_val = float(snr) if snr is not None else 0.0
            snr_support = max(0.0, min(1.0, (snr_val - 3.0) / 7.0))
            if prox_db is not None and isfinite(float(prox_db)):
                prox_db_support = max(0.0, min(1.0, (float(prox_db) - 3.0) / 7.0))
                snr_support = max(snr_support, 0.5 * snr_support + 0.5 * prox_db_support)
            near_clear_support = max(0.0, min(1.0, proximity_score)) * snr_support
        fused_similarity = raw_similarity
        fusion_reason = "raw_similarity_only"
        # 加分 > 0 才允许近场清晰度参与身份判定；CAM++ 的后端判据把它设为 0，
        # 于是声学清晰度只影响复核优先级，绝不改身份结论。
        if near_clear_support > 0 and near_clear_bonus > 0:
            # At most +0.20 similarity (48-dim scale): strong near-field clarity can
            # tip a borderline match, while strongly negative scores stay put.
            fused_similarity = min(1.0, raw_similarity + near_clear_bonus * near_clear_support)
            fusion_reason = "near_clear_with_plausible_voice_match"
            if raw_similarity < config.self_threshold <= fused_similarity:
                record["source_label"] = "self"
                record["review_required"] = False
                record["decision_reason"] = "near_clear_fused_voice_match"
        elif near_clear_support > 0:
            fusion_reason = "near_clear_bonus_disabled_for_backend"
        record["speaker_fused_score"] = (
            round(fused_similarity, 6) if fused_similarity is not None else None
        )
        record["speaker_fusion_delta"] = (
            round(fused_similarity - raw_similarity, 6)
            if fused_similarity is not None and raw_similarity is not None
            else 0.0
        )
        record["speaker_fused_reason"] = fusion_reason

        priority_score = (
            0.45 * identity_support
            + 0.25 * proximity_score
            + 0.25 * support
            + 0.05 * duration_support
        )
        if similarity is None and duration < 0.8:
            priority_score *= 0.60
        if priority_score >= 0.54:
            priority: ReviewPriority = "high"
        elif priority_score >= 0.32:
            priority = "medium"
        else:
            priority = "low"
        record["review_priority_score"] = round(priority_score, 6)
        record["review_priority"] = priority
        if priority == "high":
            layer: ReviewLayer = "focus"
        elif priority == "medium":
            layer = "standard"
        else:
            layer = "background"
        record["review_layer"] = layer

        can_downgrade = (
            record.get("source_label") == "unknown"
            and record.get("decision_reason")
            == "short_segment_low_similarity_review"
            and similarity is not None
            and float(similarity) < config.unknown_threshold
            and record.get("acoustic_proximity") == "far"
            and support < 0.15
        )
        if can_downgrade:
            record["source_label"] = "non_self"
            record["review_required"] = False
            record["decision_reason"] = "multi_evidence_far_low_similarity"
            record["review_priority"] = "low"
            record["review_layer"] = "background"
            record["review_priority_score"] = min(
                float(record["review_priority_score"]),
                0.20,
            )

    return ordered


def should_extract_life_events(
    label: str,
    *,
    text: str = "",
    review_priority: str | None = None,
) -> bool:
    """Allow self speech plus important uncertain-speaker review candidates."""

    if label == "self":
        return True
    if label not in {"unknown", "non_self"}:
        return False
    return any(marker in text for marker in _IMPORTANT_UNKNOWN_MARKERS)


def build_attribution_record(
    segment: Mapping[str, Any],
    decision: AttributionDecision,
    *,
    method: str,
    decision_reason: str | None = None,
) -> dict[str, Any]:
    """Attach a source decision to a transcript segment without losing evidence."""

    record = {
        key: segment[key]
        for key in (
            "segment_id",
            "recording_id",
            "start_offset_seconds",
            "end_offset_seconds",
            "absolute_started_at",
            "absolute_ended_at",
            "text",
        )
        if key in segment
    }
    record.update(
        {
            "source_label": decision.label,
            "voice_similarity": (
                round(decision.score, 6) if decision.score is not None else None
            ),
            "source_method": method,
            "review_required": decision.is_review_required,
            "decision_reason": decision_reason or decision.reason,
        }
    )
    return record


def partition_attributions(
    records: Sequence[Mapping[str, Any]],
) -> dict[SourceLabel, list[Mapping[str, Any]]]:
    """Group attribution records while preserving their chronological order."""

    result: dict[SourceLabel, list[Mapping[str, Any]]] = {
        "self": [],
        "non_self": [],
        "unknown": [],
    }
    for record in records:
        label = record.get("source_label")
        if label not in result:
            raise ValueError(f"unknown source label: {label!r}")
        result[label].append(record)
    return result
