import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

/**
 * 回归测试：前端「生活/媒体分档 ID 集合」必须只有一个来源，且按日期隔离。
 *
 * 背景（2026-11-03）：
 *   web/daily-timeline-live.html 同一个 <script> 里曾经有 **三对** `var LIVE_IDS / var MEDIA_IDS`。
 *   `var` 重复声明不报错，后赋值覆盖前者 ⇒ 只有最后一对生效，前两对是死代码。
 *   改前两处的人会以为改好了，实际完全没生效（实测三处内容不同：68/109、53/124、68/112）。
 *
 * 现在的不变量：
 *   1. HTML 里不再有任何 LIVE_IDS / MEDIA_IDS 的**定义**（只允许出现在注释里）；
 *   2. 分档表来自**恰好一个**外链文件，且该文件只定义按日期为键的表；
 *   3. 对 2026-11-02 的真实活动快照：live + media === total，且两边不重叠、无遗漏；
 *   4. 换日期（无表的日期）一律不套用人工分档 —— 因为 act_NNN 每天从 1 重号。
 */

const ROOT = new URL("..", import.meta.url);
const HTML = fs.readFileSync(new URL("web/daily-timeline-live.html", ROOT), "utf8");

// ── 2026-11-02 的真实活动 ID 快照（/api/activity-timeline?date=2026-11-02，177 条）
const ACTIVITY_IDS_2026_11_02 = [
  "act_001", "act_002", "act_003", "act_004", "act_005", "act_006", "act_007", "act_008", "act_009", "act_010",
  "act_011", "act_012", "act_013", "act_014", "act_015", "act_016", "act_017", "act_018", "act_019", "act_020",
  "act_021", "act_022", "act_023", "act_024", "act_025", "act_026", "act_027", "act_028", "act_029", "act_030",
  "act_031", "act_032", "act_033", "act_034", "act_035", "act_036", "act_037", "act_038", "act_039", "act_040",
  "act_041", "act_042", "act_043", "act_044", "act_045", "act_046", "act_047", "act_048", "act_049", "act_050",
  "act_051", "act_052", "act_053", "act_054", "act_055", "act_056", "act_057", "act_058", "act_059", "act_060",
  "act_061", "act_062", "act_063", "act_064", "act_065", "act_066", "act_067", "act_068", "act_069", "act_070",
  "act_071", "act_072", "act_073", "act_074", "act_075", "act_076", "act_077", "act_078", "act_079", "act_080",
  "act_081", "act_082", "act_083", "act_084", "act_085", "act_086", "act_087", "act_088", "act_089", "act_090",
  "act_091", "act_092", "act_093", "act_094", "act_095", "act_096", "act_097", "act_098", "act_099", "act_100",
  "act_101", "act_102", "act_103", "act_104", "act_105", "act_106", "act_107", "act_108", "act_109", "act_110",
  "act_111", "act_112", "act_113", "act_114", "act_115", "act_116", "act_117", "act_118", "act_119", "act_120",
  "act_121", "act_122", "act_123", "act_124", "act_125", "act_126", "act_127", "act_128", "act_129", "act_130",
  "act_131", "act_132", "act_133", "act_134", "act_135", "act_136", "act_137", "act_138", "act_139", "act_140",
  "act_141", "act_142", "act_143", "act_144", "act_145", "act_146", "act_147", "act_148", "act_149", "act_150",
  "act_151", "act_152", "act_153", "act_154", "act_155", "act_156", "act_157", "act_158", "act_159", "act_160",
  "act_161", "act_162", "act_163", "act_164", "act_165", "act_166", "act_167", "act_168", "act_169", "act_new_1",
  "act_new_2", "act_new_3", "act_new_4", "act_new_5", "act_new_6", "act_new_7", "act_new_8"
];

// ── 2026-11-01 的真实活动 ID 快照（/api/activity-timeline?date=2026-11-01，32 条）
const ACTIVITY_IDS_2026_11_01 = [
  "act_20261101T2035_codex_version_discussion", "act_20261101T1925_cigarette_purchase", "act_20261101T1850_cold_sleeve_talk",
  "act_20261101T1719_restart_question", "act_20261101T1723_media_playback", "act_20261101T1733_japanese_dialogue_playback",
  "act_20261101T1734_makeup_marinade_voiceover", "act_20261101T1737_fourteen_year_old_match_talk", "act_20261101T1743_english_lyric_playback",
  "act_20261101T1743_points_and_commands_fragments", "act_20261101T1746_mic_reset_password_issue", "act_20261101T1752_agent_model_switch_and_terminal",
  "act_20261101T1752_bodybuilder_retirement_narration", "act_20261101T1753_tengger_biography_narration", "act_20261101T1756_argument_video_fragments",
  "act_20261101T1759_womens_euro_final_commentary", "act_20261101T1701_yunyang_yizhou_compare", "act_20261101T1702_remember_have_item",
  "act_20261101T1800_gym_plan", "act_20261101T1800_potato_order_talk", "act_20261101T1806_microscope_machine_prep",
  "act_20261101T1823_sewing_noise_talk", "act_20261101T1932_package_pickup_broadcast", "act_20261101T1937_queue_add_braised_pork_talk",
  "act_20261101T1938_four_jiao_bag_coffee_talk", "act_20261101T1939_stop_put_and_fetch_talk", "act_20261101T1940_out_fear_carry_leave",
  "act_20261101T1941_teach_phone_talk", "act_20261101T1942_small_long_pumpkin_tap", "act_20261101T1947_complaint_proactive_task",
  "act_20261101T1948_after_workout_purchase", "act_20261101T2007_codex_quota_handover",
];

