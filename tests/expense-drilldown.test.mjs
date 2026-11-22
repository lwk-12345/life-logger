import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const SCRIPT_PATH = path.resolve("web/expense-drilldown.js");
const scriptCode = fs.readFileSync(SCRIPT_PATH, "utf-8");

function loadModule() {
  const context = {
    window: {},
    document: {
      getElementById: () => null,
      createElement: () => ({ id: "", textContent: "" }),
      head: { appendChild: () => {} },
      addEventListener: () => {},
    },
  };
  context.globalThis = context.window;
  vm.createContext(context);
  vm.runInContext(scriptCode, context);
  return context.window.ExpenseDrilldown;
}

test("ExpenseDrilldown: clearly separates confirmed 5 yuan, pending mentions, and broadcast", () => {
  const ExpenseDrilldown = loadModule();

  // Simulating 2026-10-26 expense ledger
  const mockExpense = {
    total_amount: 5.0,
    currency: "CNY",
    totals_by_currency: { CNY: 5.0 },
    category_totals_by_currency: { CNY: { other: 5.0 } },
    transactions: [
      {
        clock: "01:56",
        title: "凌晨购买炒饼丝消费5元",
        item: "炒饼丝",
        amount: 5.0,
        currency: "CNY",
        category: "other",
        quote: "五块钱买了一份炒饼丝儿",
      },
    ],
    pending_transactions: [
      {
        clock: "06:14",
        title: "购买哇，这个瘦肉粉、这个瘦粉 (¥10.00)",
        item: "哇，这个瘦肉粉、这个瘦粉",
        proposed_amount: 10.0,
        currency: "CNY",
        reason: "order_action_missing",
        quote: "哇，这个瘦肉粉 要这个瘦粉 要多少钱呢 10块钱 在这里吃哈",
      },
      {
        clock: "06:28",
        title: "花十元吃了一碗（汤粉）",
        item: "汤粉",
        amount: null,
        currency: "CNY",
        reason: "amount_unknown",
        quote: "十块钱吃了一碗，嗯。",
      },
      {
        clock: null,
        title: "购买紫云香烟和打火机",
        item: "购买紫云香烟和打火机",
        amount: null,
        currency: "CNY",
        reason: "amount_unknown",
        quote: "拿包紫云……硬盒的……另外你们打火机",
      },
    ],
    pending_broadcast: [
      {
        clock: "06:15",
        title: "线下支付：在这里吃哈 (¥10.00)",
        item: "在这里吃哈",
        proposed_amount: 10.0,
        currency: "CNY",
        reason: "pos_broadcast_pending_confirmation",
        quote: "支付宝到账10元",
      },
    ],
    pending_broadcast_total_cny: 10.0,
  };

  const html = ExpenseDrilldown.build(mockExpense);

  // 1. Confirmed total is explicitly marked
  assert.match(html, /已确认付款消费/);
  assert.match(html, /¥5\.00 CNY/);

  // 2. Auxiliary hint prevents user from thinking ¥5 is total day spend
  assert.match(html, /另有 3 笔口头提及待确认、1 笔收款播报待确认/);

  // 3. Pending block has distinct heading and items
  assert.match(html, /待确认口头点单 \/ 报价提及/);
  assert.match(html, /哇，这个瘦肉粉/);
  assert.match(html, /口头点单\/报价提及，缺少实际付款动作凭证/);
  assert.match(html, /十块钱吃了一碗/);
  assert.match(html, /原话未提及具体金额，未核实不猜钱/);

  // 4. Quote drawers exist for verifiable transparency
  assert.match(html, /查看对话原话/);

  // 5. Broadcast block is neutrally labeled as pending payer confirmation
  assert.match(html, /收款播报，付款方待确认/);
  assert.match(html, /收款播报，付款方待确认 · 暂未计入已确认消费/);
  assert.match(html, /支付宝到账10元/);
});

test("ExpenseDrilldown: marks merchant broadcast only when merchant proof is present", () => {
  const ExpenseDrilldown = loadModule();
  const mockWithMerchant = {
    total_amount: 0,
    totals_by_currency: { CNY: 0 },
    pending_broadcast: [
      {
        proposed_amount: 25,
        currency: "CNY",
        quote: "微信收款二十五元",
        merchant: "好邻居便利店",
      },
    ],
  };
  const html = ExpenseDrilldown.build(mockWithMerchant);
  assert.match(html, /商户明确播报 · 待确认是否本人支出/);
  assert.doesNotMatch(html, /绝不计入本人支出/);
});

test("ExpenseDrilldown: does not guess payment status as unpaid and does not mix multiple currencies", () => {
  const ExpenseDrilldown = loadModule();
  const multiCurrencyPending = {
    total_amount: 5,
    totals_by_currency: { CNY: 5 },
    pending_transactions: [
      {
        item: "日元手办",
        proposed_amount: 1500,
        currency: "JPY",
        reason: "order_action_missing",
      },
      {
        item: "美元软件",
        proposed_amount: 10,
        currency: "USD",
        reason: "order_action_missing",
      },
      {
        item: "未知币种打车",
        proposed_amount: 30,
        currency: "UNKNOWN",
        reason: "order_action_missing",
      },
    ],
  };
  const html = ExpenseDrilldown.build(multiCurrencyPending);

  // 1. MUST NOT guess payment status as '(未付)'
  assert.doesNotMatch(html, /（未付）/);
  assert.match(html, /付款状态待确认/);

  // 2. MUST NOT add 1500 JPY + 10 USD + 30 UNKNOWN into a single fake sum
  assert.doesNotMatch(html, /1540/);

  // 3. MUST separate currencies in header summary
  assert.match(html, /JP¥1500\.00 JPY/);
  assert.match(html, /\$10\.00 USD/);
  assert.match(html, /30\.00 未知币种/);

  // 4. Unknown currency item must be explicitly labeled
  assert.match(html, /提及 30\.00 未知币种 · 付款状态待确认/);
});
