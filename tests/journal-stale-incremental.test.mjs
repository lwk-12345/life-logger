import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

test('增量同步支持缺失节点插入（.journal-report-stale 能从无到有）', () => {
  assert.match(app, /function insertJournalDayNode\(/);
  const body = app.slice(app.indexOf('function insertJournalDayNode('));
  const fn = body.slice(0, body.indexOf('\n}\n') + 3);
  assert.match(fn, /anchor\.before\(node\)/, '按后续节点做 anchor 插入');
  assert.match(fn, /dashboard\.appendChild\(node\)/, '没有 anchor 时追加');
  const sync = app.slice(app.indexOf('function syncJournalDayNodes('));
  const syncFn = sync.slice(0, sync.indexOf('\n}\n') + 3);
  assert.match(syncFn, /if \(!oldNode\)/, '缺旧节点要进入插入分支');
  assert.match(syncFn, /insertJournalDayNode\(dashboard, selector, nextNode\)/);
  // 原有行为保留
  assert.match(syncFn, /nextNode\.open = oldNode\.open/, 'details 展开态仍保留');
  assert.match(syncFn, /oldNode\.remove\(\)/, '新模板没有的节点仍移除');
});

test('整块回退守卫仍在（缺 header/summary 时）', () => {
  assert.match(app, /function journalDayIncrementalReady\(/);
  const branches = app.slice(app.indexOf('const mountedFrame = dashboard.querySelector?.(".day-timeline-frame");'));
  assert.match(branches, /dashboard\.innerHTML = markup;/, '缺节点回退整块重绘');
});
