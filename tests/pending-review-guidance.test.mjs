// 需确认 / 抽取异常 两组说明文案的可读性与诚实性（只查文案，不查后端行为）。
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(new URL(".", import.meta.url).pathname, "..");
const src = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");

function section(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start + startMarker.length);
  assert.ok(start >= 0 && end > start, `找不到片段: ${startMarker}`);
  return src.slice(start, end);
}

const pendingBody = section("function pendingReviewBodyHtml(", "function pendingReviewErrorText(");
const groupFn = section("function pendingReviewGroupHtml(", "const PENDING_CONFIRM_CATEGORIES");
const added = pendingBody + groupFn;

// 1) 需确认：三条动作语义 + 缺 job 的诚实说明
for (const text of ["查看原文", "编辑并保存", "写入人工事件", "只关闭提醒", "不入账", "缺少原始 job", "不能保存确认"]) {
  assert.ok(pendingBody.includes(text), `需确认说明缺少：${text}`);
}
assert.match(pendingBody, /不改原 events\.json/, "必须说明不会改原始事件");

// 2) 抽取异常：真实含义（未产出/校验未过）、不谎称自动修复、未知原因写未知
for (const text of ["没能产出可用结果", "未通过校验", "未知原因就写未知", "不会", "自动修复", "不需要逐条清理", "查看原文与原因", "技术细节"]) {
  assert.ok(groupFn.includes(text), `抽取异常说明缺少：${text}`);
}
assert.match(groupFn, /不一律归为系统故障/, "不得把全部异常写成系统故障");
assert.ok(!/正在修复|修复中|自动重试/.test(added), "不得声称系统正在修复/自动重试");

// 3) 只对 extraction_gap 组显示该说明，其他组不误加
assert.match(groupFn, /key === "extraction_gap"/, "说明必须按分组判定");
assert.match(groupFn, /\?\s*`<p class="pending-review-gap-help">/, "说明应是条件渲染");

// 4) 不新增行为（文案块里不能出现请求/定时/存储调用）
for (const forbidden of ["fetch(", "api(", "setInterval", "setTimeout", "localStorage", "POST"]) {
  assert.ok(!added.includes(forbidden), `文案块不得包含行为调用：${forbidden}`);
}

// 5) 可读性（1440/390 同一套文案）：只看**用户可见文案**，不看 JS 代码
function copyBlocks(text) {
  const blocks = [];
  const re = /<p class="pending-review-(?:help|gap-help)">([\s\S]*?)<\/p>/g;
  let match;
  while ((match = re.exec(text)) !== null) blocks.push(match[1]);
  return blocks;
}
const visibleCopy = copyBlocks(added).join(" ").replace(/<[^>]+>/g, "").replace(/\s+/g, " ");
assert.ok(visibleCopy.length > 80, "应能找到两段说明文案");
const plain = visibleCopy;
const sentences = plain.split(/[。；]/).map((s) => s.trim()).filter((s) => s.length > 8);
for (const sentence of sentences) {
  assert.ok(sentence.length <= 120, `句子过长（>120 字）：${sentence.slice(0, 40)}…`);
}
assert.ok(!/[A-Z]{4,}/.test(plain), "不要用全大写强调（读屏会逐字母读）");

// 6) 说明必须出现在渲染路径里（不是孤立常量）
assert.ok(pendingBody.includes("${help}"), "help 必须插入 body 渲染");
assert.ok(groupFn.includes("${gapHelp}"), "gapHelp 必须插入分组渲染");

console.log("pending-review guidance: 6 组断言全部通过");
