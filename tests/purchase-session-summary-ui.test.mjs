import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";
import { makeElement } from './helpers/page-loader.mjs';

const html = fs.readFileSync(new URL("../web/daily-timeline-live.html", import.meta.url), "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function loadTimelineContext(overrides = {}) {
  const context = {
    // [Q854] 页面脚本里有轮询/防抖，沙箱缺 setInterval/setTimeout → 7 条全红。
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    Date, Number, String, Math, Array, Object, JSON, Map, Set, Promise,
    URLSearchParams,
    window: { location: { search: "" }, addEventListener: () => {}, parent: {} },
    eventDisplayTime: (ev) => ev.time?.clock || '12:00',
    eventOccurrenceUnknown: () => false,
    document: {
      // [Q854] 页面会调 removeAttribute/insertAdjacentHTML，光有 textContent 不够。
      getElementById: () => makeElement(),
      addEventListener: () => {},
      querySelector: () => null,
      querySelectorAll: () => [],
      documentElement: { classList: { add: () => {} } },
      body: { classList: { add: () => {} } }
    },
    state: { date: "2026-10-25", density: "normal", activityGroupOpenStates: {}, selectedEventId: null },
    CATEGORY_META: { meal: { label: "饮食", color: "#eab308", bg: "#fef9c3" }, expense: { label: "消费", color: "#9a5b13", bg: "#fbf6ec" } },
    ACTIVITY_PRESENTATION: { groups: [] },
    activityTypeLabel: (t) => t,
    precisionLabel: (p) => p,
    extractDataChips: () => [],
    duplicateBadgeHtml: () => "",
    activityFactsHtml: () => "",
    escapeHtml: (s) => s,
    escapeAttr: (s) => s,
    ...overrides
  };
  vm.createContext(context);
  // 执行必要全局和函数
  const start = script.indexOf("function sameSpanRun");
  const end = script.indexOf("function renderTimeline()");
  assert.ok(start > 0 && end > start);
  vm.runInContext(script.slice(start, end) + `
    result = {
      applyData,
      getEventClusterKey,
      getClusterRepresentativeTitle,
      buildClusterSentenceSummary,
      renderSingleEventCard,
      getPurchaseSessionByEvent: (id) => PURCHASE_SESSION_BY_EVENT.get(id),
      getPurchaseSessions: () => PURCHASE_SESSIONS,
    };
  `, context);
  return context;
}

test("规范字段装载：daily.purchase_sessions 与 pending 正确注入索引", () => {
  const ctx = loadTimelineContext();
  const mockSession = {
    session_id: "ps_duck_sausage",
    member_ids: ["evt_d1", "evt_s1"],
    items: [
      { event_id: "evt_d1", item: "鸭腿", amount: 10.0, currency: "CNY" },
      { event_id: "evt_s1", item: "烤肠", amount: 3.0, currency: "CNY" }
    ],
    addon_links: [{ from_event_id: "evt_d1", to_event_id: "evt_s1", basis: "explicit_addon" }],
    recording_id: "rec_1"
  };
  const mockPending = [
    { source_id: "src_add", quote: "加个烤肠", recording_id: "rec_1", reason: "addon_mentioned_but_no_canonical_item" }
  ];

  ctx.result.applyData({}, {
    purchase_sessions: [mockSession],
    purchase_session_pending: mockPending,
    events: [
      { event_id: "evt_d1", title: "购买鸭腿 (¥10.00)", time: { clock: "12:00" }, data: { expense: { amount: 10 } } },
      { event_id: "evt_s1", title: "购买烤肠 (¥3.00)", time: { clock: "12:01" }, data: { expense: { amount: 3 } } }
    ]
  }, { intervals: [] });

  assert.equal(ctx.result.getPurchaseSessions().length, 1);
  assert.equal(ctx.result.getPurchaseSessionByEvent("evt_d1")?.session_id, "ps_duck_sausage");
  assert.equal(ctx.result.getPurchaseSessionByEvent("evt_s1")?.session_id, "ps_duck_sausage");
});

test("跨商品动态一句话摘要：逐笔对应、不求和、不宣称已支付", () => {
  const ctx = loadTimelineContext();
  const mockSession = {
    session_id: "ps_123",
    member_ids: ["e1", "e2"],
    items: [
      { event_id: "e1", item: "鸭腿", amount: 10.0 },
      { event_id: "e2", item: "烤肠", amount: 3.0 }
    ],
    addon_links: [{ from_event_id: "e1", to_event_id: "e2", basis: "explicit_addon" }],
    amounts_listed_individually: [10.0, 3.0],
    not_a_ledger_total: true,
    recording_id: "rec_1"
  };

  ctx.result.applyData({}, {
    purchase_sessions: [mockSession],
    events: []
  }, {});

  const summary = ctx.result.buildClusterSentenceSummary([
    { event_id: "e1", title: "购买鸭腿" },
    { event_id: "e2", title: "购买烤肠" }
  ], "ps:ps_123");

  assert.equal(summary, "鸭腿10元，随后追加烤肠3元", "精确生成规范动态摘要");
  assert.doesNotMatch(summary, /13元/, "绝对不能把两笔金额求和成 13元");
  assert.doesNotMatch(summary, /已支付|吃完|已付款/, "不宣称已支付或吃完");
});

