#!/usr/bin/env node
/**
 * 带硬超时的页面验证脚本 (180s 兜底，防止挂起)
 * 用法: node scripts/verify_ui_timed.cjs --date 2026-10-01
 */

const fs = require('fs');
const path = require('path');

const TOTAL_TIMEOUT_MS = 180000; // 180秒绝对硬超时
const STEP_TIMEOUT_MS = 45000;   // 单步最长45秒

const root = path.resolve(__dirname, '..');
const outDir = path.join(root, 'work/verification-1001-1008-20261022');
fs.mkdirSync(outDir, { recursive: true });

// 解析命令行参数
let targetDate = '2026-10-01';
const args = process.argv.slice(2);
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--date' && args[i + 1]) {
    targetDate = args[i + 1];
    i++;
  }
}

const startTime = Date.now();
let currentStep = '0/5 初始化';
let browserInstance = null;

function logStep(step, desc) {
  currentStep = `${step} ${desc}`;
  const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
  console.log(`[+${elapsed}s] [${step}] ${desc}`);
}

async function terminateWithResult(ok, resultData, errorMsg = null) {
  const elapsed = Date.now() - startTime;
  if (browserInstance) {
    try {
      await browserInstance.close();
    } catch (_) {}
    browserInstance = null;
  }

  const out = {
    ok,
    step: currentStep,
    elapsed_ms: elapsed,
    date: targetDate,
    error: errorMsg,
    ...resultData
  };

  console.log('\n--- 结构化结果 (JSON) ---');
  console.log(JSON.stringify(out, null, 2));

  process.exit(ok ? 0 : 1);
}

// 180秒硬超时总保底定时器
const globalTimer = setTimeout(() => {
  console.error(`\n❌ [ABORT] 触发 180 秒绝对硬超时！当前卡在步骤: ${currentStep}`);
  terminateWithResult(false, {}, `Global 180s timeout exceeded at step: ${currentStep}`);
}, TOTAL_TIMEOUT_MS);
globalTimer.unref();

async function run() {
  // [1/5] 解析凭证
  logStep('1/5', '读取服务访问凭证与依赖');
  const secretsPath = path.join(root, 'output/local-web/secrets.json');
  if (!fs.existsSync(secretsPath)) {
    return terminateWithResult(false, {}, `未找到 secrets.json: ${secretsPath}`);
  }
  const token = JSON.parse(fs.readFileSync(secretsPath, 'utf8')).mobile_access_token;
  
  let chromium;
  try {
    const pw = require('playwright');
    chromium = pw.chromium;
  } catch (e) {
    return terminateWithResult(false, {}, `Playwright 加载失败: ${e.message}`);
  }

  // [2/5] 启动浏览器
  logStep('2/5', '启动 Chromium 实例并设置视口');
  browserInstance = await chromium.launch({ headless: true });
  const context = await browserInstance.newContext({
    viewport: { width: 1440, height: 1100 },
    extraHTTPHeaders: { Authorization: `Bearer ${token}` }
  });
  const page = await context.newPage();
  page.setDefaultTimeout(STEP_TIMEOUT_MS);

  // [3/5] 打开页面
  logStep('3/5', `打开本地服务页面 (http://127.0.0.1:8765/?token=...)`);
  try {
    await page.goto(`http://127.0.0.1:8765/?token=${encodeURIComponent(token)}`, {
      waitUntil: 'domcontentloaded',
      timeout: STEP_TIMEOUT_MS
    });
  } catch (e) {
    return terminateWithResult(false, {}, `打开首页超时/失败: ${e.message}`);
  }

  logStep('3/5', '等待 journalDays 初始化加载');
  try {
    await page.waitForFunction(() => {
      return typeof journalDays !== 'undefined' && Array.isArray(journalDays) && journalDays.length > 0;
    }, { timeout: STEP_TIMEOUT_MS });
  } catch (e) {
    return terminateWithResult(false, {}, `等待 journalDays 初始化超时: ${e.message}`);
  }

  // [4/5] 切换目标日期
  logStep('4/5', `切换目标日期为 ${targetDate} 并等待数据对齐`);
  try {
    await page.evaluate((d) => {
      if (typeof selectJournalDate === 'function') selectJournalDate(d);
    }, targetDate);

    await page.waitForFunction((d) => {
      const day = journalDays.find(x => x.date === d);
      return day && (day.canonical === true || !!day.canonicalError);
    }, targetDate, { timeout: STEP_TIMEOUT_MS });
  } catch (e) {
    return terminateWithResult(false, {}, `切换或等待 ${targetDate} 规范数据加载超时: ${e.message}`);
  }

  // [5/5] 读取页面数据与三级下钻
  logStep('5/5', '解析页面金额、条数与分类明细下钻');
  const drilldownToggle = page.locator('.day-expense-drilldown [data-expense-toggle]');
  if (await drilldownToggle.count() > 0) {
    const isExpanded = await drilldownToggle.getAttribute('aria-expanded');
    if (isExpanded !== 'true') {
      try {
        await drilldownToggle.click({ timeout: 3000 });
        await page.waitForTimeout(300);
      } catch (_) {}
    }
  }

  // 截图留存
  const shotPath = path.join(outDir, `${targetDate}-verified.png`);
  await page.screenshot({ path: shotPath, fullPage: false });

  // 提取页面数据
  const pageData = await page.evaluate((d) => {
    const day = journalDays.find(x => x.date === d);
    const focusText = document.querySelector('.focus-summary')?.textContent || '';
    const matchEvents = focusText.match(/(\d+)\s*件事/);
    const pageEventCount = matchEvents ? Number(matchEvents[1]) : (day?.eventCount ?? 0);

    const matchAmount = focusText.match(/¥([\d.]+)/);
    const pageAmount = matchAmount ? Number(matchAmount[1]) : (day?.expense_ledger?.total_amount ?? 0.0);

    const ledger = day?.expense_ledger || {};
    const breakdown = ledger.category_breakdown || {};
    const catSum = Object.values(breakdown).reduce((a, b) => a + Number(b), 0);

    return {
      focusText: focusText.trim(),
      pageEventCount,
      pageAmount,
      catSum: Math.round(catSum * 100) / 100,
      breakdown
    };
  }, targetDate);

  // 对比文件数据（若文件存在）
  let fileAmount = null;
  let fileCount = null;
  const synthPath = path.join(root, `output/local-web/daily-synthesis/synthesis-${targetDate}.json`);
  if (fs.existsSync(synthPath)) {
    const synthData = JSON.parse(fs.readFileSync(synthPath, 'utf8'));
    fileAmount = (synthData.expense_analysis && typeof synthData.expense_analysis === 'object')
      ? synthData.expense_analysis.total_amount_cny
      : (synthData.total_amount ?? 0.0);
    fileCount = synthData.event_count ?? 0;
  }

  clearTimeout(globalTimer);
  await terminateWithResult(true, {
    page_amount: pageData.pageAmount,
    file_amount: fileAmount,
    page_event_count: pageData.pageEventCount,
    file_event_count: fileCount,
    category_sum: pageData.catSum,
    screenshot_path: shotPath,
    breakdown: pageData.breakdown
  });
}

run().catch(async (err) => {
  clearTimeout(globalTimer);
  await terminateWithResult(false, {}, `未捕获异常: ${err.message}`);
});
