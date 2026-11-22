import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../web/daily-timeline-live.html", import.meta.url), "utf8");

test("CSS: 移动端 (max-width: 620px) 媒体查询包含关键适配规则", () => {
  // 1. 窄屏下列宽必须保证 minmax(0, 1fr) 或 1fr，防止内容溢出
  assert.match(html, /grid-template-columns:\s*46px 24px 30px minmax\(0,\s*1fr\)/);

  // 2. window-activity-card 自适应宽度与 box-sizing
  assert.match(html, /\.window-activity-card\s*\{[^}]*max-width:\s*100%/);
  assert.match(html, /\.window-activity-card\s*\{[^}]*box-sizing:\s*border-box/);

  // 3. 头部信息允许换行 (flex-wrap: wrap)
  assert.match(html, /\.window-activity-card \.ev-header\s*\{[^}]*flex-wrap:\s*wrap/);
  assert.match(html, /\.window-activity-card \.ev-meta\s*\{[^}]*flex-wrap:\s*wrap/);

  // 4. badge 允许换行并不超出卡片
  assert.match(html, /\.window-activity-target-badge\s*\{[^}]*max-width:\s*100%/);
  assert.match(html, /\.window-activity-target-badge\s*\{[^}]*flex-wrap:\s*wrap/);

  // 5. 顶栏分类过滤器在移动端允许折行
  assert.match(html, /\.window-category-filters\s*\{[^}]*flex-wrap:\s*wrap/);
  assert.match(html, /\.window-cat-pill\s*\{[^}]*max-width:\s*100%/);
});

test("CSS: 不存在破坏布局的硬编码大固定宽度", () => {
  // 检查在移动端下没有写死大 px 宽度的 .window-activity-card 或 .window-activity-target-badge
  const mobileCssSection = html.slice(html.indexOf("@media (max-width: 620px)"), html.indexOf("</style>"));
  // 只看“声明”（前面是 ; 或 {），否则会把 @media (max-width: 620px) 的前导句误当成声明。
  const DECL_WIDTH = /(?<=[;{])\s*(?:min-)?width:\s*[2-9]\d{2,}px/;
  assert.doesNotMatch(mobileCssSection, /(?<=[;{])\s*min-width:\s*[2-9]\d{2,}px/);
  assert.doesNotMatch(mobileCssSection, DECL_WIDTH);
});
