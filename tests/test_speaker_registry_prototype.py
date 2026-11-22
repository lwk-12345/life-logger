"""常驻联系人声纹：原型 + 接线契约的最小回归。

只跑纯函数，不读音频、不碰 output/、不调模型。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "speaker_registry_prototype", ROOT / "scripts/speaker_registry_prototype.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

from life_logger.speaker_registry import ResidentSpeakerRegistry  # noqa: E402


def unit(*weights: float) -> list[float]:
    """构造一个可复现的单位向量。"""
    n = sum(w * w for w in weights) ** 0.5 or 1.0
    return [w / n for w in weights]


A = unit(1.0, 0.0, 0.05)
B = unit(0.0, 1.0, 0.05)
A2 = unit(1.0, 0.10, 0.05)
B2 = unit(0.05, 1.0, 0.0)


# ─────────────────────────────────────────── 1) 聚类稳定
def test_clustering_is_deterministic():
    """同一输入跑两次必须完全一致（簇号抖动会让名字在两次运行间乱跳）。"""
    embs = [A, B, A2, B2, A]
    first = P.cluster_embeddings(embs, threshold=0.40)
    second = P.cluster_embeddings(embs, threshold=0.40)
    assert first == second
    assert len(set(first)) == 2, f"两组合成向量应分成 2 簇，实际 {first}"


def test_clustering_is_order_stable_for_same_membership():
    """把输入顺序打乱，**分组结果**（等价关系）必须一致。"""
    base = [A, B, A2, B2]
    order = [2, 0, 3, 1]
    shuffled = [base[i] for i in order]
    lab_a = P.cluster_embeddings(base, threshold=0.40)
    lab_b = P.cluster_embeddings(shuffled, threshold=0.40)

    def same_group(labels, i, j):
        return labels[i] == labels[j]

    for i in range(len(base)):
        for j in range(len(base)):
            ii = order.index(i)
            jj = order.index(j)
            assert same_group(lab_a, i, j) == same_group(lab_b, ii, jj), (
                f"({i},{j}) 的分组关系随顺序变了：{lab_a} vs {lab_b}")


def test_cluster_summary_sorts_and_counts():
    labels = [0, 0, 1]
    durs = [30.0, 20.0, 5.0]
    texts = ["甲", "乙", "丙"]
    out = P.summarize_clusters(labels, durs, texts)
    assert [c["cluster"] for c in out] == [0, 1], "必须按时长降序"
    assert out[0]["seconds"] == 50.0 and out[0]["count"] == 2
    assert out[1]["seconds"] == 5.0


# ─────────────────────────────────────────── 2) self 不被改标
def test_named_annotation_never_touches_self():
    """self 段**永远**不挂联系人名字（"本人"与"某联系人"互斥）。"""
    rec = {"source_label": "self", "voice_similarity": 0.81, "review_required": False}
    out = P.annotate_speaker_name(rec, cluster_id="c1", name="挚友甲哥", similarity=0.9)
    assert out == rec, "self 记录被改了"
    assert "speaker_name" not in out


def test_named_annotation_only_adds_keys():
    rec = {"source_label": "non_self", "voice_similarity": 0.52, "review_required": True}
    out = P.annotate_speaker_name(rec, cluster_id="c7", name="挚友甲哥", similarity=0.61234)
    assert out["source_label"] == "non_self", "source_label 不许改"
    assert out["voice_similarity"] == 0.52, "相似度不许改"
    assert out["review_required"] is True, "复核标记不许清"
    assert out["speaker_name"] == "挚友甲哥"
    assert out["speaker_cluster_id"] == "c7"
    assert out["speaker_similarity"] == 0.6123
    assert set(rec) <= set(out), "只增不删"


def test_named_annotation_refuses_half_fields():
    rec = {"source_label": "unknown"}
    assert P.annotate_speaker_name(rec, cluster_id="c1") == rec, "只有簇号没名字，不许写半截"
    assert P.annotate_speaker_name(rec, name="挚友甲哥") == rec, "只有名字没簇号，不许写半截"


def test_registry_identify_does_not_mutate_input():
    reg = ResidentSpeakerRegistry()
    reg.register_primary_speaker("spk_primary", [1.0, 0.0, 0.0], name="Self")
    reg.register_contact("c_heming", [0.0, 1.0, 0.0], name="挚友甲哥")
    probe = [0.02, 1.0, 0.0]
    snapshot = list(probe)
    hit = reg.identify_speaker(probe)
    assert probe == snapshot, "identify_speaker 不该改动入参"
    assert hit["label"] == "contact" and hit["name"] == "挚友甲哥"
    assert hit["speaker_id"] == "c_heming"


def test_registry_contact_does_not_steal_primary():
    """本人向量必须判 primary，不能被联系人抢走。"""
    reg = ResidentSpeakerRegistry()
    reg.register_primary_speaker("spk_primary", [1.0, 0.0, 0.0], name="Self")
    reg.register_contact("c_heming", [0.30, 0.95, 0.0], name="挚友甲哥")
    hit = reg.identify_speaker([1.0, 0.02, 0.0])
    assert hit["label"] == "primary" and hit["name"] == "Self"


# ─────────────────────────────────────────── 3) 空数据不崩
@pytest.mark.parametrize("empty", [None, [], ()])
def test_empty_embeddings_do_not_crash(empty):
    assert P.cluster_embeddings(empty) == []
    assert P.summarize_clusters([], [], []) == []
    q = P.quality_report([], empty)
    assert q["clusters"] == 0


def test_all_empty_vectors_give_only_negatives():
    labels = P.cluster_embeddings([None, [], None])
    assert labels == [-1, -1, -1], "全是空向量时应全部标 -1，且不新开簇"


def test_empty_vectors_are_skipped_not_crashing():
    labels = P.cluster_embeddings([A, [], B], threshold=0.40)
    assert labels[1] == -1, "空向量标 -1，不参与聚类"
    assert len(labels) == 3


@pytest.mark.parametrize("bad", [None, 42, "x", []])
def test_annotate_handles_non_dict(bad):
    assert P.annotate_speaker_name(bad, cluster_id="c1", name="x") == {}


@pytest.mark.parametrize("bad", [{}, {"source_label": ""}, {"source_label": "self"}])
def test_annotate_skips_when_label_not_named_contact(bad):
    """缺 source_label / 空值 / self 都不许挂名字（与 NAMED_CONTACT_LABELS 同口径）。"""
    assert P.annotate_speaker_name(bad, cluster_id="c1", name="x") == bad


def test_registry_empty_probe_is_unknown_not_self():
    reg = ResidentSpeakerRegistry()
    reg.register_primary_speaker("spk_primary", [1.0, 0.0], name="Self")
    for probe in (None, [], [0.0, 0.0]):
        hit = reg.identify_speaker(probe)
        assert hit["speaker_id"] == "unknown" or hit["label"] == "cluster"
        assert hit["confidence"] == 0.0, "空探针不能给出置信度"
