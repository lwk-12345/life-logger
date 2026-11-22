import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

test('增量渲染三条路径都同步 .journal-empty：新模板没有就移除旧的', () => {
  assert.match(app, /function syncJournalDayNodes\(/);
  const body = app.slice(app.indexOf('function syncJournalDayNodes('));
  const fn = body.slice(0, body.indexOf('\n}\n') + 3);
  assert.match(fn, /if \(nextNode\)/, '有则替换');
  assert.match(fn, /if \(oldNode\) oldNode\.remove\(\)/, '没有则移除旧节点');
  const selectors = app.slice(app.indexOf('const JOURNAL_DAY_INCREMENTAL_SELECTORS'), app.indexOf('function syncJournalDayNodes('));
  assert.match(selectors, /\.journal-empty/, '.journal-empty 必须在同步列表里');
  // 产品契约是三条**互斥**的增量路径，缺一条就会有人白屏/残留旧节点：
  //   ① !day.canonical && canReuseFrame（2026-10-24 未就绪但已有 iframe：保留 iframe，只同步外围节点）
  //   ② day.canonical && sameDate（同日期补充更新计数/详情，不重载 iframe）
  //   ③ day.canonical && mountedFrame && 日期不同（切日：保留 iframe，只换头部/摘要/详情）
  // 三者分别位于 `if (!day.canonical)` 内部与 canonical 块内的 if / else-if，天然互斥，故精确等于 3。
  const syncCalls = (app.match(/syncJournalDayNodes\(dashboard, template\);/g) || []).length;
  assert.equal(syncCalls, 3, `三条增量路径都必须同步节点，实际 ${syncCalls} 处`);
  // 旧的“只替换、不清理”的内联循环不应残留
  assert.doesNotMatch(app, /for \(const selector of \["\.journal-day-header", "\.focus-summary", "\.day-expense-drilldown", "\.today-details"\]\)/);
});

test('增量路径要求旧 DOM 结构完整，缺节点时整块重绘', () => {
  assert.match(app, /function journalDayIncrementalReady\(/);
  const branches = app.slice(app.indexOf('const mountedFrame = dashboard.querySelector?.(".day-timeline-frame");'));
  assert.match(branches, /if \(sameDate && journalDayIncrementalReady\(dashboard\)\)/, 'sameDate 增量分支要检查结构');
  assert.match(branches, /else if \(mountedFrame && journalDayIncrementalReady\(dashboard\)\)/, '切日增量分支要检查结构');
  assert.match(branches, /dashboard\.innerHTML = markup;/, '缺节点时回退整块重绘');
});
