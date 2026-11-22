"""Q806：声纹滚动池跨进程持久化。

要守的四条：
  1. **跨段累积**：段 2 必须能看到段 1 的观测（这是本任务的唯一目标）；
  2. **并发安全**：多个子进程同时写同一池文件，文件永远合法、条目不互相覆盖；
  3. **坏文件不致命**：截断/换 schema/换维度 → 退回"只有初始模板"，绝不半信半疑地用；
  4. **默认零变化**：没配置池路径时，行为与改造前**逐字节等价**（history 只有模板）。
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.source_attribution import RollingVoiceprintPool  # noqa: E402
from life_logger.voiceprint_pool_store import (  # noqa: E402
    CONTACTS_SCHEMA,
    DEFAULT_MAX_CONTACT_EMBEDDINGS,
    DEFAULT_MAX_ENTRIES,
    ENV_POOL_ENABLED,
    ENV_POOL_PATH,
    SCHEMA,
    VoiceprintPoolStore,
    env_pool_path,
    load_contacts,
    match_contact,
    register_contact_embedding,
)

DIM = 16


def unit(*weights: float) -> list[float]:
    """构造一个可复现的定向向量（长度 DIM）。"""
    vec = [0.0] * DIM
    for i, w in enumerate(weights):
        vec[i % DIM] += float(w)
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


BASE = unit(1.0, 0.0)
SEG1 = unit(1.0, 0.10)
SEG2 = unit(1.0, 0.20)
SEG3 = unit(1.0, 0.30)


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


# ─────────────────────────────────────────────── 1) 三段累积（本任务的核心）
def test_pool_accumulates_across_three_segments(tmp_path: Path):
    pool_file = tmp_path / "pool.json"

    # ---- 段 1：全新进程 ----
    p1 = RollingVoiceprintPool.from_template(BASE, pool_path=pool_file)
    assert p1.pool_status["status"] == "missing"        # 首次没有池文件
    assert len(p1.history) == 1
    before = _cos(p1.current_template, SEG1)
    assert p1.maybe_update(SEG1, 0.95) is True
    assert _cos(p1.current_template, SEG1) > before    # 模板朝段 1 漂了一点
    assert pool_file.is_file()

    # ---- 段 2：模拟新子进程（重新 from_template）----
    p2 = RollingVoiceprintPool.from_template(BASE, pool_path=pool_file)
    assert p2.pool_status["status"] == "ok"
    assert p2.pool_status["hydrated_history"] >= 2     # 段 1 的观测被带过来了
    assert _cos(p2.current_template, SEG1) > _cos(BASE, SEG1)  # 当前模板来自落盘
    # 段 2 的候选与段 1 的观测更接近（证明池子真的累积了）
    assert max(_cos(v, SEG2) for v in p2.history) > _cos(BASE, SEG2)
    assert p2.maybe_update(SEG2, 0.95) is True

    # ---- 段 3 ----
    p3 = RollingVoiceprintPool.from_template(BASE, pool_path=pool_file)
    assert p3.pool_status["status"] == "ok"
    assert p3.pool_status["entries"] >= 2
    assert max(_cos(v, SEG1) for v in p3.history) > 0.9
    assert max(_cos(v, SEG2) for v in p3.history) > 0.9
    assert p3.maybe_update(SEG3, 0.95) is True

    payload = json.loads(pool_file.read_text(encoding="utf-8"))
    assert payload["schema"] == SCHEMA
    assert payload["embedding_dim"] == DIM
    assert len(payload["entries"]) >= 3
    assert payload["last_merge"]["incoming"] >= 1


def test_history_grows_monotonically_across_segments(tmp_path: Path):
    pool_file = tmp_path / "pool.json"
    sizes = []
    for emb in (SEG1, SEG2, SEG3):
        pool = RollingVoiceprintPool.from_template(BASE, pool_path=pool_file)
        pool.maybe_update(emb, 0.95)
        sizes.append(len(pool.history))
    assert sizes == sorted(sizes), sizes
    assert sizes[-1] > sizes[0]


# ─────────────────────────────────────────────── 2) 并发安全
RUNNER = r'''
import sys, json
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from life_logger.source_attribution import RollingVoiceprintPool
pool_path = Path(sys.argv[2]); dim = int(sys.argv[3]); idx = int(sys.argv[4])
base = [1.0] + [0.0] * (dim - 1)
cand = [1.0] + [0.0] * (dim - 1)
cand[1 + (idx % (dim - 1))] = 0.5
pool = RollingVoiceprintPool.from_template(base, pool_path=pool_path)
pool.maybe_update(cand, 0.95)
print(json.dumps({"pid": __import__("os").getpid(), "history": len(pool.history)}))
'''


def test_concurrent_writers_keep_file_valid(tmp_path: Path):
    runner = tmp_path / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    pool_file = tmp_path / "pool.json"

    procs = [
        subprocess.Popen(
            [sys.executable, str(runner), str(ROOT / "src"), str(pool_file), str(DIM), str(i)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for i in range(6)
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err[-600:]
        outs.append(json.loads(out.strip().splitlines()[-1]))

    payload = json.loads(pool_file.read_text(encoding="utf-8"))       # 必须仍是合法 JSON
    assert payload["schema"] == SCHEMA
    assert payload["embedding_dim"] == DIM
    assert 1 <= len(payload["entries"]) <= DEFAULT_MAX_ENTRIES
    assert len(outs) == 6
    # 至少有一个进程看到了别人的条目（说明真的共享了同一个池，而不是各写各的）
    assert max(o["history"] for o in outs) > 1
    assert list(pool_file.parent.glob("*tmp*")) == []


# ─────────────────────────────────────────────── 3) 坏文件不致命
def test_corrupt_json_falls_back_to_template(tmp_path: Path):
    pool_file = tmp_path / "pool.json"
    pool_file.write_text('{"schema": "voiceprint-pool-v1", "entries": [', encoding="utf-8")

    pool = RollingVoiceprintPool.from_template(BASE, pool_path=pool_file)
    assert pool.pool_status["status"] == "corrupt"
    assert len(pool.history) == 1                       # 只有模板，没有半信半疑的数据
    assert pool.current_template == pytest.approx(BASE)

    # 仍可正常更新，并**修复**文件
    assert pool.maybe_update(SEG1, 0.95) is True
    fixed = json.loads(pool_file.read_text(encoding="utf-8"))
    assert fixed["schema"] == SCHEMA and fixed["entries"]


def test_schema_mismatch_is_ignored(tmp_path: Path):
    pool_file = tmp_path / "pool.json"
    pool_file.write_text(json.dumps({"schema": "old-v0", "entries": [{"vector": SEG1}]}),
                         encoding="utf-8")
    pool = RollingVoiceprintPool.from_template(BASE, pool_path=pool_file)
    assert pool.pool_status["status"] == "schema_mismatch"
    assert len(pool.history) == 1


def test_dim_mismatch_is_ignored(tmp_path: Path):
    pool_file = tmp_path / "pool.json"
    store = VoiceprintPoolStore(pool_file, backend="campplus")
    store.persist(current_template=[1.0] * 8, history=[[1.0] * 8], embedding_dim=8)
    pool = RollingVoiceprintPool.from_template(BASE, pool_path=pool_file, backend="campplus")
    assert pool.pool_status["status"] == "dim_mismatch"
    assert len(pool.history) == 1


def test_backend_change_drops_old_entries(tmp_path: Path):
    pool_file = tmp_path / "pool.json"
    VoiceprintPoolStore(pool_file, backend="numpy48").persist(
        current_template=[1.0] * 48, history=[[1.0] * 48], embedding_dim=48)
    store = VoiceprintPoolStore(pool_file, backend="campplus")
    result = store.load()
    assert result.status == "backend_mismatch"
    written = store.persist(current_template=[1.0] * 8, history=[[1.0] * 8], embedding_dim=8)
    assert written["status"] == "ok"
    payload = json.loads(pool_file.read_text(encoding="utf-8"))
    assert payload["embedding_dim"] == 8
    assert all(len(e["vector"]) == 8 for e in payload["entries"])


def test_entries_are_capped_and_deduped(tmp_path: Path):
    pool_file = tmp_path / "pool.json"
    store = VoiceprintPoolStore(pool_file, max_entries=3)
    for i in range(10):
        vec = unit(1.0, 0.01 * (i + 1))
        store.persist(current_template=vec, history=[vec], embedding_dim=DIM)
    payload = json.loads(pool_file.read_text(encoding="utf-8"))
    assert len(payload["entries"]) <= 3

    # 同一条重复写不会长条目
    before = len(payload["entries"])
    store.persist(current_template=unit(1.0, 0.01), history=[unit(1.0, 0.01)], embedding_dim=DIM)
    after = len(json.loads(pool_file.read_text(encoding="utf-8"))["entries"])
    assert after == before


# ─────────────────────────────────────────────── 4) 默认零变化
def test_default_behavior_unchanged_without_store(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(ENV_POOL_PATH, raising=False)
    monkeypatch.delenv(ENV_POOL_ENABLED, raising=False)
    pool = RollingVoiceprintPool.from_template(BASE)
    assert pool.pool_store is None
    assert pool.pool_status is None
    assert len(pool.history) == 1
    assert pool.maybe_update(SEG1, 0.95) is True
    assert pool.flush_to_store() is None               # 没有 store 时是 no-op
    assert list(tmp_path.iterdir()) == []


def test_env_switch_can_disable_store(tmp_path: Path, monkeypatch):
    pool_file = tmp_path / "pool.json"
    monkeypatch.setenv(ENV_POOL_PATH, str(pool_file))
    monkeypatch.setenv(ENV_POOL_ENABLED, "0")
    assert env_pool_path() is None
    pool = RollingVoiceprintPool.from_template(BASE)
    assert pool.pool_store is None


def test_env_path_is_picked_up(tmp_path: Path, monkeypatch):
    pool_file = tmp_path / "pool.json"
    monkeypatch.setenv(ENV_POOL_PATH, str(pool_file))
    monkeypatch.delenv(ENV_POOL_ENABLED, raising=False)
    pool = RollingVoiceprintPool.from_template(BASE)
    assert pool.pool_store is not None
    assert pool.pool_status["status"] == "missing"
    pool.maybe_update(SEG1, 0.95)
    assert pool_file.is_file()


def test_update_below_threshold_does_not_write(tmp_path: Path):
    pool_file = tmp_path / "pool.json"
    pool = RollingVoiceprintPool.from_template(BASE, pool_path=pool_file)
    assert pool.maybe_update(SEG1, 0.10) is False       # 低于 min_update_similarity
    assert not pool_file.exists()


# ─────────────────────────────────────────────── 5) watch_formal_asr 接线
def test_watch_wires_pool_path_per_device(tmp_path: Path):
    """`_voiceprint_pool_path` 必须给出「按设备区分」的路径，且能被配置改/关。"""
    from types import SimpleNamespace

    from life_logger.watch_formal_asr import WatchFormalASRProcessor

    stub = SimpleNamespace(store=SimpleNamespace(root=tmp_path))
    fn = WatchFormalASRProcessor._voiceprint_pool_path

    default = fn(stub, {}, "watch-abc")
    assert default == tmp_path / "voiceprints" / "pool-watch-abc.json"

    custom = fn(stub, {"voiceprint_pool": {"path": str(tmp_path / "custom-{device}.json")}}, "watch-xyz")
    assert custom == tmp_path / "custom-watch-xyz.json"

    assert fn(stub, {"voiceprint_pool": {"enabled": False}}, "watch-abc") is None


def test_from_template_records_backend_for_next_process(tmp_path: Path):
    """moss_transcribe 必须把生效 backend 带进 store；下一段换后端要硬拒绝。"""
    pool_file = tmp_path / "pool.json"

    p1 = RollingVoiceprintPool.from_template(BASE, pool_path=pool_file, backend="campplus")
    assert p1.maybe_update(SEG1, 0.95) is True
    payload = json.loads(pool_file.read_text(encoding="utf-8"))
    assert payload["backend"] == "campplus"

    same = RollingVoiceprintPool.from_template(BASE, pool_path=pool_file, backend="campplus")
    assert same.pool_status["status"] == "ok"
    assert len(same.history) >= 2

    other = RollingVoiceprintPool.from_template(BASE, pool_path=pool_file, backend="numpy48")
    assert other.pool_status["status"] == "backend_mismatch"
    assert len(other.history) == 1          # 不继承别的后端的向量


# ═══════════════════════════════════════════ 5) 命名联系人槽位（2026-11-04 Q807）
#: 联系人的子进程脚本：register 或 match，结果以 JSON 打到 stdout 最后一行
CONTACT_RUNNER = r"""
import json, math, sys
sys.path.insert(0, sys.argv[1])
from life_logger.voiceprint_pool_store import match_contact, register_contact_embedding

