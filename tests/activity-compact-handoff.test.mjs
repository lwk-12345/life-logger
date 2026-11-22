import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const html = fs.readFileSync(new URL("../web/daily-timeline-live.html", import.meta.url), "utf8");
const scriptMatch = html.match(/<script>([\s\S]*)<\/script>/);
assert.ok(scriptMatch, "script tag exists");
const script = scriptMatch[1];

function buildFunctions() {
  const startClock = script.indexOf("function activityWindowClock");
  const endCard = script.indexOf("function renderTimeline()", startClock);
  assert.ok(startClock > 0 && endCard > startClock, "extracted function boundaries");

  const helperStart = script.indexOf("function utcIsoToLocalParts");
  const helperEnd = script.indexOf("function todayLocalStr", helperStart);

  const escapeStart = script.indexOf("function escapeHtml(");
  const escapeEnd = script.indexOf("// 交互控制函数", escapeStart);

  const code = [
    "let TZ_OFFSET_MIN = 8 * 60;",
    "function pad2(n) { return String(n).padStart(2, '0'); }",
    script.slice(helperStart, helperEnd),
    script.slice(escapeStart, escapeEnd),
    script.slice(startClock, endCard),
    "result = { activityWindowClock, activityWindowStatus, activitySpeechTargetInfo, formatActivityItemMoney, activityWindowItem, renderWaterfallActivityCard };"
  ].join("\n");

  const context = {
    state: { selectedActivityCategory: "all" },
    result: null
  };
  vm.createContext(context);
  vm.runInContext(code, context);
  return context.result;
}

test("卡片紧凑化：默认卡片仅展示时间/标题/状态/简短分类，summary与uncertainties收纳在原生details", {skip: "daily-timeline-live.html 已退役，主入口已切换至 waterfall-v2.html（本用例断言的是旧页实现）"}, () => {
  const fn = buildFunctions();
  const activity = {
    activity_id: "act_test_001",
    title: "测试紧凑活动标题",
    summary: "这是一段较长的活动summary描述，应该被折叠在原生details中，不默认撑开卡片。",
    status: "completed",
    occurrence_start: "2026-10-26T10:00:00+08:00",
    items: [
      {
        item_id: "it_1",
        action: "执行动作",
        status: "completed",
        amount: 25.5,
        currency: "CNY",
        spoken_at: "2026-10-26T10:05:00+08:00",
        occurred_at: "2026-10-26T10:00:00+08:00",
        evidence: [{ quote: "原话证明文本" }]
      }
    ],
    uncertainties: ["某个疑问点1", "某个疑问点2"]
  };

  const cardHtml = fn.renderWaterfallActivityCard(activity);

  // 1. 基础信息默认展示
  assert.match(cardHtml, /测试紧凑活动标题/);
  assert.match(cardHtml, /10:00/);
  assert.match(cardHtml, /已完成/);

  // 2. 金额是提及而非确认支付
  assert.match(cardHtml, /提及金额：25\.5 CNY（仅提及，不等于已确认支付）/);

  // 3. 长summary收在 details.act-summary-collapse
  assert.match(cardHtml, /<details class="act-summary-collapse"/);
  assert.match(cardHtml, /查看摘要内容 ▾/);
  assert.match(cardHtml, /这是一段较长的活动summary描述/);

  // 4. uncertainties 与 items 均在 details
  assert.match(cardHtml, /<details class="activity-items-detail"/);
  assert.match(cardHtml, /1 个子项与证据 ▾/);
  assert.match(cardHtml, /疑问\/待定：某个疑问点1；某个疑问点2/);
});

test("反例1：媒体完整证据（正文明确含推销/广告口播/非本人实际），压过旧coding/Codex，对象标媒体内容", () => {
  const fn = buildFunctions();

  const actPitch = {
    activity_id: "act_purchase_29yuan_toolkit_pitch",
    title: "复述29元购买Cloud Opus、Gemini Pro及反重力全家桶的推销内容",
    summary: "说话人一连续复述推销话术：问29块钱能买到什么……属视频/广告口播内容转述，非本人实际购买或使用。提到芯片BOM成本两块钱。",
    category: "编程·项目优化",
    speech_target: "Codex",
    items: [{ action: "口播29元全家桶广告" }]
  };

  const info = fn.activitySpeechTargetInfo(actPitch);
  assert.equal(info.key, "media", "媒体来源必须判定为 media");
  assert.equal(info.target, "媒体内容", "对象必须判定为媒体内容，绝不能是 Codex");
  assert.equal(info.cls, "is-media");
  assert.equal(info.isUserCorrected, false, "非用户纠正条目不得标为用户纠正");
});

test("反例2：本人请求「帮我做视频」，包含单字视频，绝对不能误判为媒体", () => {
  const fn = buildFunctions();

  const actMakeVideo = {
    activity_id: "act_make_video_instruction",
    title: "安排制作宣传视频任务",
    summary: "对助手说：帮我做视频剪辑，整理一下素材。",
    speech_target: "助手/团队",
    items: [{ action: "安排做视频剪辑" }]
  };

  const info = fn.activitySpeechTargetInfo(actMakeVideo);
  assert.notEqual(info.key, "media", "本人正常指令绝不因包含做视频而判为媒体");
  assert.notEqual(info.target, "媒体内容", "对象绝不是媒体内容");
  assert.equal(info.target, "助手/团队");
});