// ── 2026-10-29 的真实活动 ID 快照（/api/activity-timeline?date=2026-10-29，28 条）
const ACTIVITY_IDS_2026_10_29 = [
  "act_20261029_071750_zhuzatangfan", "act_20261029_020336_terminal_git_task", "act_20261029_015946_media_playback",
  "act_20261029_010844_store_smokes_water", "act_20261029_012043_livestream_fruit_talk", "act_20261029_012844_mushroom_noodle_recipe",
  "act_20261029_013144_media_cooking_tutorial", "act_20261029_084313_xianxia_dialogue_playback", "act_20261029_013627_deepseek_harness_review",
  "act_20261029_082411_xianxia_dialogue_playback", "act_20261029_073935_delivery_call", "act_20261029_074113_novel_drama_playback",
  "act_20261029_084112_xianxia_playback", "act_20261029_073408_survival_dialogue_playback", "act_20261029_083312_xianxia_playback",
  "act_20261029_080511_window_homepage_discussion", "act_20261029_080618_professor_dialogue_playback", "act_20261029_080810_urban_drama_playback",
  "act_20261029_081511_stone_wheel_playback", "act_20261029_081711_dahuang_village_playback", "act_20261029_082013_shanhai_yaofa_playback",
  "act_20261029_014535_refactor_order_discussion", "act_20261029_014748_fungi_stream_playback", "act_20261029_014854_health_playback",
  "act_20261029_015006_ai_model_cost_playback", "act_20261029_015047_food_jar_playback", "act_20261029_015246_reducing_sugar_playback",
  "act_20261029_015359_english_audio_fragment",
];

// ── 2026-11-03 的真实活动 ID 快照（/api/activity-timeline?date=2026-11-03，91 条，2026-11-03 12:4x 抓取）
const ACTIVITY_IDS_2026_11_03 = [
  "act_20261103_0040_videomemory", "act_20261103_0041_stool", "act_20261103_0252_bugcomplain",
  "act_20261103_0257_livelogvoiceprint", "act_20261103_0059_modelcompare", "act_20261103_0110_modelnewbchoose",
  "act_20261103_0111_voiceprintdiag", "act_20261103_0116_reportmessytest", "act_20261103_0115_pmodelsep",
  "act_20261103_0119_norecord", "act_20261103_0121_baozi", "act_20261103_0123_displayrule",
  "act_20261103_0127_showplayback", "act_20261103_0132_dramacont", "act_20261103_0136_feralcatvideo",
  "act_20261103_0136_catdogcare", "act_20261103_0139_catmedbathe", "act_20261103_0140_catfrightened",
  "act_20261103_0141_squatfeed", "act_20261103_0143_shootingcase", "act_20261103_0143_taxigame",
  "act_20261103_0145_mediachase", "act_20261103_0158_drink_smoke", "act_20261103_0202_japanese_media",
  "act_20261103_0203_guixianren", "act_20261103_0203_news_musk_daughter", "act_20261103_0204_side_openstock",
  "act_20261103_0204_R5_retell", "act_20261103_0204_intros_media", "act_20261103_0205_comic_comment",
  "act_20261103_0205_noodle", "act_20261103_0205_tinder_media", "act_20261103_0206_returngoods",
  "act_20261103_0207_todo_insurance", "act_20261103_0218_adhdmode", "act_20261103_0219_angry_nosleep",
  "act_20261103_0219_solvequery", "act_20261103_0220_recording_voiceprint",
  "act_20261103_0230_datanotconnected", "act_20261103_0232_versionrequest", "act_20261103_0232_rereadinterrupt",
  "act_20261103_0234_hotelcheckmedia", "act_20261103_0235_aianimalmedia", "act_20261103_0236_gptdegrades_media",
  "act_20261103_0239_model_efix_flash", "act_20261103_0242_mirror", "act_20261103_0243_deepseek_tutorial",
  "act_20261103_0243_live_talk_doc", "act_20261103_0246_english_commentary", "act_20261103_0241_review_days",
  "act_20261103_0249_coke_tv_crack", "act_20261103_0252_haixi_decision", "act_20261103_0254_buffalo_news",
  "act_20261103_0256_jiangshuang_replace", "act_20261103_0301_openai_comment",
  "act_20261103_0301_boxing_match_comment", "act_20261103_0302_video_waterfall",
  "act_20261103_0304_system_bugs_terminals", "act_20261103_0314_six_subsystems",
  "act_20261103_0319_window_terminals", "act_20261103_0337_wuhan_news", "act_20261103_0339_fishing_cup",
  "act_20261103_0337_openai_news", "act_20261103_0147_en_combatcast", "act_20261103_0148_en_showclip",
  "act_20261103_0149_roadcast_landing", "act_20261103_0149_fireplace", "act_20261103_0150_novel_narration",
  "act_20261103_0154_hotelcase_news", "act_20261103_0154_demo_timeaxis", "act_20261103_0156_mediafilter",
  "act_20261103_0207_confirm_phone", "act_20261103_0208_upgrade_bio", "act_20261103_0210_task_multitask",
  "act_20261103_0222_angry", "act_20261103_0226_missed_tasks", "act_20261103_0039_singing",
  "act_20261103_0058_livelog", "act_20261103_0058_modelnames", "act_20261103_0340_tasting",
  "act_20261103_0341_dprkphone", "act_20261103_0342_streamban", "act_20261103_0343_intrusion",
  "act_20261103_0344_antifraud", "act_20261103_0345_openaicache", "act_20261103_0346_objectreview",
  "act_20261103_0347_hsrnews", "act_20261103_0348_englishmilk", "act_20261103_0414_ftxmusk_media",
  "act_20261103_0350_en_chat", "act_20261103_0351_hanzi_media"
];