path, action, cid = sys.argv[2], sys.argv[3], sys.argv[4]
dim = int(sys.argv[5]); seed = float(sys.argv[6])

vec = [0.0] * dim
vec[0] = 1.0
vec[1] = seed
norm = math.sqrt(sum(v * v for v in vec)) or 1.0
vec = [v / norm for v in vec]

if action == "register":
    out = register_contact_embedding(path, cid, vec, label=cid)
else:
    m = match_contact(path, vec)
    out = m.to_dict() if m else {"matched": False}
print(json.dumps(out, ensure_ascii=False))
"""

MOM = unit(1.0, 0.02)          # 妈妈（近场）
MOM_FAR = unit(1.0, 0.35)      # 妈妈（远场/免提，同一人不同信道；与近场余弦≈0.94）
STRANGER = unit(0.05, 1.0)     # 路人


def test_contact_register_and_match_roundtrip(tmp_path: Path):
    pool = tmp_path / "pool.json"
    out = register_contact_embedding(pool, "mom", MOM, label="妈妈")
    assert out["status"] == "ok" and out["added"] == 1 and out["label"] == "妈妈"

    payload = json.loads(pool.read_text(encoding="utf-8"))
    assert payload["schema"] == SCHEMA                 # 顶层 schema 不变
    assert payload["contacts_schema"] == CONTACTS_SCHEMA
    assert "mom" in payload["contacts"]
    assert len(payload["contacts"]["mom"]["embeddings"]) == 1

    hit = match_contact(pool, MOM)
    assert hit is not None and hit.contact_id == "mom" and hit.label == "妈妈"
    assert 0.99 < hit.similarity <= 1.000001
    assert hit.to_dict()["matched"] is True


def test_contact_match_multi_channel_and_stranger_rejected(tmp_path: Path):
    pool = tmp_path / "pool.json"
    register_contact_embedding(pool, "mom", MOM, label="妈妈")
    register_contact_embedding(pool, "mom", MOM_FAR, label="妈妈")    # 第二次：另一个信道
    assert len(load_contacts(pool)["mom"].embeddings) == 2

    far = match_contact(pool, MOM_FAR, threshold=0.9)
    assert far is not None and far.contact_id == "mom", "多点注册应覆盖远场那一次"
    near = match_contact(pool, MOM, threshold=0.9)
    assert near is not None and near.contact_id == "mom", "近场那次也不能丢"

    assert match_contact(pool, STRANGER) is None, "陌生人必须判不中"
    assert match_contact(pool, STRANGER, threshold=0.0).contact_id == "mom", "阈值放宽才会命中"


def test_contact_duplicate_increments_hits_not_vectors(tmp_path: Path):
    pool = tmp_path / "pool.json"
    register_contact_embedding(pool, "mom", MOM)
    again = register_contact_embedding(pool, "mom", MOM)
    assert again["added"] == 0 and again["duplicate"] is True
    entry = load_contacts(pool)["mom"]
    assert len(entry.embeddings) == 1 and entry.hits == 2


def test_contact_entries_are_capped(tmp_path: Path):
    pool = tmp_path / "pool.json"
    for i in range(12):
        register_contact_embedding(pool, "mom", unit(1.0, 0.3 + i * 0.05), max_per_contact=3)
    entry = load_contacts(pool)["mom"]
    assert len(entry.embeddings) == 3, "按 max_per_contact 截断"
    assert len(entry.embeddings) <= DEFAULT_MAX_CONTACT_EMBEDDINGS


def test_contact_rejects_bad_ids_and_dims(tmp_path: Path):
    pool = tmp_path / "pool.json"
    assert register_contact_embedding(pool, "妈妈", MOM)["status"] == "invalid_contact_id"
    assert register_contact_embedding(pool, "MOM", MOM)["status"] == "ok"      # 大写会被规范化
    assert register_contact_embedding(pool, "dad", [0.0] * 8)["status"] == "dim_mismatch"
    assert register_contact_embedding(pool, "dad", [float("nan")] * DIM)["status"] == "invalid_embedding"


def test_contact_survives_self_pool_persist(tmp_path: Path):
    """关键回归：本人池的 persist() 重写整份文件时，不得把联系人抹掉。"""
    pool = tmp_path / "pool.json"
    register_contact_embedding(pool, "mom", MOM, label="妈妈")

    p1 = RollingVoiceprintPool.from_template(BASE, pool_path=pool)
    assert p1.maybe_update(SEG1, 0.95) is True

    payload = json.loads(pool.read_text(encoding="utf-8"))
    assert "mom" in payload["contacts"], "persist 必须原样带过 contacts"
    assert match_contact(pool, MOM) is not None


def test_contact_persists_across_processes(tmp_path: Path):
    runner = tmp_path / "runner.py"
    runner.write_text(CONTACT_RUNNER, encoding="utf-8")
    pool = tmp_path / "pool.json"

    def run(action: str, cid: str, seed: float) -> dict:
        proc = subprocess.run(
            [sys.executable, str(runner), str(ROOT / "src"), str(pool), action, cid, str(DIM), str(seed)],
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-600:]
        return json.loads(proc.stdout.strip().splitlines()[-1])

    assert run("register", "mom", 0.02)["status"] == "ok"
    # 换一个进程：注册第二个联系人，必须看到 mom（说明真的读得到别人的写入）
    assert run("register", "dad", 0.04)["status"] == "ok"
    contacts = load_contacts(pool)
    assert set(contacts) == {"mom", "dad"}

    # 第三个进程做匹配：不传任何"上一次的内存状态"，只靠文件
    hit = run("match", "mom", 0.02)
    assert hit["matched"] is True and hit["contact_id"] == "mom"


def test_contact_concurrent_registration_keeps_file_valid(tmp_path: Path):
    runner = tmp_path / "runner.py"
    runner.write_text(CONTACT_RUNNER, encoding="utf-8")
    pool = tmp_path / "pool.json"

    procs = [
        subprocess.Popen(
            [sys.executable, str(runner), str(ROOT / "src"), str(pool), "register",
             "c%d" % i, str(DIM), str(0.01 * (i + 1))],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for i in range(6)
    ]
    outs = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err[-600:]
        outs.append(json.loads(out.strip().splitlines()[-1]))

    payload = json.loads(pool.read_text(encoding="utf-8"))       # 永远合法 JSON
    assert payload["schema"] == SCHEMA
    assert len(payload["contacts"]) == 6, "6 个进程各注册一个联系人，一个都不能丢"
    assert all(len(v["embeddings"]) >= 1 for v in payload["contacts"].values())
    assert list(tmp_path.glob("*tmp*")) == []
    assert all(o["status"] == "ok" for o in outs)


def test_contact_file_without_contacts_still_works(tmp_path: Path):
    """老文件（没有 contacts 字段）读到 {}，不报错、不影响本人池。"""
    pool = tmp_path / "pool.json"
    p1 = RollingVoiceprintPool.from_template(BASE, pool_path=pool)
    assert p1.maybe_update(SEG1, 0.95) is True
    assert load_contacts(pool) == {}
    assert match_contact(pool, MOM) is None


# ══════════════════════════════════════════════════════════════════════════
# Q816 接线：联系人匹配 → 归属字典（可选字段，不改 source_label 三值契约）
# ══════════════════════════════════════════════════════════════════════════
def test_named_contact_fields_no_pool_is_noop(tmp_path: Path):
    """没有联系人池时：返回空 dict，归属字典**一个键都不多**（行为零变化）。"""
    from life_logger.source_attribution import apply_named_contact, named_contact_fields

    empty_pool = tmp_path / "pool.json"          # 文件不存在
    assert named_contact_fields(MOM, pool_path=empty_pool) == {}

    base = {"source_label": "non_self", "voice_similarity": 0.21, "review_required": False,
            "decision_reason": "below_non_self_threshold", "acoustic_proximity": "far"}
    out = apply_named_contact(base, MOM, pool_path=empty_pool)
    assert out == base, "无池时必须逐键等于原字典"


def test_named_contact_fields_hits_best_contact(tmp_path: Path):
    """有注册联系人时：命中相似度最高的那位，并带上 label。"""
    from life_logger.source_attribution import apply_named_contact, named_contact_fields

    pool = tmp_path / "pool.json"
    register_contact_embedding(pool, "mom", MOM, label="妈妈")
    register_contact_embedding(pool, "dad", STRANGER, label="爸爸")

    fields = named_contact_fields(unit(1.0, 0.03), pool_path=pool)
    assert fields["contact_id"] == "mom"
    assert fields["contact_label"] == "妈妈"
    assert fields["contact_similarity"] > 0.99

    out = apply_named_contact({"source_label": "unknown", "voice_similarity": 0.28},
                              unit(1.0, 0.03), pool_path=pool)
    assert out["contact_id"] == "mom" and out["contact_label"] == "妈妈"
    assert out["source_label"] == "unknown", "联系人匹配不得改 source_label"


def test_named_contact_never_touches_self(tmp_path: Path):
    """判成 self 的段不查联系人（本人不需要"这是谁"），字典保持原样。"""
    from life_logger.source_attribution import apply_named_contact

    pool = tmp_path / "pool.json"
    register_contact_embedding(pool, "mom", MOM, label="妈妈")
    base = {"source_label": "self", "voice_similarity": 0.71}
    assert apply_named_contact(base, MOM, pool_path=pool) == base


def test_named_contact_below_threshold_is_noop(tmp_path: Path):
    """相似度不到阈值 → 不认（宁可未知，也不瞎认人）。"""
    from life_logger.source_attribution import named_contact_fields

    pool = tmp_path / "pool.json"
    register_contact_embedding(pool, "mom", MOM, label="妈妈")
    assert named_contact_fields(STRANGER, pool_path=pool) == {}
