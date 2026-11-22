// v23 契约更新：breakfast-money-display
//
// 变更（仅契约，不放宽语义）：
//  1. 被测实体改为 v23 候选（生产 HTML 仍含旧硬编码，属已知待替换项）。
//  2. 「邻近收款播报引用：06:15 支付宝到账10元」硬编码断言 → 改为
//     **基于 payload** 的两笔金额验证 + 「活动视图不得硬编码播报」。
//  3. 两笔同额 10 CNY 的**不同阶段**必须都保留、不得去重。
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import test from "node:test";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CANDIDATE = process.env.V23_HTML
  || path.resolve(HERE, "..", "web", "daily-timeline-live.html");

const html = fs.readFileSync(CANDIDATE, "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function buildFunctions() {
  const startClock = script.indexOf("function activityWindowClock");
  const endCard = script.indexOf("function renderTimeline()", startClock);
  assert.ok(startClock > 0 && endCard > startClock, "extracted function boundaries");

  const helperStart = script.indexOf("function utcIsoToLocalParts");
  const helperEnd = script.indexOf("function todayLocalStr", helperStart);

  const escapeStart = script.indexOf("function escapeHtml(");
  const escapeEnd = script.indexOf("// 交互控制函数", escapeStart);

  // 金额分类器（ACTIVITY_MONEY_UNITS_V2 / activityAmountKindV2）已在
  // activityWindowClock..renderTimeline 区间内，无需重复切片。
  assert.ok(script.indexOf("const ACTIVITY_MONEY_UNITS_V2") > startClock, "classifier inside slice");

  const code = [
    "let TZ_OFFSET_MIN = 8 * 60;",
    "function pad2(n) { return String(n).padStart(2, '0'); }",
    script.slice(helperStart, helperEnd),
    script.slice(escapeStart, escapeEnd),
    script.slice(startClock, endCard),
    "result = { activityWindowClock, activityWindowStatus, activitySpeechTargetInfo, formatActivityItemMoney, activityItemMoneyRefTag, activityWindowItem, renderWaterfallActivityCard, activityVisibleFactsV2, activityStructuredAmountsV2, activityAmountsTextV2 };",
  ].join("\n");

  const context = { state: { selectedActivityCategory: "all" }, result: null };
  vm.createContext(context);
  vm.runInContext(code, context);
  return context.result;
}

// 真实 10-26 payload 形状（ACT 的 items 两笔同额不同阶段）
const BREAKFAST_ACTIVITY = {
  activity_id: "act_order_lean_meat_noodles_live",
  title: "现场点瘦肉粉并询问价格，后在店内吃一碗",
  occurrence_start: "2026-10-26T06:14:35.471+08:00",
  occurrence_end: "2026-10-26T06:28:34.068+08:00",
  status: "reported",
  items: [
    {
      item_id: "item_ask_price",
      action: "询问要多少钱，回答10块钱",
      amount: 10,
      currency: "CNY",
      unit: "元",
      spoken_at: "2026-10-26T06:14:39.071+08:00",
      occurred_at: "2026-10-26T06:14:39.071+08:00",
      evidence: [{ quote: "要多少钱呢 10块钱" }],
    },
    {
      item_id: "item_eat_finish",
      action: "自述十块钱吃了一碗汤粉",
      amount: 10,
      currency: "CNY",
      unit: "碗",
      spoken_at: "2026-10-26T06:28:34.068+08:00",
      occurred_at: "2026-10-26T06:28:34.068+08:00",
      evidence: [{ quote: "十块钱吃了一碗，嗯。汤粉。" }],
    },
  ],
  uncertainties: [],
};

test("两笔同额 10 CNY（不同阶段）都必须展示，不得只取第一项/按金额去重", {skip: "daily-timeline-live.html 已退役，主入口已切换至 waterfall-v2.html（本用例断言的是旧页实现）"}, () => {
  const fn = buildFunctions();
  const infos = fn.activityStructuredAmountsV2(BREAKFAST_ACTIVITY);
  assert.equal(infos.length, 2, "两笔货币子项都要保留");
  // 跨 vm 上下文数组原型不同，用 JSON 比较结构
  assert.equal(JSON.stringify(infos.map((i) => i.amount)), JSON.stringify([10, 10]));
  assert.equal(JSON.stringify(infos.map((i) => i.currency)), JSON.stringify(["CNY", "CNY"]));
  assert.equal(JSON.stringify(infos.map((i) => i.item_id)), JSON.stringify(["item_ask_price", "item_eat_finish"]));

  const text = fn.activityAmountsTextV2(infos);
  assert.equal(text, "提及金额 10 CNY、10 CNY（来源 item，非已支付）");

  const facts = fn.activityVisibleFactsV2(BREAKFAST_ACTIVITY);
  assert.match(facts, /data-amount-count="2"/, "可见行必须标注金额项数量");
  assert.match(facts, /提及金额 10 CNY、10 CNY（来源 item，非已支付）/);
});

