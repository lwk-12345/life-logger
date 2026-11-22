import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { execFileSync } from "node:child_process";

const root = path.resolve(new URL(".", import.meta.url).pathname, "..");
const appSrc = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");

function loadPendingActionHarness(mockApi, state = {}) {
  const start = appSrc.indexOf("async function pendingReviewAction(");
  const end = appSrc.indexOf("async function pendingReviewConfirmSubmit(");
  assert.ok(start > 0 && end > start);

  const statusMessages = [];
  let setPendingCalled = false;

  const context = {
    api: mockApi,
    pendingReviewStatus: (msg, type) => statusMessages.push({ msg, type }),
    setPendingReviewState: async () => { setPendingCalled = true; return true; },
    refreshPendingReviewBlock: async () => {},
    currentPendingReviewDate: () => "2026-10-25",
    escapeHtml: (s) => s,
    pendingReviewState: { answeredQuestions: {}, ...state },
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

  return { fn, context, statusMessages, setPendingCalled: () => setPendingCalled };
}

test("操作标识稳定性：网络失败重试复用同一 op_id，更换答案才生成新 op_id", async () => {
  const sentRequests = [];
  let shouldFail = true;

  const mockApi = async (url, opts) => {
    const body = JSON.parse(opts.body);
    sentRequests.push({ url, body });
    if (shouldFail) {
      throw new Error("Failed to fetch (模拟浏览器 abort 或连接中断)");
    }
    return { ok: true, state: "answered", operation: body.operation, op_id: body.op_id };
  };

  const harness = loadPendingActionHarness(mockApi);

  // 1. 首次提交：遭遇网络失败
  shouldFail = true;
  await harness.fn.pendingReviewAction("answer-question", encodeURIComponent("src_retry_1"), "query");
  assert.equal(sentRequests.length, 1);
  const firstOpId = sentRequests[0].body.op_id;
  assert.ok(firstOpId.startsWith("op_"), "必须生成规范的 op_ 前缀");
  assert.equal(harness.setPendingCalled(), false, "失败时绝不冒充 handled/ignored");

  // 2. 模拟用户点击重试提交（同一意图、同一来源、同一答案）
  await harness.fn.pendingReviewAction("answer-question", encodeURIComponent("src_retry_1"), "query");
  assert.equal(sentRequests.length, 2);
  const secondOpId = sentRequests[1].body.op_id;
  assert.equal(secondOpId, firstOpId, "网络失败重试必须复用同一个稳定 op_id，保证后端幂等性");

  // 3. 用户更换答案（切换为 media）
  await harness.fn.pendingReviewAction("answer-question", encodeURIComponent("src_retry_1"), "media");
  assert.equal(sentRequests.length, 3);
  const thirdOpId = sentRequests[2].body.op_id;
  assert.notEqual(thirdOpId, firstOpId, "更换答案必须生成全新的操作标识");

  // 4. 重试更换后的答案，直到成功
  shouldFail = false;
  await harness.fn.pendingReviewAction("answer-question", encodeURIComponent("src_retry_1"), "media");
  assert.equal(sentRequests.length, 4);
  const fourthOpId = sentRequests[3].body.op_id;
  assert.equal(fourthOpId, thirdOpId, "重试更换后的答案复用该答案的稳定标识");
  
  // 5. 成功后清理暂存的 pendingOp
  assert.equal(harness.context.pendingReviewState.pendingQuestionOps["src_retry_1"], undefined, "成功后必须清理暂存的操作标识");
  assert.equal(harness.context.pendingReviewState.answeredQuestions["src_retry_1"].state, "answered");
});

test("防重复并发：同一来源请求 inflight 期间禁止重复提交", async () => {
  let inFlight = false;
  let calls = 0;
  const mockApi = async () => {
    calls++;
    inFlight = true;
    await new Promise((r) => setTimeout(r, 50));
    inFlight = false;
    return { ok: true, state: "answered" };
  };

  const harness = loadPendingActionHarness(mockApi);
  const p1 = harness.fn.pendingReviewAction("answer-question", encodeURIComponent("src_rapid"), "query");
  const p2 = harness.fn.pendingReviewAction("answer-question", encodeURIComponent("src_rapid"), "query");
  await Promise.all([p1, p2]);
  assert.equal(calls, 1, "同一来源在请求在途期间必须阻断并发重复点击");
});

test("隔离真实后端 SQLite 幂等性：响应丢失重试仅生成一条审计记录", () => {
  const script = `
import json, tempfile, shutil
from pathlib import Path
from life_logger.review_questions import answer_review_question, _db

tmp = Path(tempfile.mkdtemp(prefix="idempotency-test-"))
try:
    op_id = "op_idempotent_test_123"
    # 第一次提交（模拟写入成功但随后网络断开响应丢失）
    res1 = answer_review_question(tmp, source_id="src_test_idem", operation="query", op_id=op_id)
    assert res1["ok"] is True
    assert res1["idempotent"] is False

    # 第二次重试（复用同一 op_id）
    res2 = answer_review_question(tmp, source_id="src_test_idem", operation="query", op_id=op_id)
    assert res2["ok"] is True
    assert res2["idempotent"] is True

    # 查数据库验证 question_audit 只有一条记录
    conn = _db(tmp, create=False)
    audit_rows = conn.execute("SELECT * FROM question_audit WHERE source_id='src_test_idem'").fetchall()
    conn.close()
    assert len(audit_rows) == 1, f"期望只有 1 条审计，实际有 {len(audit_rows)} 条"
    print("BACKEND_IDEMPOTENT_OK")
finally:
    shutil.rmtree(tmp)
`;
  const pythonBin = path.join(root, ".venv", "bin", "python");
  const pyOut = execFileSync(pythonBin, ["-c", script], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: path.join(root, "src") },
    encoding: "utf8"
  });
  assert.match(pyOut, /BACKEND_IDEMPOTENT_OK/);
});

