import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const timeline = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');

function named(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} exists`);
  // 先跳过参数表再数大括号：`{background = false} = {}` 这类解构默认值里的大括号
  // 会让「从第一个 { 开始裸数」提前收尾，切出半截函数（Unexpected end of input）。
  let closeParen = -1;
  for (let i = source.indexOf('(', start), parens = 0; i < source.length; i += 1) {
    if (source[i] === '(') parens += 1;
    else if (source[i] === ')' && !--parens) { closeParen = i; break; }
  }
  assert.ok(closeParen > 0, `${name} parameter list ends`);
  let depth = 0, opened = false;
  for (let i = source.indexOf('{', closeParen); i < source.length; i += 1) {
    if (source[i] === '{') { depth += 1; opened = true; }
    if (source[i] === '}' && !--depth && opened) return source.slice(start, i + 1);
  }
  throw new Error(`missing end of ${name}`);
}

function load(source, names, extra = {}) {
  const context = {console, Math, Number, String, Set, Error, ...extra};
  vm.createContext(context);
  // 顶层 const 不会随函数一起被截取，按源码取值补进去，避免测试环境与真实作用域不一致。
  const maxAttempts = /const PENDING_MAX_ATTEMPTS = (\d+);/.exec(source);
  const statuses = /const PENDING_READ_STATUSES = new Set\(\[([^\]]+)\]\);/.exec(source);
  if (maxAttempts) vm.runInContext(`const PENDING_MAX_ATTEMPTS = ${maxAttempts[1]};`, context);
  if (statuses) vm.runInContext(`const PENDING_READ_STATUSES = new Set([${statuses[1]}]);`, context);
  for (const name of names) vm.runInContext(named(source, name), context);
  return context;
}

test('202 and explicit read_status are pending; a normal 200 is not', () => {
  const {pendingReadInfo} = load(app, ['pendingReadInfo']);
  const pending = pendingReadInfo({read_status: 'pending', task_id: 'a'.repeat(32), retry_after_ms: 1200}, 202);
  assert.equal(pending.pending, true);
  assert.equal(pending.taskId, 'a'.repeat(32));
  assert.equal(pending.retryAfterMs, 1200);
  assert.equal(pendingReadInfo({read_status: 'computing'}, 202).pending, true);
  assert.equal(pendingReadInfo({read_status: 'read_busy'}, 200).pending, true);
  assert.equal(pendingReadInfo({events: [], expense_ledger: {}}, 200).pending, false);
  assert.equal(pendingReadInfo({kind: 'ENV', ok: false}, 503).pending, false);
  assert.equal(pendingReadInfo(null, 200).pending, false);
});

test('retry delay is bounded and never becomes an unbounded poll loop', () => {
  const {pendingRetryDelay, pendingReadInfo} = load(app, ['pendingRetryDelay', 'pendingReadInfo']);
  const info = pendingReadInfo({read_status: 'pending', retry_after_ms: 10}, 202);
  assert.ok(info.retryAfterMs >= 250, 'tiny server hint must still be clamped up');
  for (const attempt of [0, 1, 3, 12, 99]) {
    const delay = pendingRetryDelay(info, attempt);
    assert.ok(Number.isFinite(delay) && delay >= 250 && delay <= 5000, `delay ${delay} bounded`);
  }
  assert.ok(pendingRetryDelay(info, 3) >= pendingRetryDelay(info, 0));
  assert.equal(info.maxAttempts, 10);
});

test('pending scheduling is single-timer, bounded, and stops after max attempts', () => {
  const timers = [];
  const calls = [];
  const day = {date: '2026-10-21'};
  const context = load(app, ['scheduleJournalPendingRetry', 'pendingRetryDelay', 'pendingReadInfo'], {
    journalDays: [day],
    selectedJournalDate: '2026-10-21',
    setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
    clearTimeout: () => {},
    ensureJournalDaySynthesis: (target, options) => calls.push({date: target.date, options}),
    renderJournalNavigation: () => {},
  });
  const info = context.pendingReadInfo({read_status: 'pending', retry_after_ms: 400}, 202);
  context.scheduleJournalPendingRetry(day, info);
  assert.equal(timers.length, 1, 'one timer per day');
  assert.ok(timers[0].ms >= 250 && timers[0].ms <= 5000);
  timers[0].fn();
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [{date: '2026-10-21', options: {force: true}}]);

  for (let i = 0; i < 12; i += 1) {
    timers.length = 0;
    context.scheduleJournalPendingRetry(day, info);
    if (timers[0]) timers[0].fn();
  }
  const before = calls.length;
  timers.length = 0;
  context.scheduleJournalPendingRetry(day, info);
  assert.equal(timers.length, 0, 'polling must stop, not loop forever');
  assert.equal(calls.length, before);
  assert.equal(day.canonicalPending.exhausted, true);
});

test('a pending timer for a superseded date never refreshes the newly selected date', () => {
  const timers = [];
  const calls = [];
  const oldDay = {date: '2026-10-20'};
  const context = load(app, ['scheduleJournalPendingRetry', 'pendingRetryDelay', 'pendingReadInfo'], {
    journalDays: [oldDay],
    selectedJournalDate: '2026-10-21',
    setTimeout: (fn, ms) => { timers.push(fn); return 1; },
    clearTimeout: () => {},
    ensureJournalDaySynthesis: (target) => calls.push(target.date),
    renderJournalNavigation: () => {},
  });
  context.scheduleJournalPendingRetry(oldDay, context.pendingReadInfo({read_status: 'pending'}, 202));
  timers[0]();
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [], 'stale date timer must be ignored');
});

test('iframe treats 202 read_status as pending, never as an empty day', async () => {
  const context = load(timeline, ['pendingReadInfo', 'pendingRetryDelay']);
  const info = context.pendingReadInfo({read_status: 'pending', retry_after_ms: 900}, 202);
  assert.equal(info.pending, true);
  assert.ok(info.retryAfterMs >= 250 && info.retryAfterMs <= 5000);
  assert.equal(context.pendingReadInfo({ok: true, date: '2026-10-21', events: [], intervals: []}, 200).pending, false);
});
