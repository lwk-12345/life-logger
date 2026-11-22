import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const html = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function named(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} exists`);
  let depth = 0, opened = false;
  for (let i = source.indexOf('{', start); i < source.length; i += 1) {
    if (source[i] === '{') { depth += 1; opened = true; }
    if (source[i] === '}' && !--depth && opened) return source.slice(start, i + 1);
  }
  throw new Error(`missing end of ${name}`);
}

function load() {
  const labelStart = script.indexOf('const CATEGORY_META');
  const labelEnd = script.indexOf('// =====================================================================================', labelStart);
  const context = {TZ_OFFSET_MIN: 480, result: null};
  vm.createContext(context);
  vm.runInContext(script.slice(labelStart, labelEnd), context);
  for (const name of ['pad2', 'utcIsoToLocalParts', 'eventOccurrenceUnknown', 'formatReportedAt', 'unlocatedItemMarkup', 'escapeHtml', 'escapeAttr']) {
    vm.runInContext(named(script, name), context);
  }
  return context;
}

const SAMPLE = {
  event_id: 'evt_ctx_21595457e7007e25',
  title: '说明项目涉及六个县城',
  activity_type: 'work_discussion',
  time: {absolute: null, precision: 'unknown', relative_seconds: null},
  evidence: {reported_at: '2026-10-22T14:07:00Z', quote: '项目要覆盖六个县城'},
};

test('absolute=null 仍归入未定位', () => {
  const c = load();
  const verdict = c.eventOccurrenceUnknown(SAMPLE);
  assert.equal(verdict.unknown, true);
  assert.equal(verdict.reason, 'no_absolute_time');
});

test('precision=unknown 时即使 absolute 被错误回填也不进发生时间轴', () => {
  const c = load();
  const backfilled = {...SAMPLE, time: {absolute: '2026-10-22T14:07:00Z', precision: 'unknown'}};
  const verdict = c.eventOccurrenceUnknown(backfilled);
  assert.equal(verdict.unknown, true, '后端回填 absolute 也必须判定为未知');
  assert.equal(verdict.reason, 'precision_unknown');
});

test('date_basis=reported_local 与 temporal_relation=unknown 同样归入未定位', () => {
  const c = load();
  assert.equal(c.eventOccurrenceUnknown({...SAMPLE, date_basis: 'reported_local'}).unknown, true);
  assert.equal(c.eventOccurrenceUnknown({...SAMPLE, temporal_relation: 'unknown'}).unknown, true);
  assert.equal(c.eventOccurrenceUnknown({...SAMPLE, evidence: {...SAMPLE.evidence, temporal_relation: 'unknown'}}).unknown, true);
});

test('真实发生时间的事件照常进入时间轴', () => {
  const c = load();
  const exact = {event_id: 'evt_ok', title: '午饭', activity_type: 'meal', categories: ['food'],
    time: {absolute: '2026-10-22T04:30:00Z', precision: 'minute'}, evidence: {reported_at: '2026-10-22T04:31:00Z'}};
  assert.equal(c.eventOccurrenceUnknown(exact).unknown, false);
  const parts = c.utcIsoToLocalParts(exact.time.absolute);
  assert.equal(parts.hh, 12);
});

test('未定位条目显示讲述时间与“发生时间未知”，不写成 22:07 发生', () => {
  const c = load();
  const markup = c.unlocatedItemMarkup({
    event_id: SAMPLE.event_id, title: SAMPLE.title, activity_type: 'work_discussion',
    quote: SAMPLE.evidence.quote, precision: 'unknown', reported_at: SAMPLE.evidence.reported_at,
  }, '');
  assert.match(markup, /说明项目涉及六个县城/);
  assert.match(markup, /项目要覆盖六个县城/);
  assert.match(markup, /工作讨论/);
  assert.match(markup, /讲述于 2026-10-22 22:07/);
  assert.match(markup, /发生时间未知/);
  assert.equal(/发生于\s*22:07/.test(markup), false, '不得把讲述时间写成发生时间');
  assert.equal(/work_discussion/.test(markup.replace(/title="[^"]*"/g, '')), false, '可见文本不得露 snake_case');
});

test('缺少 reported_at 时不编造时间', () => {
  const c = load();
  assert.equal(c.formatReportedAt(''), '');
  const markup = c.unlocatedItemMarkup({event_id: 'e', title: 'x', activity_type: 'general', quote: '', precision: 'unknown', reported_at: ''}, '');
  assert.match(markup, /讲述时间未知/);
  assert.equal(/\d{4}-\d{2}-\d{2}/.test(markup), false);
});


test('capture/inferred display_time 有值时可进入时间位置，reported_local 仍未知', () => {
  const c = load();
  const capture = {event_id:'c', title:'裸重', event_time_basis:'capture_statement',
    time:{absolute:null, precision:'approximate'}, display_time:{value:'2026-10-24T14:34:17+08:00', precision:'approximate', label:'约14:34（按讲述时间）', basis:'capture_statement'}};
  assert.equal(c.eventOccurrenceUnknown(capture).unknown, false);
  const inferred = {event_id:'i', title:'入睡', event_time_basis:'inferred_occurrence',
    time:{absolute:null, precision:'inferred'}, display_time:{value:'2026-10-17T17:20:44+08:00', precision:'inferred', label:'17:20（按段转录时间）', basis:'inferred_occurrence'}};
  assert.equal(c.eventOccurrenceUnknown(inferred).unknown, false);
  const reported = {event_id:'r', title:'讨论', event_time_basis:'reported_local', date_basis:'reported_local',
    time:{absolute:null, precision:'unknown'}, display_time:null};
  assert.equal(c.eventOccurrenceUnknown(reported).unknown, true);
});
