import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import test from 'node:test';
const require = createRequire(import.meta.url);
const { timelineProgress, compactTimelineEpisodes } = require('../web/timeline-progress.js');
const date = '2026-10-18';
const stamp = time => `${date}T${time}:00+08:00`;
const iv = (start='09:00', end='10:00') => ({device_id:'watch',session_id:'s1',start_at:stamp(start),end_at:stamp(end)});
const session = formal_asr => ({device_id:'watch',session_id:'s1',formal_asr});
const adapt = (intervals, sessions, day=date, now=stamp('10:05')) => timelineProgress(day,480,intervals,{sessions},Date.parse(now));

test('missing ASR does not turn received recordings or event absence into completion', () => {
 const p=adapt([iv()],[]);
 assert.equal(p.received,600);assert.equal(p.position,null);assert.equal(p.available,false);assert.deepEqual(p.asrSpans,[]);
});
test('no-speech segments are real completed ASR, even with no life event', () => {
 const p=adapt([iv()],[session({status:'completed',counts:{no_speech:60,completed:0},latest_completed_segment:{status:'no_speech',start_at:stamp('09:59'),end_at:stamp('10:00')}})]);
 assert.equal(p.position,600);assert.equal(p.completed,60);assert.equal(p.status,'completed');assert.equal(p.asrSpans.length,1);
});
test('active position uses actual current segment, never a synthetic five minute gap', () => {
 const p=adapt([iv()],[session({status:'processing',counts:{completed:58,processing:1,pending:1},current_segment:{status:'processing',start_at:stamp('09:58'),end_at:stamp('09:59')},latest_completed_segment:{status:'completed',end_at:stamp('09:58')}})]);
 assert.equal(p.position,598);assert.equal(p.received,600);assert.equal(p.now,605);assert.equal(p.lag,2);assert.equal(p.active.end,599);assert.deepEqual(p.asrSpans,[]);
});
test('out-of-order completion does not claim the whole recording is complete', () => {
 const p=adapt([iv()],[session({status:'processing',counts:{completed:59,pending:1},current_segment:{status:'pending',start_at:stamp('09:10'),end_at:stamp('09:11')},latest_completed_segment:{status:'completed',end_at:stamp('10:00')}})]);
 assert.equal(p.status,'pending');assert.equal(p.position,550);assert.equal(p.latestCompleted,600);assert.equal(p.lag,50);assert.deepEqual(p.asrSpans,[]);
});
test('a failed segment is not hidden behind a latest completed segment', () => {
 const p=adapt([iv()],[session({status:'failed',counts:{failed:1,completed:59},latest_completed_segment:{status:'completed',end_at:stamp('10:00')}})]);
 assert.equal(p.failed,1);assert.equal(p.status,'failed');assert.deepEqual(p.asrSpans,[]);
});
test('history does not display todays NOW marker and timezone is explicit', () => {
 const p=adapt([iv()],[],date,'2026-10-19T01:00:00+08:00');assert.equal(p.isToday,false);assert.equal(p.now,null);assert.equal(p.received,600);
});
test('recordings are clipped at midnight and unrelated sessions do not supply status', () => {
 const intervals=[{...iv(),start_at:'2026-10-17T23:50:00+08:00',end_at:stamp('00:10')}];
 const p=adapt(intervals,[{...session({status:'completed'}),session_id:'other'}]);assert.equal(p.received,10);assert.equal(p.available,false);assert.equal(p.position,null);
});
test('empty recording coverage remains unknown, not midnight or simulated progress', () => {
 const p=adapt([],[]);assert.equal(p.received,null);assert.equal(p.position,null);assert.equal(p.status,'unknown');assert.equal(p.now,605);
});
test('completed recording across midnight is clipped without losing historical completion', () => {
 const intervals=[{...iv(),start_at:'2026-10-18T23:50:00+08:00',end_at:'2026-10-19T00:10:00+08:00'}];
 const p=adapt(intervals,[session({status:'completed',counts:{no_speech:20},latest_completed_segment:{status:'no_speech',start_at:'2026-10-19T00:09:00+08:00',end_at:'2026-10-19T00:10:00+08:00'}})],date,'2026-10-19T01:00:00+08:00');
 assert.equal(p.received,1440);assert.equal(p.position,1440);assert.equal(p.asrSpans[0].end,1440);assert.equal(p.now,null);
});

test('normal days retain their exact episode boundaries in the compact overview', () => {
 const episodes=[{start:540,end:580,scene:'office',events:[{event_id:'one',minute:560}]}];
 const rows=compactTimelineEpisodes(episodes);assert.equal(rows.length,1);assert.equal(rows[0].start,540);assert.equal(rows[0].end,580);assert.equal(rows[0].events[0].event_id,'one');
});
test('a dense day fits six windows without dropping events or environment spans', () => {
 const episodes=Array.from({length:96},(_,i)=>({start:i*15,end:(i+1)*15,scene:i%2?'office':'conversation',events:[{event_id:'e'+i,minute:i*15+5}]}));
 const rows=compactTimelineEpisodes(episodes);
 assert.equal(rows.length,6);assert.equal(rows.reduce((n,r)=>n+r.events.length,0),96);assert.equal(new Set(rows.flatMap(r=>r.events.map(e=>e.event_id))).size,96);assert.equal(rows.flatMap(r=>r.parts).reduce((n,p)=>n+p.end-p.start,0),1440);assert.equal(rows.at(-1).end,1440);
});
test('overview keeps recordings with no life event, and defaults to collapsed details', () => {
 const rows=compactTimelineEpisodes([{start:10,end:50,scene:null,events:[]}]);assert.equal(rows.length,1);assert.equal(rows[0].events.length,0);
 const html=fs.readFileSync(new URL('../web/daily-timeline-live.html',import.meta.url),'utf8');
 // 现行实现：静默时段折叠为 folded-hours-row，点击展开（setFoldIdle(false)）
 assert.match(html,/class="folded-hours-row/);assert.match(html,/setFoldIdle\(false\)/);assert.match(html,/\.page-wrap"?\)?\?\.getBoundingClientRect\(\)\.height/);
});

test('waterfall keeps visible recording/environment rails and semantic event colors', () => {
 const html=fs.readFileSync(new URL('../web/daily-timeline-live.html',import.meta.url),'utf8');
 assert.match(html,/class="col-rec-track"/);assert.match(html,/class="col-scene-track"/);
 assert.match(html,/id="liveStatusPill"/);assert.match(html,/\/api\/watch-coverage/);
 // 录音段是否已抽取事件：现行实现明确按事件区间标记（旧断言针对已废弃实现）
 assert.match(html,/r\.processed = EVENTS_DATA\.some/);
 assert.match(html,/--card-accent:\$\{catMeta\.color\}/);
 assert.doesNotMatch(html,/scrollIntoView\(/);
});
