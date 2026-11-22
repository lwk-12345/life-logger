import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const html = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function loadContinuity() {
  const start = script.indexOf('function sameSpanRun');
  const end = script.indexOf('function markSpanContinuity');
  assert.notEqual(start, -1, 'missing sameSpanRun');
  assert.notEqual(end, -1, 'missing markSpanContinuity');
  const block = script.slice(start, script.indexOf('}', script.indexOf('return list;', end)) + 1);
  const ctx = { result: null };
  vm.createContext(ctx);
  vm.runInContext(block + '\nresult = { sameSpanRun, markSpanContinuity };', ctx);
  return ctx.result;
}
const { markSpanContinuity } = loadContinuity();

test('相邻同场景且首尾相接 → 合并为同一连续段（含跨小时）', () => {
  const list = [
    { scene: 'home_quiet', start: 540, end: 570 },   // 09:00–09:30
    { scene: 'home_quiet', start: 570, end: 600 },   // 09:30–10:00
    { scene: 'home_quiet', start: 600, end: 630 },   // 10:00–10:30（跨小时边界）
  ];
  markSpanContinuity(list, (s) => s.scene);
  assert.equal(list[0].contNext, true);
  assert.equal(list[1].contPrev, true);
  assert.equal(list[1].contNext, true);
  assert.equal(list[2].contPrev, true);
  assert.equal(list[2].contNext, false);
});

test('真实缺口/场景变化保留分界', () => {
  const gap = [{ scene: 'work', start: 540, end: 570 }, { scene: 'work', start: 571, end: 600 }];
  markSpanContinuity(gap, (s) => s.scene);
  assert.equal(gap[0].contNext, false, '1 分钟真实缺口不得抹平');
  assert.equal(gap[1].contPrev, false);

  const changed = [{ scene: 'work', start: 540, end: 570 }, { scene: 'meal', start: 570, end: 600 }];
  markSpanContinuity(changed, (s) => s.scene);
  assert.equal(changed[0].contNext, false, '场景变化必须分界');
});

test('已抽取绿段与待抽取黄绿段不合并', () => {
  const list = [
    { processed: true, start: 540, end: 570 },
    { processed: true, start: 570, end: 600 },
    { processed: false, start: 600, end: 630 },
  ];
  markSpanContinuity(list, (s) => (s.processed ? 'processed' : 'pending'));
  assert.equal(list[0].contNext, true);
  assert.equal(list[1].contNext, false, '状态变化（已抽取→待抽取）不得连成一段');
  assert.equal(list[2].contPrev, false);
});

test('单段没有接缝标记', () => {
  const list = [{ scene: 'work', start: 540, end: 600 }];
  markSpanContinuity(list, (s) => s.scene);
  assert.equal(list[0].contPrev, false);
  assert.equal(list[0].contNext, false);
});

test('渲染层：接缝无圆角/阴影，跨小时不断缝，图标只在整段起点', () => {
  const css = html.slice(html.indexOf('.rec-bar-seg.cont-up'), html.indexOf('.rec-bar-seg.pending-digest'));
  assert.match(css, /border-bottom-left-radius: 0/, '向下续接：底端不留圆角');
  assert.match(css, /border-top-left-radius: 0/, '向上续接：顶端不留圆角');
  assert.match(css, /\.rec-bar-seg\.cont-up\.cont-down \{ border-radius: 0; box-shadow: none; \}/, '中间段无圆角无阴影');
  const sceneCss = html.slice(html.indexOf('.scene-bar-seg.cont-down'), html.indexOf('.scene-bar-seg.dimmed'));
  assert.match(sceneCss, /border-bottom-left-radius: 0/);
  assert.match(sceneCss, /border-radius: 0; box-shadow: none/);

  // [2026-10-29 几何修复] 相邻段改为严格相接：高度恒为 heightPct%，
  // 不再用 calc(… - 2px)（固定 2px 在 71px 带里 = 1.7 分钟、在 860px 带里 = 0.14 分钟），
  // 真实分界改由 .sep-bottom 的内描边表达；语义仍是「连续处不留缝、真实分界才留」。
  assert.match(html, /const segHeight = `\$\{heightPct\}%`;/, '段高必须严格按真实分钟比例');
  assert.doesNotMatch(html, /calc\(\$\{heightPct\}% - 2px\)/, '不得回退到固定 2px 缝');
  assert.match(html, /\$\{contDown \? "" : " sep-bottom"\}/, '真实分界才加 sep-bottom');
  assert.match(html, /const contUp = \(r\.contPrev \|\| Number\(r\.start\) < hStart\)/, '录音跨小时接缝');
  assert.match(html, /const contDown = \(s\.contNext \|\| Number\(s\.end\) > hEnd\)/, '场景跨小时接缝');
  assert.match(html, /const showIcon = !s\.contPrev && Number\(s\.start\) >= hStart && \(ovEnd - ovStart\) >= 25 \? meta\.icon : "";/, '重复图标只在整段起点');
  assert.match(html, /aria-label="录音 \$\{fmtMin\(r\.start\)\} 至 \$\{fmtMin\(r\.end\)\}/, '每段保留 aria');
  assert.match(html, /aria-label="\$\{escapeAttr\(meta\.label \|\| ''\)\} \$\{fmtMin\(s\.start\)\} 至 \$\{fmtMin\(s\.end\)\}"/, '场景每段保留经过 escapeAttr 严格转义的 aria-label');
  assert.match(html, /onclick="toggleSceneHighlight\('\$\{escapeAttr\(s\.scene\)\}'\)"/, '点击高亮行为保留');
  assert.doesNotMatch(html, /margin-(top|bottom):\s*-\d+px[\s\S]{0,80}(rec|scene)-bar/, '不得用负 margin 抹掉真实缺口');
});

test('场景轨 aria-label HTML转义语义验证：包含恶意双引号或尖括号的场景label必须严格转义，杜绝属性破损', () => {
  const escapeStart = script.indexOf('function escapeAttr(');
  const escapeEnd = script.indexOf('function setCategory(');
  assert.ok(escapeStart > 0 && escapeEnd > escapeStart);
  const escapeBlock = script.slice(escapeStart, escapeEnd);
  const ctx = { result: null };
  vm.createContext(ctx);
  vm.runInContext(escapeBlock + '\nresult = { escapeAttr };', ctx);
  const escapeAttr = ctx.result.escapeAttr;

  const maliciousLabel = '商场" onclick="alert(1)" label=\'测试\'><script>';
  const escaped = escapeAttr(maliciousLabel);
  assert.ok(!escaped.includes('"'), '绝不可包含裸双引号');
  assert.ok(!escaped.includes("'"), '绝不可包含裸单引号');
  assert.ok(!escaped.includes('<'), '绝不可包含裸尖括号');
  assert.ok(escaped.includes('&quot;'), '双引号转义为 &quot;');
  assert.ok(escaped.includes('&#39;'), '单引号转义为 &#39;');
  assert.ok(escaped.includes('&lt;'), '左尖括号转义为 &lt;');
});
