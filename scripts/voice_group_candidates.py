#!/usr/bin/env python3
"""Offline CAM++ call-voice candidates. Never writes watch/output/config data.

scan -> embed (at most 8 clips per invocation, designated FunASR Python) -> group.
ASR speaker numbers are local to a chunk; they never identify a person across chunks.
All call/name/self labels are evidence proxies, not verified identities.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TZ = timezone(timedelta(hours=8))
SR = 16000
CALL = re.compile(r"(?:^|[，。！？,\s])喂(?:[，。！？,\s]|$)|^喂[你您]|(?:^|[，。！？,\s])(?:听得到|听得见)(?:吗|不|我|[，。！？?？])|(?:我|你|您)(?:听不清|听不到|听不见|能听见|能听到)")
NAMES = {"妈": re.compile(r"妈妈|我妈|老妈|^妈[，,。！!?？]?"), "姐": re.compile(r"姐姐|我姐|老姐|^姐[，,。！!?？]?"), "室友乙": re.compile(r"室友乙|一飞|亦飞|艺飞|易飞|逸菲")}
PHONE = re.compile(r"(?<!\d)\+?\d(?:[\d ()-]*\d){6,}(?!\d)")
COORD = re.compile(r"(?<!\d)[+-]?\d{1,3}\.\d{4,}(?!\d)")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_text(text):
    return COORD.sub("[坐标/长小数已遮挡]", PHONE.sub("[长号码已遮挡]", str(text)))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def work_path(path):
    path = path.resolve()
    if not path.is_relative_to((ROOT / "work").resolve()):
        raise ValueError("all artifacts must be beneath repository work/")
    return path


def stamp(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return dt.astimezone(TZ)


def read_rows(root, first, last):
    rows, issues, manifests = [], [], {}
    stats = {"state_files": 0, "in_range_rows": 0, "duplicate_rows": 0, "missing_audio_rows": 0}
    seen = set()
    for path in sorted(root.glob("*/*/formal-asr/segments/*/state.json")):
        stats["state_files"] += 1
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            issues.append({"state": str(path), "error": type(exc).__name__})
            continue
        if state.get("status") != "completed":
            continue
        rec = path.parents[3]
        if rec not in manifests:
            try:
                manifest = json.loads((rec / "manifest.json").read_text())
                manifests[rec] = {int(s["sequence"]): s for s in manifest.get("segments", [])}
            except (OSError, ValueError, KeyError, TypeError) as exc:
                manifests[rec] = {}
                issues.append({"recording": str(rec), "error": "manifest_" + type(exc).__name__})
        meta = manifests[rec].get(int(path.parent.name))
        for index, r in enumerate(state.get("rows") or []):
            try:
                when = stamp(r.get("absolute_started_at") or r.get("started_at_local"))
                start, end = float(r["start_offset_seconds"]), float(r["end_offset_seconds"])
                if not math.isfinite(start + end) or end <= start:
                    raise ValueError("invalid offsets")
            except (ValueError, KeyError, TypeError) as exc:
                issues.append({"state": str(path), "row": index, "error": "row_" + type(exc).__name__})
                continue
            if not first <= when.date() <= last or not str(r.get("text") or "").strip():
                continue
            stats["in_range_rows"] += 1
            if not meta:
                stats["missing_audio_rows"] += 1
                continue
            audio = rec / "segments" / str(meta["file"])
            if not audio.is_file():
                stats["missing_audio_rows"] += 1
                continue
            origin = float(meta["start_offset_ms"]) / 1000
            if start < origin - 0.05 or end > origin + float(meta["duration_ms"]) / 1000 + 0.1:
                issues.append({"state": str(path), "row": index, "error": "outside_manifest_audio_interval"})
                continue
            text = str(r["text"]).strip()
            key = digest([str(rec), start, end, text])
            if key in seen:
                stats["duplicate_rows"] += 1
                continue
            seen.add(key)
            rows.append({"id": key, "source_id": r.get("source_id"), "when": when.isoformat(),
                         "epoch": when.timestamp(), "day": when.date().isoformat(), "text": text,
                         "source_label": r.get("source_label"), "speaker": r.get("speaker"),
                         "proximity": r.get("acoustic_proximity"), "content_class": r.get("audio_content_class"),
                         "recording": str(rec), "chunk": str(path.parent), "audio": str(audio),
                         "audio_origin": origin, "audio_sha256": meta.get("sha256"),
                         "start": start, "end": end})
    rows.sort(key=lambda r: (r["recording"], r["epoch"], r["id"]))
    stats["readable_rows"] = len(rows)
    return rows, stats, issues


def meaningful(row):
    return row["source_label"] == "self" and len(re.sub(r"[\W_\d]", "", row["text"])) >= 3


def call_candidates(rows, radius=90, reply_gap=45):
    """Keep candidates bracketed by meaningful self replies, not proximity/media class.

    A cue plus alternating self/other/self is only a call candidate. Media with
    no self response fails this gate; interactive media remains an audited risk.
    """
    recordings = {}
    for row in rows:
        recordings.setdefault(row["recording"], []).append(row)
    selected, references, windows = {}, {}, []
    rejected = {"cue_without_bidirectional_reply": 0, "unbracketed_other": 0}
    cue_count = 0
    for recording, rr in recordings.items():
        epochs = [r["epoch"] for r in rr]
        for cue in rr:
            if not CALL.search(cue["text"]):
                continue
            cue_count += 1
            nearby = rr[bisect.bisect_left(epochs, cue["epoch"] - radius):bisect.bisect_right(epochs, cue["epoch"] + radius)]
            own = [r for r in nearby if meaningful(r)]
            others = [r for r in nearby if r["source_label"] != "self"]
            bracketed = [r for r in others if any(0 < r["epoch"] - s["epoch"] <= reply_gap for s in own)
                         and any(0 < s["epoch"] - r["epoch"] <= reply_gap for s in own)]
            if not bracketed:
                rejected["cue_without_bidirectional_reply"] += 1
                continue
            wid = "call_" + cue["id"][:16]
            hints = [{"name": name, "when": r["when"], "text": safe_text(r["text"]), "source_id": r["source_id"]}
                     for r in nearby for name, rx in NAMES.items() if rx.search(r["text"])]
            windows.append({"id": wid, "recording": recording, "cue_when": cue["when"], "cue": safe_text(cue["text"]),
                            "self_rows": len(own), "other_rows": len(others), "bracketed_rows": len(bracketed),
                            "name_hints": hints,
                            "context": [{"when": r["when"], "source_label": r["source_label"], "text": safe_text(r["text"])} for r in nearby]})
            rejected["unbracketed_other"] += len(others) - len(bracketed)
            for r in bracketed:
                entry = selected.setdefault(r["id"], {**r, "window_ids": [], "name_hints": []})
                entry["window_ids"].append(wid)
                entry["name_hints"].extend(hints)
            for r in own:
                references[r["id"]] = {**r, "window_ids": [wid], "name_hints": []}
    return list(selected.values()), list(references.values()), windows, {"cue_rows": cue_count, **rejected}


def units_from_rows(rows, role):
    """Merge only contiguous rows of one local speaker in one chunk, never across chunks."""
    units = []
    for r in sorted(rows, key=lambda r: (r["chunk"], r["start"], r["id"])):
        prev = units[-1] if units else None
        join = (prev and r["speaker"] is not None and prev["chunk"] == r["chunk"] and prev["speaker"] == r["speaker"]
                and 0 <= r["start"] - prev["end"] <= 0.6 and r["end"] - prev["start"] <= 15)
        if join:
            prev["end"] = r["end"]
            prev["utterances"].append(r)
        else:
            units.append({**r, "role": role, "utterances": [r]})
    kept = []
    for unit in units:
        if unit["end"] - unit["start"] < 1.2:
            continue
        unit["id"] = digest([role, [r["id"] for r in unit["utterances"]]])
        unit["clip_seconds"] = unit["end"] - unit["start"]
        unit["text"] = safe_text(" | ".join(r["text"] for r in unit["utterances"]))
        unit["utterances"] = [{**r, "text": safe_text(r["text"])} for r in unit["utterances"]]
        unit["name_hints"] = list({digest(h): h for r in unit["utterances"] for h in r.get("name_hints", [])}.values())
        kept.append(unit)
    return kept


def scan(args):
    last = date.fromisoformat(args.end_date) if args.end_date else datetime.now(TZ).date()
    first = last - timedelta(days=args.days - 1)
    rows, stats, issues = read_rows(args.input_root, first, last)
    candidates, own, windows, gate = call_candidates(rows)
    units = units_from_rows(candidates, "candidate") + units_from_rows(own, "self_reference")
    # Raw text is never persisted; display text may contain explicit privacy markers.
    payload = {"schema": "voice-call-inventory-v1", "first_day": first.isoformat(), "last_day": last.isoformat(),
               "stats": stats, "gate": gate, "issues": issues, "windows": windows, "units": units,
               "selection": {"radius_seconds": 90, "reply_gap_seconds": 45, "minimum_clip_seconds": 1.2}}
    save(args.work_dir / "inventory.json", payload)
    print(json.dumps({"stats": stats, "gate": gate, "windows": len(windows),
                      "candidate_units": sum(u["role"] == "candidate" for u in units),
                      "self_reference_units": sum(u["role"] == "self_reference" for u in units)}, ensure_ascii=False))


def decode(unit):
    import numpy as np
    relative = unit["start"] - unit["audio_origin"]
    if relative < -0.05:
        raise ValueError("negative segment-relative offset")
    command = ["ffmpeg", "-v", "error", "-threads", "1", "-ss", str(max(0, relative)), "-t", str(unit["clip_seconds"]),
               "-i", unit["audio"], "-ar", str(SR), "-ac", "1", "-f", "f32le", "-"]
    proc = subprocess.run(command, capture_output=True, timeout=25)
    if proc.returncode:
        raise RuntimeError("ffmpeg_rc_" + str(proc.returncode))
    arr = np.frombuffer(proc.stdout, dtype="<f4")
    if len(arr) / SR < max(0.8, unit["clip_seconds"] - 0.15):
        raise ValueError("truncated_audio")
    return arr


def verified_audio_hash(path, expected):
    actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if expected and actual != str(expected).removeprefix("sha256:"):
        raise ValueError("audio_hash_changed")
    return actual


def embed(args):
    import numpy as np
    import sherpa_onnx
    from life_logger.campplus_voiceprint import resolve_campplus_model_dir, resolve_campplus_onnx_path
    os.nice(10)
    model_dir, source = resolve_campplus_model_dir()
    model = resolve_campplus_onnx_path(model_dir)
    if model is None:
        raise RuntimeError("local CAM++ ONNX weights required; no numpy48 fallback")
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=1, debug=False))
    inventory = json.loads((args.work_dir / "inventory.json").read_text())
    cache_path = args.work_dir / "embeddings.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {"backend": "campplus_onnx", "dimension": 192, "num_threads": 1,
                                                                         "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
                                                                         "python_prefix": sys.prefix, "model_source": source, "items": {}}
    if cache["model_sha256"] != hashlib.sha256(model.read_bytes()).hexdigest():
        raise ValueError("model changed since checkpoint")
    pending = [u for u in inventory["units"] if u["id"] not in cache["items"]]
    for unit in pending[:args.batch_size]:
        try:
            audio_hash = verified_audio_hash(unit["audio"], unit.get("audio_sha256"))
            arr = decode(unit)
            stream = extractor.create_stream()
            stream.accept_waveform(SR, arr)
            stream.input_finished()
            vector = np.asarray(extractor.compute(stream), dtype=float).reshape(-1)
            norm = float(np.linalg.norm(vector))
            if len(vector) != 192 or not np.isfinite(vector).all() or norm <= 1e-9:
                raise ValueError("invalid_CAM++_embedding")
            cache["items"][unit["id"]] = {"status": "ok", "embedding": (vector / norm).tolist(), "audio_sha256": audio_hash, "decoded_seconds": len(arr) / SR}
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            cache["items"][unit["id"]] = {"status": "failed", "error": type(exc).__name__ + ":" + str(exc)}
        save(cache_path, cache)
        time.sleep(args.pause)
    save(cache_path, cache)
    print(json.dumps({"batch": min(len(pending), args.batch_size), "remaining": max(0, len(pending) - args.batch_size),
                      "success": sum(v["status"] == "ok" for v in cache["items"].values()),
                      "failed": sum(v["status"] != "ok" for v in cache["items"].values())}))
    return 1 if any(v["status"] != "ok" for v in cache["items"].values()) else 0


def cosine(a, b):
    if len(a) != len(b) or not a:
        raise ValueError("incompatible embeddings")
    norm = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("invalid embeddings")
    return max(-1, min(1, sum(x*y for x, y in zip(a, b)) / norm))


def cluster(units, embeddings, threshold):
    """Deterministic complete-link insertion; a centroid cannot hide a weak member."""
    groups = []
    for u in sorted(units, key=lambda u: u["id"]):
        scores = [min(cosine(embeddings[u["id"]], embeddings[v["id"]]) for v in g) for g in groups]
        if scores and max(scores) >= threshold:
            groups[scores.index(max(scores))].append(u)
        else:
            groups.append([u])
    return groups


def summarize(members, embeddings, references, threshold):
    pairs = [(cosine(embeddings[a["id"]], embeddings[b["id"]]), a, b)
             for i, a in enumerate(members) for b in members[i+1:]]
    centroid = [sum(v[k] for v in [embeddings[u["id"]] for u in members]) / len(members) for k in range(192)]
    ordered = sorted(members, key=lambda u: (-cosine(embeddings[u["id"]], centroid), u["id"]))
    if pairs:
        worst = min(pairs, key=lambda p:p[0])
        ordered = [ordered[0], worst[1], worst[2]] + ordered
    diverse, days = [], {ordered[0]["day"]}
    for unit in sorted(members, key=lambda u:(u["day"],u["id"])):
        if unit["day"] not in days:
            diverse.append(unit)
            days.add(unit["day"])
    ordered = list({u["id"]:u for u in ordered[:3] + diverse + ordered}.values())
    unique, reps = set(), []
    # Round-robin preserves worst-pair / other-day coverage before taking a second
    # sentence from the same clip. Never manufacture three examples for a singleton.
    for index in range(max(len(u["utterances"]) for u in ordered)):
        for unit in ordered:
            if index >= len(unit["utterances"]):
                continue
            r = unit["utterances"][index]
            if r["id"] in unique:
                continue
            unique.add(r["id"])
            reps.append({"when": r["when"], "text": r["text"], "unit_id": unit["id"], "source_id": r["source_id"],
                         "source_label": r["source_label"], "proximity": r["proximity"], "content_class": r["content_class"],
                         "audio": "clips/" + unit["id"] + ".wav"})
            if len(reps) == 5:
                break
        if len(reps) == 5:
            break
    local_speakers = {}
    for u in members:
        if u["speaker"] is not None:
            local_speakers.setdefault(u["chunk"], set()).add(u["speaker"])
    conflicts = [{"chunk": c, "local_speakers": sorted(ss)} for c, ss in local_speakers.items() if len(ss)>1]
    overlap = max((cosine(embeddings[u["id"]], embeddings[r["id"]]) for u in members for r in references), default=None)
    return {"group_id": "vg_" + digest(sorted(u["id"] for u in members))[:16], "unit_count": len(members),
            "seconds": round(sum(u["clip_seconds"] for u in members), 3), "day_count": len({u["day"] for u in members}),
            "days": sorted({u["day"] for u in members}), "pair_min": round(min(p[0] for p in pairs), 4) if pairs else None,
            "pair_mean": round(sum(p[0] for p in pairs)/len(pairs), 4) if pairs else None,
            "max_self_reference_similarity": round(overlap, 4) if overlap is not None else None,
            "local_speaker_conflicts": conflicts, "representatives": reps, "representative_shortfall": max(0,3-len(reps)),
            "name_hints": list({digest(h):h for u in members for h in u.get("name_hints", [])}.values()),
            "members": [u["id"] for u in members], "identity_verified": False,
            "review_status": "needs_audio_purity_review", "naming_eligible": False,
            "risk_flags": (["same_chunk_multiple_local_speakers"] if conflicts else [])
                          + (["overlaps_self_reference_proxy"] if overlap is not None and overlap >= threshold else [])
                          + (["fewer_than_three_representative_sentences"] if len(reps) < 3 else [])}


def group(args):
    inventory = json.loads((args.work_dir / "inventory.json").read_text())
    cache = json.loads((args.work_dir / "embeddings.json").read_text())
    pending = [u for u in inventory["units"] if u["id"] not in cache["items"]]
    if pending:
        raise ValueError("embedding batches incomplete")
    good = [u for u in inventory["units"] if cache["items"][u["id"]]["status"] == "ok"]
    units = [u for u in good if u["role"] == "candidate"]
    references = [u for u in good if u["role"] == "self_reference"]
    embs = {k:v["embedding"] for k,v in cache["items"].items() if v["status"]=="ok"}
    sweep = []
    for threshold in [0.35,0.45,0.55,0.65,0.75,0.85]:
        gg = cluster(units, embs, threshold)
        sweep.append({"threshold": threshold, "groups": len(gg), "singletons": sum(len(g)==1 for g in gg),
                      "multi_day_groups": sum(len({u["day"] for u in g})>=2 for g in gg), "largest_group": max(map(len,gg),default=0)})
    groups = [summarize(g, embs, references, args.threshold) for g in cluster(units,embs,args.threshold)]
    groups.sort(key=lambda g:(-g["seconds"],g["group_id"]))
    trials = [{"minimum_units": n,"minimum_days": d,"minimum_seconds": s,
               "qualifying_groups": sum(g["unit_count"]>=n and g["day_count"]>=d and g["seconds"]>=s for g in groups)}
              for n,d,s in [(3,2,15),(5,2,30),(5,3,60)]]
    payload = {"schema": "voice-call-groups-v1", "range": [inventory["first_day"],inventory["last_day"]],
               "backend": {k:v for k,v in cache.items() if k!="items"}, "threshold": args.threshold,
               "threshold_validated": False, "threshold_sweep": sweep, "notification_threshold_trials": trials,
               "groups": groups, "embedding_failures": [{"unit_id":k,**v} for k,v in cache["items"].items() if v["status"]!="ok"],
               "quality_gate": {"product_ready":False, "audio_purity_review":"not_performed",
                                "reason":"Internal similarity and ASR labels cannot certify group purity; inspect risk flags and representative audio before naming."}}
    save(args.out,payload)
    clips = args.work_dir / "clips"
    clips.mkdir(exist_ok=True)
    wanted = {r["unit_id"] for g in groups for r in g["representatives"]}
    import numpy as np
    import wave
    for unit in units:
        if unit["id"] not in wanted:
            continue
        arr = decode(unit)
        with wave.open(str(clips/(unit["id"]+".wav")),"wb") as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(SR)
            f.writeframes(np.clip(arr*32767,-32768,32767).astype("<i2").tobytes())
    sections = []
    for g in groups:
        rows = "".join('<li>'+html.escape(r["when"]+" "+r["text"])+'<audio controls src="'+r["audio"]+'"></audio></li>' for r in g["representatives"])
        sections.append('<section><h2>'+g["group_id"]+'</h2><p>'+html.escape(str({k:g[k] for k in ["seconds","day_count","pair_min","max_self_reference_similarity","representative_shortfall"]}))+'</p><ul>'+rows+'</ul></section>')
    (args.work_dir/"review.html").write_text('<!doctype html><meta charset="utf-8"><title>离线通话候选复核</title><h1>候选组：身份未验证，未绑定姓名</h1><p>原话中的长号码及坐标已遮挡；音频为本地原始节选。</p>'+"".join(sections))
    print(json.dumps({"candidate_embeddings":len(units),"self_references":len(references),"groups":len(groups),
                      "threshold":args.threshold,"product_ready":False,"sweep":sweep,"notification_trials":trials},ensure_ascii=False))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase",choices=["scan","embed","group"])
    parser.add_argument("--input-root",type=Path,default=ROOT/"output/local-web/watch-sessions")
    parser.add_argument("--work-dir",type=Path,default=ROOT/"work/voice-contacts-20261105")
    parser.add_argument("--out",type=Path,default=ROOT/"work/speaker-registry-20261105/groups.json")
    parser.add_argument("--days",type=int,default=30)
    parser.add_argument("--end-date")
    parser.add_argument("--batch-size",type=int,default=8)
    parser.add_argument("--pause",type=float,default=0.2)
    parser.add_argument("--threshold",type=float,default=0.65,help="exploratory grouping threshold; never authorizes identity")
    args=parser.parse_args(argv)
    if not 1<=args.batch_size<=8 or args.pause<0 or args.days<1 or not 0<args.threshold<=1:
        parser.error("invalid limits")
    args.work_dir=work_path(args.work_dir)
    args.out=work_path(args.out)
    args.work_dir.mkdir(parents=True,exist_ok=True)
    return {"scan":scan,"embed":embed,"group":group}[args.phase](args) or 0


if __name__=="__main__":
    raise SystemExit(main())
