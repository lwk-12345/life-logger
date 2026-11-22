/* Same-origin parent/iframe request cache. No storage, credentials or cross-origin payloads persist. */
(function(root){
  function normalizeTimelineKey(path){
    const url=new URL(path,'http://timeline.local');
    if(url.pathname==='/api/daily'){
      if(!url.searchParams.has('scope'))url.searchParams.set('scope','provisional');
      if(!url.searchParams.has('view'))url.searchParams.set('view','display');
    }
    url.searchParams.sort();
    return url.pathname+url.search;
  }
  function createTimelineCache(now=()=>Date.now()){
    const entries=new Map();
    let selectedDate=null,lastPriorityAt=-Infinity;
    function selectDate(date){selectedDate=date;lastPriorityAt=now();}
    async function waitForQuiet(date){
      for(;;){
        if(date&&selectedDate&&date!==selectedDate){const error=new Error('date superseded');error.kind='superseded';throw error;}
        const critical=Array.from(entries).some(([key,entry])=>key.startsWith('/api/daily?')&&entry.pending);
        if(!critical&&now()-lastPriorityAt>=800)return;
        await new Promise(resolve=>setTimeout(resolve,50));
      }
    }
    function peek(path,maxAge=300000){
      const entry=entries.get(normalizeTimelineKey(path));
      return entry&&entry.loadedAt!==null&&now()-entry.loadedAt<maxAge?entry.value:undefined;
    }
    function read(path,loader,{force=false,maxAge=300000,background=false,date=null}={}){
      const key=normalizeTimelineKey(path),previous=entries.get(key);
      if(key.startsWith('/api/daily?')){
        lastPriorityAt=now();
        selectedDate=new URL(key,'http://timeline.local').searchParams.get('date')||selectedDate;
      }
      if(previous?.pending)return previous.pending;
      if(!force){const value=peek(key,maxAge);if(value!==undefined)return Promise.resolve(value);}
      const entry={loadedAt:null,value:undefined,pending:null};
      entry.pending=Promise.resolve().then(async()=>{if(background)await waitForQuiet(date);return loader();}).then(value=>{
        if(entries.get(key)===entry){entry.value=value;entry.loadedAt=now();entry.pending=null;}
        return value;
      },error=>{if(entries.get(key)===entry)entries.delete(key);throw error;});
      entries.set(key,entry);
      // Bound per-tab memory; active requests remain coalesced while in flight.
      if(entries.size>100)for(const [oldKey,old] of entries){if(entries.size<=100)break;if(!old.pending&&oldKey!==key)entries.delete(oldKey);}
      return entry.pending;
    }
    function clear(prefix){
      for(const key of entries.keys())if(!prefix||key.startsWith(prefix))entries.delete(key);
    }
    return {read,peek,clear,selectDate};
  }
  if(typeof module!=='undefined'&&module.exports){module.exports={createTimelineCache,normalizeTimelineKey};return;}
  try{
    if(root.parent!==root&&root.parent.location.origin===root.location.origin&&root.parent.LifeTimelineData){root.LifeTimelineData=root.parent.LifeTimelineData;return;}
  }catch(_){/* An independent/cross-origin embed receives its own cache. */}
  root.LifeTimelineData=createTimelineCache();
  // Keep shared requests owned by the parent window when an iframe is replaced.
  if(typeof root.fetch==='function')root.LifeTimelineData.fetch=root.fetch.bind(root);
})(typeof globalThis==='undefined'?this:globalThis);
