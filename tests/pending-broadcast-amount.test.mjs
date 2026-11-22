import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const appSource = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const start = appSource.indexOf("function broadcastAmountText");
const end = appSource.indexOf("function renderBroadcastPending", start);
assert.notEqual(start, -1);
assert.notEqual(end, -1);

const context = {
  formatMoney: (value) => `¥${Number(value).toFixed(2)}`,
  formatFixed2: (value) => Number(value).toFixed(2),
  escapeHtml: (value) => String(value == null ? "" : value)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;"),
  formatCount: (value) => String(value),
  console,
};
vm.createContext(context);
vm.runInContext(appSource.slice(start, end), context);

const day = {date: "2026-10-23"};

test("accepted row displays actual ledger amount, not 金额未识别", () => {
  const html = context.broadcastRow(day, {
    event_id: "evt_accept",
    clock: "12:34",
    title: "线下支付：汉堡王",
    amount: 26.8,
    proposed_amount: null,
    currency: "CNY",
    decision: "pending_broadcast_accept",
  }, "accepted");
  assert.match(html, /¥26\.80/);
  assert.doesNotMatch(html, /金额未识别/);
});

test("pending and rejected rows display proposed_amount", () => {
  for (const state of ["pending", "rejected"]) {
    const html = context.broadcastRow(day, {
      event_id: `evt_${state}`,
      clock: "12:34",
      title: "线下支付：红塔山",
      amount: null,
      proposed_amount: 11,
      currency: "CNY",
    }, state);
    assert.match(html, /¥11\.00/, state);
    assert.doesNotMatch(html, /金额未识别/, state);
  }
});

test("missing amount stays unknown rather than becoming zero", () => {
  const html = context.broadcastRow(day, {
    event_id: "evt_missing",
    clock: "12:34",
    title: "旧数据缺金额",
    amount: null,
    proposed_amount: null,
    currency: "CNY",
  }, "accepted");
  assert.match(html, /未知/);
  assert.doesNotMatch(html, /¥0\.00/);
  assert.doesNotMatch(html, /金额未识别/);
});
