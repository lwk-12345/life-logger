import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {spawnSync} from 'node:child_process';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const viewer = fs.readFileSync(new URL('../web/viewer.js', import.meta.url), 'utf8');
const live = fs.readFileSync(new URL('../web/daily-timeline-live.html', import.meta.url), 'utf8');
function named(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, name);
  // These helpers have no brace characters inside strings other than balanced templates.
  const body = source.indexOf(') {', start) + 2;
  let depth = 0;
  for (let i = body; i < source.length; i++) {
    if (source[i] === '{') depth++;
    if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(name);
}
const definitions = source => [...source.matchAll(/^const INT_(?:DAY_KEY|MONTH_DAY|WEEKDAY) = .*;$/gm)].map(x => x[0]).join('\n');
// [2026-11-04] 时区标签由「北京时间」统一改成「UTC+8」（commit 7a446e4 统一 UTC+8 文案），
// 换算本身没变：16:00Z → 次日 00:00 +08。
for (const tz of ['America/Los_Angeles', 'UTC', 'Asia/Shanghai']) {
  test(`${tz}: midnight, historical keys, DST ranges and Beijing manual input`, () => {
    const script = `
      const assert = require('node:assert/strict');
      ${definitions(app)}
      ${['localDayDate','formatDayKey','formatMonthDay','formatWeekday','dateTimeInputValue','beijingDateTimeInputDate','localDateString','ensureStatisticsRange','setStatisticsRange','ensurePatternRange','setPatternRange','beijingManagementTime'].map(n => named(app,n)).join('\n')}
      assert.equal(formatDayKey(new Date('2026-10-25T15:59:59Z')), '2026-10-25');
      assert.equal(formatDayKey(new Date('2026-10-25T16:00:00Z')), '2026-10-26');
      assert.equal(formatWeekday('2026-10-26'), '周一');
      assert.equal(formatDayKey('2026-03-08'), '2026-03-08');
      assert.equal(formatMonthDay('2026-10-26'), '10-26');
      assert.equal(localDayDate('2026-02-30'), null);
      assert.equal(localDayDate('2026-2-03'), null);
      assert.equal(dateTimeInputValue(new Date('2026-10-25T16:00:00Z')), '2026-10-26T00:00');
      assert.equal(beijingManagementTime('2026-10-25T16:00:00Z'), '2026-10-26 00:00 UTC+8');
      assert.equal(beijingManagementTime('2026-10-25T16:00:00'), '时间时区未提供');
      let journalDays = [];
      const fields = {}; const $ = id => fields[id] ||= {value:''};
      for (const day of ['2026-03-09','2026-11-02','2026-10-26']) {
        journalDays = [{date:day}];
        for (const [fn,prefix] of [[ensureStatisticsRange,'stats'],[setStatisticsRange,'stats'],[ensurePatternRange,'patterns'],[setPatternRange,'patterns']]) {
          $(prefix+'-date-from').value = ''; $(prefix+'-date-to').value = '';
          fn(7);
          assert.equal($(prefix+'-date-to').value, day);
          assert.equal($(prefix+'-date-from').value, formatDayKey(new Date(localDayDate(day).getTime()-6*86400000)));
          assert.equal((localDayDate(day)-localDayDate($(prefix+'-date-from').value))/86400000,6);
        }
      }
      assert.equal(beijingDateTimeInputDate('2026-10-26T00:00').toISOString(), '2026-10-25T16:00:00.000Z');
      assert.equal(beijingDateTimeInputDate('2026-03-08T02:30').toISOString(), '2026-03-07T18:30:00.000Z');
      assert.equal(beijingDateTimeInputDate('2026-02-30T00:00'), null);
      assert.equal(beijingDateTimeInputDate('2026-10-26T24:00'), null);
      assert.equal(beijingDateTimeInputDate(''), null);

      { ${definitions(viewer)} ${['localDayDate','formatDayKey','formatWeekday'].map(n=>named(viewer,n)).join('\n')}
        assert.equal(formatDayKey('2026-10-26'),'2026-10-26'); assert.equal(formatWeekday('2026-10-26'),'周一'); }
    `;
    const result = spawnSync(process.execPath, ['-e', script], {env:{...process.env,TZ:tz},encoding:'utf8'});
    assert.equal(result.status,0,result.stderr);
  });
  test(`${tz}: JSON import keeps Beijing date and unknown occurrence off-axis`, () => {
    const script = `const assert=require('node:assert/strict'); const TZ_OFFSET_MIN=480;
      const pad2=n=>String(n).padStart(2,'0');
      ${['utcIsoToLocalParts','eventOccurrenceUnknown','handleFileUpload'].map(n=>named(live,n)).join('\n')}
      let EVENTS_DATA=[], UNLOCATED_EVENTS=[]; const state={}; const CATEGORY_META={routine:{}};
      const renderSceneLegend=()=>{},renderCategoryFilters=()=>{},renderTimeline=()=>{};
      const alert=message=>{throw new Error(message)};
      class FileReader {readAsText(file){this.onload({target:{result:JSON.stringify(file)}})}}
      handleFileUpload({target:{files:[[
        {event_id:'known',time:{absolute:'2026-10-25T16:00:00Z'}},
        {event_id:'unknown',evidence:{reported_at:'2026-10-25T16:00:00Z'}},
        {event_id:'explicit-unknown',time:{absolute:'2026-10-25T16:00:00Z',precision:'unknown'}},
        {event_id:'invalid',time:{absolute:'bad'}}
      ]]}});
      assert.equal(EVENTS_DATA.length,1); assert.equal(EVENTS_DATA[0].orig_date,'2026-10-26');
      assert.equal(EVENTS_DATA[0].time_str,'00:00'); assert.equal(EVENTS_DATA[0].minute,0);
      assert.equal(UNLOCATED_EVENTS.length,3);
    `;
    const result=spawnSync(process.execPath,['-e',script],{env:{...process.env,TZ:tz},encoding:'utf8'});
    assert.equal(result.status,0,result.stderr);
  });
}
