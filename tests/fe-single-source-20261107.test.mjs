// 唯一事实来源·前端第一阶段：数字只由后端给，前端只展示。
//
// 覆盖（对应派单 1–5 条）：
//   1. 四个 extract*Ledger 不再有"回退重算"，ledger 缺失 → 暂无数据
//   2. nutritionCoverageFor 直通后端 totals_coverage
//   3. spendText 合计只用 total_amount；笔数用 payment_transaction_count
//   4. mealMacros 缺宏量时热量仍计入（不再整条跳过）
//   5. 时间一律按北京时间（TZ=America/Los_Angeles 下也一样）
import {test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import {execFileSync} from "node:child_process";

const ROOT = path.resolve(import.meta.dirname, "..");
const APP = fs.readFileSync(path.join(ROOT, "web/app.js"), "utf8");
const WATERFALL = fs.readFileSync(path.join(ROOT, "web/waterfall-v2.html"), "utf8");

/** 按 `function NAME(` 切片（大括号配平）。 */
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

const APP_PRELUDE = `
  const INT_CLOCK = new Intl.NumberFormat("zh-CN", {minimumIntegerDigits: 2, useGrouping: false});
`;
function loadApp(names) {
  const body = names.map((n) => slice(APP, n)).join("\n");
  return new Function(`${APP_PRELUDE}\n${body}\nreturn {${names.join(",")}};`)();
}
const WF_PRELUDE = `
  const esc = (s) => String(s == null ? "" : s);
  const MEAL_LABEL = {breakfast: "早餐", lunch: "午餐", dinner: "晚餐", snacks: "加餐", unknown: "未归餐"};
`;
function loadWaterfall(names) {
  const body = names.map((n) => slice(WATERFALL, n)).join("\n");
  return new Function(`${WF_PRELUDE}\n${body}\nreturn {${names.join(",")}};`)();
}

const app = loadApp([
  "extractDayNutritionLedger", "extractDayFitnessLedger",
  "extractDaySmokingLedger", "extractDayWorkCognitionLedger", "nutritionCoverageFor",
  "shanghaiTimeParts", "formatClockPair", "journalEventTimestamp", "eventClock",
]);
const wf = loadWaterfall(["spendText", "spendCurrencyNote", "spendClockHtml", "mealMacros", "macrosHtml", "macroNum"]);

// ── 1. 不再回退重算 ────────────────────────────────────────────────
test("ledger 存在但 has_records=false：原样返回，不再从 timeline 造数字", () => {
  const day = {
    nutrition_ledger: {has_records: false, meals: {}, totals: {}, record_count: 0, consumed_record_count: 0},
    fitness_ledger: {has_records: false, workouts: [], workout_count: 0},
    smoking_ledger: {has_records: false, total_count: 0, sessions: []},
    work_cognition_ledger: {has_records: false, items: [], item_count: 0},
    // 故意塞满时间线：老实现会据此"数出"记录
    timeline: [
      {title: "吃了一碗面", categories: ["food"], data: {food: {item: "面", estimated_calories: 500}}},
      {title: "抽根烟", categories: ["smoking"], data: {smoking: {action: "smoke"}}},
      {title: "做了三组卧推", data: {fitness: {exercise: "卧推", sets: 3}}},
    ],
  };
  assert.equal(app.extractDayNutritionLedger(day).has_records, false);
  assert.equal(app.extractDayNutritionLedger(day).record_count, 0, "不得从 timeline 数出记录");
  assert.equal(app.extractDayFitnessLedger(day).workout_count, 0);
  assert.equal(app.extractDaySmokingLedger(day).total_count, 0);
  assert.equal(app.extractDayWorkCognitionLedger(day).item_count, 0);
});

test("ledger 整个缺失（老接口）：返回“暂无数据”，同样不重算", () => {
  const day = {timeline: [{title: "抽根烟", categories: ["smoking"], data: {smoking: {action: "smoke"}}}]};
  for (const [fn, kind] of [[app.extractDayNutritionLedger, "nutrition"],
                            [app.extractDayFitnessLedger, "fitness"],
                            [app.extractDaySmokingLedger, "smoking"],
                            [app.extractDayWorkCognitionLedger, "work"]]) {
    const led = fn(day);
    assert.equal(led.unavailable, true, `${kind} 应标 unavailable`);
    assert.equal(led.has_records, false);
  }
  assert.equal(app.extractDaySmokingLedger(day).total_count, 0, "不得按关键词数出 1 根烟");
});

// ── 2. 覆盖率直通后端 ──────────────────────────────────────────────
test("nutritionCoverageFor 直通 nutrition_ledger.totals_coverage", () => {
  const ledger = {
    has_records: true, consumed_record_count: 3,
    totals_coverage: {
      consumed_records: 3, calories_known: 2, calories_unknown: 1, calories_known_ratio: 0.667,
      macros: {protein_g: {known: 1, unknown: 2}},
    },
  };
  assert.deepEqual(app.nutritionCoverageFor(ledger, "calories_kcal"),
    {known: 2, unknown: 1, total: 3, source: "backend"});
  assert.deepEqual(app.nutritionCoverageFor(ledger, "protein_g"),
    {known: 1, unknown: 2, total: 3, source: "backend"});
});

test("后端没有 totals_coverage 时不做前端统计，只透传条数并标未知", () => {
  const cov = app.nutritionCoverageFor({consumed_record_count: 4}, "calories_kcal");
  assert.equal(cov.known, null);
  assert.equal(cov.unknown, null);
  assert.equal(cov.total, 4);
});

// ── 3. 消费金额/笔数 ───────────────────────────────────────────────
test("spendText 合计只用 total_amount，不按币种重新求和", () => {
  const ledger = {total_amount: 100, totals_by_currency: {CNY: 20, USD: 5}};
  assert.equal(wf.spendText(ledger), "¥100.00", "合计不得变成 25 或 20");
  assert.match(wf.spendCurrencyNote(ledger), /USD 5\.00 不计入合计/);
  assert.equal(wf.spendCurrencyNote({totals_by_currency: {CNY: 20}}), "", "只有 CNY 时不显示附注");
});

test("spendClockHtml 笔数用 payment_transaction_count，不用数组长度", () => {
  const ledger = {
    total_amount: 10, payment_transaction_count: 3,
    payment_transactions: [{amount: 10}],   // 数组只有 1 条
    totals_by_currency: {},
  };
  const html = wf.spendClockHtml({expense_ledger: ledger}, "ok");
  assert.match(html, /3 笔/);
  assert.doesNotMatch(html, /1 笔/);
  assert.match(html, /¥10\.00/);
});

// ── 4. mealMacros：缺宏量时热量仍计入 ──────────────────────────────
test("mealMacros 缺宏量时热量仍计入，并标“部分未知”", () => {
  const items = [{item: "三全猪肉芹菜速冻水饺", calories_kcal: 774,
                  nutrients: {carbohydrates_g: null, protein_g: null, fat_g: null}}];
  const m = wf.mealMacros(items);
  assert.equal(m.kcal, 774, "热量必须计入（旧实现整条跳过 → 0）");
  assert.equal(m.kcalKnown, 1);
  assert.equal(m.cKnown, 0);
  assert.equal(m.missing, 1);

  const html = wf.macrosHtml({nutrition_ledger: {
    has_records: true, consumed_record_count: 1, unknown_nutrition_count: 1,
    totals: {calories_kcal: 774, carbohydrates_g: null, protein_g: null, fat_g: null},
    totals_unknown_counts: {calories_kcal: 0, carbohydrates_g: 1, protein_g: 1, fat_g: 1},
    meals: {breakfast: items},
  }}, "ok");
  assert.match(html, /774 kcal/);
  assert.match(html, /部分未知/);   // 文案：`… 1 项缺数据（部分未知）`
});

test("mealMacros 全已知时不标部分未知", () => {
  const m = wf.mealMacros([{item: "土豆", calories_kcal: 231,
                            nutrients: {carbohydrates_g: 51.6, protein_g: 6, fat_g: 0.6}}]);
  assert.equal(m.missing, 0);
  assert.equal(m.kcal, 231);
});

// ── 5. 时间一律北京时间 ────────────────────────────────────────────
test("eventClock 按北京时间格式化（与浏览器时区无关）", () => {
  // 2026-11-04T16:30:00Z = 北京 11-05 00:30
  const clock = app.eventClock({time: {absolute: "2026-11-04T16:30:00Z"}});
  assert.equal(clock, "00:30");
  const parts = app.shanghaiTimeParts(Date.parse("2026-11-04T16:30:00Z"), {hour: "2-digit", minute: "2-digit"}, "en-GB");
  assert.equal(`${parts.hour}:${parts.minute}`, "00:30");
});

test("在 TZ=America/Los_Angeles 下仍是北京时间", () => {
  const script = `
    const fs = require("node:fs");
    const src = fs.readFileSync(${JSON.stringify(path.join(ROOT, "web/app.js"))}, "utf8");
    function slice(s, name) {
      const start = s.indexOf("function " + name + "(");
      let depth = 0, started = false;
      for (let i = start; i < s.length; i++) {
        if (s[i] === "{") { depth++; started = true; }
        else if (s[i] === "}") { depth--; if (started && depth === 0) return s.slice(start, i + 1); }
      }
    }
    const body = ["shanghaiTimeParts", "formatClockPair", "journalEventTimestamp", "eventClock"]
      .map((n) => slice(src, n)).join("\\n");
    const INT_CLOCK = new Intl.NumberFormat("zh-CN", {minimumIntegerDigits: 2, useGrouping: false});
    const f = new Function("INT_CLOCK", body + "\\nreturn {eventClock, shanghaiTimeParts};")(INT_CLOCK);
    process.stdout.write(JSON.stringify({
      tz: process.env.TZ,
      clock: f.eventClock({time: {absolute: "2026-11-04T16:30:00Z"}}),
      localHour: new Date("2026-11-04T16:30:00Z").getHours(),
    }));
  `;
  const out = execFileSync(process.execPath, ["-e", script], {
    env: {...process.env, TZ: "America/Los_Angeles"}, encoding: "utf8",
  });
  const result = JSON.parse(out);
  assert.equal(result.tz, "America/Los_Angeles");
  assert.equal(result.localHour, 8, "LA 本地应是 08:30（证明进程时区生效）");
  assert.equal(result.clock, "00:30", "但界面必须显示北京时间 00:30");
});
