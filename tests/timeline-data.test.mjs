import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import test from 'node:test';
const require=createRequire(import.meta.url);
const {createTimelineCache,normalizeTimelineKey}=require('../web/timeline-data.js');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('parent and iframe reads coalesce the same canonical date and reuse its result',async()=>{
 const cache=createTimelineCache();let calls=0,release;
 const first=cache.read('/api/daily?date=2026-10-17&scope=provisional&view=display',()=>{calls++;return new Promise(r=>{release=r;});});
 const second=cache.read('/api/daily?date=2026-10-17',()=>{calls++;throw Error('duplicate read');});
 await tick();assert.equal(calls,1);release({events:[]});assert.equal(await first,await second);
 await cache.read('/api/daily?view=display&date=2026-10-17&scope=provisional',()=>{calls++;});assert.equal(calls,1);
});
test('different dates, views and scopes never share an event response',async()=>{
 const cache=createTimelineCache();let calls=0;
 for(const path of ['/api/daily?date=2026-10-17','/api/daily?date=2026-10-18','/api/daily?date=2026-10-17&view=all','/api/daily?date=2026-10-17&scope=settled'])await cache.read(path,()=>++calls);
 assert.equal(calls,4);assert.notEqual(normalizeTimelineKey('/api/daily?view=all'),normalizeTimelineKey('/api/daily?view=display'));
});
test('failed requests are evicted and a retry performs an actual read',async()=>{
 const cache=createTimelineCache();let calls=0;
 await assert.rejects(cache.read('/api/watch-reception',()=>{calls++;throw Error('503');}),/503/);
 assert.equal(await cache.read('/api/watch-reception',()=>{calls++;return 'ready';}),'ready');assert.equal(calls,2);
});
test('expired reads and explicit refresh bypass settled values',async()=>{
 let now=0,calls=0;const cache=createTimelineCache(()=>now);
 const read=(options={})=>cache.read('/api/watch-coverage',()=>++calls,{maxAge:100,...options});
 assert.equal(await read(),1);now=99;assert.equal(await read(),1);now=100;assert.equal(await read(),2);assert.equal(await read({force:true}),3);
});
test('invalidation prevents a late old response from overwriting a new value',async()=>{
 const cache=createTimelineCache();let release;
 const old=cache.read('/api/daily?date=2026-10-17',()=>new Promise(r=>{release=r;}));await tick();
 cache.clear('/api/daily?');await cache.read('/api/daily?date=2026-10-17',()=>({version:2}));release({version:1});await old;
 assert.equal(cache.peek('/api/daily?date=2026-10-17').version,2);
});
test('a background scene request for a superseded date is canceled before network work',async()=>{
 const cache=createTimelineCache();cache.selectDate('2026-10-16');let calls=0;
 const old=cache.read('/api/timeline-scenes?date=2026-10-16',()=>{calls++;return {};},{background:true,date:'2026-10-16'});
 cache.selectDate('2026-10-17');await assert.rejects(old,e=>e.kind==='superseded');assert.equal(calls,0);
});
test('slow supplemental work starts only after canonical navigation settles',async()=>{
 const cache=createTimelineCache();cache.selectDate('2026-10-17');let release,calls=0;
 const primary=cache.read('/api/daily?date=2026-10-17',()=>new Promise(r=>{release=r;}));
 const background=cache.read('/api/watch-reception',()=>{calls++;return {};},{background:true});
 await tick();assert.equal(calls,0);release({events:[]});await primary;await background;assert.equal(calls,1);
});
test('same-origin iframe adopts its parent cache rather than creating a new one',()=>{
 const shared=createTimelineCache();const context={parent:{location:{origin:'http://local'},LifeTimelineData:shared},location:{origin:'http://local'},URL,Map,Promise,Date,setTimeout};vm.createContext(context);
 vm.runInContext(fs.readFileSync(new URL('../web/timeline-data.js',import.meta.url),'utf8'),context);
 assert.equal(context.LifeTimelineData,shared);
});
