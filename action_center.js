(() => {
  const esc=value=>String(value??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  let loading=false,request=0;

  function install(){
    const overview=document.getElementById('overview'),grid=overview?.querySelector('.dashboard-grid');
    if(!overview||!grid||document.getElementById('actionCenter'))return;
    grid.insertAdjacentHTML('beforebegin','<article class="panel action-center" id="actionCenter"><div class="panel-head"><div><p class="eyebrow">Wat vraagt aandacht?</p><h3>Actiecentrum <span id="actionCenterCount" class="action-count">0</span></h3></div><button class="button ghost" type="button" id="refreshActionCenter">Vernieuwen</button></div><div id="actionCenterList" class="action-center-list"><div class="empty">Acties laden…</div></div></article>');
    const style=document.createElement('style');style.textContent='.action-center{margin-top:12px}.action-count{display:inline-grid;place-items:center;min-width:24px;height:24px;padding:0 7px;border-radius:99px;background:var(--green-2);color:var(--green);font:800 11px "DM Sans"}.action-center-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-top:14px}.action-card{display:grid;grid-template-columns:10px minmax(0,1fr) auto;gap:10px;align-items:center;border:1px solid var(--line);border-radius:11px;padding:11px;background:var(--paper)}.action-dot{width:9px;height:9px;border-radius:50%;background:#5b8190}.action-card.warning .action-dot{background:#d97706}.action-card.danger .action-dot{background:#c2413b}.action-card strong,.action-card small{display:block}.action-card small{color:var(--muted);margin-top:3px;font-size:9px}.action-card button{white-space:nowrap}.action-center-clear{grid-column:1/-1;padding:18px;text-align:center;color:var(--green);font-weight:700}@media(max-width:760px){.action-center-list{grid-template-columns:1fr}.action-card{grid-template-columns:10px minmax(0,1fr)}.action-card button{grid-column:2;width:100%}}';document.head.appendChild(style);
  }

  function render(actions){
    install();const list=document.getElementById('actionCenterList'),count=document.getElementById('actionCenterCount');if(!list||!count)return;
    count.textContent=actions.length;count.hidden=!actions.length;
    list.innerHTML=actions.length?actions.map(action=>`<div class="action-card ${esc(action.severity)}"><span class="action-dot"></span><div><strong>${esc(action.title)}</strong><small>${esc(action.detail)}</small></div><button class="button ghost" type="button" data-action-view="${esc(action.targetView)}">${esc(action.actionLabel||'Bekijken')}</button></div>`).join(''):'<div class="action-center-clear">Alles is bijgewerkt. Er zijn geen open acties.</div>';
  }

  async function refresh(){
    if(loading)return;loading=true;const current=++request,list=document.getElementById('actionCenterList');if(list)list.innerHTML='<div class="empty">Acties vernieuwen…</div>';
    try{const response=await fetch('/api/action-center',{cache:'no-store'}),data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||'Actiecentrum kon niet worden geladen.');if(current===request)render(data.actions||[])}catch(error){if(current===request&&list)list.innerHTML=`<div class="empty">${esc(error.message)}</div>`}finally{loading=false}
  }

  document.addEventListener('click',event=>{if(event.target.closest('#refreshActionCenter'))refresh();const button=event.target.closest('[data-action-view]');if(button)document.querySelector(`[data-view="${CSS.escape(button.dataset.actionView)}"]`)?.click()});
  document.addEventListener('stockroom:refresh',event=>{if(event.detail?.view==='overview')refresh()});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&document.getElementById('overview')?.classList.contains('active-view'))refresh()});
  install();refresh();
})();

