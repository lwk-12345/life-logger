import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";
import { makeDom } from './helpers/page-loader.mjs';

const html = fs.readFileSync(new URL("../web/daily-timeline-live.html", import.meta.url), "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function loadWorkoutContext(overrides = {}) {
  const context = {
    // [Q854] 切片里有顶层 document.addEventListener，沙箱必须提供 DOM（N66-N69 全红的原因）。
    document: makeDom(),
    state: { date: "2026-10-25", density: "normal", activityGroupOpenStates: {}, selectedEventId: null },
    CATEGORY_META: { exercise: { label: "运动", color: "#e11d48", bg: "#fff0f3" } },
    ACTIVITY_CARDS_BY_EVENT: new Map(), ACTIVITY_LINKS_EDGES: [],
    activityTypeLabel: (t) => t,
    precisionLabel: (p) => p,
    extractDataChips: () => [],
    duplicateBadgeHtml: () => "",
    escapeHtml: (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    escapeAttr: (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    ...overrides
  };
  vm.createContext(context);
  const start = script.indexOf("const ACTIVITY_STATE_LABELS");
  const end = script.indexOf("function renderTimeline()");
  assert.ok(start > 0 && end > start);
  vm.runInContext(script.slice(start, end) + `
    result = {
      renderSingleEventCard,
      activityFactsHtml,
      workoutSetsSummary,
      activityFactValue,
      setCard: (evId, card) => { ACTIVITY_CARDS_BY_EVENT.set(evId, card); },
    };
  `, context);
  return context;
}

test("主卡显示明确动作名深蹲而非泛力量训练，保留原标题追溯", () => {
  const ctx = loadWorkoutContext();
  const mockCard = {
    source_event_id: "evt_squat_1",
    title: "深蹲",
    title_original: "力量训练",
    title_source: "data.exercise.exercises[0].name",
    facts: [
      { path: "data.exercise.exercises[0].name", value: "深蹲" },
      { path: "data.exercise.exercises[0].sets[0].set", value: 1 },
      { path: "data.exercise.exercises[0].sets[0].weight_kg", value: 40, unit: "kg", support: "model_quote_only" }
    ]
  };
  ctx.result.setCard("evt_squat_1", mockCard);

  const ev = {
    event_id: "evt_squat_1",
    title: "力量训练",
    category: "exercise",
    activity_type: "exercise",
    time_str: "10:00"
  };

  const cardHtml = ctx.result.renderSingleEventCard(ev);
  assert.match(cardHtml, /<div class="ev-title">深蹲<span class="ev-title-orig">（原：力量训练）<\/span><\/div>/, "主标题必须是具体动作名深蹲，原泛标题保留追溯");
  assert.match(cardHtml, /🏋️ 深蹲 1组/, "组次摘要正确出现在卡片概要中");
});

test("证据强度 support 标注：model_only / model_quote_only 标未逐字核实，绝不当实测", () => {
  const ctx = loadWorkoutContext();

  const factModelQuote = { path: "data.exercise.exercises[0].sets[3].weight_kg", value: 90, unit: "kg", support: "model_quote_only" };
  const valHtml1 = ctx.result.activityFactValue(factModelQuote);
  assert.match(valHtml1, /90 kg/);
  assert.match(valHtml1, /未逐字核实/, "模型概述出来的数值必须标未逐字核实");

  const factVerbatim = { path: "data.exercise.exercises[0].sets[0].weight_kg", value: 40, unit: "kg", support: "quote_verbatim" };
  const valHtml2 = ctx.result.activityFactValue(factVerbatim);
  assert.match(valHtml2, /40 kg/);
  assert.match(valHtml2, /逐字已证/);

  const factUnknown = { path: "data.exercise.exercises[0].sets[1].weight_kg", value: null, unit: "kg" };
  const valHtml3 = ctx.result.activityFactValue(factUnknown);
  assert.match(valHtml3, /未记录/, "未知重量保持未记录，绝不自动补 0 或补猜测值");
});

test("回顾事件时间口径：只有讲述时间时不挂成训练时刻，标明确切回顾与讲述时间", () => {
  const ctx = loadWorkoutContext();
  const mockCard = {
    source_event_id: "evt_retro",
    title: "深蹲",
    retrospective: true,
    time_basis: "reported",
    pending_reason: "retrospective_statement",
    facts: []
  };
  ctx.result.setCard("evt_retro", mockCard);

  const ev = {
    event_id: "evt_retro",
    title: "深蹲回顾",
    category: "exercise",
    activity_type: "exercise",
    time_str: "20:42"
  };

  const cardHtml = ctx.result.renderSingleEventCard(ev);
  assert.match(cardHtml, /is-retrospective/, "必须带有回顾时间样式");
  assert.match(cardHtml, /回顾 · 讲述 20:42/, "必须明确说明为回顾且为讲述时间，绝不能把 20:42 当成实际训练时刻");
  assert.match(cardHtml, /回顾记录/, "概要行带有回顾记录 pill");
});


test("逐组原句来源追溯：有 related_quotes 显逐字引用，无对应原句明确注明未提供", () => {
  const ctx = loadWorkoutContext();
  const mockCard = {
    source_event_id: "evt_squat_quotes",
    title: "深蹲",
    title_original: "力量训练",
    kind: "fitness",
    time: { precision: "unknown" },
    evidence: {
      quote: "今天做了深蹲训练，冲到了九十。",
      related_quotes: [
        { role: "weight", exercise: "深蹲", set: 1, quote: "深蹲第一组四十公斤，做了十个" }
      ]
    },
    facts: [
      { path: "data.exercise.exercises[0].sets[0].weight_kg", value: 40, unit: "kg", support: "quote_verbatim" },
      { path: "data.exercise.exercises[0].sets[3].weight_kg", value: 90, unit: "kg", support: "model_quote_only" }
    ]
  };
  ctx.result.setCard("evt_squat_quotes", mockCard);

  const ev = { event_id: "evt_squat_quotes", title: "力量训练", category: "exercise", activity_type: "exercise" };
  const factsHtml = ctx.result.activityFactsHtml(ev);

  // 1. 第1组有明确 related_quote: 必须显示真实逐字原句
  assert.match(factsHtml, /原句：“深蹲第一组四十公斤，做了十个”/, "第一组存在真实对应引文时必须显示逐字引用");

  // 2. 第4组无逐字对应: 必须明确提示未提供，绝不编造
  assert.match(factsHtml, /本组未提供逐字原句，模型整理待核实/, "第四组无逐字对应时必须明确写出未提供原句");
});


test("静态契约：renderTimeline 空态判断必须包含 UNLOCATED_EVENTS.length === 0，防止纯未知事件早退", () => {
  assert.match(html, /if \(EVENTS_DATA\.length === 0 && UNLOCATED_EVENTS\.length === 0 && SCENE_SPAN_LIST\.length === 0 && RECORDING_SPAN_LIST\.length === 0\)/, "必须检查 UNLOCATED_EVENTS 避免纯未知时刻早退");
});

test("纯未知事件正例：unlocatedItemMarkup 消费 activity_cards 动作名与逐组 facts", () => {
  const start = script.indexOf("function unlocatedItemMarkup");
  const end = script.indexOf("function buildEvents");
  assert.ok(start > 0 && end > start);

  const context = {
    activityCardFor: () => ({
      source_event_id: "evt_unlocated_squat",
      title: "深蹲",
      title_original: "力量训练",
      facts: [
        { path: "data.exercise.exercises[0].sets[0].weight_kg", value: 90, unit: "kg", support: "model_quote_only" }
      ]
    }),
    activityTypeLabel: (t) => t,
    timeBasisLabel: () => "",
    occurrenceReasonLabel: () => "时间未定位",
    formatReportedAt: () => "2026-10-25 20:42",
    activityFactsHtml: () => '<div class="act-facts">深蹲 1组 · 本组未提供逐字原句，模型整理待核实</div>',
    escapeHtml: (s) => String(s || ""),
    escapeAttr: (s) => String(s || ""),
  };
  vm.createContext(context);
  vm.runInContext(script.slice(start, end) + "\nresult = { unlocatedItemMarkup };", context);

  const unlocatedEvent = {
    event_id: "evt_unlocated_squat",
    title: "力量训练",
    activity_type: "exercise",
    reported_at: "2026-10-25T12:42:00Z"
  };

  const itemHtml = context.result.unlocatedItemMarkup(unlocatedEvent, "");
  assert.match(itemHtml, /深蹲<span class="ev-title-orig">（原：力量训练）<\/span>/, "动作名优先，原题保留");
  assert.match(itemHtml, /讲述于 2026-10-25 20:42 · 发生时间未知/, "不把讲述时间当发生时刻，明确发生时间未知");
  assert.match(itemHtml, /本组未提供逐字原句，模型整理待核实/, "展示逐组事实与未逐字核实标注");
});


test("reported 非回顾负例：time_basis=reported 但 retrospective=false 绝不造回顾", () => {
  const ctx = loadWorkoutContext();
  const mockCard = {
    source_event_id: "evt_squat_non_retro",
    title: "深蹲",
    retrospective: false,
    time_basis: "reported",
    pending_reason: null,
    facts: []
  };
  ctx.result.setCard("evt_squat_non_retro", mockCard);

  const ev = {
    event_id: "evt_squat_non_retro",
    title: "深蹲",
    category: "exercise",
    activity_type: "exercise",
    time_str: "22:30"
  };

  const cardHtml = ctx.result.renderSingleEventCard(ev);
  // 1. 绝不能出现“回顾”字样或回顾记录 pill
  assert.doesNotMatch(cardHtml, /回顾记录/, "reported 非回顾事件绝不能打上回顾记录 pill");
  assert.doesNotMatch(cardHtml, /is-retrospective/, "不能带有 is-retrospective 类名");
  assert.doesNotMatch(cardHtml, /回顾 ·/, "时间标签不能假造回顾前缀");

  // 2. 必须明确显示为讲述时刻且发生时间未知
  assert.match(cardHtml, /讲述 22:30 · 发生时间未知/, "只表示讲述时刻，明确发生时间未知");

  // 3. 事实区也绝不出现回顾记录 pill
  const factsHtml = ctx.result.activityFactsHtml(ev);
  assert.doesNotMatch(factsHtml, /act-retrospective-pill/, "事实区不出现回顾标签");
});

test("回顾正例：retrospective=true 或 retrospective_statement 正确标记回顾", () => {
  const ctx = loadWorkoutContext();
  const mockCard = {
    source_event_id: "evt_squat_true_retro",
    title: "深蹲",
    retrospective: true,
    time_basis: "reported",
    pending_reason: "retrospective_statement",
    facts: []
  };
  ctx.result.setCard("evt_squat_true_retro", mockCard);

  const ev = {
    event_id: "evt_squat_true_retro",
    title: "深蹲",
    category: "exercise",
    activity_type: "exercise",
    time_str: "20:42"
  };

  const cardHtml = ctx.result.renderSingleEventCard(ev);
  assert.match(cardHtml, /act-retrospective-pill/, "必须带有回顾记录 pill");
  assert.match(cardHtml, /is-retrospective/, "时间标签带有 is-retrospective 类");
  assert.match(cardHtml, /回顾 · 讲述 20:42/, "回顾事件时间明确标注回顾与讲述时刻");
});