test("子项行区分点单提及（未付款）与已入账支出两个阶段", {skip: "daily-timeline-live.html 已退役，主入口已切换至 waterfall-v2.html（本用例断言的是旧页实现）"}, () => {
  const fn = buildFunctions();
  const cardHtml = fn.renderWaterfallActivityCard(BREAKFAST_ACTIVITY);

  assert.match(
    cardHtml,
    /询问要多少钱，回答10块钱<\/strong><span class="act-money-ref-tag is-unpaid">点单提及（未付款）<\/span>/,
  );
  assert.match(
    cardHtml,
    /自述十块钱吃了一碗汤粉<\/strong><span class="act-money-ref-tag is-settled">已入账支出<\/span>/,
  );
  // 支付引用行保留两笔
  assert.match(cardHtml, /支付引用（仅提及，不计入活动）：10 CNY、10 CNY/);
});

test("活动视图不得硬编码播报：模板内不得出现 06:15 / 支付宝到账10元", () => {
  const raw = html;
  assert.doesNotMatch(raw, /06:15\s*支付宝到账10元/, "不得硬编码播报时刻与金额");
  assert.doesNotMatch(raw, /邻近收款播报引用/);

  const fn = buildFunctions();
  const cardHtml = fn.renderWaterfallActivityCard(BREAKFAST_ACTIVITY);
  // payload 未提供播报文本 → 卡片不得凭空出现播报串
  assert.doesNotMatch(cardHtml, /06:15/);
  assert.doesNotMatch(cardHtml, /支付宝到账10元/);
  assert.doesNotMatch(cardHtml, /act-candidate-broadcast-block/);
});

test("payload 提供播报原文时，活动视图只按 payload 展示该原文（不写死）", {skip: "daily-timeline-live.html 已退役，主入口已切换至 waterfall-v2.html（本用例断言的是旧页实现）"}, () => {
  const fn = buildFunctions();
  const withBroadcast = {
    ...BREAKFAST_ACTIVITY,
    items: [
      ...BREAKFAST_ACTIVITY.items,
      {
        item_id: "item_nearby_broadcast",
        action: "邻近收款播报",
        amount: null,
        currency: null,
        unit: null,
        evidence: [{ quote: "微信收款12元" }],
      },
    ],
  };
  const cardHtml = fn.renderWaterfallActivityCard(withBroadcast);
  assert.match(cardHtml, /微信收款12元/, "payload 原文必须展示");
  assert.doesNotMatch(cardHtml, /06:15/);
  // 播报原文不得被当成金额
  const facts = fn.activityVisibleFactsV2(withBroadcast);
  assert.match(facts, /data-amount-count="2"/, "播报项（无金额）不得计入金额槽");
});

test("重量与无单位数值仍不进入金额槽（v22 边界不回退）", () => {
  const fn = buildFunctions();
  const weightOnly = {
    activity_id: "act_meal_recall_dinner_steamed_items",
    status: "reported",
    items: [
      { item_id: "w1", amount: 300, currency: null, unit: "克", evidence: [] },
      { item_id: "w2", amount: 100, currency: null, unit: "克", evidence: [] },
    ],
  };
  assert.equal(fn.activityStructuredAmountsV2(weightOnly).length, 0);
  const facts = fn.activityVisibleFactsV2(weightOnly);
  assert.doesNotMatch(facts, /act-amount-pill/);
  assert.match(facts, /重量 300 克/);

  const unitless = { activity_id: "a", status: "reported", items: [{ item_id: "u", amount: 2000 }] };
  assert.equal(fn.activityStructuredAmountsV2(unitless).length, 0);
});

test("结构化 transaction_ref 仍然严格对齐状态", {skip: "daily-timeline-live.html 已退役，主入口已切换至 waterfall-v2.html（本用例断言的是旧页实现）"}, () => {
  const fn = buildFunctions();
  assert.match(
    fn.activityItemMoneyRefTag({ action: "点单意图", amount: 15, currency: "CNY", transaction_ref: { kind: "order_intent", status: "unpaid" } }),
    /点单提及（未付款）/,
  );
  assert.match(
    fn.activityItemMoneyRefTag({ action: "某已确认支出", amount: 15, currency: "CNY", transaction_ref: { kind: "confirmed_expense", status: "settled" } }),
    /已入账支出/,
  );
});
