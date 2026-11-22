import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const app = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const index = fs.readFileSync(new URL("../web/index.html", import.meta.url), "utf8");
const timeline = fs.readFileSync(new URL("../web/daily-timeline-live.html", import.meta.url), "utf8");

test("primary nav preserves approved kanban baseline", () => {
  const nav = index.slice(index.indexOf('<nav class="app-nav"'), index.indexOf("</nav>"));
  const views = [...nav.matchAll(/data-view="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(views, ["today", "kanban", "stats", "review", "more"]);
});

test("no page-level scrollIntoView or stepped iframe timers", () => {
  assert.equal((app.match(/scrollIntoView/g) || []).length, 0);
  assert.equal((app.match(/setTimeout\(fit, 600\)/g) || []).length, 0);
  assert.equal((app.match(/setTimeout\(fit, 1800\)/g) || []).length, 0);
  assert.match(app, /life-logger-timeline-height/);
});

test("timeline is a single-column flow with quiet gaps and expense drilldown", () => {
  // 现行实现为三轨瀑布（小时带 + 折叠静默段）；旧的 timeline-list/flow-row 设计已被取代
  assert.match(timeline, /class="hour-band/);
  assert.match(timeline, /class="timeline-body"/);
  // 折叠静默段的 class 现在是模板串 `class="folded-hours-row${sceneNote ? " has-scenes" : ""}"`，
  // 仍然只允许这一个入口类，所以用前缀匹配而不是精确等号。
  assert.match(timeline, /class="folded-hours-row/);
  assert.match(timeline, /id="detailDrawer"[^>]*aria-hidden="true"/);
  assert.match(timeline, /expense-drilldown\.js/);
  assert.match(timeline, /id="expenseDrilldown"/);
  assert.match(timeline, /renderExpenseDrilldown/);
});

test("mic button uses an SVG icon", () => {
  const button = index.slice(index.indexOf('id="natural-correction-mic-btn"'), index.indexOf("</button>", index.indexOf('id="natural-correction-mic-btn"')));
  assert.match(button, /<svg/);
  assert.doesNotMatch(button, /◉/);
});

test('空态判定：有时间轴事实时绝不显示“没有记录”，3 秒只升级文案', async () => {
  const vm = await import('node:vm');
  const fs = await import('node:fs');
  const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
  const start = source.indexOf('function journalLoadState(');
  const bodyStart = source.indexOf(') {', start) + 2;
  let depth = 0, end = -1;
  for (let i = bodyStart; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}' && !--depth) { end = i; break; }
  }
  const context = {result: null, Number, Date, String};
  vm.createContext(context);
  vm.runInContext(source.slice(start, end + 1) + '\nresult = journalLoadState;', context);
  const state = context.result;
  const withFacts = {events: 11, unlocated: 2, captures: 0, empty: false};
  assert.equal(state({facts: withFacts, elapsedMs: 9000}).kind, 'partial');
  assert.notEqual(state({facts: withFacts, elapsedMs: 9000}).text, '这一天没有记录');
  assert.equal(state({facts: {events: 0, unlocated: 0, captures: 0, empty: true}}).kind, 'empty');
  assert.equal(state({facts: null, elapsedMs: 500}).kind, 'loading');
  assert.equal(state({facts: null, elapsedMs: 3500}).kind, 'slow');
  assert.equal(state({facts: null, elapsedMs: 3500, pending: true}).kind, 'pending');
  assert.equal(state({facts: null, error: 'boom'}).kind, 'error');
});