test("真实合同对齐：支持 1号扁平字段与旧嵌套字段透传恢复撤销入口", () => {
  const start = appSrc.indexOf("// ── 生活记录优先的审核首版");
  const end = appSrc.indexOf("function pendingReviewBodyHtml(");
  assert.ok(start > 0 && end > start);

  const factory = new Function("escapeHtml", "pendingReviewState", `
    ${appSrc.slice(start, end)}
    return { pendingQuestionDrawerHtml };
  `);

  const api = factory((s) => String(s ?? ""), { answeredQuestions: {} });

  // 1. 真实 1号后端 GET /api/pending-sources 扁平投影字段
  const itemRealFlat = {
    source_id: "src_real_flat_1",
    quote: "查一下天气",
    reason: "model_no_event_but_directive",
    review_question_state: "answered",
    review_question_operation: "query",
    review_question_op_id: "op_flat_123",
    review_question_undoable: true,
    job_id: "job_1"
  };
  const htmlFlat = api.pendingQuestionDrawerHtml(itemRealFlat);
  assert.match(htmlFlat, /已记录来源判断：<strong>answered<\/strong> \(query\)/, "必须能根据1号真实扁平字段恢复撤销栏");
  assert.match(htmlFlat, /撤销上一次判断/);

  // 2. 真实 1号扁平字段：muted 状态
  const itemFlatMuted = {
    source_id: "src_real_flat_2",
    quote: "这是一条广告",
    reason: "unknown_speaker",
    review_question_state: "muted",
    review_question_operation: "media",
    review_question_undoable: true,
    job_id: "job_1"
  };
  const htmlMuted = api.pendingQuestionDrawerHtml(itemFlatMuted);
  assert.match(htmlMuted, /已记录来源判断：<strong>muted<\/strong> \(media\)/);
  assert.match(htmlMuted, /撤销上一次判断/);

  // 3. 真实 1号扁平字段：review_question_undoable=false 时不渲染撤销按钮
  const itemNotUndoable = {
    source_id: "src_real_flat_3",
    quote: "已经锁定的条目",
    reason: "model_no_event_but_directive",
    review_question_state: "answered",
    review_question_operation: "query",
    review_question_undoable: false,
    job_id: "job_1"
  };
  const htmlNotUndoable = api.pendingQuestionDrawerHtml(itemNotUndoable);
  assert.ok(!htmlNotUndoable.includes("pq-undo-bar"), "不可撤销条目不得渲染撤销栏");

  // 4. 真实 1号扁平字段：review_question_state='unknown' (缺省状态) 不渲染撤销栏
  const itemUnknown = {
    source_id: "src_real_flat_4",
    quote: "全新未回答条目",
    reason: "model_no_event_but_directive",
    review_question_state: "unknown",
    review_question_undoable: false,
    job_id: "job_1"
  };
  const htmlUnknown = api.pendingQuestionDrawerHtml(itemUnknown);
  assert.ok(!htmlUnknown.includes("pq-undo-bar"), "未回答 unknown 状态不得渲染撤销栏");

  // 5. 旧嵌套对象兼容 (review_question 嵌套对象)
  const itemLegacyNested = {
    source_id: "src_legacy_nested",
    quote: "旧格式测试",
    reason: "model_no_event_but_directive",
    review_question: { state: "answered", operation: "query" },
    job_id: "job_1"
  };
  const htmlLegacy = api.pendingQuestionDrawerHtml(itemLegacyNested);
  assert.match(htmlLegacy, /已记录来源判断：<strong>answered<\/strong> \(query\)/, "必须保持对旧嵌套 review_question 对象的兼容");

  // 6. 旧 question_state 字符串兼容
  const itemLegacyState = {
    source_id: "src_legacy_state",
    quote: "旧格式测试2",
    reason: "model_no_event_but_directive",
    question_state: "answered",
    job_id: "job_1"
  };
  const htmlLegacyState = api.pendingQuestionDrawerHtml(itemLegacyState);
  assert.match(htmlLegacyState, /已记录来源判断：<strong>answered<\/strong>/, "必须保持对旧 question_state 字符串字段的兼容");
});
