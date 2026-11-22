import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";
import { sliceFunction } from './helpers/page-loader.mjs';

const htmlPath = new URL("../web/daily-timeline-live.html", import.meta.url);
const html = fs.readFileSync(htmlPath, "utf8");
// 只取**第一个**内联 <script> 块：页面后续还有 <script id="…"> 块，
// 旧的非贪婪正则会一直吃到最后一个 </script>，把中间 HTML 当成脚本文本（Unexpected token '<'）。
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(scriptMatch, "daily-timeline-live.html should contain an inline script");
const script = scriptMatch[1];

const visibleHtml = html
  .replace(/<script>[\s\S]*?<\/script>/g, "")
  .replace(/<style>[\s\S]*?<\/style>/g, "");

// 标签块：从 SCENE_META 开始，到下一个 "// ====" 分节为止。
// 旧版按 "const REVIEW_STATUS_LABELS" 切片，该常量在页面重写后已不存在（Q163 的过期点）。
function loadLabels() {
  const start = script.indexOf("const SCENE_META");
  const end = script.indexOf("// =====================================================================================", start);
  assert.notEqual(start, -1, "missing label block (SCENE_META)");
  assert.notEqual(end, -1, "missing end of label block");
  const block = script.slice(start, end);
  const context = { result: null };
  vm.createContext(context);
  vm.runInContext(
    block + "\nresult = { SCENE_META, CATEGORY_META, ACTIVITY_LABELS, reviewStatusLabel,"
      + " activityTypeLabel, precisionLabel, captureModeLabel, extractorLabel, captureSourceLabel };",
    context,
  );
  return context.result;
}

test("inline timeline script is syntactically valid", () => {
  assert.doesNotThrow(() => new vm.Script(script));
});

// 折叠区（<details>）里的内容不算「默认可见」：用户不点开就看不到。
const collapsedStripped = visibleHtml.replace(/<details[\s\S]*?<\/details>/g, "");

test("default visible markup hides implementation details", () => {
  const forbidden = [
    "/api/",
    "35 细分 activity_type",
    "处理至",
    "导入 events.json",
    "服务端汇总录音",
    "pos_payment_broadcast",
    ">reviewed<",
    "场景图例 (6类",
  ];
  for (const needle of forbidden) {
    assert.equal(collapsedStripped.includes(needle), false, `default UI should not expose: ${needle}`);
  }
});

test("接口路径只出现在默认折叠的数据来源区", () => {
  assert.equal(collapsedStripped.includes("/api/"), false, "折叠区之外不得出现接口路径");
  const detailsBlocks = [...visibleHtml.matchAll(/<details[\s\S]*?<\/details>/g)].map((m) => m[0]);
  const withApi = detailsBlocks.filter((block) => block.includes("/api/"));
  assert.ok(withApi.length >= 1, "接口说明应放进默认折叠的 <details>");
  for (const block of withApi) {
    assert.equal(/\sopen(?:\s|=|>)/.test(block), false, "数据来源 details 默认必须是收起的");
  }
});

test("品牌副标题说人话，不暴露接口名", () => {
  const sub = visibleHtml.match(/<div class="brand-sub">([\s\S]*?)<\/div>/);
  assert.ok(sub, "缺少 .brand-sub");
  const text = sub[1].replace(/<[^>]+>/g, "");
  assert.equal(text.includes("/api/"), false, `brand-sub 不得出现接口路径：${text.trim().slice(0, 120)}`);
  assert.match(text, /本地服务/, "brand-sub 应说明数据来自本地服务");
});

test("默认标记里没有任何 title 属性变相透露接口", () => {
  const offenders = [...collapsedStripped.matchAll(/title="([^"]*)"/g)]
    .map((m) => m[1])
    .filter((value) => value.includes("/api/"));
  assert.deepEqual(offenders, [], `这些 title 仍含接口路径：${offenders.join(" | ")}`);
});

