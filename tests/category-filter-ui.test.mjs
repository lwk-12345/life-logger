import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const html = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');

test('“更多 N 类 + / 收起” 文案与 aria 契约', () => {
  assert.match(html, /更多 \$\{zeros\.length\} 类 \+/, '收起态文案');
  assert.match(html, /sceneLegendExpanded \? "收起"/);
  assert.match(html, /catLegendExpanded \? "收起"/);
  assert.match(html, /aria-expanded="\$\{catLegendExpanded\}"/);
  assert.match(html, /aria-controls="\$\{moreId\}"/);
  assert.match(html, /id="\$\{moreId\}"\$\{catLegendExpanded \? "" : " hidden"\}/, '零计数默认收起');
  assert.doesNotMatch(html, /显示其余 \$\{zeros\.length\} 类/, '旧文案已移除');
});

test('幽灵按钮样式与分类 chips 同级（不再用浏览器默认按钮）', () => {
  const css = html.slice(html.indexOf('.legend-more {'), html.indexOf('.legend-more-items'));
  assert.match(css, /font-size: 11px/, '与 cat-pill 同为 11px');
  assert.match(css, /border-radius: var\(--radius-pill\)/, '胶囊形');
  assert.match(css, /background: transparent/, '幽灵底色');
  assert.match(css, /border: 1px dashed var\(--line-strong\)/, '细虚线，不是 2px outset');
  assert.match(css, /color: var\(--muted\)/, '弱化，不与“全部事件”抢注意力');
  assert.match(css, /:focus-visible/, '键盘焦点可见');
  assert.match(html, /@media \(pointer: coarse\)[\s\S]{0,160}\.legend-more \{[^}]*min-height: 44px/, '触控目标用真实 44px 尺寸');
  assert.doesNotMatch(html, /\.legend-more::before[^}]*inset:\s*-\d/, '不得用负 inset 覆盖相邻 chip');
  // 不存在大号描边按钮写法
  assert.doesNotMatch(css, /border-width: 2px|padding: 12px 24px|font-size: 16px/);
});

test('展开/收起复用同一渲染函数，保留非零分类优先', () => {
  assert.match(html, /function toggleCatLegendMore\(\)/);
  assert.match(html, /const withData = entries\.filter\(e => e\.cnt > 0\)/);
  assert.match(html, /withData\.map\(e => catPillHtml/);
});
