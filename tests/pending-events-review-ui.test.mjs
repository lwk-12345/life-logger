import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const appSource = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const blockStart = appSource.indexOf("// ==== 待人工审核（pending_sources）");
const blockEnd = appSource.indexOf("\nfunction renderJournalDay(day) {", blockStart);
assert.notEqual(blockStart, -1, "待人工审核区应该存在");
assert.notEqual(blockEnd, -1, "待人工审核区应该以 renderJournalDay 作为结束边界");
const blockSource = appSource.slice(blockStart, blockEnd);

const PAYLOAD = {
  ok: true,
  scope: {date: "2026-10-18"},
  totals: {open_actionable: 2, scoped_open_actionable: 2},
  groups: [
    {
      key: "needs_review",
      label: "需要你确认",
      collapsed: false,
      count: 2,
      items_truncated: 0,
      items: [
        {
          source_id: "pending_event:jev:evt_38",
          review_key: "pending_event:jev:evt_38",
          kind: "pending_event",
          event_id: "evt_38",
          branch: "jev",
          original_source_id: "src_38",
          quote: `<img src=x onerror="alert('quote')"> 买了鸡排 38.8`,
          reason_label: `金额 <b>待确认</b>，不是已入账`,
          group: "needs_review",
          state: "open",
          date: "2026-10-18",
          time: "04:28",
          tentative: true,
          proposed_amount: 38.8,
          proposed_currency: "CNY",
          proposed_item: "半成品黑椒鸡排",
          proposal_title: "购买四斤半成品黑椒鸡排，38.8元",
          artifact: {kind: "watch-session", label: "Apple Watch 会话 SESSION1"},
        },
        {
          source_id: "pending_event:clef:evt_bad",
          kind: "pending_event",
          event_id: "evt_bad",
          branch: "clef",
          original_source_id: "src_bad",
          quote: "金额写错了。",
          reason_label: "金额证据待确认",
          group: "needs_review",
          state: "open",
          date: "2026-10-18",
          time: "04:30",
          tentative: true,
          proposed_amount: "not-a-number",
          proposed_currency: "CNY",
          artifact: {kind: "watch-session", label: "Apple Watch 会话 SESSION1"},
        },
      ],
    },
  ],
};

function escaped(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function buildContext() {
  const context = {
    escapeHtml: escaped,
    selectedJournalDate: "2026-10-18",
    document: {getElementById: () => null, addEventListener() {}, querySelectorAll: () => []},
    window: {addEventListener() {}},
    URLSearchParams,
    navigator: {clipboard: {writeText: async () => {}}},
    payload: JSON.parse(JSON.stringify(PAYLOAD)),
    api: async () => JSON.parse(JSON.stringify(PAYLOAD)),
  };
  vm.createContext(context);
  vm.runInContext(blockSource, context);
  return context;
}

test("pending_events 卡片展示模型建议金额但明确是待确认、非已入账", () => {
  const context = buildContext();
  const html = vm.runInContext(
    "pendingReviewBodyHtml('2026-10-18', {payload, expandedOpen: false})",
    context,
  );

  assert.match(html, /模型建议/);
  assert.match(html, /待确认，非已入账消费/);
  assert.match(html, /38\.8 元/);
  assert.match(html, /半成品黑椒鸡排/);
  assert.match(html, /分支 jev/);
  assert.match(html, /来源 src_38/);
  // 原文与原因都必须经过 HTML 转义，绝不能把模型/转写文本当标签执行。
  assert.match(html, /&lt;img src=x onerror=/);
  assert.match(html, /金额 &lt;b&gt;待确认&lt;\/b&gt;/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.doesNotMatch(html, /<b>待确认<\/b>/);
});

test("非法 suggested amount 不显示 NaN，也不伪造金额", () => {
  const context = buildContext();
  const html = vm.runInContext(
    "pendingReviewBodyHtml('2026-10-18', {payload, expandedOpen: false})",
    context,
  );

  assert.doesNotMatch(html, /NaN/);
  assert.match(html, /模型建议金额未通过数值校验/);
});

test("待确认区保持不渲染大库内部计数", () => {
  const context = buildContext();
  context.payload.totals.needs_review = 3668;
  context.payload.totals.batch_failure = 3623;
  const html = vm.runInContext(
    "pendingReviewBodyHtml('2026-10-18', {payload, expandedOpen: false})",
    context,
  );
  assert.doesNotMatch(html, /3,668/);
  assert.doesNotMatch(html, /3,623/);
  assert.match(html, /本日开放需确认 2/);
});