test("缺口提示：缺 canonical 成员保留 pending 提醒，不伪装已有", () => {
  const ctx = loadTimelineContext();
  const mockSession = {
    session_id: "ps_gap",
    member_ids: ["e1"],
    items: [{ event_id: "e1", item: "鸭腿", amount: 10.0 }],
    recording_id: "rec_with_gap"
  };
  const mockPending = [
    { recording_id: "rec_with_gap", reason: "addon_mentioned_but_no_canonical_item" }
  ];

  ctx.result.applyData({}, {
    purchase_sessions: [mockSession],
    purchase_session_pending: mockPending,
    events: []
  }, {});

  const summary = ctx.result.buildClusterSentenceSummary([{ event_id: "e1", title: "鸭腿" }], "ps:ps_gap");
  assert.match(summary, /录音提到追加但未形成正式记录/, "保留未入账追加提醒");
});

test("单品原页不回归：仅 1 件商品不成组，保持独立卡片", () => {
  const ctx = loadTimelineContext();
  const singleSession = {
    session_id: "ps_single",
    member_ids: ["e_solo"],
    items: [{ event_id: "e_solo", item: "单买可乐", amount: 3.0 }]
  };
  ctx.result.applyData({}, {
    purchase_sessions: [singleSession],
    events: [{ event_id: "e_solo", title: "单买可乐" }]
  }, {});

  const key = ctx.result.getEventClusterKey({ event_id: "e_solo" });
  assert.equal(key, "ps:ps_single");
  // 当小时内只有 1 个事件时，渲染层不会生成 .activity-card-group
});

test("未关联 POS 交易不被吞并", () => {
  const ctx = loadTimelineContext();
  const session = {
    session_id: "ps_duck",
    member_ids: ["e_duck"],
    items: [{ event_id: "e_duck", item: "鸭腿", amount: 10.0 }]
  };
  ctx.result.applyData({}, {
    purchase_sessions: [session],
    events: []
  }, {});

  const unlinkedPos = { event_id: "pos_broadcast_99", activity_type: "pos_payment_broadcast", title: "支付宝到账15元" };
  const posKey = ctx.result.getEventClusterKey(unlinkedPos);
  assert.equal(posKey, null, "未关联的 POS 记录不会获得 session 聚合键，独立保留展示");
});

test("共享 member 融合：purchase session 与 presentation group 合并为唯一主摘要，主句动态完整", () => {
  const ctx = loadTimelineContext();
  const session = {
    session_id: "ps_fused",
    member_ids: ["e_duck", "e_sausage"],
    items: [
      { event_id: "e_duck", item: "鸭腿", amount: 10.0 },
      { event_id: "e_sausage", item: "烤肠", amount: 3.0 }
    ],
    addon_links: [{ from_event_id: "e_duck", to_event_id: "e_sausage", basis: "explicit_addon" }],
    recording_id: "rec_1"
  };
  const presentationGroup = {
    activity_ref: "inferred:meal:鸭腿",
    group_id: "group:inferred:meal:2026-10-25:00:鸭腿",
    member_event_ids: ["e_duck", "e_flavor1", "e_flavor2"],
    summary: { title: "购买鸭腿 (¥10.00)" }
  };

  const allEvents = [
    { event_id: "e_duck", title: "购买鸭腿 (¥10.00)", data: { expense: { amount: 10 } } },
    { event_id: "e_flavor1", title: "鸭腿口味要求辣" },
    { event_id: "e_flavor2", title: "鸭腿口味调整为微辣" },
    { event_id: "e_sausage", title: "购买烤肠 (¥3.00)", data: { expense: { amount: 3 } } },
  ];

  ctx.result.applyData({}, {
    purchase_sessions: [session],
    activity_presentation: { groups: [presentationGroup] },
    events: allEvents
  }, {});

  // 1. 验证所有 4 个事件的 cluster key 统一归入 purchase session
  assert.equal(ctx.result.getEventClusterKey({ event_id: "e_duck" }), "ps:ps_fused");
  assert.equal(ctx.result.getEventClusterKey({ event_id: "e_sausage" }), "ps:ps_fused");
  assert.equal(ctx.result.getEventClusterKey({ event_id: "e_flavor1" }), "ps:ps_fused", "属性卡融合至主会话");
  assert.equal(ctx.result.getEventClusterKey({ event_id: "e_flavor2" }), "ps:ps_fused", "属性卡融合至主会话");

  // 2. 验证动态主句
  const sentence = ctx.result.buildClusterSentenceSummary(allEvents, "ps:ps_fused");
  assert.equal(sentence, "鸭腿10元，口味调整为微辣，随后追加烤肠3元");
});

test("无共享 member 保持独立，绝不跨实体强并", () => {
  const ctx = loadTimelineContext();
  const session = {
    session_id: "ps_duck",
    member_ids: ["e_duck"],
    items: [{ event_id: "e_duck", item: "鸭腿", amount: 10.0 }]
  };
  const coffeeGroup = {
    activity_ref: "inferred:meal:咖啡",
    group_id: "group:inferred:meal:2026-10-25:00:咖啡",
    member_event_ids: ["e_coffee1", "e_coffee2"],
    summary: { title: "买咖啡" }
  };

  ctx.result.applyData({}, {
    purchase_sessions: [session],
    activity_presentation: { groups: [coffeeGroup] },
    events: []
  }, {});

  assert.equal(ctx.result.getEventClusterKey({ event_id: "e_duck" }), "ps:ps_duck");
  assert.equal(ctx.result.getEventClusterKey({ event_id: "e_coffee1" }), "pres:inferred:meal:咖啡");
  assert.equal(ctx.result.getEventClusterKey({ event_id: "e_coffee2" }), "pres:inferred:meal:咖啡");
});
