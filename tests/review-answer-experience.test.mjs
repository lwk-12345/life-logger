import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

const root = path.resolve(new URL(".", import.meta.url).pathname, "..");
const appSrc = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");

test("抽屉渲染：包含为什么问、具体未知、选项影响与说明", () => {
  const start = appSrc.indexOf("// ── 生活记录优先的审核首版");
  const end = appSrc.indexOf("function pendingReviewBodyHtml(");
  assert.ok(start > 0 && end > start);

  const factory = new Function("escapeHtml", "pendingReviewState", `
    ${appSrc.slice(start, end)}
    return { pendingQuestionDrawerHtml, pendingQuestionKind };
  `);
  const api = factory((s) => String(s ?? ""), { answeredQuestions: {} });

  // 1. 查询类条目
  const queryItem = {
    source_id: "src_query_1",
    quote: "查一下明天天气",
    reason: "model_no_event_but_directive",
    reason_label: "模型没有产出事件",
    speaker: "本人",
    job_id: "job_1"
  };
  const htmlQuery = api.pendingQuestionDrawerHtml(queryItem);
  assert.match(htmlQuery, /为什么问：/);
  assert.match(htmlQuery, /具体未知：/);
  assert.match(htmlQuery, /未知项：说话人性质\/意图归属/);
  assert.match(htmlQuery, /是说给其他应用还是记录/);
  assert.match(htmlQuery, /是给其他助手 \/ 应用说的/);
  assert.match(htmlQuery, /暂不确定，保留待回答/);
  assert.match(htmlQuery, /来源判断不等于生成生活事件、不记账、不执行 Agent 命令/);

  // 2. 媒体类条目
  const mediaItem = {
    source_id: "src_media_1",
    quote: "现在插播一条广告",
    reason: "unknown_speaker",
    reason_label: "未知说话人",
    speaker: "未知",
    job_id: null
  };
  const htmlMedia = api.pendingQuestionDrawerHtml(mediaItem);
  assert.match(htmlMedia, /是影视 \/ 广告/);
  assert.match(htmlMedia, /仅关闭本来源提醒（可撤销），不影响其他录音/);
  assert.match(htmlMedia, /无法找到对应录音，此来源暂不能确认写入/);

  // 3. 已有状态时显示撤销栏
  const stateWithAnswer = { answeredQuestions: { "src_answered": { state: "muted" } } };
  const apiWithState = factory((s) => String(s ?? ""), stateWithAnswer);
  const htmlAnswered = apiWithState.pendingQuestionDrawerHtml({ source_id: "src_answered", quote: "hello" });
  assert.match(htmlAnswered, /已记录来源判断：<strong>muted<\/strong>/);
  assert.match(htmlAnswered, /撤销上一次判断/);
});

test("交互闭环与异常保护：answer 真实调用接口，失败绝不冒充 ignored", async () => {
  const start = appSrc.indexOf("async function pendingReviewAction(");
  const end = appSrc.indexOf("async function pendingReviewConfirmSubmit(");
  assert.ok(start > 0 && end > start);

  let apiCalls = [];
  let statusMessages = [];
  let setPendingCalled = false;

  const mockApi = async (url, opts) => {
    apiCalls.push({ url, opts });
    if (url.includes("/api/review-questions/answer")) {
      const body = JSON.parse(opts.body);
      if (body.source_id === "src_fail") {
        throw new Error("服务未响应 (500)");
      }
      return { ok: true, state: "muted", operation: body.operation };
    }
    if (url.includes("/api/review-questions/undo")) {
      return { ok: true, state: "open" };
    }
    return { ok: true };
  };

  const context = {
    api: mockApi,
    pendingReviewStatus: (msg, type) => statusMessages.push({ msg, type }),
    setPendingReviewState: async () => { setPendingCalled = true; return true; },
    refreshPendingReviewBlock: async () => {},
    currentPendingReviewDate: () => "2026-10-25",
    escapeHtml: (s) => s,
    pendingReviewState: { answeredQuestions: {} },
    document: {
      querySelector: () => ({ remove: () => {} })
    }
  };

  const fn = new Function("context", `
    with(context) {
      ${appSrc.slice(start, end)}
      return { pendingReviewAction };
    }
  `)(context);

  // 1. 成功提交 media 判断
  await fn.pendingReviewAction("answer-question", encodeURIComponent("src_1"), "media");
  assert.equal(apiCalls.length, 1);
  assert.equal(apiCalls[0].url, "/api/review-questions/answer");
  const reqBody = JSON.parse(apiCalls[0].opts.body);
  assert.equal(reqBody.source_id, "src_1");
  assert.equal(reqBody.operation, "media");
  assert.ok(reqBody.op_id.startsWith("op_"), "必须生成唯一 op_id 保证幂等");
  assert.equal(context.pendingReviewState.answeredQuestions["src_1"].state, "muted");

  // 2. 失败情况：服务报错时诚实展示，绝不调用 setPendingReviewState (不写 ignored 冒充)
  apiCalls = [];
  setPendingCalled = false;
  await fn.pendingReviewAction("answer-question", encodeURIComponent("src_fail"), "media");
  assert.equal(apiCalls.length, 1);
  assert.equal(setPendingCalled, false, "失败时绝对不能调用 setPendingReviewState 写 ignored 冒充");
  const lastErr = statusMessages[statusMessages.length - 1];
  assert.match(lastErr.msg, /未做冒充标记，保留原始状态/);

  // 3. 撤销操作：undo-question
  apiCalls = [];
  await fn.pendingReviewAction("undo-question", encodeURIComponent("src_1"));
  assert.equal(apiCalls.length, 1);
  assert.equal(apiCalls[0].url, "/api/review-questions/undo");
  assert.equal(context.pendingReviewState.answeredQuestions["src_1"], undefined);

  // 4. keep-pending 只关抽屉，不调写接口
  apiCalls = [];
  setPendingCalled = false;
  await fn.pendingReviewAction("keep-pending");
  assert.equal(apiCalls.length, 0);
  assert.equal(setPendingCalled, false);
});
