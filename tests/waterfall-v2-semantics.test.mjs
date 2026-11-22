// 新版瀑布图必须保住旧页的金额和时间语义：币种不猜、重量不当钱、同额不去重、时间未知不编。
import {test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const html = fs.readFileSync(process.env.WATERFALL_V2_SOURCE || new URL("../web/waterfall-v2.html", import.meta.url), "utf8");
const start = html.indexOf("function moneyText(it)");
const end = html.indexOf("const HIDE_STATUS");
const {moneyText, moneyOf} = new Function(`${html.slice(start, end)}; return {moneyText, moneyOf};`)();

test("币种照实显示，未知币种不默认人民币", () => {
  assert.equal(moneyText({amount: 16, currency: "CNY", unit: "元"}), "¥16");
  assert.equal(moneyText({amount: 20, currency: "USD"}), "$20");
  assert.equal(moneyText({amount: 5, currency: "JPY"}), "JPY 5");
  assert.equal(moneyText({amount: 4, unit: "块"}), "4块");
  assert.equal(moneyText({amount: 4}), "4");
});

test("单价和周期保留单位", () => {
  assert.equal(moneyText({amount: 25, currency: "CNY", unit: "元/公斤"}), "¥25/公斤");
  assert.equal(moneyText({amount: 30, currency: "USD", unit: "美元/月"}), "$30/月");
  assert.equal(moneyText({amount: 10, currency: "USD", unit: "月"}), "$10/月");
  assert.equal(moneyText({amount: 10, currency: "CNY", unit: "碗"}), "¥10");
});

test("重量和数量不进金额，同额两笔都保留", () => {
  const items = [
    {amount: 300, unit: "克", amount_kind: "weight"},
    {amount: 500, unit: "条", amount_kind: "quantity"},
    {amount: 10, currency: "CNY", amount_kind: "money"},
    {amount: 10, currency: "CNY", amount_kind: "money"},
  ];
  assert.deepEqual(moneyOf({items}), ["¥10", "¥10"]);
});

test("提及金额标明未确认付款，时间未知不编造", () => {
  assert.match(html, /提及 \$\{esc\(moneyText\(it\)\)\}（未确认付款）/);
  assert.match(html, /x\.minute != null \? fmt\(x\.minute\) : "时间未定"/);
});

// ─────────────────────────────────────────────────────────────────────────────
// [2026-11-05 旧页退役准备] 继续把旧页守护的**用户可见语义**补到新版。
// 只新增、不删除：上面 4 组断言原样保留。
//
// 做法：从 web/waterfall-v2.html 里按函数名切出纯函数（含嵌套大括号配平），
// 在 node 里跑；不启动浏览器、不改页面。
// ─────────────────────────────────────────────────────────────────────────────

/** 按 `function NAME(` 或 `const NAME =` 切出一段源码（大括号配平）。 */
function slice(html, name) {
  let i = html.indexOf(`function ${name}(`);
  let arrow = false;
  if (i < 0) { i = html.indexOf(`const ${name} =`); arrow = true; }
  if (i < 0) throw new Error(`waterfall-v2.html 里找不到 ${name}`);
  if (arrow) {
    const semi = html.indexOf(";\n", i);
    return html.slice(i, semi + 1);
  }
  let depth = 0, started = false;
  for (let j = i; j < html.length; j++) {
    if (html[j] === "{") { depth++; started = true; }
    else if (html[j] === "}") { depth--; if (started && depth === 0) return html.slice(i, j + 1); }
  }
  throw new Error(`切不出 ${name} 的函数体`);
}

/** 装载一组纯函数；`state` 供 rowHtml/detailHtml 使用。 */
function load(names) {
  const prelude = `
    const TZ_MS = 8 * 3600000;
    const WEEK = "日一二三四五六";
    const pad = n => String(n).padStart(2, "0");
    const fmt = m => \`\${pad(Math.floor(m / 60))}:\${pad(Math.floor(m % 60))}\`;
    const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[c]));
    const CAT_COLORS = [[/餐饮|消费|支付/,"#a06b26"],[/运动|健身/,"#c4513f"],[/编程|工作|项目/,"#002fa7"],[/生活/,"#5e6673"]];
    const state = {quotes: false, open: new Set(), expanded: new Set(), cat: "", filter: "live"};
  `;
  const body = [...new Set(names)].map(n => slice(html, n)).join("\n");
  const tail = `; return {${names.join(",")}};`;
  return new Function(`${prelude}\n${body}\n${tail}`)();
}

// 这些是上面那些函数互相依赖的最小闭包，统一带上，避免每个用例各挑一遍。
const CORE = ["WORK_LABEL", "CODING_APPS", "CHAT_APPS", "FRAGMENT_TITLE", "isChatTarget", "isFragmentNoise", "catOf", "foldReason", "APP_BUNDLES", "appFromBundle", "appFromContext", "resolveApp", "appBadge", "HIDE_STATUS", "ROW_STATUS", "SPEECH_HIDE", "isUserCorrected",
              "cleanTitle", "speechBadge", "moneyText", "moneyOf", "itemHtml", "catColor"];

const PRELUDE = `
  const TZ_MS = 8 * 3600000;
  const WEEK = "日一二三四五六";
  const pad = n => String(n).padStart(2, "0");
  const fmt = m => \`\${pad(Math.floor(m / 60))}:\${pad(Math.floor(m % 60))}\`;
  const fmtDur = m => m >= 60 ? \`\${Math.floor(m / 60)} 小时\${m % 60 ? \` \${Math.round(m % 60)} 分\` : ""}\` : \`\${Math.round(m)} 分钟\`;
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[c]));
  const CAT_COLORS = [[/餐饮|消费|支付/,"#a06b26"],[/运动|健身/,"#c4513f"],[/编程|工作|项目/,"#002fa7"],[/生活/,"#5e6673"]];
  const state = {quotes: false, open: new Set(), expanded: new Set(), cat: "", filter: "live"};
`;

/** 把若干函数切出来，连同 prelude 一起求值。 */
function evalWith(names, expr) {
  const body = [...new Set([...CORE, ...names])].map(n => slice(html, n)).join("\n");
  return new Function(`${PRELUDE}\n${body}\nreturn (${expr});`)();
}

// [2026-11-06] clocksHtml 现在还会渲染三大营养素 → 闭包里补上它的依赖。
// 只补依赖，不改断言。
const S = load([...CORE, "localParts", "placeActivity", "shape", "spendText", "spendClockHtml", "clocksHtml",
                "overviewHtml", "MEAL_LABEL", "macroNum", "mealMacros", "foodCardHtml", "macrosHtml"],
               );

test("时间口径：有发生时间用它；只有讲述时间就标记讲述；都没有就是「时间未定」", () => {
  const D = "2026-11-02";
  assert.deepEqual(S.placeActivity({occurrence_start: "2026-11-02T03:30:00Z"}, D),
                   {minute: 11 * 60 + 30, basis: "occurred"});
  const spokenOnly = S.placeActivity({spoken_at: "2026-11-02T04:00:00Z"}, D);
  assert.equal(spokenOnly.basis, "spoken", "只有讲述时间必须标 spoken，不能冒充发生时间");
  assert.equal(spokenOnly.minute, 12 * 60);
  assert.deepEqual(S.placeActivity({reported_at: "2026-11-02T04:00:00Z"}, D),
                   {minute: 12 * 60, basis: "spoken"});
  assert.deepEqual(S.placeActivity({}, D), {minute: null, basis: "none"}, "无时间就是未定，不编造");
  // 发生时间在别的日期 → 不算这一天，回落到讲述时间
  const crossDay = S.placeActivity(
    {occurrence_start: "2026-11-01T03:30:00Z", spoken_at: "2026-11-02T04:00:00Z"}, D);
  assert.equal(crossDay.basis, "spoken", "跨日的发生时间不能算到当天");
});

test("媒体与生活分桶：lifecycle 原样映射，缺失或非法一律「待确认」", () => {
  const acts = [
    {activity_id: "a", lifecycle: "live", occurrence_start: "2026-11-02T01:00:00Z"},
    {activity_id: "b", lifecycle: "media", occurrence_start: "2026-11-02T02:00:00Z"},
    {activity_id: "c", lifecycle: "unknown", occurrence_start: "2026-11-02T03:00:00Z"},
    {activity_id: "d", occurrence_start: "2026-11-02T04:00:00Z"},
    {activity_id: "e", lifecycle: "LIVE", occurrence_start: "2026-11-02T05:00:00Z"},
  ];
  const out = S.shape("2026-11-02", {activities: acts}, {}, {});
  assert.deepEqual(out.acts.map(x => [x.a.activity_id, x.life]),
    [["a", "live"], ["b", "media"], ["c", "unknown"], ["d", "unknown"], ["e", "unknown"]],
    "缺失/大小写不合法的 lifecycle 不能猜成 live");
  // 「我的生活」计数只算 live，媒体与待确认都不计入
  // [2026-11-07 改版] 条目数降到概览底部一行小字：“整理出 N 条 · 本人 M 条”
  const clocks = S.clocksHtml(out, null);
  assert.match(clocks, /整理出 5 条/);
  assert.match(clocks, /本人 <b class="tnum" data-live-count>1<\/b> 条/);
});

test("状态未知不硬显成「已完成」，无信息量的状态不上行", () => {
  for (const v of ["", "未知", "unknown", "状态未知", "待确认"])
    assert.ok(S.HIDE_STATUS.has(v), `「${v}」必须被 HIDE_STATUS 收掉`);
  assert.ok(!S.HIDE_STATUS.has("已完成"), "已完成要保留");
  assert.deepEqual([...S.ROW_STATUS].sort(), ["已咨询", "已要求", "已完成", "已点单", "已请求", "计划"].sort());
});

test("标题不暴露内部降级前缀，原题仍可追溯", () => {
  assert.equal(S.cleanTitle({title: "录音中疑似：给猫洗澡"}), "给猫洗澡");
  assert.equal(S.cleanTitle({title: "未确认：讨论退货"}), "讨论退货");
  assert.equal(S.cleanTitle({title: "录音中提及：报销四百"}), "报销四百");
  assert.equal(S.cleanTitle({title_original: "原始标题", title: "录音中疑似：X"}), "原始标题",
    "有 title_original 时优先原题");
  assert.equal(S.cleanTitle({}), "活动记录", "没有标题也不能渲染 undefined");
});

test("说话对象未知就不标；用户纠正过要明示", () => {
  assert.equal(S.speechBadge({speech_target: "未知"}), "");
  assert.equal(S.speechBadge({speech_target: "未知对象"}), "");
  assert.match(S.speechBadge({speech_target: "Codex"}), /对Codex/);
  assert.match(S.speechBadge({speech_target: "Codex", user_corrected: true}), /已纠正/);
});

test("子项分桶：没标 item_lifecycle 的按本人处理，media/unknown 单独收起", () => {
  const a = {activity_id: "x", items: [
    {action: "买土豆", amount: 8, currency: "CNY", amount_kind: "money"},
    {action: "电视里的旁白", item_lifecycle: "media"},
    {action: "不知道谁说的", item_lifecycle: "unknown"},
  ]};
  const detail = evalWith([],
    "x => (x.a.items||[]).filter(it => (it.item_lifecycle||'live')==='live').map(itemHtml).join('')");
  const pos = detail({a});
  assert.match(pos, /买土豆/, "本人子项要显示");
  assert.doesNotMatch(pos, /电视里的旁白/, "媒体子项不能混进主列表");
  assert.doesNotMatch(pos, /不知道谁说的/, "待确认子项不能混进主列表");
});

test("子项金额：钱才带 ¥ 且注明未确认付款；重量/数量照原值带单位", () => {
  const money = S.itemHtml({action: "点单", amount: 10, currency: "CNY", amount_kind: "money"});
  assert.match(money, /¥10/);
  assert.match(money, /未确认付款/, "提及金额必须说明不是已支付");
  const weight = S.itemHtml({action: "称重", amount: 400, unit: "克", amount_kind: "weight"});
  assert.doesNotMatch(weight, /¥/, "重量不能当钱");
  assert.match(weight, /400克/);
  const qty = S.itemHtml({action: "验收", amount: 500, unit: "条", amount_kind: "quantity"});
  assert.doesNotMatch(qty, /¥/, "数量不能当钱");
});

test("全天概览的时间基准写明 UTC+8，讲述时间与发生时间在视觉上可区分", () => {
  const D = S.shape("2026-11-02",
    {activities: [{activity_id: "a", lifecycle: "live", spoken_at: "2026-11-02T04:00:00Z"}]},
    {timeline_scenes: []}, {});
  const legend = evalWith(["ribbonHtml"], "D => ribbonHtml(D, null)")(D);
  assert.match(legend, /UTC\+8/, "全站统一 UTC+8，概览要写出来");
  assert.match(legend, /只知讲述时间/, "讲述时间要有图例");
  assert.match(evalWith(["rowHtml"], "D => rowHtml(D.acts[0])")(D),
    /这是讲述时间，事情何时发生未说明/, "讲述时间的行要有 title 说明");
});

test("生活/媒体/待确认分开数：默认只看生活，另给出「N 条待确认 · M 条媒体内容」", () => {
  const mk = (id, life) => ({a: {activity_id: id, title: id}, life, minute: 600, basis: "occurred"});
  const list = [mk("l1", "live"), mk("l2", "live"), mk("u1", "unknown"), mk("m1", "media"), mk("m2", "media")];
  const rowsBlock = evalWith(["rowHtml", "detailHtml", "CARD_PREVIEW", "groupCards", "cardRange", "cardHtml", "rowsBlock"], "rowsBlock");
  const htmlLive = rowsBlock("day", list);
  assert.equal((htmlLive.match(/class="row"/g) || []).length, 2, "默认只渲染生活条目");
  assert.match(htmlLive, /1 条待确认/, "待确认要单独计数，不能折进媒体");
  assert.match(htmlLive, /2 条媒体内容/, "媒体单独计数");
  // 超过 10 条要给「再显示 N 条」，不能静默截断
  const many = Array.from({length: 13}, (_, i) => mk(`x${i}`, "live"));
  assert.match(rowsBlock("day2", many), /再显示 \d+ 件/);
});

// v2 parity: real samples were inspected before adding these regressions.
const PARITY = ["moneyText", "dailyField", "eventDomId", "workoutTitle", "workoutFactsHtml", "purchaseSummary",
  "eventRelationsHtml", "eventHtml", "eventRowsHtml", "captureRows", "capturesHtml", "eventClock", "localParts"];
function parity() {
  return new Function(`${PRELUDE}\n${PARITY.map(n => slice(html, n)).join("\n")}\nreturn {${PARITY.join(",")}};`)();
}

test("健身事实使用结构化动作名，原泛标题保留追溯，不借用其他事件的动作", () => {
  const p = parity();
  const card = {kind: "fitness", title: "力量训练", facts: [{path: "data.exercise.exercises[0].name", value: "深蹲"}]};
  assert.equal(p.workoutTitle({title: "力量训练"}, card), "深蹲");
  assert.match(p.workoutFactsHtml(card), /深蹲/);
  assert.equal(p.workoutTitle({title: "力量训练第三组"}, {kind: "fitness", facts: [{path: "data.exercise.exercises[0].name", value: "未说明"}]}), "力量训练第三组");
  const rows = p.eventRowsHtml([{event_id: "squat", title: "力量训练"}], {activity_cards: [{...card, source_event_id: "squat"}]});
  assert.match(rows, /原：力量训练/);
});

test("模型重量逐值标未逐字核实，逐字证据独立标注，缺失不补零", () => {
  const p = parity();
  const h = p.workoutFactsHtml({kind: "fitness", facts: [
    {path: "data.exercise.exercises[0].sets[0].weight_kg", value: 80, unit: "kg", support: "model_only"},
    {path: "data.exercise.exercises[0].sets[1].weight_kg", value: 90, unit: "kg", support: "model_quote_only"},
    {path: "data.exercise.exercises[0].sets[2].weight_kg", value: 40, unit: "kg", support: "quote_verbatim"},
    {path: "data.exercise.exercises[0].sets[3].weight_kg", value: null, unit: "kg"},
  ]});
  assert.equal((h.match(/未逐字核实/g) || []).length, 2);
  assert.match(h, /80 kg/); assert.match(h, /90 kg/); assert.match(h, /逐字已证/);
  assert.match(h, /未记录/); assert.doesNotMatch(h, /实测|>0 kg/);
});

test("局部数组下标不是实际第几组：缺组号不编造，明确组号才显示", () => {
  const p = parity();
  const facts = [{path: "data.exercise.exercises[0].sets[0].weight_kg", value: 80, unit: "kg", support: "model_only"}];
  assert.doesNotMatch(p.workoutFactsHtml({kind: "fitness", facts}), /第 1 组/);
  assert.match(p.workoutFactsHtml({kind: "fitness", facts: [...facts, {path: "data.exercise.exercises[0].sets[0].set", value: 3}]}), /第 3 组/);
});

test("健身回顾与推断时间分开说明，讲述时刻换算 UTC+8", () => {
  const p = parity();
  const retro = p.workoutFactsHtml({kind: "fitness", retrospective: true, time_basis: "reported",
    time: {reported_at: "2026-10-25T12:42:26Z"}, facts: []});
  assert.match(retro, /回顾/); assert.match(retro, /讲述时间 20:42/); assert.match(retro, /发生时间未说明/);
  assert.match(p.workoutFactsHtml({kind: "fitness", time: {precision: "inferred"}, facts: []}), /推断时间.*未核实/);
});

test("同次购物聚合一行、商品逐笔对应且同额不去重，不计算已付合计", () => {
  const p = parity();
  const events = [{event_id: "p1", title: "商品甲"}, {event_id: "p2", title: "商品乙"}];
  const session = {session_id: "basket", member_ids: ["p1", "p2"], state: "unknown", items: [
    {event_id: "p1", item: "商品甲", amount: 10, currency: "CNY"},
    {event_id: "p2", item: "商品乙", amount: 10, currency: "CNY"}]};
  const h = p.eventRowsHtml(events, {purchase_sessions: [session]});
  assert.equal((h.match(/class="purchase-session"/g) || []).length, 1);
  assert.equal((p.purchaseSummary(session, session.items).match(/¥10/g) || []).length, 2);
  assert.match(h, /付款未确认/); assert.doesNotMatch(h, /¥20|已支付|合计/);
  assert.match(h, /id="event-p1"/); assert.match(h, /id="event-p2"/);
  const visible = p.eventRowsHtml([events[0]], {purchase_sessions: [session]});
  assert.doesNotMatch(visible, /id="event-p2"|商品乙/);
});

test("活动声明未核实，关联成员可定位；缺失或非活动命名空间不给假跳转", () => {
  const p = parity(), events = [{event_id: "e1", title: "甲"}, {event_id: "e2", title: "乙"}];
  const daily = {activity_presentation: {groups: [{activity_ref: "a", basis: {label: "显式声明 · 未核实"}, member_event_ids: ["e1", "e2", "missing"]}]},
    activity_links: {edges: [{from_id: "e1", to_id: "recording", namespace: "recording", relation: "references"}]}};
  const h = p.eventRelationsHtml(events[0], daily, events);
  assert.match(h, /未核实/); assert.match(h, /data-locate-event="e2"/);
  assert.match(h, /目标不在本日/); assert.doesNotMatch(h, /data-locate-event="missing"|data-locate-event="recording"/);
  assert.match(html, /function locateEvent\(/);
});

test("抓取按 UTC+8 归日、去掉明确事件引用；同录音和文字相同不能单独去重", () => {
  const p = parity();
  const caps = [
    {id: "a", captured_at: "2026-11-01T16:12:00Z", extracted_content: "线索甲", metadata: {recording_id: "same"}},
    {id: "b", captured_at: "2026-11-01T16:12:01Z", extracted_content: "线索甲", metadata: {recording_id: "same"}},
    {id: "c", captured_at: "2026-11-01T16:12:02Z", extracted_content: "已由事件承载", source_event_id: "e"},
    {id: "other", captured_at: "2026-11-01T12:00:00Z", extracted_content: "其他日期"}];
  const events = [{event_id: "e", time: {absolute: "2026-11-01T16:12:02Z"}}];
  const rows = p.captureRows("2026-11-02", {captured_todos: caps}, events);
  assert.deepEqual(rows.map(r => r.id), ["a", "b"]); assert.equal(rows[0].clock, "00:12");
  const h = p.capturesHtml({date: "2026-11-02", captureSource: {captured_todos: caps}}, events);
  assert.match(h, /即时抓取/); assert.match(h, /未核实/); assert.match(h, /线索甲/);
  assert.doesNotMatch(h, /其他日期|已由事件承载/);
});

test("未提供捕捉时刻就显示未定；来源定位不等于事实已证", () => {
  const p = parity();
  const D = {date: "2026-11-02", captureSource: {captured_ideas: [{id: "unknown", extracted_content: "线索乙",
    capture_provenance: {status: "resolved", recording_id: "recording", segment_id: "segment"}}]}};
  const h = p.capturesHtml(D, []);
  assert.match(h, /时间未定/); assert.match(h, /来源已定位/); assert.match(h, /未核实/);
  assert.doesNotMatch(h, /事实已证|00:00/);
});

const spendStart = html.indexOf("function spendText(ledger)");
const spendEnd = html.indexOf("function clocksHtml(D, nowMin)");
const {spendClockHtml} = new Function("esc", `${html.slice(spendStart, spendEnd)}; return {spendClockHtml};`)(value => String(value));

test("页面顶部显示当天消费总额，来自账本", () => {
  const out = spendClockHtml({expense_ledger: {total_amount: 678.6, totals_by_currency: {CNY: 678.6}, payment_transactions: [1, 2, 3]}}, "ready");
  assert.match(out, /¥678\.60/);
  assert.match(out, /3 笔/);
  assert.match(out, /data-jump-expense/);
});

test("消费读不到时不说成 0 元", () => {
  assert.doesNotMatch(spendClockHtml(null, "failed"), /¥/);
  assert.match(spendClockHtml(null, "failed"), /不代表没有消费/);
  assert.doesNotMatch(spendClockHtml(null, "loading"), /¥/);
});

test("没有消费和外币都照实显示", () => {
  assert.match(spendClockHtml({expense_ledger: {total_amount: 0, totals_by_currency: {}}}, "ready"), /¥0\.00.*还没有记到消费/);
  // [2026-11-07 唯一事实来源·P1] **规格变更**：合计只认后端 total_amount（CNY 基准），
  // 外币只作附注、不参与合计（原来这里断言的是"逐币种相加"的旧口径，已按新规格改写）。
  const mixed = spendClockHtml({expense_ledger: {total_amount: 10, totals_by_currency: {CNY: 10, USD: 20}}}, "ready");
  assert.match(mixed, /¥10\.00/, "合计必须等于 total_amount，不能把外币加进来");
  assert.match(mixed, /USD 20\.00 不计入合计/, "外币要明确标注不计入合计");
  // 后端没给 total_amount（老接口）时显示「—」，不能把缺失写成 0
  assert.match(spendClockHtml({expense_ledger: {totals_by_currency: {CNY: 10}}}, "ready"), /—/);
});

test("待确认金额单独说明，不混进总额", () => {
  const out = spendClockHtml({expense_ledger: {total_amount: 10, totals_by_currency: {CNY: 10}, pending_broadcast_total_cny: 4}}, "ready");
  assert.match(out, /¥10\.00/);
  assert.match(out, /另有 ¥4\.00 待确认/);
});

const healthStart = html.indexOf("function healthStripHtml(health)");
const healthEnd = html.indexOf("function spendText(ledger)") > healthStart ? html.indexOf("function spendText(ledger)") : html.indexOf("function clocksHtml(D, nowMin)");
const {healthStripHtml} = new Function("fmtDur", `${html.slice(healthStart, healthEnd)}; return {healthStripHtml};`)(m => `${m}分`);

test("健康条有数据显示数值，没有数据如实说，不写成 0", () => {
  const out = healthStripHtml({summary: {avg_heart_rate: 84, min_heart_rate: 71, max_heart_rate: 130, total_steps: 1904, sleep_minutes: 0}});
  assert.match(out, /84/);
  assert.match(out, /71–130/);
  assert.match(out, /1904/);
  assert.match(out, /睡眠 还没有数据/);
  assert.equal(healthStripHtml(null), "");
});

const cardSrc = html.slice(html.indexOf("const CARD_PREVIEW = 3;"), html.indexOf("function rowsBlock(key, list)"));
const cardApi = new Function("fmt", "esc", "catColor", "state", "rowHtml", "resolveApp", "cleanTitle",
  `${cardSrc}; return {groupCards, cardHtml, sceneLabel, catOf, foldReason};`)(
  m => `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`, v => String(v), () => "#000",
  {expanded: new Set(), open: new Set()}, x => `<li>${x.a.activity_id}</li>`,
  a => a.input_context ? {name: a.input_context.app_label} : null, a => a.title || "");
const liveRow = (id, category, minute) => ({life: "live", minute, a: {activity_id: id, category}});

test("同一场景按类别成卡，每张按首条时间排序", () => {
  const cards = cardApi.groupCards([liveRow("b", "生活", 610), liveRow("a", "运动", 600), liveRow("c", "运动", 630)]);
  assert.deepEqual(cards.map(c => c.label), ["运动", "生活"]);
  assert.deepEqual(cards[0].rows.map(r => r.a.activity_id), ["a", "c"]);
});

test("卡片默认露出 3 件，其余可展开，没有内容被丢掉", () => {
  const rows = ["a", "b", "c", "d", "e"].map((id, i) => liveRow(id, "运动", 600 + i));
  const out = cardApi.cardHtml("k", cardApi.groupCards(rows)[0]);
  assert.match(out, /5 件/);
  assert.match(out, /10:00–10:04/);
  assert.match(out, /再显示 2 件/);
  assert.doesNotMatch(out, /<li>d<\/li>/);
});

test("媒体和待确认不混进本人类别", () => {
  const cards = cardApi.groupCards([liveRow("a", "生活", 600), {life: "media", minute: 601, a: {activity_id: "m", category: "生活"}}]);
  assert.deepEqual(cards.map(c => c.label).sort(), ["媒体内容", "生活"].sort());
});

test("有本人活动时不再标无对话", () => {
  const rows = [liveRow("a", "运动", 1), liveRow("b", "运动", 2), liveRow("c", "运动", 3)];
  assert.equal(cardApi.sceneLabel("安静少言（无对话）", rows), "日常活动");
  assert.equal(cardApi.sceneLabel("安静少言（无对话）", rows.slice(0, 1)), "安静少言（无对话）");
  assert.equal(cardApi.sceneLabel("影音娱乐", rows), "影音娱乐");
});

test("媒体折成一行，点开才显示，判错也找得回来", () => {
  const mk = (id, life) => ({a: {activity_id: id, title: id}, life, minute: 600, basis: "occurred"});
  const rowsBlock = evalWith(["rowHtml", "detailHtml", "CARD_PREVIEW", "groupCards", "cardRange", "cardHtml", "rowsBlock"], "rowsBlock");
  const out = rowsBlock("z", [mk("l1", "live"), mk("m1", "media"), mk("m2", "media"), mk("u1", "unknown")]);
  assert.equal((out.match(/class="row"/g) || []).length, 1);
  assert.match(out, /看视频\/媒体 · 2 条媒体内容 · 1 条待确认/);
  assert.match(out, /data-expand="z-noise"/);
});

const hourSrc = html.slice(html.indexOf("function hourSegments(ranges, hour)"), html.indexOf("function hourLabel(block)"));
const {hourBlocks} = new Function(`${hourSrc}; return {hourBlocks};`)();

test("按小时对齐：没内容也没录音的连续小时折成一行", () => {
  const D = {
    acts: [{minute: 125, life: "live"}, {minute: 600, life: "live"}],
    scenes: [{start_minute: 120, end_minute: 180, color: "#000", label: "聊天"}],
    rec: [[120, 150]],
  };
  const blocks = hourBlocks(D, null);
  assert.deepEqual(blocks.map(b => b.kind), ["gap", "hour", "gap", "hour"]);
  assert.deepEqual([blocks[0].from, blocks[0].to], [0, 2]);
  assert.equal(blocks[1].hour, 2);
  assert.equal(blocks[1].recSegs[0].height.toFixed(0), "50");
  assert.equal(blocks[1].sceneSegs[0].height.toFixed(0), "100");
});

test("跨小时的场景和录音会切到各自的小时", () => {
  const D = {acts: [], scenes: [{start_minute: 90, end_minute: 150, color: "#000", label: "聊天"}], rec: [[90, 150]]};
  const blocks = hourBlocks(D, null).filter(b => b.kind === "hour");
  assert.deepEqual(blocks.map(b => b.hour), [1, 2]);
  assert.equal(blocks[0].recSegs[0].top.toFixed(0), "50");
  assert.equal(blocks[1].recSegs[0].height.toFixed(0), "50");
});

test("正在录音的当天只排到现在这一小时", () => {
  const D = {acts: [], scenes: [], rec: [[60, 70]]};
  const blocks = hourBlocks(D, 130);
  assert.equal(Math.max(...blocks.map(b => b.kind === "hour" ? b.hour : b.to - 1)), 2);
});

const appSrc = html.slice(html.indexOf("const APP_BUNDLES = {"), html.indexOf("function speechBadge(a)"));
const appApi = new Function("esc", `${appSrc}; return {resolveApp, appBadge};`)(v => String(v));

test("应用标记：有就显示，没有不留空壳，且不说成发给了它", () => {
  assert.equal(appApi.appBadge({items: []}), "");
  assert.match(appApi.appBadge({input_context: {app_label: "Alma", kind: "dictation"}}), /语音输入到 Alma/);
  assert.match(appApi.appBadge({input_context: {target_bundle: "com.tencent.xinWeChat"}}), /微信/);
  const viaEvidence = {items: [{evidence: [{input_context: {app_label: "Zen"}}]}]};
  assert.equal(appApi.resolveApp(viaEvidence).name, "Zen");
  assert.doesNotMatch(appApi.appBadge({input_context: {app_label: "Alma"}}), /发送给|发给 Alma/);
  assert.match(appApi.appBadge({input_context: {app_label: "Alma"}}), /环境线索/);
});

test("对编程工具说的话归编程·工作并折叠，微信归沟通", () => {
  const row = a => ({life: "live", minute: 1, a: {activity_id: "x", category: "生活·日常", items: [], ...a}});
  for (const app of ["Alma", "Codex", "Terminal", "VS Code", "Claude"]) {
    assert.equal(cardApi.catOf(row({input_context: {app_label: app}}).a), "编程·工作", app);
    assert.equal(cardApi.foldReason(row({input_context: {app_label: app}})), "编程·工作");
  }
  assert.equal(cardApi.catOf(row({input_context: {app_label: "微信"}}).a), "沟通");
  assert.equal(cardApi.foldReason(row({input_context: {app_label: "微信"}})), "");
  assert.equal(cardApi.catOf(row({category_model: "工作·项目"}).a), "编程·工作");
});

test("零碎短句折叠，但说到钱的绝不折叠", () => {
  const row = a => ({life: "live", minute: 1, a: {activity_id: "x", items: [], ...a}});
  assert.equal(cardApi.foldReason(row({title: "现场短句：提到下次再统计"})), "零碎片段");
  assert.equal(cardApi.foldReason(row({title: "买菜"})), "");
  assert.equal(cardApi.foldReason(row({title: "零散片段", items: [{fact_type: "expense"}]})), "");
  assert.equal(cardApi.foldReason(row({input_context: {app_label: "Alma"}, items: [{amount: 16, amount_kind: "money"}]})), "");
});

test("汇总卡和模型分类：钱不折、媒体折叠、成员合并", () => {
  const src = html;
  assert.match(src, /function applyDigest\(D, digest\)/);
  assert.match(src, /looks_like_media === true && x\.life !== "media" && !hasMoney\(x\.a\)/);
  assert.match(src, /按说话时间，不代表实际时长/);
  assert.match(src, /\/api\/activity-digest\?date=/);
});

// ─────────────────────────────────────────────────────────────────────────────
// [2026-11-06] 瀑布图顶部：当天三大营养素 + 总热量（数据只来自 nutrition_ledger）
//
// 三条规则是用户明确要求的，所以每条一个用例：
//   1) 有值：碳水/蛋白/脂肪（克）+ 合计 kcal
//   2) 没记录：「—」，**不能显示 0**（接口 has_records=false 时会把 totals 填 0）
//   3) 有食物缺营养数据：数字后面标「（N 项缺数据）」
// ─────────────────────────────────────────────────────────────────────────────
const M = load([...CORE, "MEAL_LABEL", "macroNum", "mealMacros", "foodCardHtml", "macrosHtml"]);

const LEDGER_1026 = {
  has_records: true,
  record_count: 19,
  consumed_record_count: 11,
  unknown_nutrition_count: 2,
  totals: {calories_kcal: 1629, carbohydrates_g: 195.2, protein_g: 58.9, fat_g: 67.9, sodium_mg: 1942.1},
  totals_unknown_counts: {calories_kcal: 2, carbohydrates_g: 2, protein_g: 2, fat_g: 2},
  meals: {
    lunch: [
      {item: "可乐", calories_kcal: 142, nutrients: {carbohydrates_g: 35, protein_g: 0, fat_g: 0}},
      {item: "汉堡", calories_kcal: 480, nutrients: {carbohydrates_g: 44, protein_g: 24, fat_g: 22}},
    ],
    dinner: [
      {item: "蒸土豆", calories_kcal: 231, nutrients: {carbohydrates_g: 51.6, protein_g: 6, fat_g: 0.6}},
      {item: "含五十克青椒的一餐", calories_kcal: null, nutrients: {carbohydrates_g: null, protein_g: null, fat_g: null}},
    ],
    unknown: [],
  },
};

// [2026-11-07 改版] 饮食块：大字只放总热量（或“热量待定”），三大营养素一行小字，每餐一行。
// 三条用户规则仍然各有用例：有值照实显示；没记录显示「—」绝不显示 0；缺数据要说清楚。
test("有记录：大字总热量，三大营养素按克一行，没算的食物点名并给“去处理”", () => {
  const html = M.macrosHtml({nutrition_ledger: LEDGER_1026}, "ok");
  assert.match(html, /<dt>饮食<\/dt><dd class="tnum">1629<span class="unit"> 千卡<\/span><\/dd>/);
  assert.match(html, /碳水 195\.2 克 · 蛋白 58\.9 克 · 脂肪 67\.9 克/);
  assert.match(html, /另有 1 样没算：含五十克青椒的一餐 没说份量/);
  assert.match(html, /data-open-review="calories"/);
  assert.doesNotMatch(html, /0 千卡/, "有记录时不该出现 0 千卡");
});

test("没有记录：显示「—」，绝不显示 0（接口 has_records=false 时 totals 全是 0）", () => {
  const ledger = {
    has_records: false, record_count: 0, consumed_record_count: 0, unknown_nutrition_count: 0,
    totals: {calories_kcal: 0, carbohydrates_g: 0, protein_g: 0, fat_g: 0, sodium_mg: 0},
    totals_unknown_counts: {calories_kcal: 0, carbohydrates_g: 0, protein_g: 0, fat_g: 0},
    meals: {breakfast: [], lunch: [], dinner: [], snacks: []},
  };
  const html = M.macrosHtml({nutrition_ledger: ledger}, "ok");
  assert.match(html, /<dt>饮食<\/dt><dd class="tnum">—<\/dd>/, "没记录要显示破折号");
  assert.doesNotMatch(html, /0 克/, "不能把「没记录」写成 0 克");
  assert.doesNotMatch(html, /0 千卡/, "不能把「没记录」写成 0 千卡");
  assert.match(html, /还没有饮食记录/);
  assert.match(M.macrosHtml({}, "failed"), /没读取成功，不代表没有饮食/);
});

test("部分缺数据：缺的营养素写「未知」不写 0；整餐都缺时这一餐标“待定”", () => {
  const ledger = {
    has_records: true, record_count: 3, consumed_record_count: 2, unknown_nutrition_count: 3,
    totals: {calories_kcal: 520, carbohydrates_g: null, protein_g: 12.5, fat_g: null, sodium_mg: null},
    totals_unknown_counts: {calories_kcal: 3, carbohydrates_g: 3, protein_g: 1, fat_g: 3},
    meals: {dinner: [{item: "未知餐", calories_kcal: null, nutrients: {carbohydrates_g: null, protein_g: null, fat_g: null}}]},
  };
  const html = M.macrosHtml({nutrition_ledger: ledger}, "ok");
  assert.match(html, /520<span class="unit"> 千卡/);
  assert.match(html, /碳水 未知 · 蛋白 12\.5 克 · 脂肪 未知/);
  assert.match(html, /晚餐<\/span> 未知餐<\/span><b class="f-pending">待定<\/b>/);
});

test("全部热量待定：大字写“热量待定”，不显示一排破折号", () => {
  const ledger = {
    has_records: true, record_count: 1, consumed_record_count: 1, unknown_nutrition_count: 1,
    totals: {calories_kcal: null, carbohydrates_g: null, protein_g: null, fat_g: null, sodium_mg: null},
    meals: {dinner: [{item: "炒河粉", calories_kcal: null, nutrients: {}}]},
  };
  const html = M.macrosHtml({nutrition_ledger: ledger}, "ok");
  assert.match(html, /<dd class="tnum">热量待定<\/dd>/);
  assert.match(html, /炒河粉 没说份量/);
  assert.doesNotMatch(html, /碳水 —|合计 —/);
});

test("按餐一行：午餐/晚餐分开显示已知热量（与后端 meal_totals 或逐项回退同口径）", () => {
  const html = M.macrosHtml({nutrition_ledger: LEDGER_1026}, "ok");
  assert.match(html, /午餐<\/span> 可乐、汉堡<\/span><b class="tnum">622 千卡<\/b>/);
  assert.match(html, /晚餐<\/span> 蒸土豆、含五十克青椒的一餐<\/span><b class="tnum">231 千卡<\/b>/);
});

test("390 宽不横向溢出：三块在手机上竖着排，网格列可收缩", () => {
  assert.match(html, /\.clocks\{display:grid;grid-template-columns:repeat\(3,minmax\(0,1fr\)\)/);
  assert.match(html, /@media \(max-width:640px\)\{[\s\S]{0,400}\.clocks\{grid-template-columns:minmax\(0,1fr\)\}/);
});

// [2026-11-07] /api/daily 的 totals 改成"没有已知值就是 null"之后新增的形态：
// has_records=true（确实吃了东西）但所有营养都未知 → 每一项都是 null。
// 这一格必须显示「—」，不能显示 0，也不能抛错。
test("有记录但营养全未知时，显示“热量待定”而不是 0", () => {
  const ledger = {
    has_records: true, record_count: 2, consumed_record_count: 2, unknown_nutrition_count: 2,
    totals: {calories_kcal: null, carbohydrates_g: null, protein_g: null, fat_g: null, sodium_mg: null},
    totals_unknown_counts: {calories_kcal: 2, carbohydrates_g: 2, protein_g: 2, fat_g: 2},
    meals: {lunch: [{item: "未知食品", calories_kcal: null,
                     nutrients: {carbohydrates_g: null, protein_g: null, fat_g: null}}]},
  };
  const html = M.macrosHtml({nutrition_ledger: ledger}, "ok");
  assert.match(html, /<dd class="tnum">热量待定<\/dd>/);
  assert.doesNotMatch(html, /0 克/, "全未知时不得显示 0 克");
  assert.doesNotMatch(html, /0 千卡/, "全未知时不得显示 0 千卡");
  // 该餐也要照实标“待定”，而不是 0
  assert.match(html, /午餐<\/span> 未知食品<\/span><b class="f-pending">待定<\/b>/);
});

// ─────────────────────────────────────────────────────────────────────────────
// [2026-11-08] 展示层 A4–A7 + 任务 C（消费项名称清洗）回归守卫
// ─────────────────────────────────────────────────────────────────────────────
test("任务 C：消费块优先用结构化 item，且洗掉口语赘述与尾部金额", () => {
  const out = spendClockHtml({
    expense_ledger: {
      total_amount: 36.5,
      payment_transaction_count: 4,
      payment_transactions: [
        {time: "2026-11-05T14:10:00+08:00", item: "冰杯", title: "买冰杯花费2元", amount: 2},
        {time: "2026-11-05T15:20:00+08:00", item: "去买菜，买菜花了十六", title: "去买菜，买菜花了十六", amount: 16},
        {time: "2026-11-05T16:30:00+08:00", item: "称买了一瓶椰子水，四块五", title: "称买了一瓶椰子水，四块五", amount: 4.5},
        {time: "2026-11-02T18:00:00+08:00", item: "陈述一桶水并花了四块", title: "陈述一桶水并花了四块", amount: 4},
      ],
    },
  }, "ready");
  assert.match(out, /冰杯/);
  assert.doesNotMatch(out, /买冰杯花费2元/);
  assert.match(out, /买菜/);
  assert.doesNotMatch(out, /花了十六/);
  assert.match(out, /椰子水/);
  assert.doesNotMatch(out, /称买了一瓶|四块五/);
  assert.match(out, /一桶水/);
  assert.doesNotMatch(out, /陈述一桶水|并花了四块/);
});

test("展示层 A4–A7：同小时折叠条合一、主卡只标语音输入、双竖条有图例、不单独占行写没有整理出活动", () => {
  // A6：主行只保留「语音输入到 X」，单纯 front_app 放进详情
  assert.match(S.speechBadge({input_context: {app_label: "Zen", kind: "dictation"}}), /语音输入到 Zen/);
  assert.equal(S.speechBadge({input_context: {app_label: "Zen", kind: "front_app"}}), "");
  const detailFn = evalWith(["detailHtml"], "detailHtml");
  assert.match(detailFn({basis: "occurred", a: {input_context: {app_label: "Zen", kind: "front_app"}, items: []}}), /当时前台应用：Zen/);

  // A4：同一小时既有工作/零碎折叠又有媒体/待确认时，收拢成一条虚线框，展开不丢内容
  const rowsBlock = evalWith(["rowHtml", "detailHtml", "CARD_PREVIEW", "groupCards", "cardRange", "cardHtml", "rowsBlock"], "rowsBlock");
  const list = [
    {a: {activity_id: "w1", title: "改脚本", category: "工作·项目", items: []}, life: "live", minute: 130, basis: "occurred"},
    {a: {activity_id: "m1", title: "看直播", category: "看视频/媒体", items: []}, life: "media", minute: 140, basis: "occurred"},
  ];
  const h = rowsBlock("h2", list);
  assert.equal((h.match(/class="fold noise"/g) || []).length, 1, "同一小时最多一条虚线折叠框");
  assert.match(h, /编程·工作 · 1 条 · 看视频\/媒体 · 1 条媒体内容/);

  // A5 & A7：图例写明左竖条/右竖条含义；空小时不再单独渲染 <p class="r-note">这段没有整理出活动</p>
  const ribbon = evalWith(["ribbonHtml"], "ribbonHtml")({scenes: [], rec: [], breakdown: {}}, null);
  assert.match(ribbon, /左竖条/);
  assert.match(ribbon, /右竖条/);
  assert.doesNotMatch(html, /<p class="r-note">这段没有整理出活动<\/p>/);
});

test("识别层 A1 & A3：工作·项目不被旧 category_model=闲聊·其他 降级；小时主标签优先显示本人内容", () => {
  assert.equal(
    cardApi.catOf({activity_id: "act_openclaw_L011", category: "工作·项目", category_model: "闲聊·其他", items: []}),
    "编程·工作",
  );
  const {hourLabel, a123OwnHourLabel} = evalWith(
    ["PLACE_TEXT", "sceneLabel", "hourLabel", "a123OwnHourLabel"],
    "({hourLabel, a123OwnHourLabel})",
  );
  const block03 = {
    hour: 3,
    sceneSegs: [{height: 98, label: "聊天", place: "Home"}],
    list: [
      {
        life: "live",
        a: {
          category: "工作·项目",
          items: [{fact_type: "other", evidence: [{source_label: "self", source_id: "w1", quote: "请帮我检查六个终端窗口的任务验收并向我汇报一下一晚上的工作"}]}],
        },
      },
      {
        life: "live",
        a: {
          category: "消费·购物",
          items: [{fact_type: "expense", evidence: [{source_label: "self", source_id: "s1", quote: "十五的吧我记得是，啊，对，再点一份炒河粉"}]}],
        },
      },
    ],
  };
  assert.equal(a123OwnHourLabel(block03), "本人内容：工作·项目 / 助手沟通；采购 / 点餐");
  assert.equal(hourLabel(block03), "本人内容：工作·项目 / 助手沟通；采购 / 点餐");

  // 没有本人 self 证据时回退到原有场景标签
  const mediaOnly = {
    hour: 4,
    sceneSegs: [{height: 80, label: "影音娱乐", place: "Home"}],
    list: [{life: "media", a: {category: "媒体内容", items: [{fact_type: "other", evidence: [{source_label: "non_self", quote: "剧集对白"}]}]}}],
  };
  assert.equal(a123OwnHourLabel(mediaOnly), "");
  assert.equal(hourLabel(mediaOnly), "影音娱乐 · 在家");
});

test("瀑布流降噪：AI工作指令提消费不误留主卡、剧情对白归媒体、有内容的小时不再重复叠灰字h-scene", () => {
  // 06:26 语音输入到 Antigravity 核对消费记录：无实际金额子项，必须按编程·工作折叠
  assert.equal(
    cardApi.foldReason({
      life: "live",
      minute: 386,
      a: {
        activity_id: "act_0626",
        title: "回顾并核对十一月六/七号凌晨两三点至四点的消费记录与统计",
        category: "工作·项目",
        input_context: {app_label: "Antigravity", kind: "dictation"},
        items: [{action: "要求查看那笔消费是什么、金额多少、有没有统计好", fact_type: "none"}],
      },
    }),
    "编程·工作",
  );
  // 05:09 播放/混入大量来源不明的剧情化对白：shape 归入 media
  const shaped = S.shape("2026-11-07", {
    activities: [{
      activity_id: "act_media_garbled_dialogue_01",
      lifecycle: "live",
      spoken_at: "2026-11-07T05:09:48+08:00",
      title: "播放/混入大量来源不明的剧情化对白（剑势、水警、内府、押金、干粮等）",
      items: [],
    }],
  }, {}, {});
  assert.equal(shaped.acts[0].life, "media");

  // 有折叠条或主卡的小时不再叠顶部的 .h-scene 灰字
  const {hourHtml} = evalWith(
    ["rowHtml", "detailHtml", "CARD_PREVIEW", "groupCards", "cardRange", "cardHtml", "rowsBlock", "PLACE_TEXT", "sceneLabel", "hourLabel", "a123OwnHourLabel", "hourHtml"],
    "({hourHtml})",
  );
  const quietHour = hourHtml({
    hour: 7,
    recSegs: [],
    sceneSegs: [{top: 0, height: 100, label: "聊天", color: "#000"}],
    list: [{life: "media", minute: 425, a: {activity_id: "m1", title: "看视频", items: []}}],
  }, false);
  assert.doesNotMatch(quietHour, /class="h-scene"/);
  assert.match(quietHour, /class="hour quiet"/);
});

test("我的生活与对电脑说的话完全分离：默认我的生活只显示真实生活事项，点编程·工作或媒体内容才显示对应条目", () => {
  const {state, filterCounts, timelineHtml} = evalWith(
    ["FILTERS", "rowHtml", "detailHtml", "CARD_PREVIEW", "groupCards", "cardRange", "cardHtml", "rowsBlock",
     "PLACE_TEXT", "sceneLabel", "hourLabel", "a123OwnHourLabel", "hourSegments", "hourBlocks", "hourHtml",
     "sceneHtml", "cursorHtml", "timelineHtml", "filterCounts"],
    "({state, filterCounts, timelineHtml})",
  );
  state.cleanLive = true;
  const D = {
    date: "2026-11-08",
    scenes: [],
    rec: [[120, 180], [600, 660]],
    acts: [
      {
        life: "live",
        minute: 155,
        basis: "spoken",
        a: {
          activity_id: "act_web_check_L078",
          title: "查看网页端加载情况",
          category: "工作·项目",
          fragment_money_protected: true,
          input_context: {app_label: "Terminal", kind: "front_app"},
          items: [{action: "肯定截图右侧消费区域设计", fact_type: "other"}],
        },
      },
      {
        life: "live",
        minute: 221,
        basis: "spoken",
        a: {
          activity_id: "act_remark_L022",
          title: "称今天崩了（指代未明）",
          category: "闲聊·其他",
          category_model: "健康·作息",
          input_context: {app_label: "Zen", kind: "dictation"},
          items: [{action: "感叹今天崩了", fact_type: "other"}],
        },
      },
      {
        life: "live",
        minute: 637,
        basis: "spoken",
        a: {
          activity_id: "act_cola_purchase_1108_L038",
          title: "想买冰杯嫌远放弃，就近买一瓶可乐并支付",
          category: "消费·购物",
          input_context: {app_label: "Terminal", kind: "front_app"},
          items: [{action: "买了一瓶可乐", fact_type: "expense", amount: 1.8, currency: "CNY", amount_kind: "money"}],
        },
      },
      {
        life: "media",
        minute: 645,
        basis: "occurred",
        a: {activity_id: "act_media_1", title: "背景玄幻剧情", category: "媒体内容", items: []},
      },
    ],
  };
  const counts = filterCounts(D);
  assert.deepEqual(counts, {live: 1, work: 2, media: 1, unknown: 0, all: 4});

  state.filter = "live";
  const liveView = timelineHtml(D, null).html;
  assert.match(liveView, /想买冰杯嫌远放弃，就近买一瓶可乐并支付/);
  assert.doesNotMatch(liveView, /查看网页端加载情况|称今天崩了|编程·工作|看视频\/媒体|02:00|03:00/);

  state.filter = "work";
  const workView = timelineHtml(D, null).html;
  assert.match(workView, /查看网页端加载情况/);
  assert.match(workView, /称今天崩了（指代未明）/);
  assert.doesNotMatch(workView, /想买冰杯嫌远放弃/);
});

test("waterfall-v2 keeps WeChat speech in 我的生活 even with dictation, work category, or fragment flag", () => {
  const {state, timelineHtml, filterCounts, catOf, foldReason} = evalWith(
    ["FILTERS", "rowHtml", "detailHtml", "CARD_PREVIEW", "groupCards", "cardRange", "cardHtml", "rowsBlock",
     "PLACE_TEXT", "sceneLabel", "hourLabel", "a123OwnHourLabel", "hourSegments", "hourBlocks", "hourHtml",
     "sceneHtml", "cursorHtml", "timelineHtml", "filterCounts"],
    "({state, timelineHtml, filterCounts, catOf, foldReason})",
  );
  state.cleanLive = true;
  const wechatAct = {
    activity_id: "act_wechat_1",
    title: "给朋友发微信讨论网页端代码",
    category: "工作·项目",
    category_model: "工作·项目",
    looks_like_fragment: true,
    input_context: {app_label: "微信", target_bundle: "com.tencent.xinWeChat", kind: "dictation"},
    items: [{action: "发微信回复马上回去", fact_type: "other"}],
  };
  assert.equal(catOf(wechatAct), "沟通");
  assert.equal(foldReason({a: wechatAct}), "");
  const D = {
    date: "2026-11-08",
    scenes: [],
    rec: [[540, 600]],
    acts: [{life: "live", minute: 572, basis: "spoken", a: wechatAct}],
  };
  assert.deepEqual(filterCounts(D), {live: 1, work: 0, media: 0, unknown: 0, all: 1});
  state.filter = "live";
  const liveView = timelineHtml(D, null).html;
  assert.match(liveView, /给朋友发微信讨论网页端代码/);
  assert.match(liveView, /语音输入到 微信/);
});

test("饮食卡片以静日消耗 2000 kcal 为基准展示纯减脂(1700)/净增肌(2150)/塑形维持(2000)三大营养素进度条", () => {
  const gapHtml = M.macrosHtml({nutrition_ledger: LEDGER_1026}, "ok");
  assert.match(gapHtml, /目标：纯减脂（70kg · 静日消耗 2000 kcal）/);
  assert.match(gapHtml, /蛋白目标 58\.9 \/ 105 克（差 46\.1 克 · 70kg×1\.5）/);
  assert.match(gapHtml, /195\.2 \/ 126g（超 \+69\.2g）/);
  assert.match(gapHtml, /67\.9 \/ 42g（超 \+25\.9g）/);
  assert.match(gapHtml, /热量上限 1700 kcal（差 71 kcal）/);
  assert.match(gapHtml, /纯减脂（1700 kcal · 蛋白1\.5\/碳1\.8\/脂0\.6）/);
  assert.match(gapHtml, /净增肌（2150 kcal · 蛋白1\.7\/碳2\.8\/脂0\.65）/);
  assert.match(gapHtml, /塑形维持（2000 kcal · 蛋白1\.6\/碳2\.5\/脂0\.65）/);
  assert.match(gapHtml, /data-goal-field="baseKcal"/);

  const metLedger = {
    ...LEDGER_1026,
    unknown_nutrition_count: 0,
    totals: {calories_kcal: 1465, carbohydrates_g: 130, protein_g: 112.5, fat_g: 22.9},
  };
  const metHtml = M.macrosHtml({nutrition_ledger: metLedger}, "ok");
  assert.match(metHtml, /蛋白达标 112\.5 \/ 105 克 ✓（70kg×1\.5）/);
  assert.match(metHtml, /热量上限 1700 kcal（差 235 kcal）/);
  assert.match(metHtml, /适量 130 \/ 126g ✓/);
  assert.match(metHtml, /未超 22\.9 \/ 42g ✓/);
});

test("catOf：lifecycle===media 的外放视频/直播不会因前台是编程软件误入编程·工作，微信优先规则保持不变", () => {
  const {catOf, foldReason} = evalWith([], "({catOf, foldReason})");
  const mediaWithCodingFg = {
    activity_id: "act_media_L002",
    lifecycle: "media",
    title: "背景剧情谈交易与医疗舱",
    category: "浏览·视频",
    category_model: "媒体内容",
    input_context: {app_label: "Antigravity", kind: "dictation"},
    foreground_context: {status: "resolved", foreground_app: "Terminal"},
    items: [{action: "播放剧情对白", fact_type: "none"}],
  };
  assert.equal(catOf(mediaWithCodingFg), "媒体内容");
  assert.equal(foldReason({a: mediaWithCodingFg, life: "media"}), "");

  const mediaWithoutDigest = {
    activity_id: "act_20261109_media_stool_ad_0216",
    lifecycle: "media",
    title: "背景播放小凳子带货广告",
    category: "浏览·视频",
    input_context: {app_label: "Antigravity", kind: "front_app"},
    items: [],
  };
  assert.equal(catOf(mediaWithoutDigest), "浏览·视频");

  const wechatStillWins = {
    activity_id: "act_wechat_priority",
    lifecycle: "media",
    title: "对微信发消息",
    category: "工作·项目",
    speech_target: "微信",
    input_context: {app_label: "微信", target_bundle: "com.tencent.xinWeChat", kind: "dictation"},
    items: [],
  };
  assert.equal(catOf(wechatStillWins), "沟通");
});

test("消费卡片改金额：每笔带 entry_key 可点改、已改显示灰字与原金额悬停及撤销、没对上的修改显式提示不静默丢弃", () => {
  const out = spendClockHtml({
    expense_ledger: {
      total_amount: 255.1,
      payment_transaction_count: 2,
      payment_transactions: [
        {
          entry_key: "act_20261109_expense_report_01#it3",
          event_id: "act_20261109_expense_report_01#it3",
          time: "2026-11-09T00:25:30+08:00",
          clock: "00:25",
          item: "面条",
          amount: 15.2,
          edited: true,
          original_amount: 5.2,
        },
        {
          entry_key: "act_20261109_expense_report_01#it1",
          event_id: "act_20261109_expense_report_01#it1",
          time: "2026-11-09T00:25:30+08:00",
          clock: "00:25",
          item: "外卖",
          amount: 239.9,
        },
      ],
      unmatched_edits: [
        {
          entry_key: "act_old_deleted#it9",
          amount: 18.5,
          original_amount: 8.5,
          label: "夜宵烧烤",
          time: "01:10",
          quote: "烧烤十八块五",
        },
      ],
    },
  }, "ready");
  assert.match(out, /¥255\.10/);
  assert.match(out, /data-expense-edit="act_20261109_expense_report_01#it3"/);
  assert.match(out, /<span class="s-edited-tag" title="原 5\.2 元">已改<\/span>/);
  assert.match(out, /data-expense-undo="act_20261109_expense_report_01#it3"/);
  assert.match(out, /有 1 笔修改没对上，点开看/);
  assert.match(out, /夜宵烧烤/);
  assert.match(out, /原 8\.5 元 → ¥18\.50/);
  assert.match(out, /data-expense-undo="act_old_deleted#it9"/);
});

test("饮食卡片改名字：待核卡片与底部列表都可改名字、已改显示灰字与原名悬停及撤销、没对上的改名显式提示不静默丢弃", () => {
  const evRenamed = "activity:2026-11-12:act_lunch_20261112_132206:item_lunch_132206_02";
  const evRice = "activity:2026-11-12:act_lunch_20261112_132206:item_lunch_132206_01";
  const out = M.macrosHtml({
    nutrition_ledger: {
      has_records: true,
      record_count: 2,
      consumed_record_count: 2,
      unknown_nutrition_count: 2,
      totals: {calories_kcal: null, carbohydrates_g: null, protein_g: null, fat_g: null, sodium_mg: null},
      meals: {
        lunch: [
          {
            event_id: evRice,
            item: "米饭",
            calories_kcal: null,
            nutrients: {},
            portion_estimate_candidate: {grams: 150, calories_kcal: 174},
          },
          {
            event_id: evRenamed,
            rename_key: "activity:2026-11-12:act_old:item_02",
            item: "土豆牛肉料理包",
            renamed: true,
            original_name: "热力宝",
            raw_original_name: "热力宝（转写名称待核）",
            weight_g: 200,
            calories_kcal: null,
            nutrients: {},
            quote: "中午吃了米饭，吃了热力宝。",
          },
        ],
      },
      unweighed_items: [
        {
          event_id: evRenamed,
          rename_key: "activity:2026-11-12:act_old:item_02",
          item: "土豆牛肉料理包",
          renamed: true,
          original_name: "热力宝",
          raw_original_name: "热力宝（转写名称待核）",
          quote: "中午吃了米饭，吃了热力宝。",
        },
      ],
      unmatched_renames: [
        {
          event_id: "activity:2026-11-12:act_deleted:item_99",
          name: "红烧牛肉面",
          original_name: "红烧牛面",
          raw_original_name: "红烧牛面（转写名称待核）",
          time: "2026-11-12T19:15:00+08:00",
          clock: "19:15",
          quote: "晚上吃了红烧牛面",
        },
      ],
    },
  }, "ok");
  assert.match(out, /土豆牛肉料理包/);
  assert.doesNotMatch(out, /转写名称待核/);
  assert.match(out, new RegExp(`data-food-rename="${evRenamed}" data-food-rename-scope="panel"`));
  assert.match(out, new RegExp(`data-food-rename="${evRenamed}" data-food-rename-scope="list"`));
  assert.match(out, /<span class="s-edited-tag" title="原：热力宝">已改<\/span>/);
  assert.match(out, /data-food-rename-undo="activity:2026-11-12:act_old:item_02"/);
  assert.match(out, /value="200" data-food-grams="activity:2026-11-12:act_lunch_20261112_132206:item_lunch_132206_02"/);
  assert.match(out, /有 1 处改名没对上，点开看/);
  assert.match(out, /原：红烧牛面 → 红烧牛肉面/);
  assert.match(out, /data-food-rename-undo="activity:2026-11-12:act_deleted:item_99"/);

  // 即使当天活动重跑后饮食记录全部消失（has_records=false），没对上的改名也不静默丢弃
  const emptyWithUnmatched = M.macrosHtml({
    nutrition_ledger: {
      has_records: false,
      record_count: 0,
      consumed_record_count: 0,
      totals: {calories_kcal: 0, carbohydrates_g: 0, protein_g: 0, fat_g: 0},
      meals: {},
      unmatched_renames: [
        {
          event_id: "activity:2026-11-12:act_deleted:item_99",
          name: "红烧牛肉面",
          original_name: "红烧牛面",
          clock: "19:15",
        },
      ],
    },
  }, "ok");
  assert.match(emptyWithUnmatched, /有 1 处改名没对上，点开看/);
  assert.match(emptyWithUnmatched, /原：红烧牛面 → 红烧牛肉面/);
});







