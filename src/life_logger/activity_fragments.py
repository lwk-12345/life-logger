"""Read-only fragment validity hints; never changes speaker labels or facts.

Local diarization names are scoped to one formal-ASR chunk, not global people.
Repeated copies of the same short phrase do not constitute continuous context.
"""
from __future__ import annotations

import copy
import hashlib
import math
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from life_logger.jsonio import COMPRESSED_SUFFIX, read_json, resolve_readable_json_path

TZ = ZoneInfo('Asia/Shanghai')
SPEND_WORDS = re.compile(r'购买|买|支付|付款|消费|收款|到账|转账|多少钱|花钱|充值|订阅|订单|下单|优惠券|代金券|运费|快递费|退款|退钱|现金|扫码|点餐|点单|点[一两二三四五六七八九十\d]+(?:个|份|碗|瓶|杯)|[¥￥$]|[零〇一二两三四五六七八九十百千万亿点\d.]+(?:元|块|美元|港币|欧元|日元|刀)')
_LOCK = threading.Lock()
_CACHE = {}  # at most two day snapshots; no filesystem cache/cursor writes


def compact(text):
    return ''.join(c for c in str(text or '') if unicodedata.category(c)[0] in 'LN')


def stamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def score(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and -1 <= number <= 1 else None
    except (ValueError, TypeError):
        return None


CHAT_APPS = re.compile(
    r'^(?:微信|企业微信|qq|飞书|钉钉|telegram|whatsapp|imessage|信息|slack|wechat|wework)|(?:微信|企业微信|wechat)',
    re.I,
)
CHAT_BUNDLES = frozenset({
    'com.tencent.xinwechat',
    'com.tencent.weworkmac',
    'com.tencent.qq',
    'com.bytedance.feishu',
})


def _ctx_is_chat(ctx):
    if not isinstance(ctx, dict):
        return False
    for key in ('app_label', 'foreground_app'):
        val = str(ctx.get(key) or '').strip()
        if val and CHAT_APPS.search(val):
            return True
    for key in ('target_bundle', 'target_app', 'foreground_bundle'):
        val = str(ctx.get(key) or '').strip()
        if val and (val.lower() in CHAT_BUNDLES or CHAT_APPS.search(val)):
            return True
    return False


def _is_chat_activity(activity):
    if not isinstance(activity, dict):
        return False
    target = re.sub(r'^对', '', str(activity.get('speech_target') or '')).strip()
    if target and CHAT_APPS.search(target):
        return True
    if _ctx_is_chat(activity.get('input_context')) or _ctx_is_chat(activity.get('foreground_context')):
        return True
    for item in activity.get('items') or []:
        if not isinstance(item, dict):
            continue
        if _ctx_is_chat(item.get('input_context')):
            return True
        for ev in item.get('evidence') or []:
            if isinstance(ev, dict) and _ctx_is_chat(ev.get('input_context')):
                return True
    return False


def spend_protected(activity):
    # Titles/actions can supply a positive ordering cue (e.g. 点“一个红烧”).
    # Never scan summary/uncertainties: the salary sample says 未到账/未消费 there.
    title = str(activity.get('title') or '')
    if SPEND_WORDS.search(title) or SPEND_WORDS.search(compact(title)):
        return True
    is_live_activity = activity.get('lifecycle') not in ('media', 'unknown')
    if is_live_activity and _is_chat_activity(activity):
        return True
    for item in activity.get('items') or []:
        if item.get('fact_type') == 'expense':
            return True
        if is_live_activity and item.get('item_lifecycle') != 'media' and item.get('fact_type') in ('smoke', 'food', 'exercise'):
            evs = [ev for ev in (item.get('evidence') or []) if isinstance(ev, dict)]
            if not (evs and all(ev.get('source_label') == 'non_self' for ev in evs)):
                return True
        if item.get('amount') is not None and item.get('amount_kind') not in ('weight', 'quantity'):
            return True
        if SPEND_WORDS.search(str(item.get('action') or '')):
            return True
        if any(SPEND_WORDS.search(str(ev.get('quote') or '')) for ev in item.get('evidence') or []):
            return True
    return False


_MANIFEST_ERRORS = (OSError, ValueError, TypeError, AttributeError)
_STATE_ERRORS = (OSError, ValueError, TypeError)
# [2026-11-08] Process-local parse caches keyed by (resolved file, mtime_ns, size).
# Read-only: nothing is written to disk. A changed file gets a new key and is re-read.
_MANIFEST_CACHE = OrderedDict()   # str(target) -> ((mtime_ns, size), (began, offsets))
_STATE_CACHE = OrderedDict()      # str(target) -> ((mtime_ns, size), prepared)
_MANIFEST_CACHE_MAX = 2000
_STATE_CACHE_MAX = 6000


def _file_key(path):
    """(resolved readable file, (mtime_ns, size)) or (target, None) when stat fails."""
    target = resolve_readable_json_path(path)
    try:
        st = os.stat(target)
    except OSError:
        return target, None
    return target, (st.st_mtime_ns, st.st_size)


def _cache_get(cache, target, stat_key):
    if stat_key is None:
        return None
    hit = cache.get(str(target))
    if hit is not None and hit[0] == stat_key:
        cache.move_to_end(str(target))
        return hit
    return None


def _cache_put(cache, limit, target, stat_key, value):
    if stat_key is None:
        return
    cache[str(target)] = (stat_key, value)
    cache.move_to_end(str(target))
    while len(cache) > limit:
        cache.popitem(last=False)


def _parse_manifest(manifest):
    """Same parse as the original inline code; raises exactly what it raised."""
    doc = read_json(manifest)
    began = stamp(doc.get('started_at'))
    offsets = {str(s['sequence']).lstrip('0') or '0': float(s['start_offset_ms'])
               for s in doc.get('segments') or [] if isinstance(s, dict)
               and 'sequence' in s and 'start_offset_ms' in s}
    return began, offsets


def _manifest_info(manifest):
    """(began, offsets); caught failures -> (None, {}) like before (never cached)."""
    target, stat_key = _file_key(manifest)
    hit = _cache_get(_MANIFEST_CACHE, target, stat_key)
    if hit is not None:
        return hit[1]
    try:
        info = _parse_manifest(manifest)
    except _MANIFEST_ERRORS:
        return None, {}
    _cache_put(_MANIFEST_CACHE, _MANIFEST_CACHE_MAX, target, stat_key, info)
    return info


def _out_of_day(began, offsets, name, start):
    """True only when the declared capture date is > 1 day from the target day."""
    offset = offsets.get(name.lstrip('0') or '0')
    if began is not None and offset is not None and math.isfinite(offset):
        try:
            captured = began + timedelta(milliseconds=offset)
        except OverflowError:
            captured = None
        if captured is not None and abs((captured.date() - (start + timedelta(seconds=60)).date()).days) > 1:
            return True
    return False


def _candidate_state_paths(base, start):
    """Same set and order as glob_json(base, '*/*/formal-asr/segments/*/state.json'),
    minus segments whose manifest offset puts them > 1 day away (those were skipped
    before reading anyway). Out-of-day segments are not even stat'ed.

    Returns (paths, manifests) where manifests is a prefilled {manifest: (began, offsets)}.
    """
    found, manifests = [], {}
    for level1 in _scan_dirs(base):
        for session in _scan_dirs(level1):
            segments_dir = session / 'formal-asr' / 'segments'
            names = [entry.name for entry in _scan_entries(segments_dir, dirs_only=True)]
            if not names:
                continue
            manifest = session / 'manifest.json'
            try:
                began, offsets = _manifest_info(manifest)
            except Exception:  # noqa: BLE001 - unexpected: let the main loop raise as before
                began, offsets = None, {}
            else:
                manifests[manifest] = (began, offsets)
            for name in names:
                if _out_of_day(began, offsets, name, start):
                    continue
                plain = segments_dir / name / 'state.json'
                if os.path.lexists(plain):
                    found.append(plain)
                    continue
                packed = segments_dir / name / ('state.json' + COMPRESSED_SUFFIX)
                if os.path.lexists(packed):
                    found.append(packed)
    return sorted(found), manifests


def _scan_entries(path, *, dirs_only):
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except OSError:
        return []
    if not dirs_only:
        return entries
    keep = []
    for entry in entries:
        try:
            if entry.is_dir():
                keep.append(entry)
        except OSError:
            continue
    return keep


def _scan_dirs(path):
    return [path / entry.name for entry in _scan_entries(path, dirs_only=True)]


def _prepare_state(path):
    """Day-independent parse of one state.json, cached by (file, mtime_ns, size).

    Returns a tuple of per-row entries (row order kept, non-dict / no-timestamp /
    empty-text rows dropped exactly as before):
      [ts, text, idx, recording_id, segment_id, start_offset_seconds, speaker,
       voice_similarity, memo]
    The output fields (source_id, ...) are computed lazily by `_row_output` only
    for rows inside the requested day, in the original order, so any exception is
    raised at the same point as before. Or ('file_err', exc) if iterating rows raised.
    Read/parse failures propagate (never cached), exactly like read_json before.
    """
    target, stat_key = _file_key(path)
    hit = _cache_get(_STATE_CACHE, target, stat_key)
    if hit is not None:
        return hit[1]
    state = read_json(path)
    entries = []
    if isinstance(state, dict) and state.get('status') == 'completed':
        try:
            rows = enumerate(state.get('rows') or [], 1)
        except Exception as exc:  # noqa: BLE001 - replayed when the file is used
            entries = [('file_err', exc)]
            rows = ()
        for idx, row in rows:
            if not isinstance(row, dict):
                continue
            ts = stamp(row.get('absolute_started_at') or row.get('started_at_local'))
            text = str(row.get('text') or '').strip()
            if ts is None or not text:
                continue
            entries.append([ts, text, idx, row.get('recording_id'), row.get('segment_id'),
                            row.get('start_offset_seconds'), row.get('speaker'),
                            row.get('voice_similarity'), None])
    entries = tuple(entries)
    _cache_put(_STATE_CACHE, _STATE_CACHE_MAX, target, stat_key, entries)
    return entries


def _row_head(entry):
    """source_id / timestamp / speaker, same expressions and order as before."""
    ts, text, idx, rec, seg, offset, speaker = entry[:7]
    # Same source_id definition as ActivityTimelineRuntime's scanner.
    rec = str(rec or '').strip()
    seg = str(seg or f'seg_{idx}').strip()
    offset = str(offset or 0.0)
    sid = 'src_' + hashlib.sha256(f'{rec}:{seg}:{offset}:{text}'.encode()).hexdigest()[:16]
    return sid, ts.isoformat(), str(speaker or '').strip()


def load_day_rows(root, day):
    """Read canonical raw rows without invoking the runtime's cursor-writing scanner."""
    key = (str(Path(root).resolve()), day)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1], cached[2]
        start = datetime.fromisoformat(day).replace(tzinfo=TZ) - timedelta(seconds=60)
        end = start + timedelta(days=1, seconds=120)
        rows, errors = [], 0
        # [2026-11-08] Was glob_json over *every* session's state.json (~16k files,
        # ~3.9 s cold). Same paths and order, but out-of-day segments are pruned
        # by manifest offsets before any stat; parsed files are cached in-process.
        paths, manifests = _candidate_state_paths(Path(root) / 'watch-sessions', start)
        for path in paths:
            # Use declared capture offsets, never sequence * 60. A wide day margin
            # keeps boundary chunks. Missing/bad manifests fall back to reading.
            manifest = path.parent.parent.parent.parent / 'manifest.json'
            if manifest not in manifests:
                try:
                    manifests[manifest] = _parse_manifest(manifest)
                except _MANIFEST_ERRORS:
                    manifests[manifest] = (None, {})
            began, offsets = manifests[manifest]
            if _out_of_day(began, offsets, path.parent.name, start):
                continue
            try:
                scope = None
                for entry in _prepare_state(path):
                    if entry[0] == 'file_err':
                        raise entry[1]
                    ts = entry[0]
                    if not start <= ts < end:
                        continue
                    memo = entry[8]
                    if memo is None:
                        # Order as before: source_id/timestamp/speaker, scope, score.
                        head = _row_head(entry)
                        if scope is None:
                            # One resolve per segment directory (was once per row).
                            scope = str(path.parent.resolve())
                        memo = entry[8] = (head, score(entry[7]))
                    elif scope is None:
                        scope = str(path.parent.resolve())
                    (sid, iso, speaker), similarity = memo
                    rows.append({'source_id': sid, 'text': entry[1], 'timestamp': iso,
                                 'speaker': speaker, 'scope': scope, 'voice_similarity': similarity})
            except _STATE_ERRORS:
                errors += 1
        # Identical source copies are one utterance. Conflicts cannot supply confidence/context.
        index, conflicts = {}, set()
        for row in rows:
            sid = row['source_id']
            if sid in index and any(index[sid][k] != row[k] for k in ('text', 'timestamp', 'speaker', 'voice_similarity')):
                conflicts.add(sid)
            else:
                index.setdefault(sid, row)
        for sid in conflicts:
            index.pop(sid, None)
        rows = list(index.values())
        if len(_CACHE) >= 2:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (time.monotonic(), rows, errors + len(conflicts))
        return rows, errors + len(conflicts)


