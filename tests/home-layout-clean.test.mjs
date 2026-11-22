import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const style = fs.readFileSync('web/style.css', 'utf8');
const app = fs.readFileSync('web/app.js', 'utf8');
const index = fs.readFileSync('web/index.html', 'utf8');

test('首页外层信息层次精简：保留关键入口，去除冗余占位与实现术语阻断', () => {
  // 1. 关键业务入口必须保留
  assert.ok(index.includes('id="natural-correction-card"'), '口语纠错输入卡片必须存在');
  assert.ok(index.includes('id="journal-card"'), '每日记录卡片必须存在');
  assert.ok(index.includes('id="journal-dashboard"'), '日记主面板必须存在');

  // 2. 增量节点选择器完整保留，确保 DOM 增量同步不中断
  assert.ok(app.includes('const JOURNAL_DAY_INCREMENTAL_SELECTORS = ['), '增量选择器定义必须存在');
  for (const sel of [
    '.journal-day-header',
    '.journal-switch-banner',
    '.journal-report-stale',
    '.focus-summary',
    '.day-expense-drilldown',
    '.today-details',
    '.journal-empty',
  ]) {
    assert.ok(app.includes(sel), `必须保留增量同步选择器: ${sel}`);
  }
});

test('样式优化：消除巨大 127px 状态占位块，转化为紧凑横向状态条', () => {
  // 验证 journal-empty 的精简规则
  const emptyBlockRegex = /body\[data-active-view="today"\]\s+\.journal-empty\s*\{[^}]+\}/;
  const match = style.match(emptyBlockRegex);
  assert.ok(match, '今日视图下必须对 .journal-empty 提供紧凑样式定义');
  const css = match[0];
  assert.ok(css.includes('flex-direction: row') || css.includes('flex-direction:row'), '必须为横向排列');
  assert.ok(css.includes('align-items: center') || css.includes('align-items:center'), '必须居中对齐');
  assert.ok(css.includes('padding: 5px') || css.includes('padding:5px') || css.includes('padding: 4px'), '必须压缩内边距');
});

test('样式优化：消费下钻模块紧凑化，避免首屏时间流被大幅挤下', () => {
  const drilldownRegex = /body\[data-active-view="today"\]\s+\.day-expense-drilldown\s+\.expense-drilldown\s*\{[^}]+\}/;
  const match = style.match(drilldownRegex);
  assert.ok(match, '必须对今日视图下的消费下钻容器做紧凑样式约束');
  const css = match[0];
  assert.ok(css.includes('margin: 4px') || css.includes('margin:4px'), '必须压缩外部边距');
  assert.ok(css.includes('padding: 6px') || css.includes('padding:6px'), '必须压缩内部内边距');
});

test('移动端 (390px) 紧凑层级：压缩顶部边距与输入框高度，让时间流提早出现', () => {
  const mobileMediaRegex = /@media\(max-width:620px\)\s*\{([\s\S]*?)\n\}/;
  const match = style.match(mobileMediaRegex);
  assert.ok(match, '必须包含移动端 620px 响应式精简规则');
  const mobileCss = match[1];
  assert.ok(mobileCss.includes('body[data-active-view="today"] .workspace'), '必须对今日工作区边距进行移动端优化');
  assert.ok(mobileCss.includes('body[data-active-view="today"] .topbar'), '必须对顶栏标题高度进行移动端优化');
  assert.ok(mobileCss.includes('body[data-active-view="today"] #journal-card'), '必须对日记卡片内边距进行移动端优化');
});
