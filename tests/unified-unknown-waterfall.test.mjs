import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const html = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');

test('取消独立“时间未定位”下钻区，改为瀑布流内的“时刻未知”全天带', () => {
  assert.doesNotMatch(html, /class="unknown-time-row"/, '不得再有独立未知区');
  assert.match(html, /class="hour-band unknown-band"/, '未知事件要在同一条瀑布流里');
  assert.match(html, /data-hour="unknown"/);
  assert.match(html, /时刻<br \/>未知|时刻未知/, '左侧标注“时刻未知”');
  assert.match(html, /unknown-band-note/, '带内说明发生时间未知');
});

test('未知卡片保留讲述时间与“发生时间未知”，且不占虚假小时点', () => {
  assert.match(html, /讲述于/);
  assert.match(html, /发生时间未知/);
  assert.match(html, /时间未定位<\/span>/, '卡片仍有明确标签');
  // 未知带里不生成 time/绝对分钟
  const band = html.slice(html.indexOf('class="hour-band unknown-band"'), html.indexOf('body.innerHTML = html'));
  assert.doesNotMatch(band, /ev-time/, '未知卡片不渲染小时点');
});

test('总事件数单一主口径：含时刻未知，不再重复计数', () => {
  // 口径没变（含时刻未知、只加一次），只是文案从「事件 N 条」改成「日摘要要点 N 条」。
  assert.match(html, /const eventTrack = m\.events \+ m\.crossDate \+ \(m\.unlocated \|\| 0\);/);
  assert.match(html, /日摘要要点 <strong>\$\{eventTrack\}<\/strong> 条/);
  assert.match(html, /时刻未知 \$\{m\.unlocated\}/);
  assert.doesNotMatch(html, /时间未定位 <strong>\$\{m\.unlocated\}<\/strong> 条/, '不再单独重复计数');
});
