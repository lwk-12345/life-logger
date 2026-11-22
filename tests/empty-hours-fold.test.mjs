import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const html = fs.readFileSync(new URL("../web/daily-timeline-live.html", import.meta.url), "utf8");
const start = html.indexOf("function foldRangeKey");
const end = html.indexOf("function renderTimeline()", start);
assert.ok(start >= 0 && end > start, "fold helpers exist");

function build() {
  const context = {
    state: {date: "2026-10-25", nowMinute: 630, procMinute: 600, foldOpenRanges: {}},
    empty: Array.from({length: 24}, () => false),
    RECORDING_SPAN_LIST: [{start: 60, end: 90}, {start: 120, end: 150}],
    fmtMin: (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`,
    fmtDur: (m) => `${m} 分钟`,
    renderTimeline() {},
  };
  vm.createContext(context);
  vm.runInContext(html.slice(start, end), context);
  return context;
}

test("只折叠连续至少 3 个已过去完整无事件小时", () => {
  const c = build();
  const empty = Array.from({length: 24}, () => false);
  assert.deepEqual(
    JSON.parse(JSON.stringify(vm.runInContext("foldableHourRuns(empty, true)", c))),
    [{start: 0, end: 10}],
    "0–10 点已完整过去且无事件；当前 10 点未完整过去",
  );
  assert.equal(vm.runInContext("foldableHourRuns(empty, false).length", c), 1, "过去日期可折叠完整空小时");

  c.twoOnly = empty.map((_, h) => h >= 2);
  assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext("foldableHourRuns(twoOnly, true)", c))), [],
    "少于 3 个连续小时不折叠");

  c.withEvent = [...empty];
  c.withEvent[5] = true;
  assert.deepEqual(
    JSON.parse(JSON.stringify(vm.runInContext("foldableHourRuns(withEvent, true)", c))),
    [{start: 0, end: 5}, {start: 6, end: 10}],
    "有事件小时不得进入折叠范围",
  );
});

test("处理未完成/当前/未来小时不折叠", () => {
  const c = build();
  const empty = Array.from({length: 24}, () => false);
  c.state.procMinute = null;
  assert.equal(vm.runInContext("foldableHourRuns(empty, true).length", c), 0, "处理游标未知时不猜");
  c.state.procMinute = 300;
  assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext("foldableHourRuns(empty, true)", c))), [{start: 0, end: 5}], "只到已处理边界");
});

test("折叠行保留录音覆盖时长并可在同一日期展开", () => {
  const c = build();
  assert.equal(vm.runInContext("foldRecordingMinutes(0, 180)", c), 60);
  assert.equal(vm.runInContext("state.foldOpenRanges['2026-10-25:0-10']", c), undefined);
  vm.runInContext("toggleFoldRange(0, 10)", c);
  assert.equal(vm.runInContext("state.foldOpenRanges['2026-10-25:0-10']", c), true);
  vm.runInContext("toggleFoldRange(0, 10)", c);
  assert.equal(vm.runInContext("state.foldOpenRanges['2026-10-25:0-10']", c), false);
});

test("展开小时仍固定 72px，无事件不是睡眠推断", () => {
  assert.match(html, /height:\s*var\(--hour-h,\s*72px\)/);
  assert.match(html, /无事件/);
  assert.match(html, /class="folded-hours-open"/);
  assert.doesNotMatch(html, /凌晨睡眠静默时段（5 小时无语音事件/);
});