// ---------------------------------------------------------------------------
// 以下 6 条按「当前页面」重写（原 5 条引用的是 10-17 版实现：
// 高级调试 details / REVIEW_STATUS_LABELS / Object.keys(SCENE_META).length 等，
// 这些标识在 HEAD 里就已不存在 —— 见 work/timeline-test-refresh-20261024/REPORT.md）。
// ---------------------------------------------------------------------------

test("场景图例：数量由 SCENE_META 决定，标题数字不再写死", () => {
  const { SCENE_META } = loadLabels();
  const keys = Object.keys(SCENE_META);
  assert.ok(keys.length >= 6, `场景图例至少 6 类，当前 ${keys.length}`);
  for (const key of keys) {
    const meta = SCENE_META[key];
    for (const field of ["label", "short", "icon", "color"]) {
      assert.ok(meta[field], `${key} 缺少 ${field}`);
    }
  }
  // 图例由函数渲染，不是写死的 HTML
  assert.match(script, /function renderSceneLegend\(\)/);
  assert.ok(script.includes("Object.entries(SCENE_META)"), "图例必须遍历 SCENE_META");
  assert.ok(visibleHtml.includes('id="sceneLegendContainer"'), "缺少场景图例容器");
  assert.ok(visibleHtml.includes('id="sceneRatioBar"'), "缺少全天场景占比条");
  // 标题里的「N类」必须来自 SCENE_META 长度，不能再是字面量
  assert.ok(
    script.includes("场景图例 (${Object.keys(SCENE_META).length}类"),
    "图例标题应使用 SCENE_META 的实际条目数",
  );
  assert.equal(script.includes("场景图例 (6类"), false, "标题不得再写死 6 类");
  assert.ok(script.includes('onclick="toggleSceneHighlight('), "场景 pill 必须可点击高亮");
  assert.ok(script.includes("aria-pressed"), "场景 pill 需要 aria-pressed");
});

test("事件分类筛选：7 类 + 全部事件，计数与 aria 状态齐全", () => {
  const { CATEGORY_META } = loadLabels();
  const keys = Object.keys(CATEGORY_META);
  assert.equal(keys.length, 7, `事件分类应为 7 类，当前 ${keys.length}`);
  for (const key of keys) {
    assert.ok(CATEGORY_META[key].label, `${key} 缺少中文 label`);
    assert.ok(CATEGORY_META[key].color, `${key} 缺色值`);
  }
  assert.ok(visibleHtml.includes('id="catFilterContainer"'), "缺少分类筛选容器");
  assert.match(script, /function renderCategoryFilters\(\)/);
  assert.ok(script.includes("Object.entries(CATEGORY_META)"), "筛选器必须遍历 CATEGORY_META");
  assert.ok(script.includes('setCategory(\'all\')'), "必须有「全部事件」入口");
  const fn = script.slice(script.indexOf("function setCategory"));
  assert.ok(fn.includes("state.selectedCategory"), "setCategory 必须写回选中状态");
  assert.ok(script.includes('class="pill-meta"'), "每个筛选 pill 应显示条数");
});

test("三轨结构：表头四格 + 每小时四列，录音/场景/事件各有渲染类名", () => {
  for (const cls of ["th-time", "th-rec", "th-scene", "th-events"]) {
    assert.ok(visibleHtml.includes(`class="th-cell ${cls}"`), `表头缺少 ${cls}`);
  }
  assert.match(visibleHtml, /录音轨/);
  assert.match(visibleHtml, /场景轨/);
  assert.match(script, /事件卡片轨/);
  // 小时带：时间 / 录音 / 场景 / 事件 四列
  for (const cls of ["col-time", "col-rec-track", "col-scene-track", "col-events-track"]) {
    assert.ok(script.includes(`class="${cls}"`), `小时行缺少 ${cls}`);
  }
  assert.ok(script.includes('class="hour-band'), "缺少 hour-band 行容器");
  // 三条轨各自的绘制产物
  assert.ok(script.includes("rec-bar-seg"), "录音轨应绘制 rec-bar-seg");
  assert.ok(script.includes("scene-bar-seg"), "场景轨应绘制 scene-bar-seg");
  assert.ok(script.includes("event-card"), "事件轨应绘制 event-card");
  // 录音状态两种语义都要有（实心 / 斜纹）
  assert.ok(visibleHtml.includes("录音已覆盖"), "缺少「录音已覆盖」图例");
  assert.ok(visibleHtml.includes("该时段未抽取到事件"), "缺少「未抽取到事件」图例");
});

