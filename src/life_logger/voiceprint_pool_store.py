"""声纹滚动池的跨进程持久化（2026-11-04 Q806）。

为什么需要
----------
`RollingVoiceprintPool` 是**自适应**的：它把每段高置信度的本人 embedding 追加进
`history`，让模板跟着一天里的嗓音变化（晨起沙哑、疲劳）缓慢漂移。但它以前**只活在
单个进程里**：`watch_formal_asr` 对每个 1 分钟分段 spawn 一次
`run_cloud_asr_pipeline.py --provider moss`，子进程里 `from_template()` 重新建池、
处理完就退出 —— 于是**每段都从模板原样开始，池子永远等于初始模板**。

本模块只解决"池子能跨段累积"这一件事：

    segment 1  子进程 A：from_template → hydrate(空) → 更新 → persist
    segment 2  子进程 B：from_template → **hydrate(段1的结果)** → 更新 → persist
    segment 3  子进程 C：from_template → **hydrate(段1+段2)** → …

**它不改任何阈值、不换模型、不判身份**。远场物理限制（已证伪 7 种修法）原样保留：
similarity 还是那个 similarity，只是"上一个分段的池子"不会再丢失。

安全约束
--------
* 落盘 JSON（人类可读，便于 `cat` 排查），原子替换（复用 `life_logger.atomic`）。
* 多子进程并发写：`file_lock()`（flock）包住「读-合并-写」，不会互相截断。
* 加载时严格校验：schema 版本 / backend / 维度 / 每个分量有限。
  **任何不合规都退回空池（= 行为与改造前完全一致），绝不半信半疑地用坏数据。**
* 条目有上限（FIFO）与按余弦近重复去重，避免文件无限膨胀。

环境变量（供子进程继承，`watch_formal_asr` 负责设置）
------------------------------------------------------
* ``LIFE_LOGGER_VOICEPRINT_POOL``         池文件路径
* ``LIFE_LOGGER_VOICEPRINT_POOL_ENABLED`` ``0``/``false`` 可关掉（默认开着）
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from datetime import datetime, timezone
from math import isfinite, sqrt
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "voiceprint-pool-v1"
#: 命名联系人槽位的 schema。**故意不改 SCHEMA**：v1 文件没有 contacts 就是"只有本人池"，
#: 旧代码读到新文件也只是忽略 contacts —— 双向兼容，不会因为改版本号把别人的池清零。
CONTACTS_SCHEMA = "voiceprint-contacts-v1"

ENV_POOL_PATH = "LIFE_LOGGER_VOICEPRINT_POOL"
ENV_POOL_ENABLED = "LIFE_LOGGER_VOICEPRINT_POOL_ENABLED"

#: 池子上限。40 条 × 192~512 维 ≈ 几十 KB，足够覆盖一天，又不会让文件失控。
DEFAULT_MAX_ENTRIES = 40
#: 超过这个维度的向量直接判为坏数据（防止把音频/日志误当 embedding 存进来）。
MAX_VECTOR_DIM = 4096
#: 余弦 ≥ 此值视为重复条目（同一个人的同一段嗓音），不重复追加。
DEDUPE_COSINE = 0.999
#: 每个联系人最多留几个向量（多个向量=同一个人不同时段/不同麦克风）。
DEFAULT_MAX_CONTACT_EMBEDDINGS = 10
#: 联系人匹配默认阈值。比"本人"低：家人的录音多在远场，且我们不希望误认成陌生人。
CONTACT_MATCH_DEFAULT_THRESHOLD = 0.55
#: 同一联系人的两条向量余弦 ≥ 此值视为重复（同一次录音被算两遍），只累加 hits。
CONTACT_DEDUPE_COSINE = 0.999
#: 联系人 id 形状：小写 slug，避免把中文名/路径/任意串写进文件名或 key。
CONTACT_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")

_STATUS_OK = "ok"
_STATUS_MISSING = "missing"
_STATUS_CORRUPT = "corrupt"
_STATUS_SCHEMA = "schema_mismatch"
_STATUS_BACKEND = "backend_mismatch"
_STATUS_DIM = "dim_mismatch"
_STATUS_DISABLED = "disabled"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sqrt(sum(x * x for x in a))
    nb = sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def _clean_vector(value: Any, dim: int | None) -> list[float] | None:
    """校验并规范化一个向量；不合规返回 None（调用方丢弃，不猜）。"""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if len(value) > MAX_VECTOR_DIM:
        return None
    if dim is not None and len(value) != dim:
        return None
    out: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not isfinite(number):
            return None
        out.append(number)
    return out


# ─────────────────────────────────────────────── 命名联系人槽位（2026-11-04 Q807）
@dataclasses.dataclass
class ContactPoolEntry:
    """一个命名联系人（如 mom）的声纹槽位。

    为什么允许多个向量：同一个人打电话时的信道（手持/免提/远场）差别很大，
    单向量会把"远场那一次"直接抹掉。多条向量 + 取最大余弦，比取平均稳。
    """

    contact_id: str
    label: str = ""
    embeddings: list[list[float]] = dataclasses.field(default_factory=list)
    updated_at: str | None = None
    hits: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "embeddings": [list(v) for v in self.embeddings],
            "updated_at": self.updated_at,
            "hits": int(self.hits or 0),
        }

    @classmethod
    def from_dict(cls, contact_id: str, raw: Mapping[str, Any], *, dim: int | None = None) -> "ContactPoolEntry | None":
        if not isinstance(raw, Mapping):
            return None
        vectors: list[list[float]] = []
        seen: list[list[float]] = []
        for item in raw.get("embeddings") or []:
            vector = _clean_vector(item, dim)
            if vector is None:
                continue
            if any(_cosine(vector, prior) >= CONTACT_DEDUPE_COSINE for prior in seen):
                continue
            seen.append(vector)
            vectors.append(vector)
        if not vectors:
            return None
        hits = raw.get("hits")
        return cls(
            contact_id=contact_id,
            label=str(raw.get("label") or "")[:40],
            embeddings=vectors,
            updated_at=str(raw.get("updated_at") or "") or None,
            hits=int(hits) if isinstance(hits, int) and not isinstance(hits, bool) else 0,
        )


@dataclasses.dataclass
class ContactMatch:
    """一次联系人匹配结果。``matched`` 为 False 时不要拿去归属说话人。"""

    contact_id: str
    similarity: float
    label: str = ""

    def __bool__(self) -> bool:      # 方便 `if match:` 用法
        return bool(self.contact_id)

    def to_dict(self) -> dict[str, Any]:
        return {"matched": bool(self.contact_id), "contact_id": self.contact_id,
                "similarity": round(float(self.similarity), 6), "label": self.label}


def _contact_records(payload: Mapping[str, Any] | None) -> dict[str, ContactPoolEntry]:
    """从整份池文件里解析 contacts（坏条目静默丢弃，绝不半信半疑地用）。"""
    if not isinstance(payload, Mapping):
        return {}
    raw = payload.get("contacts")
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, ContactPoolEntry] = {}
    for contact_id, item in raw.items():
        cid = str(contact_id).strip()
        if not CONTACT_ID_RE.match(cid):
            continue
        entry = ContactPoolEntry.from_dict(cid, item)
        if entry is not None:
            out[cid] = entry
    return out


def load_contacts(path: Path | str, *, backend: str | None = None) -> dict[str, ContactPoolEntry]:
    """读联系人槽位（只读、无副作用）。文件不存在/坏掉一律返回 {}。"""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA:
        return {}
    stored_backend = payload.get("backend")
    if backend and stored_backend and str(stored_backend) != str(backend):
        return {}
    return _contact_records(payload)


def register_contact_embedding(
    path: Path | str,
    contact_id: str,
    embedding: Sequence[float],
    *,
    label: str = "",
    backend: str | None = None,
    max_per_contact: int = DEFAULT_MAX_CONTACT_EMBEDDINGS,
) -> dict[str, Any]:
    """注册（或追加）一个命名联系人的声纹向量。返回摘要 dict。

    · 同一联系人可以多次注册（不同信道），内部按余弦去重、按 FIFO 截断；
    · 与已有联系人维度不一致 ⇒ `status="dim_mismatch"`，**不写入**（宁可拒绝也不混维度）；
    · 全程 flock + 原子替换，不会覆盖别人刚写的 `entries`（本人池）。
    """
    from life_logger.atomic import file_lock, write_json_atomic

    path = Path(path)
    cid = str(contact_id or "").strip().lower()
    if not CONTACT_ID_RE.match(cid):
        return {"status": "invalid_contact_id", "contact_id": cid,
                "hint": "contact_id 必须是小写 slug（如 mom / dad / zhangsan）"}
    vector = _clean_vector(embedding, None)
    if vector is None:
        return {"status": "invalid_embedding", "contact_id": cid}

    path.parent.mkdir(parents=True, exist_ok=True)
    now = _now_iso()
    with file_lock(path):
        payload: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping) and loaded.get("schema") == SCHEMA:
                    stored_backend = loaded.get("backend")
                    if backend and stored_backend and str(stored_backend) != str(backend):
                        return {"status": "backend_mismatch", "contact_id": cid,
                                "stored_backend": str(stored_backend)}
                    payload = dict(loaded)
            except (OSError, ValueError):
                payload = {}      # 坏文件：重建，但保留调用方随后写入的规范化内容

        contacts = _contact_records(payload)
        existing_dims = {len(v) for entry in contacts.values() for v in entry.embeddings}
        if len(vector) in existing_dims or not existing_dims:
            pass
        elif len(existing_dims) >= 1:
            return {"status": "dim_mismatch", "contact_id": cid,
                    "want_dim": sorted(existing_dims)[0], "got_dim": len(vector)}

        entry = contacts.get(cid) or ContactPoolEntry(contact_id=cid, label=label[:40], embeddings=[])
        if label:
            entry.label = label[:40]
        duplicate = any(_cosine(v, vector) >= CONTACT_DEDUPE_COSINE for v in entry.embeddings)
        if duplicate:
            entry.hits += 1
            entry.updated_at = now
            added = 0
        else:
            entry.embeddings.append(vector)
            entry.hits += 1
            entry.updated_at = now
            added = 1
        cap = max(1, int(max_per_contact))
        trimmed = max(0, len(entry.embeddings) - cap)
        entry.embeddings = entry.embeddings[trimmed:]
        contacts[cid] = entry

        payload.setdefault("schema", SCHEMA)
        if backend and not payload.get("backend"):
            payload["backend"] = str(backend)
        payload.setdefault("embedding_dim", len(vector))
        payload.setdefault("updated_at", now)
        payload["updated_at"] = now
        payload["contacts_schema"] = CONTACTS_SCHEMA
        payload["contacts"] = {k: v.to_dict() for k, v in sorted(contacts.items())}
        payload["contacts_updated_at"] = now
        write_json_atomic(path, payload)

    return {"status": "ok", "contact_id": cid, "added": added, "duplicate": bool(duplicate),
            "embeddings": len(entry.embeddings), "trimmed": trimmed,
            "label": entry.label, "path": str(path)}


def match_contact(
    path: Path | str,
    embedding: Sequence[float],
    *,
    threshold: float = CONTACT_MATCH_DEFAULT_THRESHOLD,
    backend: str | None = None,
) -> ContactMatch | None:
    """在一个 embedding 上匹配命名联系人。

    · 每个联系人取**其所有向量的最大余弦**（多点注册的意义就在这里）；
    · 全局最高分 < threshold ⇒ 返回 None（宁可"未知"，不要瞎认爹）；
    · 维度不一致的联系人直接跳过（不参与打分）。
    """
    vector = _clean_vector(embedding, None)
    if vector is None:
        return None
    contacts = load_contacts(path, backend=backend)
    if not contacts:
        return None
    best: ContactMatch | None = None
    for cid in sorted(contacts):
        entry = contacts[cid]
        score = 0.0
        for candidate in entry.embeddings:
            if len(candidate) != len(vector):
                continue
            score = max(score, _cosine(vector, candidate))
        if score < float(threshold):
            continue
        if best is None or score > best.similarity:
            best = ContactMatch(contact_id=cid, similarity=score, label=entry.label)
    return best


def env_pool_path(env: Mapping[str, str] | None = None) -> Path | None:
    """从环境变量取池文件路径；未配置/被显式关闭时返回 None。"""
    source = os.environ if env is None else env
    flag = str(source.get(ENV_POOL_ENABLED, "")).strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return None
    raw = str(source.get(ENV_POOL_PATH, "")).strip()
    if not raw:
        return None
    return Path(raw)


@dataclasses.dataclass
class LoadResult:
    """一次加载的结果。``status`` 必须能被调用方看见（不静默降级）。"""

    status: str
    path: Path
    backend: str | None = None
    embedding_dim: int | None = None
    entries: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    updated_at: str | None = None
    dropped: int = 0
    detail: str | None = None

    @property
    def usable(self) -> bool:
        return self.status == _STATUS_OK and bool(self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "path": str(self.path),
            "backend": self.backend,
            "embedding_dim": self.embedding_dim,
            "entries": len(self.entries),
            "dropped": self.dropped,
            "updated_at": self.updated_at,
            "detail": self.detail,
        }


class VoiceprintPoolStore:
    """一个 backend + 一个设备对应一份池文件。线程/进程安全由 flock 保证。"""

    def __init__(
        self,
        path: Path | str,
        *,
        backend: str | None = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.path = Path(path)
        self.backend = str(backend).strip() if backend else None
        self.max_entries = max(1, int(max_entries))

    # ---------------------------------------------------------------- 读
    def load(self) -> LoadResult:
        path = self.path
        if not path.is_file():
            return LoadResult(status=_STATUS_MISSING, path=path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return LoadResult(status=_STATUS_CORRUPT, path=path,
                              detail=f"unreadable: {type(exc).__name__}: {exc}")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            return LoadResult(status=_STATUS_CORRUPT, path=path,
                              detail=f"invalid_json: {exc}")
        if not isinstance(payload, Mapping):
            return LoadResult(status=_STATUS_CORRUPT, path=path, detail="not_an_object")

        schema = payload.get("schema")
        if schema != SCHEMA:
            return LoadResult(status=_STATUS_SCHEMA, path=path,
                              detail=f"schema={schema!r} want={SCHEMA!r}")

        stored_backend = payload.get("backend")
        if self.backend and stored_backend and str(stored_backend) != self.backend:
            # 换了声纹后端 ⇒ 维度/量纲都不同，必须弃用而不是硬灌。
            return LoadResult(status=_STATUS_BACKEND, path=path,
                              backend=str(stored_backend),
                              detail=f"backend={stored_backend!r} want={self.backend!r}")

        raw_dim = payload.get("embedding_dim")
        dim: int | None = None
        if isinstance(raw_dim, int) and not isinstance(raw_dim, bool) and 0 < raw_dim <= MAX_VECTOR_DIM:
            dim = raw_dim

        entries: list[dict[str, Any]] = []
        dropped = 0
        for item in payload.get("entries") or []:
            if not isinstance(item, Mapping):
                dropped += 1
                continue
            vector = _clean_vector(item.get("vector"), dim)
            if vector is None:
                dropped += 1
                continue
            if dim is None:
                dim = len(vector)
            entry = {
                "vector": vector,
                "created_at": str(item.get("created_at") or "") or None,
                "hits": int(item.get("hits") or 0) if isinstance(item.get("hits"), int) else 0,
            }
            entries.append(entry)

        if entries and dim is None:
            return LoadResult(status=_STATUS_DIM, path=path, dropped=dropped,
                              detail="entries present but embedding_dim unusable")
        if dim is not None and not entries:
            # 只有维度没有条目：视作空池（不是错误）
            return LoadResult(status=_STATUS_OK, path=path, backend=self.backend,
                              embedding_dim=dim, entries=[], dropped=dropped,
                              updated_at=payload.get("updated_at"))
        # 截断到上限（保留最新的）
        if len(entries) > self.max_entries:
            dropped += len(entries) - self.max_entries
            entries = entries[-self.max_entries:]

        return LoadResult(status=_STATUS_OK, path=path, backend=self.backend,
                          embedding_dim=dim, entries=entries, dropped=dropped,
                          updated_at=payload.get("updated_at"))

    # ---------------------------------------------------------------- 写
    def persist(
        self,
        *,
        current_template: Sequence[float],
        history: Sequence[Sequence[float]],
        embedding_dim: int | None = None,
    ) -> dict[str, Any]:
        """把「当前模板 + 滚动历史」合并进池文件（原子 + flock）。

        合并语义：
          * 落盘只存 ``history``（每条是一个高置信度观测），``current_template``
            单独存一份，便于诊断"模板漂到哪了"；
          * 与已有条目余弦 ≥ ``DEDUPE_COSINE`` 的视为同一条，只累加 ``hits``；
          * 超过 ``max_entries`` 时丢最旧的。
        """
        from life_logger.atomic import file_lock, write_json_atomic

        template = _clean_vector(current_template, embedding_dim)
        if template is None:
            return {"status": "skipped", "reason": "invalid_current_template",
                    "path": str(self.path)}

        incoming: list[list[float]] = []
        for candidate in history:
            vector = _clean_vector(candidate, embedding_dim or len(template))
            if vector is not None:
                incoming.append(vector)
        if not incoming:
            return {"status": "skipped", "reason": "no_valid_history",
                    "path": str(self.path)}

        dim = embedding_dim or len(template)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = _now_iso()

        existing = self.load()
        # 坏文件 / 换后端 / 维度变了 ⇒ 不继承旧条目（否则一个文件里混两种维度的向量）。
        carry: list[dict[str, Any]] = []
        dim_changed = (
            existing.embedding_dim is not None
            and int(existing.embedding_dim) != int(dim)
        )
        if existing.usable and not dim_changed:
            carry = [dict(e) for e in existing.entries]
        elif existing.status not in {_STATUS_MISSING, _STATUS_OK}:
            carry = []

        merged = carry
        added = 0
        for vector in incoming:
            hit = None
            for entry in merged:
                if _cosine(entry["vector"], vector) >= DEDUPE_COSINE:
                    hit = entry
                    break
            if hit is None:
                merged.append({"vector": vector, "created_at": now, "hits": 1})
                added += 1
            else:
                hit["hits"] = int(hit.get("hits") or 0) + 1
                hit["created_at"] = now

        trimmed = max(0, len(merged) - self.max_entries)
        merged = merged[trimmed:]

        # 联系人槽位必须原样带过去：persist 是「本人池」的写入口，
        # 不能因为重写整份文件就把别人注册的联系人抹掉。
        carried_contacts: dict[str, Any] = {}
        try:
            raw_prior = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
            if isinstance(raw_prior, Mapping):
                carried_contacts = dict(raw_prior.get("contacts") or {})
        except (OSError, ValueError):
            carried_contacts = {}

        payload = {
            "schema": SCHEMA,
            "backend": self.backend,
            "embedding_dim": dim,
            "updated_at": now,
            "current_template": template,
            "entries": merged,
            "writer_pid": os.getpid(),
            **({"contacts": carried_contacts,
                "contacts_schema": CONTACTS_SCHEMA} if carried_contacts else {}),
            "last_merge": {
                "carried": len(carry),
                "incoming": len(incoming),
                "added": added,
                "trimmed": trimmed,
                "prior_status": existing.status,
            },
        }

        try:
            with file_lock(self.path):
                # 锁内重新读一次，避免"读到旧快照 + 覆盖别人刚写的条目"
                latest = self.load()
                if latest.usable and latest.entries != carry:
                    base = [dict(e) for e in latest.entries]
                    for entry in merged:
                        if any(_cosine(base_e["vector"], entry["vector"]) >= DEDUPE_COSINE
                               for base_e in base):
                            continue
                        base.append(entry)
                    payload["entries"] = base[-self.max_entries:]
                    payload["last_merge"]["reloaded_under_lock"] = True
                write_json_atomic(self.path, payload)
        except Exception as exc:  # 池子写失败绝不能拖垮转写
            return {"status": "error", "reason": f"{type(exc).__name__}: {exc}",
                    "path": str(self.path)}

        return {"status": "ok", "path": str(self.path), "entries": len(payload["entries"]),
                "added": added, "trimmed": trimmed, "prior_status": existing.status}

    # ---------------------------------------------------------------- 灌进池
    def hydrate(self, pool: Any) -> dict[str, Any]:
        """把落盘的历史灌进一个 `RollingVoiceprintPool`（就地修改），返回结果摘要。

        只做三件事：
          1. ``history`` 用落盘条目替换（保留脚本次的模板作为第 0 条，避免丢掉基线）；
          2. 若落盘的 ``current_template`` 维度一致，则用它作为当前模板（模板漂移的载体）；
          3. 把 ``initial_template`` 保持为**本次的模板**（不覆盖基线，便于审计漂移量）。
        维度/后端不一致时**什么都不做**，只回报状态。
        """
        result = self.load()
        info = result.to_dict()
        if not result.usable:
            return info
        dim = result.embedding_dim
        current = getattr(pool, "current_template", None)
        if not isinstance(current, list) or not current:
            return {**info, "status": "skipped", "detail": "pool_has_no_template"}
        if dim is not None and len(current) != dim:
            return {**info, "status": _STATUS_DIM,
                    "detail": f"pool dim={len(current)} file dim={dim}"}

        history: list[list[float]] = [list(current)]
        seen: list[list[float]] = [list(current)]
        for entry in result.entries:
            vector = entry["vector"]
            if any(_cosine(vector, prior) >= DEDUPE_COSINE for prior in seen):
                continue
            seen.append(vector)
            history.append(list(vector))
        pool.history = history[-int(getattr(pool, "max_history", 20) or 20):]

        stored_template = _clean_vector(self._read_current_template(), dim)
        if stored_template is not None:
            pool.current_template = stored_template
            info["template_source"] = "store"
        else:
            info["template_source"] = "segment"
        info["hydrated_history"] = len(pool.history)
        return info

    def _read_current_template(self) -> Any:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, Mapping):
            return None
        return payload.get("current_template")


def store_from_env(
    *,
    backend: str | None = None,
    env: Mapping[str, str] | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> VoiceprintPoolStore | None:
    """按环境变量构造 store；未配置则返回 None（= 完全退回旧行为）。"""
    path = env_pool_path(env)
    if path is None:
        return None
    return VoiceprintPoolStore(path, backend=backend, max_entries=max_entries)


__all__ = [
    "SCHEMA", "CONTACTS_SCHEMA", "ENV_POOL_PATH", "ENV_POOL_ENABLED",
    "DEFAULT_MAX_ENTRIES", "DEFAULT_MAX_CONTACT_EMBEDDINGS",
    "CONTACT_MATCH_DEFAULT_THRESHOLD", "CONTACT_DEDUPE_COSINE", "CONTACT_ID_RE",
    "LoadResult", "VoiceprintPoolStore", "store_from_env", "env_pool_path",
    "ContactPoolEntry", "ContactMatch",
    "load_contacts", "register_contact_embedding", "match_contact",
]