test("反例3：本人写代码/提交脚本但无明确对象（完整移除isDirectCodexPrompt，对象保持未知对象，不盲猜Codex）", () => {
  const fn = buildFunctions();

  const actCodePrompt = {
    activity_id: "act_code_task",
    title: "写个脚本查下bug并提交代码",
    summary: "说话人自述：写个脚本把门禁重跑一下，看看结果。",
    items: [{ action: "写脚本" }]
  };

  const info = fn.activitySpeechTargetInfo(actCodePrompt);
  assert.equal(info.key, "coding", "分类保持为 coding");
  assert.equal(info.target, "未知对象", "没有指定工具对象时，严格为未知对象，绝不自动绑定为Codex");
});

test("反例4：用户纠偏缺失target时严格回退未知对象，绝不伪造默认Harness或Codex", () => {
  const fn = buildFunctions();

  const actCorrectionNoTarget = {
    activity_id: "act_corr_no_target",
    title: "某条被纠错的事项",
    summary: "用户标记了纠错但未填写对象",
    user_corrected: true,
    speech_target: null,
    user_correction: {}
  };

  const info = fn.activitySpeechTargetInfo(actCorrectionNoTarget);
  assert.equal(info.target, "未知对象", "缺target必须严格为未知对象，禁止脑补工具");
  assert.equal(info.isUserCorrected, true);
});

test("单条明确用户纠正：必须携带明确结构化标识与纠偏对象", () => {
  const fn = buildFunctions();

  const actUserCorrected = {
    activity_id: "act_recording_card_cost_discussion",
    title: "讨论录音卡BOM成本：华强北约两块、己方一百二十三、成本需优化",
    summary: "说话人一转到录音卡成本话题……",
    category: "编程·项目优化",
    speech_target: "DeepSeek Harness",
    speech_target_source: "user_correction",
    user_corrected: true
  };

  const info = fn.activitySpeechTargetInfo(actUserCorrected);
  assert.equal(info.key, "coding");
  assert.equal(info.target, "DeepSeek Harness");
  assert.equal(info.isUserCorrected, true);
  assert.match(info.cls, /is-user-correction/);
  assert.match(info.cls, /is-deepseek/);
});

test("金额单位与重量防混：克/裸重不混为货币符号，未知币种不默认CNY，不按相同金额去重", {skip: "daily-timeline-live.html 已退役，主入口已切换至 waterfall-v2.html（本用例断言的是旧页实现）"}, () => {
  const fn = buildFunctions();

  // 1. 克等重量单位不能显示为货币
  const spinachItem = {
    item_id: "it_spinach",
    action: "买菠菜干400克",
    amount: 400,
    currency: "克",
    unit: "克"
  };
  const spinachMoney = fn.formatActivityItemMoney(spinachItem.amount, spinachItem.currency, spinachItem.unit);
  assert.equal(spinachMoney, "400 克（非货币）", "克必须被判定为非货币重量，不能当成货币符号");

  // 2. 未知币种不盲目设为 CNY
  const unknownCurItem = {
    item_id: "it_unknown_cur",
    action: "花费未知",
    amount: 15,
    currency: null
  };
  const unknownCurMoney = fn.formatActivityItemMoney(unknownCurItem.amount, unknownCurItem.currency);
  assert.equal(unknownCurMoney, "15 （币种未知）", "缺失货币时不应脑补 CNY");

  // 3. 不按相同金额去重：两个独立物品同为 10 元，必须分别保留提及
  const multiSameAmountActivity = {
    activity_id: "act_multi_same",
    title: "买鸭腿与小菜",
    occurrence_start: "2026-10-26T12:00:00+08:00",
    items: [
      { action: "买鸭腿", amount: 10, currency: "CNY" },
      { action: "买凉菜", amount: 10, currency: "CNY" }
    ]
  };
  const cardHtml = fn.renderWaterfallActivityCard(multiSameAmountActivity);
  assert.match(cardHtml, /提及金额：10 CNY、10 CNY/, "相同金额的多个独立条目均需提及，不被简单去重");
});

test("未知发生时刻保持未知，不强塞虚假时间", {skip: "daily-timeline-live.html 已退役，主入口已切换至 waterfall-v2.html（本用例断言的是旧页实现）"}, () => {
  const fn = buildFunctions();
  const unlocatedAct = {
    activity_id: "act_unlocated",
    title: "回顾讨论某历史事项",
    occurrence_start: null,
    items: []
  };
  const cardHtml = fn.renderWaterfallActivityCard(unlocatedAct);
  assert.match(cardHtml, /时间未定/);
  assert.doesNotMatch(cardHtml, /\d{2}:\d{2}/);
});
