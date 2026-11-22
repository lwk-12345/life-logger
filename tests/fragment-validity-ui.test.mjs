import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
const html=fs.readFileSync(process.env.WATERFALL_V2_SOURCE || new URL('../web/waterfall-v2.html',import.meta.url),'utf8');
function slice(name){
 let i=html.indexOf(`function ${name}(`);
 if(i<0){i=html.indexOf(`const ${name} =`);if(i<0)throw Error(name);return html.slice(i,html.indexOf(';\n',i)+1);}
 let depth=0,started=false;
 for(let j=i;j<html.length;j++){if(html[j]==='{'){depth++;started=true;}else if(html[j]==='}'&&started&&--depth===0)return html.slice(i,j+1);}
 throw Error(name);
}
function load(){
 const names=['CARD_PREVIEW','WORK_LABEL','FRAGMENT_TITLE','HIDE_STATUS','ROW_STATUS','moneyText','moneyOf','foldReason','rowHtml','itemHtml','detailHtml','groupCards','cardRange','cardHtml','rowsBlock','sceneLabel','macrosHtml','overviewHtml','clocksHtml'];
 return new Function(`const state={filter:'live',cat:'',expanded:new Set(),open:new Set(),quotes:true};
 const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
 const pad=n=>String(n).padStart(2,'0');const fmt=m=>String(m),fmtDur=m=>String(m);const catOf=a=>a.category||'其他',catColor=()=> '#002fa7';
 const cleanTitle=a=>a.title||'',speechBadge=()=>'',spendClockHtml=()=>'';
 ${names.map(slice).join('\n')};return {state,${names.join(',')}};`)();
}
const salary=()=>({a:{activity_id:'act_20261105_002610_salary_raise_mention',title:'重复提及涨工资',category:'支付·播报',
 summary:'没有证据表明已到账或消费',looks_like_fragment:true,fragment_money_protected:false,fragment_reason:'不确定是谁说的',
 items:[{evidence:[{quote:'涨工资'},{quote:'涨工资'}]}]},life:'live',minute:26,basis:'spoken'});

test('salary is closed under fragments by default; expanding retains both quotes and uncertainty',()=>{
 const f=load(),x=salary();const closed=f.rowsBlock('scene',[x]);
 assert.match(closed,/零碎片段 · 1 条/);assert.doesNotMatch(closed,/重复提及涨工资/);
 f.state.expanded.add('scene-work-noise');f.state.open.add(x.a.activity_id);
 const open=f.rowsBlock('scene',[x]);assert.match(open,/不确定是谁说的/);
 assert.equal((open.match(/<blockquote>涨工资/g)||[]).length,2);
 assert.match(open,/重复提及涨工资/);
});

test('money and spending words override even a stale fragment flag',()=>{
 const f=load();
 for(const fields of [
  {fragment_money_protected:true},
  {items:[{amount:5}]},
  {items:[{fact_type:'expense'}]},
  {items:[{evidence:[{quote:'买水'}]}]},
  {title:'口头点“一个红烧”',items:[{evidence:[{quote:'来一个红烧'}]}]},
 ])assert.equal(f.foldReason({...salary(),a:{...salary().a,...fields}}),'');
});

test('an explicit negative read assessment overrides the old short-title heuristic',()=>{
 const f=load();
 for(const title of ['口头提醒别忘了LiveLog','步行六十与歇四分钟的口头安排','现场短句：写上我的、第二组']){
  const x={...salary(),a:{...salary().a,title,looks_like_fragment:false}};
  assert.equal(f.foldReason(x),'');assert.match(f.rowsBlock('k',[x]),new RegExp(title));
 }
 assert.equal(f.foldReason({a:{title:'现场短句',items:[]}}),'零碎片段');
});

test('fragment rows are not counted as life events or used to relabel a quiet scene',()=>{
 const f=load(),x=salary();
 assert.equal(f.sceneLabel('安静少言',[x,x,x]),'安静少言');
 const clocks=f.clocksHtml({acts:[x],audioMin:1},null);
 assert.match(clocks,/本人 <b class="tnum" data-live-count>0<\/b> 条/); // 2026-11-07 改版：条目数降到底部小字
});

test('unknown/media fragments enter the fragment fold once without changing original life',()=>{
 const f=load(),x={...salary(),life:'unknown'},before=JSON.stringify(x);
 f.state.expanded.add('s-work-noise');const body=f.rowsBlock('s',[x]);
 assert.match(body,/零碎片段 · 1 条/);assert.equal((body.match(/data-activity-id=/g)||[]).length,1);
 assert.doesNotMatch(body,/1 条待确认/);assert.equal(JSON.stringify(x),before);
});