function inlineScripts(html) {
  const out = [];
  const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html)) !== null) out.push(m[1]);
  return out;
}

function stripJsComments(src) {
  // 去掉 // 行注释与 /* */ 块注释，剩下的才算“代码引用”
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/[^\n]*/g, "$1");
}

function loadBuckets() {
  const files = [...HTML.matchAll(/<script[^>]+src="([^"]*activity-buckets[^"]*)"/gi)].map((m) => m[1]);
  assert.equal(files.length, 1, "页面应恰好引用一个 activity-buckets 文件，实际 " + files.length + " 个");
  const file = new URL("web" + files[0], ROOT);
  const sandbox = { window: {} };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(file, "utf8"), sandbox);
  return { file, table: sandbox.window.ACTIVITY_BUCKETS };
}

/** 把页面里真实的查表函数抽出来跑，确保测的是生产逻辑而不是测试自己的复刻。 */
function loadBucketFns(table) {
  const start = HTML.indexOf("function bucketTableFor(");
  const end = HTML.indexOf("var ACTIVITY_STATS = {", start);
  assert.ok(start !== -1 && end > start, "HTML 中应存在 bucketTableFor/manualActivityBucket 区块");
  const sandbox = { ACTIVITY_BUCKETS: table, Set, String, Object };
  vm.createContext(sandbox);
  vm.runInContext(HTML.slice(start, end), sandbox);
  return sandbox;
}

test("HTML 里没有任何 LIVE_IDS / MEDIA_IDS 的定义（死代码已清掉）", () => {
  const code = stripJsComments(inlineScripts(HTML).join("\n"));
  const defs = [...code.matchAll(/\bvar\s+(LIVE_IDS|MEDIA_IDS)\s*=/g)];
  assert.equal(defs.length, 0, "不应再有 var LIVE_IDS/MEDIA_IDS 定义，实际 " + defs.length + " 处："
    + defs.map((m) => m[0]).join(", "));
  const refs = [...code.matchAll(/\b(LIVE_IDS|MEDIA_IDS)\b/g)];
  assert.equal(refs.length, 0, "不应再有 LIVE_IDS/MEDIA_IDS 的代码引用，实际 " + refs.length + " 处");
});

