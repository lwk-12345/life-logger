const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const root = '$HOME/life-logger';
const outDir = path.join(root, 'work/verification-1001-1008-20261022');
fs.mkdirSync(outDir, { recursive: true });

const token = JSON.parse(fs.readFileSync(path.join(root, 'output/local-web/secrets.json'))).mobile_access_token;
const dates = ['2026-10-01', '2026-10-02', '2026-10-04', '2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08'];

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1100 },
    extraHTTPHeaders: { Authorization: 'Bearer ' + token }
  });
  const page = await context.newPage();
  page.on('console', msg => {
    const txt = msg.text();
    if (txt.includes('Error') || txt.includes('error')) console.log('PAGE LOG:', txt);
  });

  console.log('Navigating to http://127.0.0.1:8765/?token=' + token.slice(0, 6) + '...');
  await page.goto('http://127.0.0.1:8765/?token=' + token, { waitUntil: 'domcontentloaded' });

  // wait for jobs and journalDays
  console.log('Waiting for journalDays initialization...');
  await page.waitForFunction(() => {
    return typeof journalDays !== 'undefined' && Array.isArray(journalDays) && journalDays.length > 0;
  }, { timeout: 35000 });

  const totalDays = await page.evaluate(() => journalDays.length);
  console.log(`Initialized with ${totalDays} journal days.`);

  const results = [];

  for (const date of dates) {
    console.log(`\n--- Inspecting Date: ${date} ---`);
    // Select date
    await page.evaluate((d) => {
      selectJournalDate(d);
    }, date);

    // Wait for the day's canonical data to be loaded
    console.log(`  Waiting for canonical data for ${date}...`);
    await page.waitForFunction((d) => {
      const day = journalDays.find(x => x.date === d);
      return day && (day.canonical === true || !!day.canonicalError);
    }, date, { timeout: 25000 });

    await page.waitForTimeout(600);

    // Click to expand drilldown accordion if available
    const drilldownToggle = page.locator('.day-expense-drilldown [data-expense-toggle]');
    if (await drilldownToggle.count() > 0) {
      const isExpanded = await drilldownToggle.getAttribute('aria-expanded');
      if (isExpanded !== 'true') {
        try {
          await drilldownToggle.click({ timeout: 2000 });
          await page.waitForTimeout(300);
        } catch (e) {}
      }
    }

    // Take screenshot
    const shotPath = path.join(outDir, `${date}.png`);
    await page.screenshot({ path: shotPath, fullPage: false });
    console.log(`  Screenshot saved to ${shotPath}`);

    // Read synthesis file data
    const synthPath = path.join(root, `output/local-web/daily-synthesis/synthesis-${date}.json`);
    const synthData = JSON.parse(fs.readFileSync(synthPath, 'utf8'));
    const fileAmount = (synthData.expense_analysis && typeof synthData.expense_analysis === 'object')
      ? synthData.expense_analysis.total_amount_cny
      : (synthData.total_amount ?? 0.0);
    const fileCount = synthData.event_count ?? 0;

    // Read page DOM data
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
        breakdown,
        canonical: day?.canonical
      };
    }, date);

    const matchAmt = Math.abs(pageData.pageAmount - fileAmount) < 0.001;
    const matchCnt = pageData.pageEventCount === fileCount;
    const matchCat = (fileAmount === 0 && pageData.catSum === 0) || Math.abs(pageData.catSum - fileAmount) < 0.001;
    const ok = matchAmt && matchCnt && matchCat;

    results.push({
      date,
      pageAmount: pageData.pageAmount.toFixed(2),
      fileAmount: fileAmount.toFixed(2),
      pageCount: pageData.pageEventCount,
      fileCount: fileCount,
      catSum: pageData.catSum.toFixed(2),
      ok,
      shotPath
    });
  }

  console.log('\n========================================================================================');
  console.log('日期       | 页面金额   | 文件金额   | 页面条数 | 文件条数 | 分类合计   | 结论');
  console.log('-----------|------------|------------|----------|----------|------------|-------');
  for (const r of results) {
    const verdict = r.ok ? '✅ 一致' : '❌ 不一致';
    console.log(`${r.date} | ¥${r.pageAmount.padEnd(9)} | ¥${r.fileAmount.padEnd(9)} | ${String(r.pageCount).padEnd(6)}件 | ${String(r.fileCount).padEnd(6)}件 | ¥${r.catSum.padEnd(9)} | ${verdict}`);
  }
  console.log('========================================================================================\n');

  await browser.close();
})().catch(e => {
  console.error('ERROR:', e);
  process.exit(1);
});
