"""Resident speaker voiceprint registry for multi-tier speaker identification."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

import numpy as np

SpeakerLabel = Literal["primary", "contact", "cluster"]

SCHEMA_VERSION = "resident-speaker-registry-v1"

# Thresholds for voiceprint cosine similarity matching
DEFAULT_PRIMARY_THRESHOLD = 0.60
DEFAULT_CONTACT_THRESHOLD = 0.50
DEFAULT_CLUSTER_THRESHOLD = 0.40


def _cosine_similarity(v1: Sequence[float], v2: Sequence[float]) -> float:
    """Calculate cosine similarity between two non-zero vectors."""
    if len(v1) != len(v2) or not v1:
        return 0.0
    a = np.asarray(v1, dtype=np.float64)
    b = np.asarray(v2, dtype=np.float64)
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if (
        norm_a == 0.0
        or norm_b == 0.0
        or not math.isfinite(norm_a)
        or not math.isfinite(norm_b)
    ):
        return 0.0
    dot = float(np.dot(a, b))
    sim = dot / (norm_a * norm_b)
    # Clamp to [-1.0, 1.0] to guard numerical precision
    return max(-1.0, min(1.0, sim))


def _normalize_embeddings(
    embedding_or_embeddings: Any,
) -> list[list[float]]:
    """Convert 1D or 2D vector or array into list[list[float]]."""
    if embedding_or_embeddings is None:
        return []
    arr = np.asarray(embedding_or_embeddings, dtype=np.float64)
    if arr.size == 0:
        return []
    if arr.ndim == 1:
        return [arr.tolist()]
    elif arr.ndim == 2:
        return [row.tolist() for row in arr]
    else:
        arr = arr.reshape(-1, arr.shape[-1])
        return [row.tolist() for row in arr]


@dataclass
class SpeakerProfile:
    speaker_id: str
    label: SpeakerLabel
    name: str | None = None
    relation: str | None = None
    embeddings: list[list[float]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def best_similarity(self, target_embedding: Sequence[float]) -> float:
        """Find max cosine similarity against all stored exemplar embeddings."""
        if not self.embeddings:
            return 0.0
        return max(
            _cosine_similarity(exemplar, target_embedding)
            for exemplar in self.embeddings
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker_id": self.speaker_id,
            "label": self.label,
            "name": self.name,
            "relation": self.relation,
            "embeddings": self.embeddings,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SpeakerProfile:
        return cls(
            speaker_id=str(data["speaker_id"]),
            label=data["label"],
            name=data.get("name"),
            relation=data.get("relation"),
            embeddings=[list(map(float, emb)) for emb in data.get("embeddings", [])],
            metadata=dict(data.get("metadata", {})),
        )


class ResidentSpeakerRegistry:
    """Multi-tier speaker voiceprint registry for primary, contacts, and clusters."""

    def __init__(
        self,
        *,
        primary_threshold: float = DEFAULT_PRIMARY_THRESHOLD,
        contact_threshold: float = DEFAULT_CONTACT_THRESHOLD,
        cluster_threshold: float = DEFAULT_CLUSTER_THRESHOLD,
    ) -> None:
        self.primary_threshold = primary_threshold
        self.contact_threshold = contact_threshold
        self.cluster_threshold = cluster_threshold

        self.primary_speaker: SpeakerProfile | None = None
        self.contacts: dict[str, SpeakerProfile] = {}
        self.clusters: dict[str, SpeakerProfile] = {}

    def set_primary_speaker(
        self,
        template_val: Any = None,
        name: str = "Self",
        speaker_id: str = "spk_primary",
        metadata: dict[str, Any] | None = None,
    ) -> SpeakerProfile:
        """Convenience method to set primary speaker with template value or embedding."""
        if isinstance(template_val, (int, float)):
            emb: list[float] = [float(template_val)]
        elif isinstance(template_val, (list, tuple, np.ndarray)):
            emb = list(template_val)
        else:
            emb = [1.0]
        return self.register_primary_speaker(
            speaker_id=speaker_id,
            embedding=emb,
            name=name,
            metadata=metadata,
        )

    def register_primary_speaker(
        self,
        speaker_id: str,
        embedding: Sequence[float] | Sequence[Sequence[float]] | np.ndarray,
        name: str = "Self",
        metadata: dict[str, Any] | None = None,
    ) -> SpeakerProfile:
        """Register or update the primary resident speaker."""
        embs = _normalize_embeddings(embedding)
        profile = SpeakerProfile(
            speaker_id=speaker_id,
            label="primary",
            name=name,
            relation="self",
            embeddings=embs,
            metadata=metadata or {},
        )
        self.primary_speaker = profile
        return profile

    def register_contact(
        self,
        speaker_id: str,
        embedding: Sequence[float] | Sequence[Sequence[float]] | np.ndarray,
        name: str,
        relation: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SpeakerProfile:
        """Register a frequent contact (family, friend, colleague)."""
        embs = _normalize_embeddings(embedding)
        profile = SpeakerProfile(
            speaker_id=speaker_id,
            label="contact",
            name=name,
            relation=relation,
            embeddings=embs,
            metadata=metadata or {},
        )
        self.contacts[speaker_id] = profile
        return profile

    def register_cluster(
        self,
        cluster_id: str,
        embedding: Sequence[float] | Sequence[Sequence[float]] | np.ndarray,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SpeakerProfile:
        """Register a recurring raw voiceprint cluster."""
        embs = _normalize_embeddings(embedding)
        profile = SpeakerProfile(
            speaker_id=cluster_id,
            label="cluster",
            name=name,
            embeddings=embs,
            metadata=metadata or {},
        )
        self.clusters[cluster_id] = profile
        return profile

    def add_embedding(
        self,
        speaker_id: str,
        embedding: Sequence[float] | Sequence[Sequence[float]] | np.ndarray,
    ) -> bool:
        """Add additional exemplar embedding(s) to an existing speaker profile."""
        embs = _normalize_embeddings(embedding)
        if not embs:
            return False
        if self.primary_speaker and self.primary_speaker.speaker_id == speaker_id:
            self.primary_speaker.embeddings.extend(embs)
            return True
        if speaker_id in self.contacts:
            self.contacts[speaker_id].embeddings.extend(embs)
            return True
        if speaker_id in self.clusters:
            self.clusters[speaker_id].embeddings.extend(embs)
            return True
        return False

    def identify_speaker(
        self,
        embedding_or_feature: Any,
        auto_cluster: bool = False,
    ) -> dict[str, Any]:
        """Identify speaker from voiceprint embedding vector.

        Returns {speaker_id: str, label: 'primary' | 'contact' | 'cluster',
                 name: str | None, confidence: float} per interface contract.
        """
        if embedding_or_feature is None:
            return {
                "speaker_id": "unknown",
                "label": "cluster",
                "name": None,
                "confidence": 0.0,
            }

        arr = np.asarray(embedding_or_feature, dtype=np.float64).flatten()
        if arr.size == 0:
            return {
                "speaker_id": "unknown",
                "label": "cluster",
                "name": None,
                "confidence": 0.0,
            }
        embedding = arr.tolist()

        # 1. Tier 1: Evaluate primary speaker
        primary_sim = -1.0
        if self.primary_speaker and self.primary_speaker.embeddings:
            primary_sim = self.primary_speaker.best_similarity(embedding)

        # 2. Tier 2: Evaluate frequent contacts
        best_contact: SpeakerProfile | None = None
        best_contact_sim = -1.0
        for contact in self.contacts.values():
            sim = contact.best_similarity(embedding)
            if sim > best_contact_sim:
                best_contact_sim = sim
                best_contact = contact

        # If contact exceeds contact threshold and has higher similarity than primary
        if (
            best_contact is not None
            and best_contact_sim >= self.contact_threshold
            and best_contact_sim > primary_sim
        ):
            return {
                "speaker_id": best_contact.speaker_id,
                "label": "contact",
                "name": best_contact.name,
                "confidence": round(best_contact_sim, 4),
            }

        # Otherwise if primary meets primary threshold
        if self.primary_speaker and primary_sim >= self.primary_threshold:
            return {
                "speaker_id": self.primary_speaker.speaker_id,
                "label": "primary",
                "name": self.primary_speaker.name,
                "confidence": round(primary_sim, 4),
            }

        # If primary was below primary_threshold, check if contact met contact threshold
        if best_contact is not None and best_contact_sim >= self.contact_threshold:
            return {
                "speaker_id": best_contact.speaker_id,
                "label": "contact",
                "name": best_contact.name,
                "confidence": round(best_contact_sim, 4),
            }

        # 3. Tier 3: Check raw clusters
        best_cluster: SpeakerProfile | None = None
        best_cluster_sim = -1.0
        for cluster in self.clusters.values():
            sim = cluster.best_similarity(embedding)
            if sim > best_cluster_sim:
                best_cluster_sim = sim
                best_cluster = cluster

        if best_cluster is not None and best_cluster_sim >= self.cluster_threshold:
            return {
                "speaker_id": best_cluster.speaker_id,
                "label": "cluster",
                "name": best_cluster.name,
                "confidence": round(best_cluster_sim, 4),
            }

        # 4. If no existing profile matches above threshold
        if auto_cluster:
            cluster_id = f"cluster-{len(self.clusters) + 1:03d}"
            self.register_cluster(cluster_id, embedding)
            return {
                "speaker_id": cluster_id,
                "label": "cluster",
                "name": None,
                "confidence": 1.0,
            }

        # Return closest cluster or generic unknown cluster
        highest_sim = max(best_contact_sim, best_cluster_sim, 0.0)
        fallback_id = (
            best_cluster.speaker_id
            if best_cluster
            else f"spk_cluster_unknown_{abs(hash(tuple(embedding[:10]))):06x}"
        )
        return {
            "speaker_id": fallback_id,
            "label": "cluster",
            "name": best_cluster.name if best_cluster else None,
            "confidence": round(max(0.0, highest_sim), 4),
        }

    def get_speaker(self, speaker_id: str) -> dict[str, Any] | None:
        """Get profile details by speaker_id."""
        if self.primary_speaker and self.primary_speaker.speaker_id == speaker_id:
            return self.primary_speaker.to_dict()
        if speaker_id in self.contacts:
            return self.contacts[speaker_id].to_dict()
        if speaker_id in self.clusters:
            return self.clusters[speaker_id].to_dict()
        return None

    def list_speakers(self, label: str | None = None) -> list[dict[str, Any]]:
        """List all speakers or filter by label ('primary', 'contact', 'cluster')."""
        results: list[dict[str, Any]] = []
        if self.primary_speaker and (label is None or label == "primary"):
            results.append(self.primary_speaker.to_dict())
        if label is None or label == "contact":
            for contact in self.contacts.values():
                results.append(contact.to_dict())
        if label is None or label == "cluster":
            for cluster in self.clusters.values():
                results.append(cluster.to_dict())
        return results

    def delete_speaker(self, speaker_id: str) -> bool:
        """Delete speaker from registry."""
        if self.primary_speaker and self.primary_speaker.speaker_id == speaker_id:
            self.primary_speaker = None
            return True
        if speaker_id in self.contacts:
            del self.contacts[speaker_id]
            return True
        if speaker_id in self.clusters:
            del self.clusters[speaker_id]
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        """Serialize registry to JSON-compatible dictionary."""
        return {
            "schema_version": SCHEMA_VERSION,
            "primary_threshold": self.primary_threshold,
            "contact_threshold": self.contact_threshold,
            "cluster_threshold": self.cluster_threshold,
            "primary_speaker": self.primary_speaker.to_dict()
            if self.primary_speaker
            else None,
            "contacts": [c.to_dict() for c in self.contacts.values()],
            "clusters": [c.to_dict() for c in self.clusters.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResidentSpeakerRegistry:
        """Instantiate registry from serialized dictionary."""
        registry = cls(
            primary_threshold=float(
                data.get("primary_threshold", DEFAULT_PRIMARY_THRESHOLD)
            ),
            contact_threshold=float(
                data.get("contact_threshold", DEFAULT_CONTACT_THRESHOLD)
            ),
            cluster_threshold=float(
                data.get("cluster_threshold", DEFAULT_CLUSTER_THRESHOLD)
            ),
        )
        if data.get("primary_speaker"):
            registry.primary_speaker = SpeakerProfile.from_dict(data["primary_speaker"])
        for contact_data in data.get("contacts", []):
            contact = SpeakerProfile.from_dict(contact_data)
            registry.contacts[contact.speaker_id] = contact
        for cluster_data in data.get("clusters", []):
            cluster = SpeakerProfile.from_dict(cluster_data)
            registry.clusters[cluster.speaker_id] = cluster
        return registry

    def save_to_file(self, path: str | Path) -> None:
        """Save registry to JSON file."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load_from_file(cls, path: str | Path) -> ResidentSpeakerRegistry:
        """Load registry from JSON file."""
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(f"Speaker registry file not found: {target}")
        data = json.loads(target.read_text(encoding="utf-8"))
        return cls.from_dict(data)