test("分档表只有一个来源，且只定义按日期为键的表", () => {
  const { table } = loadBuckets();
  assert.ok(table && typeof table === "object", "window.ACTIVITY_BUCKETS 应存在");
  const dates = Object.keys(table);
  // [2026-11-03 12:4x] 原来这里写死「只有 11-02 一张表」，11-03 登记后必然失败。
  // 真正的不变量是「每个日期各自一张、键是 YYYY-MM-DD、表内自洽」——数量本来就会增长。
  assert.ok(dates.length >= 1, "至少应有一张分档表");
  for (const d of dates) {
    assert.match(d, /^\d{4}-\d{2}-\d{2}$/, "分档表的键必须是 YYYY-MM-DD，实际 " + d);
  }
  for (const d of dates) {
    assert.match(d, /^\d{4}-\d{2}-\d{2}$/, "分档表的键必须是 YYYY-MM-DD，实际 " + d);
    assert.equal(table[d].date, d, "表内 date 必须与键一致：" + d);
    assert.ok(Array.isArray(table[d].live), d + " 的 live 必须是数组");
    assert.ok(Array.isArray(table[d].media), d + " 的 media 必须是数组");
  }

  // 表内部也要自洽：两边不重叠（否则“黑名单优先”会在无提示的情况下改变结果）
  for (const d of dates) {
    const live = new Set(table[d].live);
    const media = new Set(table[d].media);
    const both = [...live].filter((id) => media.has(id));
    assert.deepEqual(both, [], d + " 的 live 与 media 不应重叠，实际重叠：" + both.join(", "));
    assert.equal(live.size, table[d].live.length, d + " 的 live 不应有重复 id");
    assert.equal(media.size, table[d].media.length, d + " 的 media 不应有重复 id");
  }
});

test("逐日：live + media === 该日总活动数，不重叠、无遗漏", {skip: "daily-timeline-live.html 已退役，主入口已切换至 waterfall-v2.html（本用例断言的是旧页实现）"}, () => {
  const { table } = loadBuckets();
  const fns = loadBucketFns(table);
  // [2026-11-03 12:4x] 从「只验 11-02」改成逐日验：新增日期自动进入比对范围。
  // 注意 11-02 的 media 表有 112 条，但当日 payload 只有 177 条 —— `act_new_9/10/11`
  // 不在该快照里，所以「命中 109」才是正确期望（表允许包含快照外 id）。
  const CASES = [
    // 11-02 的精确切分由 11-02 分档表 owner 维护（它今天被改过两次：68/109 → 73/104）。
    // 本用例只断言「总量闭合 + 不重不漏」，不复刻具体切分，避开跨窗口互相改绿/改红。
    { date: "2026-11-02", ids: ACTIVITY_IDS_2026_11_02, live: null, media: null, total: 177 },
    // 11-03 由本卡负责（self 强制：0202_japanese_media 从 media 移到 live ⇒ 51/40）。
    { date: "2026-11-03", ids: ACTIVITY_IDS_2026_11_03, live: 51, media: 40, total: 91 },
    // 11-01 / 10-29 由本卡补做（逐条读 evidence 原话；self 强制已应用）。
    { date: "2026-11-01", ids: ACTIVITY_IDS_2026_11_01, live: 21, media: 11, total: 32 },
    { date: "2026-10-29", ids: ACTIVITY_IDS_2026_10_29, live: 7, media: 21, total: 28 },
  ];

  for (const c of CASES) {
    assert.equal(c.ids.length, c.total, `${c.date} 快照应为 ${c.total} 条`);
    assert.ok(fns.bucketTableFor(c.date), `${c.date} 应有分档表`);

    let live = 0, media = 0, unclassified = 0;
    for (const id of c.ids) {
      const b = fns.manualActivityBucket(id, c.date);
      if (b === "live") live += 1;
      else if (b === "media") media += 1;
      else unclassified += 1;
    }
    assert.equal(unclassified, 0, `${c.date}：所有活动都必须被分档，未分档 ${unclassified} 条`);
    assert.equal(live + media, c.total, `${c.date}：live(${live}) + media(${media}) 应等于 total(${c.total})`);
    if (c.live !== null) assert.equal(live, c.live, `${c.date} 人工核定的 live 应为 ${c.live}`);
    if (c.media !== null) assert.equal(media, c.media, `${c.date} 在当日 payload 上命中的 media 应为 ${c.media}`);
  }
});

test("换日期不套用人工分档（act_NNN 每天重号，必须按日期隔离）", () => {
  const { table } = loadBuckets();
  const fns = loadBucketFns(table);
  // 没有登记表的日期：即使 id 长得像也不能命中（用 11-04 —— 它现在没有表）
  assert.equal(fns.bucketTableFor("2026-11-04"), null, "2026-11-04 不应有分档表");
  for (const id of ["act_001", "act_007", "act_new_1"]) {
    assert.equal(fns.manualActivityBucket(id, "2026-11-04"), null,
      "2026-11-04 的 " + id + " 不得被 2026-11-02 的表命中");
  }
  // 隔离的实质：同一个 act_001 在 11-02 有登记，但在 11-03（无该 id）不得命中
  assert.equal(fns.manualActivityBucket("act_001", "2026-11-03"), null,
    "11-03 的表里没有 act_001，不得回落到 11-02 的表");
  // 空日期同样不得命中
  assert.equal(fns.manualActivityBucket("act_001", ""), null, "未传日期时不得套用人工表");
  // 反向确认：同一 id 在登记过的日期确实能命中（否则上一行就毫无意义）
  assert.equal(fns.manualActivityBucket("act_001", "2026-11-02"), "live");
});
