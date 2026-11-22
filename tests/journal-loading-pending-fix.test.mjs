import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

function named(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} exists`);
  let i = source.indexOf('(', start), paren = 0, body = -1;
  for (; i < source.length; i += 1) {
    if (source[i] === '(') paren += 1;
    else if (source[i] === ')' && --paren === 0) { body = source.indexOf('{', i); break; }
  }
  let depth = 0;
  for (let j = body; j < source.length; j += 1) {
    if (source[j] === '{') depth += 1;
    else if (source[j] === '}' && --depth === 0) return source.slice(start, j + 1);
  }
  throw new Error(`missing end of ${name}`);
}

test('canonical 成功后必须清除过期 loading 状态并重绘当天视图', () => {
  const source = named(app, 'ensureJournalDaySynthesis');
  assert.match(source, /day\._loadingSince\s*=\s*0/, '成功分支要重置加载起点');
  assert.match(source, /clearTimeout\(day\._slowTimer\)/, '成功分支要清掉 3 秒升级计时器');
  assert.match(source, /renderJournalDay\(/, '成功分支必须重绘 journal day');
  assert.match(source, /selectedJournalDate\s*===\s*day\.date/, '重绘前要判断仍是当前选中日期（防旧日期覆盖）');
});

test('pending(202) 必须走 pending 分支，不能当成失败写 canonicalError', () => {
  const source = named(app, 'ensureJournalDaySynthesis');
  assert.match(source, /isPendingApiError\(error\)/, '需要识别 pending');
  const pendingBlock = source.slice(source.indexOf('isPendingApiError(error)'));
  assert.match(pendingBlock.slice(0, 500), /canonicalPending/, 'pending 分支写 canonicalPending');
  assert.match(pendingBlock.slice(0, 500), /canonicalError\s*=\s*null/, 'pending 不得留错误文案');
  assert.match(source, /scheduleJournalPendingRetry\(/, 'pending 要安排有限重试');
});

test('setPendingReviewState 调真实写接口并刷新计数', () => {
  const source = named(app, 'setPendingReviewState');
  assert.match(source, /\/api\/pending-sources\/status/, '必须是服务端唯一写入口');
  assert.match(source, /method:\s*"POST"/);
  assert.match(source, /source_id/, '要带 source_id');
  assert.match(source, /refreshPendingReviewBlock\(/, '成功后刷新开放计数');
  assert.match(source, /catch/, '失败要捕获并返回 false');
  assert.match(app, /window\.setPendingReviewState\s*=/, '要挂到 window 供按钮调用');
});

test('pending 后自动重试、成功时清空 loading 且不写 canonicalError', async () => {
  const context = {Date, Map, Set, Number, String, Error, console, setTimeout, clearTimeout};
  vm.createContext(context);
  const names = ['ensureJournalDaySynthesis', 'isPendingApiError', 'pendingReadInfo',
    'pendingRetryDelay', 'scheduleJournalPendingRetry', 'cancelJournalPendingPolling'];
  for (const name of names) vm.runInContext(named(app, name), context);
  vm.runInContext(`
    journalCanonicalGeneration = 0;
    journalCanonicalCache = new Map();
    journalDays = [{ date: "2026-10-22", canonical: false, entries: [], categories: {},
      timeline: [], workspaceEvents: [], recordingIntervals: [], expense_ledger: {} }];
    selectedJournalDate = "2026-10-22";
    activeView = "today";
    let calls = 0;
    api = async () => {
      calls += 1;
      if (calls === 1) {
        const error = new Error("正在更新这一天的数据，稍后自动刷新");
        error.pending = true;
        error.pendingInfo = { pending: true, retryAfterMs: 20, taskId: "a".repeat(32), maxAttempts: 10, workerOnline: true };
        throw error;
      }
      return { date: "2026-10-22", events: [] };
    };
    sharedTimelineRead = async (_path, loader) => loader();
    hydrateJournalDayFromCanonical = (day) => { day.canonical = true; };
    renderJournalNavigation = () => {};
    renderJournalDay = () => {};
    renderReviewDashboard = () => {};
    renderKanban = () => {};
    LifeTimelineData = undefined;
    shanghaiDate = () => "2026-10-24";
  `, context);
  const day = vm.runInContext('journalDays[0]', context);
  await vm.runInContext('ensureJournalDaySynthesis(journalDays[0])', context);
  assert.equal(day.canonical, false, '第一次是 pending，不算成功');
  assert.equal(day.canonicalError, null, 'pending 不得写成错误');
  assert.ok(day.canonicalPending, '应记录 pending 状态');
  await new Promise((resolve) => setTimeout(resolve, 400));  // 等有界重试
  assert.equal(day.canonical, true, '重试后应成为 canonical');
  assert.equal(day.canonicalPending, null, '成功后清空 pending');
  assert.equal(day._loadingSince, 0, '成功后清空 loading 起点');
});
