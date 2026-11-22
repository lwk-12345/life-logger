// 生活记录优先审核首版：问题卡/抽屉/系统健康的语义与边界（源码级 + 隔离渲染）。
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(new URL(".", import.meta.url).pathname, "..");
const src = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");

const start = src.indexOf("// ── 生活记录优先的审核首版");
const end = src.indexOf("function pendingReviewBodyHtml(");
assert.ok(start > 0 && end > start, "找不到审核首版代码段");
const block = src.slice(start, end);

const sandbox = {};
const factory = new Function("escapeHtml", "pendingReviewNumber", "pendingReviewGroupHtml", "pendingReviewState", `
  ${block}
  return { pendingQuestionAreaHtml, pendingHealthAreaHtml, pendingQuestionDrawerHtml,
           pendingQuestionText, pendingQuestionCardHtml, QUESTION_LIMIT };
`);
const api = factory((s) => String(s ?? ""), (n) => String(n ?? 0),
                     (group) => `<div class="grp" data-key="${group.key}"></div>`, {});

const mk = (over = {}) => ({
  source_id: "src_1", quote: "呃，汇报一下，汇报一下。",
  reason: "model_no_event_but_directive", reason_label: "模型没有产出事件", job_id: "job-1", ...over,
});

// 1) 默认最多 3 个问题 + 还有 N 条
const many = { payload: { groups: [{ key: "needs_review", items: [1,2,3,4,5].map((i) => mk({ source_id: `src_${i}` })) }] } };
const area = api.pendingQuestionAreaHtml("2026-10-25", many);
assert.equal((area.match(/class="pq-card"/g) || []).length, 3, "默认只展示 3 张问题卡");
assert.ok(area.includes("还有 2 条"), "必须给更多入口");

// 2) 人话提问；技术原因只在折叠详情里
assert.ok(area.includes("这段是你本人说的吗？"));
const visible = area.replace(/<details[\s\S]*?<\/details>/g, "");
assert.ok(!/model_no_event_but_directive|unknown_speaker|dictation_review/.test(visible), "可见区不得出现判定代码");
assert.ok(area.includes("<details"), "技术详情必须折叠可展开");

// 3) 查询类：不臆测用途；只给可确认的来源判断 + 保留待回答
const queryDrawer = api.pendingQuestionDrawerHtml(mk());
assert.ok(!/随口说说|我自己记一下/.test(queryDrawer), "不得用臆测分类（随口/自己记）");
assert.ok(queryDrawer.includes("是给其他助手 / 应用说的"), "应提供'对其他助手/应用说的'判断");
assert.ok(queryDrawer.includes("是明确给 Life Logger 的记录"), "应提供'明确给 Life Logger'入口");
assert.ok(queryDrawer.includes("暂不确定，保留待回答"), "必须有不消失的保留出口");
assert.ok(queryDrawer.includes("复制原文") && !queryDrawer.includes("我自己记一下（复制原文）"), "复制只叫复制原文");
assert.ok(!/kind-pending|keep-pending[\s\S]{0,40}handled/.test(queryDrawer), "");

// 3b) 关闭提醒 vs 稍后处理：稍后不得写 handled
assert.ok(!/暂不确定，保留待回答", "handled"/.test(queryDrawer), "保留待回答不能写 handled");
assert.ok(queryDrawer.includes("keep-pending"), "保留待回答应走 keep-pending（不写状态）");
const labels = [...queryDrawer.matchAll(/class="pq-option"[^>]*>([^<]+)/g)].map((m) => m[1].trim());
assert.ok(!labels.includes("关闭这条提醒"), "查询抽屉不应出现'关闭提醒'选项（那是 handled 的语义）");
assert.deepEqual(labels, ["是给其他助手 / 应用说的", "是明确给 Life Logger 的记录", "暂不确定，保留待回答", "复制原文"]);

// 4) 媒体类：声源反馈接口未就绪 → 禁用；本人确认需 job
const media = api.pendingQuestionDrawerHtml(mk({ reason: "unknown_speaker", source_id: "src_m" }));
assert.ok(media.includes("声源合同通过后才可用"), "声源反馈未就绪必须禁用并解释");
assert.ok(!/confirm/.test(media), "媒体判断未就绪前不得走确认写事件");
assert.ok(media.includes("只记来源判断，不生成事件"), "确认本人 ≠ 个人事件");
const noJob = api.pendingQuestionDrawerHtml(mk({ reason: "silent_unknown_speaker", job_id: null, artifact: {} }));
assert.ok(noJob.includes("无法找到对应录音，暂不能保存"), "缺 job 要有出口说明（通用类）");
assert.ok(noJob.includes("稍后处理（保留在待回答）"), "缺 job 时仍能保留在待回答");

// 5) 系统健康只收异常分组，不混进需要回答
const health = api.pendingHealthAreaHtml({ payload: { groups: [
  { key: "extraction_gap", items: [], count: 4 }, { key: "batch_failure", items: [], count: 2 },
  { key: "needs_review", items: [mk()], count: 1 } ] } });
assert.ok(health.includes("extraction_gap") && health.includes("batch_failure"));
assert.ok(!health.includes("needs_review"), "正常待确认不得进系统健康");
assert.ok(health.includes("不需要逐条清理") && health.includes("不会自动修复"));

// 6) 原列表仍在（不隐藏真实候选）
assert.ok(src.includes("查看全部抽取条目（含未展开的原始记录）"), "必须保留全部条目入口");

// 7) 撤销走 status=open（接口早已支持）
assert.ok(/kind === "undo"[\s\S]{0,120}setPendingReviewState\(decodeURIComponent\(encodedId\), "open"\)/.test(src),
  "撤销必须复用 status=open");

// 8) 不宣称模型会学习
for (const bad of ["越用越准", "已训练", "模型已学习"]) {
  assert.ok(!block.includes(bad), `不得宣称：${bad}`);
}
console.log("review redesign ui: 8 组断言通过");


// 9) 保留待回答 ≠ 关闭提醒：keep-pending 不得调用任何写接口
assert.ok(/kind === "keep-pending"[^\n]*remove\(\)/.test(src), "keep-pending 只关抽屉，不写状态");
assert.ok(!/keep-pending[\s\S]{0,60}setPendingReviewState/.test(src), "keep-pending 不得写 pending 状态");

// 10) 不按 reason 推断输入目标
assert.ok(!/reason[^\n]{0,40}(豆包|Codex|Agent)/.test(block), "不得根据 reason 推断说话对象");
