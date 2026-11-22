// 唯一事实来源·前端第二阶段：直接吃后端的汇总字段，不在前端重算。
//
// 哨兵法：故意让 items / 逐条事实 与后端的 meal_totals / fitness_ledger.totals / counts 给出**不同**的数字，
// 断言前端显示的是后端的数（若前端偷偷重算，就会显示另一个数）。
// 同时验证旧夹具（不带这些新字段）仍能跑。
import {test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "web/waterfall-v2.html"), "utf8");

function slice(src, name) {
  const start = src.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `找不到 ${name}()`);
  let depth = 0, started = false;
  for (let i = start; i < src.length; i++) {
    if (src[i] === "{") { depth++; started = true; }
    else if (src[i] === "}") { depth--; if (started && depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error(`切不出 ${name}`);
}

const PRELUDE = `
  const esc = (s) => String(s == null ? "" : s);
  const fmtDur = (m) => (m >= 60 ? Math.floor(m / 60) + " 小时" + (m % 60 ? " " + Math.round(m % 60) + " 分" : "") : Math.round(m) + " 分钟");
  const MEAL_LABEL = {breakfast: "早餐", lunch: "午餐", dinner: "晚餐", snacks: "加餐", unknown: "未归餐"};
  const localParts = () => null;   // workoutFactsHtml 里的"讲述时间"用；测试不关心
`;
function load(names) {
  const body = names.map((n) => slice(html, n)).join("\n");
  return new Function(`${PRELUDE}\n${body}\nreturn {${names.join(",")}};`)();
}

const M = load(["mealMacros", "macrosHtml", "macroNum", "fitnessSummaryText", "workoutBackendSummary",
                "activityCounts", "workoutFactsHtml", "spendText", "spendCurrencyNote", "spendClockHtml"]);

// ── 1. meal_totals 直通 ────────────────────────────────────────────
test("有 meal_totals 时直接读后端的按餐合计，不遍历 items 求和", () => {
  // 哨兵：items 加起来是 100/10/5/2，后端 meals_totals 说是 774/51.6/6/0.6
  const items = [
    {item: "A", calories_kcal: 60, nutrients: {carbohydrates_g: 6, protein_g: 3, fat_g: 1}},
    {item: "B", calories_kcal: 40, nutrients: {carbohydrates_g: 4, protein_g: 2, fat_g: 1}},
  ];
  const mealTotal = {
    calories_kcal: 774, carbohydrates_g: 51.6, protein_g: 6, fat_g: 0.6, sodium_mg: 3,
    known_counts: {calories_kcal: 1, carbohydrates_g: 1, protein_g: 1, fat_g: 1},
    unknown_counts: {calories_kcal: 1, carbohydrates_g: 1, protein_g: 1, fat_g: 1},
  };
  const m = M.mealMacros(items, mealTotal);
  assert.equal(m.source, "backend");
  assert.equal(m.kcal, 774, "必须用后端热量，不是 items 的 100");
  assert.equal(m.c, 51.6);
  assert.equal(m.p, 6);
  assert.equal(m.f, 0.6);
  assert.equal(m.missing, 1);
});

test("macrosHtml 的“按餐看”用后端 meal_totals", () => {
  const ledger = {
    has_records: true, consumed_record_count: 2, unknown_nutrition_count: 1,
    totals: {calories_kcal: 774, carbohydrates_g: null, protein_g: null, fat_g: null},
    totals_unknown_counts: {calories_kcal: 0, carbohydrates_g: 1, protein_g: 1, fat_g: 1},
    meals: {breakfast: [
      {item: "水饺", calories_kcal: 60, nutrients: {carbohydrates_g: 6, protein_g: 3, fat_g: 1}},
    ]},
    meal_totals: {breakfast: {
      calories_kcal: 774, carbohydrates_g: 51.6, protein_g: 6, fat_g: 0.6,
      known_counts: {calories_kcal: 1, carbohydrates_g: 1, protein_g: 1, fat_g: 1},
      unknown_counts: {calories_kcal: 1, carbohydrates_g: 1, protein_g: 1, fat_g: 1},
    }},
  };
  const out = M.macrosHtml({nutrition_ledger: ledger}, "ok");
  assert.match(out, /早餐<\/span><span class="tnum">774 kcal · 碳 51\.6 g · 蛋 6 g · 脂 0\.6 g/);
  assert.doesNotMatch(out, /60 kcal/, "不得显示 items 自己加出来的 60");
});

// ── 2. 健身 totals ─────────────────────────────────────────────────
test("fitnessSummaryText 只读后端 fitness_ledger.totals", () => {
  // 哨兵：workouts 数组里 3 条，每条 set_count=1（前端数应是 3），后端 totals 说 9 组 / 90 次
  const ledger = {
    workouts: [{set_count: 1, rep_total: 10}, {set_count: 1, rep_total: 10}, {set_count: 1, rep_total: 10}],
    totals: {workout_count: 3, set_count: 9, rep_total: 90, duration_seconds: 1800},
  };
  const text = M.fitnessSummaryText(ledger);
  assert.match(text, /3 次训练/);
  assert.match(text, /9 组/, "组数必须用后端 totals（9），不是数数组（3）");
  assert.match(text, /90 次/);
  assert.match(text, /30 分钟/);
});

test("0 组 / 0 次不当作已知值展示（线上 set_count 恒为 0）", () => {
  // 11-05 真实数据：13 次训练、set_count 0、rep_total null、3h30m → 只显示训练次数和时长
  const text = M.fitnessSummaryText({totals: {workout_count: 13, set_count: 0, rep_total: null, duration_seconds: 12600}});
  assert.match(text, /13 次训练/);
  assert.match(text, /3 小时 30 分/);
  assert.doesNotMatch(text, /0 组/, "0 组不是已知值，不能显示成精确的 0");
  // 单条 workout 同理
  assert.equal(M.workoutBackendSummary({set_count: 0, rep_total: null, volume_kg: null}), "");
});

test("没有 totals 时 fitnessSummaryText 返回空（旧夹具不回退造数）", () => {
  assert.equal(M.fitnessSummaryText({workouts: [{set_count: 3}]}), "");
  assert.equal(M.fitnessSummaryText(null), "");
});

test("单条 workout 优先用后端 set_count / rep_total / volume_kg", () => {
  const text = M.workoutBackendSummary({set_count: 4, rep_total: 48, volume_kg: 1200, duration_seconds: 600});
  assert.match(text, /4 组/);
  assert.match(text, /48 次/);
  assert.match(text, /容量 1200 kg/);
  assert.match(text, /10 分钟/);
});

test("volume_partial 会标注，且没有后端字段时不产出汇总行", () => {
  assert.match(M.workoutBackendSummary({set_count: 2, volume_kg: 100, volume_partial: true}), /部分重量未知/);
  assert.equal(M.workoutBackendSummary({}), "");
  assert.equal(M.workoutBackendSummary(null), "");
});

test("workoutFactsHtml 把后端汇总插到训练事实最前面", () => {
  const card = {kind: "fitness", facts: [{path: "data.exercise.name", value: "卧推", support: "quote_verbatim"}]};
  const out = M.workoutFactsHtml(card, {set_count: 5, rep_total: 60});
  assert.match(out, /data-workout-backend-summary/);
  assert.match(out, /5 组 · 60 次/);
  // 没有后端 workout 时保持原样（只有逐条事实）
  const old = M.workoutFactsHtml(card, null);
  assert.doesNotMatch(old, /data-workout-backend-summary/);
});

// ── 3. 活动 counts ────────────────────────────────────────────────
test("activityCounts 优先用后端 counts（total/live_count/media_count）", () => {
  // 哨兵：acts 只有 2 条（都 live），后端说 total=215 / live=33 / media=124
  const D = {
    acts: [{life: "live"}, {life: "live"}],
    backend_counts: {total: 215, live_count: 33, media_count: 124, by_fact_type: {}},
  };
  const c = M.activityCounts(D);
  assert.equal(c.source, "backend");
  assert.equal(c.all, 215, "不能显示前端数的 2");
  assert.equal(c.live, 33);
  assert.equal(c.media, 124);
  assert.equal(c.unknown, 215 - 33 - 124);
});

test("线上 counts 的键名（live/media/unknown/my_life）同样认", () => {
  // 真实 /api/activity-timeline 返回的就是这个形状，不是派单文档里的 live_count/media_count
  const D = {acts: [], backend_counts: {all: 262, total: 262, live: 44, media: 140, unknown: 78,
                                       my_life: 44, by_category: {"浏览·视频": 146}}};
  const c = M.activityCounts(D);
  assert.equal(c.source, "backend");
  assert.equal(c.all, 262);
  assert.equal(c.live, 44);
  assert.equal(c.media, 140);
  assert.equal(c.unknown, 78);
  assert.equal(c.myLife, 44);
  assert.deepEqual(c.byCategory, {"浏览·视频": 146});
});

test("没有 counts 时回退前端 filter 计数（旧接口兼容）", () => {
  const D = {acts: [{life: "live"}, {life: "media"}, {life: "unknown"}]};
  const c = M.activityCounts(D);
  assert.equal(c.source, "frontend");
  assert.equal(c.all, 3);
  assert.equal(c.live, 1);
  assert.equal(c.media, 1);
  assert.equal(c.unknown, 1);
});

test("shape() 会保留后端顶层 counts 供上面使用", () => {
  const shapeSrc = slice(html, "shape");
  assert.match(shapeSrc, /backend_counts:\s*tl\.counts \|\| null/);
});

// ── 4. 旧夹具兼容 ─────────────────────────────────────────────────
test("旧夹具（无 meal_totals / 无 totals / 无 counts）仍按 P1 行为工作", () => {
  const items = [{item: "A", calories_kcal: 60, nutrients: {carbohydrates_g: 6, protein_g: 3, fat_g: 1}}];
  const m = M.mealMacros(items);                 // 不传 mealTotal
  assert.equal(m.source, "items_fallback");
  assert.equal(m.kcal, 60);
  assert.equal(M.fitnessSummaryText({}), "");
  assert.equal(M.activityCounts({acts: []}).source, "frontend");
});

// ── 5. 消费待确认原因中文化（2026-11-07 P2 追加） ────────────────────
import vm from "node:vm";

function loadDrilldown() {
  const code = fs.readFileSync(path.join(ROOT, "web/expense-drilldown.js"), "utf8");
  const context = {
    window: {},
    document: {getElementById: () => null, createElement: () => ({id: "", textContent: ""}),
                head: {appendChild: () => {}}, addEventListener: () => {}},
  };
  context.globalThis = context.window;
  vm.createContext(context);
  vm.runInContext(code, context);
  return context.window.ExpenseDrilldown;
}

function drilldownHtml(reasons) {
  const D = loadDrilldown();
  return D.build({
    total_amount: 0, currency: "CNY", totals_by_currency: {CNY: 0},
    pending_transactions: reasons.map((reason, i) => ({
      clock: `1${i}:00`, item: `待确认项 ${i}`, reason, amount: 13.4, currency: "CNY",
    })),
  });
}

test("待确认原因：后端新增原因码都渲染成中文，不出现 snake_case", () => {
  const reasons = ["media_audio_third_person", "amount_from_other_speaker_unconfirmed",
                   "activity_spend_unconfirmed", "activity_purchase_amount_unspoken",
                   "activity_payment_broadcast_unconfirmed", "invalid_amount", "non_positive_amount",
                   "currency_unknown", "aggregate_reconciliation_ambiguous", "v3_unknown_lifecycle",
                   "v3_self_evidence_missing", "v3_review_pending", "v3_payment_unconfirmed",
                   "v3_amount_unverified"];
  const html = drilldownHtml(reasons);
  assert.match(html, /视频或音乐音轨中的他人提及/);
  assert.match(html, /店员或对方报价/);
  assert.match(html, /活动提及费用/);
  assert.match(html, /原话未说具体金额/);
  assert.match(html, /现场收款播报/);
  assert.match(html, /金额格式无法解析/);
  assert.match(html, /金额非正数/);
  assert.match(html, /币种未说明/);
  assert.match(html, /合计与分项金额存在歧义/);
  assert.match(html, /活动场景未知/);
  assert.match(html, /缺少本人语音证据/);
  assert.match(html, /待人工复核确认/);
  assert.match(html, /尚未确认完成付款/);
  assert.match(html, /金额未经原话证实/);
  // 铁律：界面上不许出现 snake_case 机器码
  assert.doesNotMatch(html, /[a-z]_[a-z]/, "不得把英文 snake_case 原因码漏到界面");
});

test("待确认原因：词表里没有的英文原因码也不漏 snake_case（退回人话）", () => {
  const html = drilldownHtml(["some_new_snake_case_reason"]);
  assert.match(html, /待确认真实付款凭证/);
  assert.doesNotMatch(html, /some_new_snake_case_reason/);
  assert.doesNotMatch(html, /[a-z]_[a-z]/);
});

test("待确认原因：中文原因原样保留（不被兜底吞掉）", () => {
  const html = drilldownHtml(["店员说是 13 块 4"]);
  assert.match(html, /店员说是 13 块 4/);
});

// ── 6. 顶栏「另有 N 笔待确认」（2026-11-07 P2 追加） ────────────────
test("spendClockHtml：合计 0 但有 pending_count 时提示「另有 N 笔待确认」", () => {
  const html = M.spendClockHtml({expense_ledger: {
    total_amount: 0, payment_transaction_count: 0, pending_count: 2,
    totals_by_currency: {CNY: 0},
    pending_transactions: [{amount: 13.4, currency: "CNY", reason: "v3_payment_unconfirmed"}],
  }}, "ready");
  assert.match(html, /另有 2 笔待确认/);
  assert.match(html, /这一天还没有记到消费/);
});

test("spendClockHtml：pending_count 缺失时用数组长度；两者都有时合计提示不丢", () => {
  const only = M.spendClockHtml({expense_ledger: {
    total_amount: 0, totals_by_currency: {CNY: 0},
    pending_transactions: [{amount: 1}, {amount: 2}, {amount: 3}],
  }}, "ready");
  assert.match(only, /另有 3 笔待确认/);
  const both = M.spendClockHtml({expense_ledger: {
    total_amount: 0, totals_by_currency: {CNY: 0},
    pending_broadcast_total_cny: 8, pending_count: 2,
  }}, "ready");
  assert.match(both, /另有 ¥8\.00 待确认/);
  assert.match(both, /共 2 笔/);
});

test("spendClockHtml：没有待确认时不出现「另有」字样", () => {
  const html = M.spendClockHtml({expense_ledger: {
    total_amount: 10, payment_transaction_count: 1, totals_by_currency: {CNY: 10},
  }}, "ready");
  assert.doesNotMatch(html, /另有/);
});

// ── 7. 健身会话数优先（2026-11-07 P2 追加） ────────────────────────
test("fitnessSummaryText：优先用去重后的 session_count", () => {
  const text = M.fitnessSummaryText({totals: {session_count: 1, workout_count: 13, duration_seconds: 3151.5}});
  assert.match(text, /1 次训练/, "要用会话数 1，不是片段数 13");
  assert.doesNotMatch(text, /13 次训练/);
  assert.match(text, /52 分钟|53 分钟/);   // 3151.5s ≈ 52.5min，fmtDur 四舍五入
});

test("fitnessSummaryText：没有 session_count 时回退 workout_count", () => {
  assert.match(M.fitnessSummaryText({totals: {workout_count: 13, duration_seconds: 3151.5}}), /13 次训练/);
  assert.match(M.fitnessSummaryText({totals: {session_count: 0, workout_count: 2}}), /2 次训练/);
});
