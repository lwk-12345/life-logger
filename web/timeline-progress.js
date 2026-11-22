/* Pure adapter: capture coverage and formal ASR are separate from life events. */
(function (root) {
  function timelineProgress(date, offsetMinutes, intervals, reception, nowMs) {
    const start = Date.parse(date + 'T00:00:00Z') - offsetMinutes * 60000;
    const end = start + 86400000;
    const timestamp = value => {
      if (!value) return null;
      const n = Date.parse(value);
      return Number.isFinite(n) ? n : null;
    };
    const minute = n => n === null ? null : Math.max(0, Math.min(1440, (n - start) / 60000));
    const inDay = n => n !== null && n > start && n <= end;
    const spans = (Array.isArray(intervals) ? intervals : []).filter(iv => {
      const a = timestamp(iv.start_at), b = timestamp(iv.end_at);
      return a !== null && b !== null && b > a && b > start && a < end;
    });
    const matches = new Set(spans.map(iv => String(iv.device_id || '') + '/' + String(iv.session_id || '')));
    const relevant = ((reception && reception.sessions) || []).filter(s =>
      matches.has(String(s.device_id || '') + '/' + String(s.session_id || ''))
    );
    const receivedMs = spans.length ? Math.max(...spans.map(iv => Math.min(end, timestamp(iv.end_at)))) : null;
    let latestCompletedMs = null, active = null, nextPending = null;
    let pending = 0, processing = 0, failed = 0, completed = 0;
    const asrSpans = [];
    for (const session of relevant) {
      const asr = session.formal_asr;
      if (!asr) continue;
      const counts = asr.counts || {};
      pending += Number(counts.pending) || 0;
      processing += Number(counts.processing) || 0;
      failed += Number(counts.failed) || 0;
      completed += (Number(counts.completed) || 0) + (Number(counts.no_speech) || 0);
      const latest = asr.latest_completed_segment || {};
      const finished = timestamp(latest.end_at);
      if (inDay(finished) && ['completed', 'no_speech'].includes(latest.status)) {
        latestCompletedMs = Math.max(latestCompletedMs || start, finished);
      }
      const current = asr.current_segment || {};
      const currentStart = timestamp(current.start_at), currentEnd = timestamp(current.end_at);
      if (currentStart !== null && currentEnd !== null && currentEnd > start && currentStart < end) {
        const clip = { start: minute(currentStart), end: minute(currentEnd), status: current.status };
        if (current.status === 'processing') {
          if (!active || clip.start < active.start) active = clip;
        } else if (current.status === 'pending') {
          if (!nextPending || clip.start < nextPending.start) nextPending = clip;
        }
      }
      // Only a wholly completed session can paint its entire coverage as transcribed.
      // A latest finished segment alone cannot prove earlier segments are complete.
      if (asr.status === 'completed' && !(Number(counts.pending) || Number(counts.processing) || Number(counts.failed))) {
        spans.filter(iv => String(iv.device_id || '') === String(session.device_id || '') &&
          String(iv.session_id || '') === String(session.session_id || '')).forEach(iv => {
          asrSpans.push({ start: minute(timestamp(iv.start_at)), end: minute(timestamp(iv.end_at)) });
        });
      }
    }
    if (asrSpans.length) {
      const provenEnd = start + Math.max(...asrSpans.map(s => s.end)) * 60000;
      latestCompletedMs = Math.max(latestCompletedMs || start, provenEnd);
    }
    const now = Number.isFinite(nowMs) ? nowMs : Date.now();
    const isToday = now >= start && now < end;
    const received = minute(receivedMs);
    const latestCompleted = minute(latestCompletedMs);
    const position = active ? active.start : (nextPending ? nextPending.start : latestCompleted);
    return {
      isToday, now: isToday ? minute(now) : null, received, latestCompleted,
      coverageSpans: spans.map(iv => ({start:minute(timestamp(iv.start_at)),end:minute(timestamp(iv.end_at))})),
      position, active, nextPending, pending, processing, failed, completed,
      available: relevant.some(s => s.formal_asr), asrSpans,
      lag: received !== null && position !== null ? Math.max(0, received - position) : null,
      status: active ? 'processing' : (nextPending ? 'pending' : (failed ? 'failed' :
        (latestCompleted !== null ? 'completed' : 'unknown')))
    };
  }
  function compactTimelineEpisodes(episodes) {
    if (episodes.length <= 6) return episodes.map(e => ({...e, parts:[{start:e.start,end:e.end,scene:e.scene}]}));
    // A dense day is grouped into six four-hour windows; every environment span and event is kept.
    const buckets = Array.from({length:6},(_,i)=>({start:i*240,end:(i+1)*240,scene:null,events:[],parts:[],grouped:true}));
    const seen = new Set();
    for (const episode of episodes) {
      for (const bucket of buckets) {
        const start=Math.max(bucket.start,episode.start),end=Math.min(bucket.end,episode.end);
        if(end>start)bucket.parts.push({start,end,scene:episode.scene});
      }
      for (const event of episode.events) {
        const key=event.event_id || event;
        if(seen.has(key))continue;
        seen.add(key);
        buckets[Math.max(0,Math.min(5,Math.floor(event.minute/240)))].events.push(event);
      }
    }
    return buckets.filter(b=>b.parts.length || b.events.length).map(b=>{
      const durations={};b.parts.forEach(p=>{if(p.scene)durations[p.scene]=(durations[p.scene]||0)+p.end-p.start;});
      b.scene=Object.keys(durations).sort((a,c)=>durations[c]-durations[a])[0]||null;
      b.events.sort((a,c)=>a.minute-c.minute);
      return b;
    });
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { timelineProgress, compactTimelineEpisodes };
  else root.LifeTimelineProgress = { timelineProgress, compactTimelineEpisodes };
})(typeof globalThis === 'undefined' ? this : globalThis);
