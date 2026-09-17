(() => {
  const euro=new Intl.NumberFormat('nl-NL',{style:'currency',currency:'EUR'});
  const number=new Intl.NumberFormat('nl-NL',{maximumFractionDigits:3});
  const esc=value=>String(value??'').replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const reasonLabels={damaged:'Beschadigd',wrong_item:'Verkeerd artikel',defective:'Defect',not_suitable:'Niet passend / geschikt',delivery_issue:'Probleem met levering',customer_changed_mind:'Klant heeft zich bedacht',supplier_error:'Fout van leverancier',other:'Overig'};
  let loading=false;
  function install(){
    if(document.getElementById('returnAnalytics'))return;
    const overview=document.getElementById('overview'),grid=overview?.querySelector('.dashboard-grid');
    if(!overview||!grid)return;
    const panel=document.createElement('article');panel.id='returnAnalytics';panel.className='panel return-analytics';
    panel.innerHTML='<div class="panel-head"><div><p class="eyebrow">Kwaliteit & kosten</p><h3>Retourdashboard</h3></div><button class="button ghost" type="button" id="refreshReturnAnalytics">Vernieuwen</button></div><div id="returnAnalyticsBody" class="empty">Retourgegevens laden…</div>';
    grid.insertAdjacentElement('beforebegin',panel);
    const style=document.createElement('style');style.textContent='.return-analytics{margin-top:12px}.return-kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin:12px 0}.return-kpi{padding:11px;border:1px solid var(--line);border-radius:11px}.return-kpi strong,.return-kpi small{display:block}.return-kpi small{color:var(--muted);margin-bottom:4px}.return-tables{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.return-table{border:1px solid var(--line);border-radius:11px;padding:11px}.return-table h4{margin:0 0 8px}.return-row{display:flex;justify-content:space-between;gap:8px;padding:6px 0;border-top:1px solid var(--line);font-size:12px}@media(max-width:900px){.return-kpis{grid-template-columns:repeat(2,1fr)}.return-tables{grid-template-columns:1fr}}';document.head.appendChild(style);
  }
  const table=(title,rows,value)=>`<section class="return-table"><h4>${title}</h4>${rows.length?rows.map(row=>`<div class="return-row"><span>${esc(row.label)}</span><strong>${value(row)}</strong></div>`).join(''):'<span class="return-muted">Nog geen gegevens</span>'}</section>`;
  async function refresh(){
    if(loading)return;loading=true;install();const body=document.getElementById('returnAnalyticsBody');if(!body){loading=false;return}body.innerHTML='Retourgegevens laden…';
    try{const response=await fetch('/api/returns/analytics',{cache:'no-store'}),data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||'Retourdashboard kon niet worden geladen.');const s=data.summary;body.className='';body.innerHTML=`<div class="return-kpis"><div class="return-kpi"><small>Retouren</small><strong>${number.format(s.return_count)}</strong></div><div class="return-kpi"><small>Wacht op verwerking</small><strong>${number.format(s.awaiting_processing)}</strong></div><div class="return-kpi"><small>Creditvoorstellen</small><strong>${euro.format(s.sales_credit)}</strong></div><div class="return-kpi"><small>Leveranciersclaims</small><strong>${euro.format(s.supplier_claims)}</strong></div><div class="return-kpi"><small>Nog te ontvangen</small><strong>${euro.format(s.claims_open)}</strong></div></div><div class="return-tables">${table('Retourredenen',data.reasons,row=>number.format(row.count))}${table('Meest geretourneerd',data.items,row=>`${number.format(row.quantity)} stuks`)}${table('Leveranciers',data.suppliers,row=>`${number.format(row.returns)} · ${euro.format(row.amount)}`)}</div>`}catch(error){body.className='empty';body.textContent=error.message}finally{loading=false}
  }
  document.addEventListener('click',event=>{if(event.target.closest('#refreshReturnAnalytics'))refresh()});
  document.addEventListener('stockroom:refresh',event=>{if(event.detail?.view==='overview')refresh()});
  install();refresh();
})();
