import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const recordingLogic=require('../web/journal_logic.js');
const app=fs.readFileSync(new URL('../web/app.js',import.meta.url),'utf8');

test('supplemental live events request only the selected day',async()=>{
 let requested='';const context={selectedJournalDate:'2026-10-22',shanghaiDate:()=> '2026-10-23',api:async path=>{requested=path;return {sessions:[]};}};
 vm.createContext(context);vm.runInContext('async '+named(app,'loadWatchLiveJournalEntries'),context);
 await context.loadWatchLiveJournalEntries();assert.equal(requested,'/api/watch-live-events?date=2026-10-22');
});

test('recording range reuse is scoped to one projection and updated source times remain visible',()=>{
 const row={started_at:'2026-10-21T22:00:00+08:00',ended_at:'2026-10-21T23:00:00+08:00'};
 const first=new WeakMap();assert.deepEqual(recordingLogic.recordingIntervalDates([row],first),['2026-10-21']);
 assert.equal(recordingLogic.recordingCoverageForDate([row],'2026-10-21',first)[0].endLabel,'23:00');
 row.ended_at='2026-10-22T01:00:00+08:00';const next=new WeakMap();assert.deepEqual(recordingLogic.recordingIntervalDates([row],next),['2026-10-21','2026-10-22']);
 assert.equal(recordingLogic.recordingCoverageForDate([row],'2026-10-21',next)[0].endLabel,'24:00');
});

const html=fs.readFileSync(new URL('../web/daily-timeline-live.html',import.meta.url),'utf8');
function named(source,name){const start=source.indexOf(`function ${name}(`);assert.ok(start>=0,name+' exists');let depth=0,opened=false;for(let i=source.indexOf('{',start);i<source.length;i++){if(source[i]==='{'){depth++;opened=true;}if(source[i]==='}'&&!--depth&&opened)return source.slice(start,i+1);}throw Error('missing function end');}

test('switching to a shell date renders its canonical view without globally refreshing or invalidating cached days',()=>{
 let renders=0,refreshes=0;const cached={date:'2026-10-21',canonical:true};const next={date:'2026-10-22',_shell:true,entries:[]};
 const context={LifeTimelineData:{selectDate(){}},journalSelectionInitialized:true,journalUserSelectedDate:'',journalDateGeneration:0,freezeJournalSwitchHeight(){},selectedJournalDate:'2026-10-21',writeStorageVersioned(){},document:{querySelectorAll(){return [];}},journalDays:[cached,next],lastJobs:[],setJournalLoading(){},refreshJournal(){refreshes++;return Promise.resolve();},renderJournalNavigation(){renders++;}};
 vm.createContext(context);vm.runInContext(named(app,'selectJournalDate'),context);context.selectJournalDate('2026-10-22');
 assert.equal(context.selectedJournalDate,'2026-10-22');assert.equal(refreshes,0,'a date selection must not force a global refresh');assert.equal(renders,1);assert.equal(cached.canonical,true);
});

test('non-canonical day still mounts the timeline iframe immediately with the target date',()=>{
 const {timelineSectionMarkup, daySwitchBannerMarkup} = (() => {
   const context = {escapeHtml: (value) => String(value ?? '').replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])), result: null};
   vm.createContext(context);
   vm.runInContext(named(app, 'timelineSectionMarkup') + '\n' + named(app, 'daySwitchBannerMarkup') + '\nresult={timelineSectionMarkup,daySwitchBannerMarkup};', context);
   return context.result;
 })();
 const markup = timelineSectionMarkup('2026-10-22', '昨天');
 assert.match(markup, /<iframe class="day-timeline-frame"/);
 assert.match(markup, /data-timeline-date="2026-10-22"/);
 assert.match(markup, /date=2026-10-22&embedded=1/);
 // 非 canonical 分支必须调用同一段标记，且带“正在切换”提示
 const branch = named(app, 'renderJournalDay');
 assert.ok(branch.includes('timelineSectionMarkup(day.date'), '非 canonical 也必须挂载 iframe');
 assert.ok(branch.includes('daySwitchBannerMarkup(day.date'), '切日必须有明确状态');
 assert.match(daySwitchBannerMarkup('2026-10-22', true), /正在切换到 2026-10-22/);
 assert.match(daySwitchBannerMarkup('2026-10-22', false), /display:none/);
});

test('reused timeline iframe accepts only its same-origin parent date updates and ignores duplicate dates',()=>{
 const parent={};const calls=[];const context={window:{parent},location:{origin:'http://local'},state:{date:'2026-10-21'},llTrace(){},setDate(date){calls.push(date);context.state.date=date;},loadDay(){calls.push('refresh');}};
 vm.createContext(context);vm.runInContext(named(html,'handleParentTimelineMessage'),context);
 const message={origin:'http://local',source:parent,data:{type:'life-logger-timeline-set-date',date:'2026-10-22'}};
 context.handleParentTimelineMessage({...message,origin:'http://evil'});context.handleParentTimelineMessage({...message,source:{}});assert.equal(calls.length,0);
 context.handleParentTimelineMessage(message);context.handleParentTimelineMessage(message);assert.deepEqual(calls,['2026-10-22']);
 context.handleParentTimelineMessage({...message,data:{type:'life-logger-timeline-data-updated',date:'2026-10-21'}});assert.equal(calls.length,1);
 context.handleParentTimelineMessage({...message,data:{type:'life-logger-timeline-data-updated',date:'2026-10-22'}});assert.deepEqual(calls,['2026-10-22','refresh']);
});