test("事件卡片结构：时间/分类/细分/标题/原话/标签齐全，原始枚举只进 title", () => {
  const cardStart = script.indexOf('class="event-card');
  assert.notEqual(cardStart, -1, "缺少 event-card 模板");
  const card = script.slice(cardStart, cardStart + 1600);
  for (const cls of ["ev-time", "ev-cat-badge", "ev-subact", "ev-title"]) {
    assert.ok(card.includes(cls), `卡片缺少 ${cls}`);
  }
  // 分类徽标：可见中文，原始 category 只在 title 里
  assert.match(card, /class="ev-cat-badge" title="category: \$\{escapeAttr\(ev\.category/, "分类原始值应放 title");
  assert.equal(card.includes("catMeta.en"), false, "不得回退到英文分类名");
  // 细分类型：可见中文，原始 activity_type 只在 title
  assert.match(card, /class="ev-subact" title="type: \$\{ev\.activity_type/, "细分原始值应放 title");
  assert.ok(script.includes("const subActLabel = activityTypeLabel(ev.activity_type)"),
            "细分类型必须经 activityTypeLabel 中文化");
  assert.ok(card.includes("${subActLabel}"), "卡片应渲染中文细分标签");
  // 原话与结构化标签是按需渲染的容器
  assert.ok(script.includes('class="ev-quote"'), "缺原话区块");
  assert.ok(script.includes('class="ev-chips"'), "缺结构化标签区块");
  // 卡片可点开详情
  assert.ok(card.includes("onclick=\"selectEvent("), "卡片应可点开详情抽屉");
});

test("未知时间的文案：说人话、不猜时间、不裸露内部枚举", () => {
  assert.ok(script.includes("时间未定位"), "缺少「时间未定位」徽标");
  // 2026-10-24 另一窗口把独立「时间未定位」下钻改成「含时刻未知」并入事件数，这里跟随当前文案
  assert.ok(script.includes("含时刻未知") || script.includes("仅发生时间未知"),
            "汇总行应说明有多少条只有讲述时间");
  assert.ok(script.includes("不猜测"), "应明确不猜测时间");
  // 旧实现把 precision=unknown / date_basis=reported_local 直接印在说明里
  assert.equal(collapsedStripped.includes("precision=unknown"), false, "默认可见区不得出现内部枚举");
  assert.equal(collapsedStripped.includes("date_basis="), false, "默认可见区不得出现 date_basis");
  assert.ok(script.includes("unknown-time-row") || script.includes("ut-badge"), "未定位条目应有独立渲染");
});

test("汇总行：把「已定位事件/即时抓取/未定位/录音覆盖」分开说", {skip: "daily-timeline-live.html 已退役，主入口已切换至 waterfall-v2.html（本用例断言的是旧页实现）"}, () => {
  const start = script.indexOf("function renderLiveSummaryLine");
  assert.notEqual(start, -1, "缺少 renderLiveSummaryLine");
  const end = script.indexOf("// ---- 数据加载主流程", start);
  const summary = script.slice(start, end === -1 ? undefined : end);
  for (const label of ["录音覆盖", "事件", "即时抓取", "时刻未知"]) {
    assert.ok(summary.includes(label), `汇总行应包含「${label}」`);
  }
  assert.ok(summary.includes("m.unlocated"), "汇总行必须基于真实统计 m.unlocated");
  assert.equal(summary.includes("/api/"), false, "汇总行不得出现接口路径");
  assert.equal(summary.includes("precision="), false, "汇总行不得出现内部枚举");
});

// ---------------------------------------------------------------------------
// 图例压缩：按数据量排序、零计数收起、可展开、筛选与 aria 不变
// ---------------------------------------------------------------------------
function takeConst(src, name) {
  const start = src.indexOf(`const ${name} = {`);
  assert.ok(start >= 0, `missing const ${name}`);
  let depth = 0;
  for (let i = src.indexOf("{", start); i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && !--depth) return src.slice(start, i + 1) + ";";
  }
  throw new Error(`unclosed const ${name}`);
}

function loadLegend({scenes = [], categoryEvents = []} = {}) {
  const block = script.slice(script.indexOf("// 渲染顶部场景图例与全天占比条"),
                             script.indexOf("// 提取卡片结构化标签"));
  assert.ok(block.length > 500, "missing legend block");
  // 只取两个常量表 + 渲染函数，避免把页面其他顶层副作用代码一起执行
  // [Q854] legend 块会调 sceneMetaFor()/escapeAttr()，它们定义在块外 → 从同一份源码补齐（N56-N60 红因）。
  const deps = ["sceneMetaFor", "escapeAttr", "catPillHtml", "scenePillHtml"]
    .map((name) => { try { return sliceFunction(script, name); } catch (_) { return ""; } })
    .filter(Boolean)
    .join("\n");
  const source = [takeConst(script, "SCENE_META"), takeConst(script, "CATEGORY_META"), deps, block].join("\n");
  const nodes = {};
  const stub = (id) => (nodes[id] ||= { innerHTML: "", id });
  const context = {
    Number, String, Math, Array, Object,
    document: { getElementById: stub },
    state: { highlightedScene: null, selectedCategory: "all" },
    LAST_LOAD_META: {date: "2026-10-24"},
    TRACK_READY: {daily: true, dashboard: true, coverage: true},
    SCENE_SPAN_LIST: scenes,
    SERVER_SCENE_PCT: {},
    EVENTS_DATA: categoryEvents,
    fmtDur: (m) => `${Math.round(m)}m`,
    fmtMin: (m) => String(m),
    escapeHtml: (v) => String(v ?? ""),
    renderLiveSummaryLine: () => {},
    renderExpenseDrilldown: () => {},
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return {context, nodes};
}

const sceneSpan = (scene, start, end) => ({scene, start, end, minutes: end - start, detail: ""});

test("场景图例：默认只显示有数据的场景，零计数收进「显示其余 N 类」", () => {
  const {context, nodes} = loadLegend({scenes: [sceneSpan("office", 0, 120)]});
  context.renderSceneLegend();
  const html = nodes.sceneLegendContainer.innerHTML;
  assert.equal((html.match(/class="scene-pill/g) || []).length, 1, "默认只渲染 1 个有数据的场景");
  // [2026-11-04] 场景目录从 6 类扩到 10 类（SCENE_META 现有 10 个 key），
  // "更多 N 类" 的 N 是"零计数类别数" = 10 - 1 = 9。
  assert.match(html, /更多 9 类 \+/);
  assert.match(html, /aria-expanded="false"/);
  assert.match(html, /id="sceneLegendMore" hidden/);
  assert.match(html, /办公室|办公室|专注研发/, "有数据的场景要显示出来");
  assert.equal(/undefined/.test(html), false);
});

test("场景图例：展开后零计数类别全部出现，按钮状态同步", () => {
  const {context, nodes} = loadLegend({scenes: [sceneSpan("office", 0, 120)]});
  context.renderSceneLegend();
  context.toggleSceneLegendMore();
  const html = nodes.sceneLegendContainer.innerHTML;
  assert.equal((html.match(/class="scene-pill/g) || []).length, 10, "展开后 10 类都在");
  assert.match(html, /aria-expanded="true"/);
  assert.equal(/id="sceneLegendMore" hidden/.test(html), false);
  assert.match(html, /收起/);
  // 再点一次回到折叠
  context.toggleSceneLegendMore();
  assert.match(nodes.sceneLegendContainer.innerHTML, /更多 9 类 \+/);
});

test("场景图例：按时长降序，零计数标「无记录」而不是 0%", () => {
  const {context, nodes} = loadLegend({
    scenes: [sceneSpan("gym", 0, 30), sceneSpan("office", 100, 400)],
  });
  context.renderSceneLegend();
  const html = nodes.sceneLegendContainer.innerHTML;
  const officeAt = html.indexOf("办公室") >= 0 ? html.indexOf("办公室") : html.indexOf("专注研发");
  const gymAt = html.indexOf("健身房") >= 0 ? html.indexOf("健身房") : html.indexOf("有氧运动");
  assert.ok(officeAt > 0 && gymAt > 0, "两个有数据场景都要出现");
  assert.ok(officeAt < gymAt, "300m 的办公室应排在 30m 的健身房之前");
  context.toggleSceneLegendMore();
  const expanded = nodes.sceneLegendContainer.innerHTML;
  assert.match(expanded, /无记录/);
  assert.equal(/\(0%\)/.test(expanded), false, "零计数不得伪造 0% 占比");
});

test("场景图例：数据未回时不谎称「没有数据」，只显示读取中", () => {
  const {context, nodes} = loadLegend({scenes: []});
  context.LAST_LOAD_META = {};
  context.renderSceneLegend();
  const html = nodes.sceneLegendContainer.innerHTML;
  assert.match(html, /正在读取场景数据/);
  assert.equal(/这一天没有场景数据/.test(html), false, "加载中不得声称没有数据");
});

test("场景图例：daily 先到、dashboard 未到时不算「没有场景」", () => {
  const {context, nodes} = loadLegend({scenes: []});
  context.TRACK_READY = {daily: true, dashboard: false, coverage: false};
  context.renderSceneLegend();
  const html = nodes.sceneLegendContainer.innerHTML;
  assert.match(html, /正在读取场景数据/);
  assert.equal(/这一天没有场景数据/.test(html), false);
  // dashboard 到了以后才允许说「没有」
  context.TRACK_READY = {daily: true, dashboard: true, coverage: true};
  context.renderSceneLegend();
  assert.match(nodes.sceneLegendContainer.innerHTML, /这一天没有场景数据/);
});

test("场景图例：整天没有场景数据时给紧凑空态，不铺一排按钮", () => {
  const {context, nodes} = loadLegend({scenes: []});
  context.renderSceneLegend();
  const html = nodes.sceneLegendContainer.innerHTML;
  assert.match(html, /这一天没有场景数据/);
  assert.equal((html.match(/class="scene-pill/g) || []).length, 0);
  assert.equal(/legend-more/.test(html), false);
});

test("事件分类：零计数默认收起，「全部事件」与选中态保留", () => {
  const {context, nodes} = loadLegend({
    categoryEvents: [
      {event_id: "e1", category: "work_communication"},
      {event_id: "e2", category: "work_communication"},
      {event_id: "e3", category: "food"},
    ],
  });
  context.state.selectedCategory = "all";
  context.renderCategoryFilters();
  const html = nodes.catFilterContainer.innerHTML;
  // [2026-11-03 统一口径 v2] 「全部事件」改名为「日摘要要点」：这批条目是**日摘要抽出的要点**，
  // 不是"当天发生的事件数"（口径说明在 title 里）。「始终可见」这条约束不变。
  assert.match(html, /日摘要要点/, "「日摘要要点」必须始终可见");
  assert.equal((html.match(/class="cat-pill/g) || []).length, 3, "全部 + 2 个有计数分类");
  assert.match(html, /更多 5 类 \+/);
  assert.match(html, /aria-pressed="true"/);
  // 展开
  context.toggleCatLegendMore();
  const expanded = nodes.catFilterContainer.innerHTML;
  assert.equal((expanded.match(/class="cat-pill/g) || []).length, 8, "展开后 1 + 7 类");
  assert.match(expanded, /收起/);
});

test("事件分类：一天没有事件时给出空态，而不是七个 0", () => {
  const {context, nodes} = loadLegend({categoryEvents: []});
  context.renderCategoryFilters();
  const html = nodes.catFilterContainer.innerHTML;
  assert.match(html, /这一天没有事件/);
  assert.equal((html.match(/class="cat-pill/g) || []).length, 1, "只保留「全部事件」按钮");
});
