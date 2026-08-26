(()=>{
  if(window.__stockroomPerformanceInstalled)return;
  window.__stockroomPerformanceInstalled=true;
  const nativeFetch=window.fetch.bind(window);
  const inflight=new Map();
  const cache=new Map();
  const ttlFor=url=>{
    const p=new URL(url,location.href).pathname;
    if(p==='/api/me')return 2000;
    if(p==='/api/state')return 750;
    if(p==='/api/billing'||p==='/api/notifications'||p==='/api/account/notification-preferences')return 1000;
    if(p.startsWith('/api/platform-admin'))return 500;
    return 0;
  };
  const cloneResponse=async r=>{
    const body=await r.arrayBuffer();
    const init={status:r.status,statusText:r.statusText,headers:new Headers(r.headers)};
    return {body,init};
  };
  const fromStored=s=>new Response(s.body.slice(0),{...s.init,headers:new Headers(s.init.headers)});
  const invalidate=()=>{cache.clear();inflight.clear()};
  window.fetch=async function(input,init={}){
    const req=input instanceof Request?input:null;
    const method=String(init.method||req?.method||'GET').toUpperCase();
    const rawUrl=typeof input==='string'?input:req?.url||String(input);
    const url=new URL(rawUrl,location.href);
    const sameOrigin=url.origin===location.origin;
    if(!sameOrigin)return nativeFetch(input,init);
    if(method!=='GET'&&method!=='HEAD'){
      const r=await nativeFetch(input,init);
      if(r.ok)invalidate();
      return r;
    }
    const ttl=ttlFor(url.href);
    const key=method+' '+url.pathname+url.search;
    const now=performance.now();
    const hit=cache.get(key);
    if(ttl&&hit&&hit.expires>now)return fromStored(hit.value);
    if(inflight.has(key))return fromStored(await inflight.get(key));
    const p=nativeFetch(input,init).then(async r=>{
      const stored=await cloneResponse(r);
      if(ttl&&r.ok)cache.set(key,{expires:performance.now()+ttl,value:stored});
      return stored;
    }).finally(()=>inflight.delete(key));
    inflight.set(key,p);
    return fromStored(await p);
  };
  window.StockroomPerformance={invalidate};
})();