def assess(activity, rows):
    evidence = [ev for item in activity.get('items') or [] for ev in item.get('evidence') or [] if isinstance(ev, dict)]
    texts = [compact(ev.get('quote')) for ev in evidence]
    result = {'looks_like_fragment': False, 'fragment_money_protected': spend_protected(activity),
              'fragment_reason': None}
    if result['fragment_money_protected']:
        return result, 'spend_protected'
    if not texts or any(not text or len(text) > 6 for text in texts):
        return result, 'not_all_short'
    by_id = {row['source_id']: row for row in rows}
    anchors = []
    for ev in evidence:
        row = by_id.get(ev.get('source_id'))
        # Resolve by source_id plus verbatim quote, never a recycled segment_id/name alone.
        if row is None or str(ev.get('quote') or '') not in row['text']:
            return result, 'source_context_unavailable'
        if len(compact(row['text'])) > 6:
            return result, 'long_source_sentence_context'
        anchors.append(row)
    similarities = [s for row in anchors if (s := score(row.get('voice_similarity'))) is not None]
    if similarities and max(similarities) >= 0.6:
        return result, 'voice_at_least_0_6'
    for anchor in anchors:
        when = stamp(anchor.get('timestamp'))
        speaker = anchor.get('speaker')
        if not speaker or speaker.lower() in {'unknown', '未知'} or when is None:
            return result, 'source_context_unavailable'
        for neighbor in rows:
            if neighbor.get('scope') != anchor.get('scope') or neighbor.get('speaker') != speaker:
                continue
            other = stamp(neighbor.get('timestamp'))
            text = compact(neighbor.get('text'))
            if other is not None and text and abs((other - when).total_seconds()) <= 60 and text != compact(anchor.get('text')):
                return result, 'same_speaker_context'
    result.update(looks_like_fragment=True,
                  fragment_reason='全部原话去标点后不超过6字；前后60秒无可验证的同说话人连续句；本人声纹相似度低于0.6或未知。不确定是谁说的。')
    return result, 'isolated_short_low_or_unknown_voice'


def tag_payload(payload, rows):
    result = copy.deepcopy(payload)
    for activity in result.get('activities') or []:
        hints, _ = assess(activity, rows)
        activity.update(hints)
    return result


def project_for_read(payload, root, day):
    rows, errors = load_day_rows(root, day)
    result = tag_payload(payload, rows)
    result['fragment_assessment'] = {'schema': 'activity-fragment-read-v1', 'source_rows': len(rows),
                                     'source_read_issues': errors, 'speaker_scope': 'formal_asr_chunk',
                                     'source_cache_seconds': 60}
    return result
