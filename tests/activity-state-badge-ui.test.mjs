// 活动成员状态徽标 vs 审核徽标：不得用「已确认」暗示完成/核实（Q430）
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(new URL(".", import.meta.url).pathname, "..");
const html = fs.readFileSync(path.join(root, "web", "daily-timeline-live.html"), "utf8");
const script = html.split("<script>")[1].split("</script>")[0];

function grab(name) {
  const start = script.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `找不到函数 ${name}`);
  // 用花括号配对取完整函数体（避免被内部字符串 / 嵌套结构截断）
  let depth = 0, started = false;
  for (let i = start; i < script.length; i += 1) {
    const ch = script[i];
    if (ch === "{") { depth += 1; started = true; }
    else if (ch === "}") {
      depth -= 1;
      if (started && depth === 0) return script.slice(start, i + 1);
    }
  }
  throw new Error(`函数 ${name} 花括号不闭合`);
}

const reviewStatusLabel = new Function(`${grab("reviewStatusLabel")}; return reviewStatusLabel;`)();
const activityStateBadge = new Function(
  `${script.slice(script.indexOf("const ACTIVITY_STATE_BADGES"), script.indexOf("function activityTitleUnverifiedMark"))}
   ${grab("activityStateBadge")}; return activityStateBadge;`
)();

// 1) 审核文案中性：不再出现「已确认」
assert.equal(reviewStatusLabel("reviewed"), "已审阅");
assert.equal(reviewStatusLabel("confirmed"), "已审阅");
assert.equal(reviewStatusLabel("unreviewed"), "待审阅");
assert.ok(!["reviewed", "confirmed"].some((v) => reviewStatusLabel(v).includes("已确认")));

// 2) 四态徽标可辨、且都不是「已确认」
const done = activityStateBadge("done");
const planned = activityStateBadge("planned");
const cancelled = activityStateBadge("cancelled");
const unknown = activityStateBadge("unknown");
const inProgress = activityStateBadge("in_progress");
assert.equal(planned.text, "计划中");
assert.equal(cancelled.text, "已取消");
assert.equal(unknown.text, "状态未知");
assert.equal(done.text, "已完成");
assert.equal(inProgress.text, "进行中");
const texts = [done, planned, cancelled, unknown, inProgress].map((b) => b.text);
assert.equal(new Set(texts).size, 5, "五种状态必须有五种不同文案");
assert.ok(!texts.some((t) => t.includes("已确认")), "状态徽标不得使用审核文案");

// 3) 未知状态回落到「状态未知」，不得回落到「已完成」
assert.equal(activityStateBadge("garbage").text, "状态未知");
assert.equal(activityStateBadge("").text, "状态未知");

// 4) 卡片 chips 使用状态徽标 + 中性审核徽标（源码级）
assert.ok(script.includes("activityStateBadge(activityCard.state)"), "卡片必须按 activity card 状态渲染徽标");
assert.ok(script.includes('cls: "act-review-tag"'), "审核徽标必须是中性样式");
assert.ok(script.includes('const icon = String(ev.review_status).toLowerCase() === "ignored" ? "✕" : "·"'),
  "审核徽标必须用中性符号（·），不得用勾号 ✓ 暗示已发生");
assert.ok(!/review_status[\s\S]{0,80}\?\s*"✕"\s*:\s*"✓"/.test(script),
  "不得再用勾号渲染审核状态");

// 5) 模型概述重量：标题标注 + 值旁标注
assert.ok(script.includes("模型概述·未逐字核实"), "标题需标注模型概述未核实");
assert.ok(script.includes("未逐字核实"), "值旁需有未逐字核实标记");
assert.ok(script.includes("activityTitleUnverifiedMark("), "标题标注函数需接入渲染");

// 6) 样式类存在且四态各自有区分（不仅靠颜色：图标+文字在 JS 里已断言）
for (const cls of ["act-state-done", "act-state-planned", "act-state-cancelled", "act-state-unknown", "act-review-tag"]) {
  assert.ok(html.includes(`.${cls}`), `缺少样式 ${cls}`);
}
console.log("activity-state-badge-ui: 6 组断言通过");
