(() => {
  const esc = v => String(v ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  let me = null, state = {items:[]}, targets = [], history = [], counts = [];

  async function api(url, options={}) {
    const r = await fetch(url,{cache:'no-store',...options});
    const data = await r.json().catch(()=>({}));
    if(r.status===401){location.href='/login'; throw new Error('session');}
    if(!r.ok) throw new Error(data.error||'Actie mislukt.');
    return data;
  }

  function itemOptions(){
    return (state.items||[]).filter(i=>!i.archived).map(i=>`<option value="${esc(i.id)}">${esc(i.name)} (${esc(i.sku||'geen SKU')}) · ${Number(i.stock||0).toLocaleString('nl-NL')} op voorraad</option>`).join('');
  }

  function installUI(){
    if(document.getElementById('warehouse')) return;
    const nav=document.querySelector('.sidebar nav'), main=document.querySelector('main'), footer=main?.querySelector('.site-footer');
    if(!nav||!main) return;
    nav.insertAdjacentHTML('beforeend','<button class="nav-item" data-view="warehouse"><span>▦</span>Magazijn</button>');
    const section=document.createElement('section'); section.id='warehouse'; section.className='view';
    section.innerHTML=`
      <div class="section-head"><div><p class="eyebrow">Magazijnprocessen</p><h2>Tellen, retouren & transfers</h2></div></div>
      <div id="warehouseMessage"></div>
      <div class="warehouse-grid">
        <article class="panel count-workflow" id="countPanel"><div class="panel-head"><div><p class="eyebrow">Gecontroleerde inventarisatie</p><h3>Voorraadtelling</h3></div></div><p class="warehouse-note">Start een telling, tel alle artikelen via mobiel of barcode en dien de verschillen ter goedkeuring in. De voorraad verandert pas na goedkeuring.</p><div id="countWorkflow"></div></article>
        <article class="panel" id="returnPanel"><div class="panel-head"><div><p class="eyebrow">Correctie</p><h3>Retour boeken</h3></div></div><p class="warehouse-note">Verkoopretour boekt terug op voorraad; inkoopretour boekt voorraad af.</p><form id="returnForm" class="warehouse-form"><label>Type<select name="return_type" id="returnType"><option value="sales">Verkoopretour</option><option value="purchase">Inkoopretour</option></select></label><label>Artikel<select name="item_id" required></select></label><div class="field-grid"><label>Aantal<input name="quantity" type="number" min="0.001" step="0.001" required></label><label>Prijs per stuk<input name="price" type="number" min="0" step="0.01" required></label></div><label>Klant / leverancier<input name="party" placeholder="Optioneel"></label><label>Referentie<input name="reference" placeholder="Bijv. retour RMA-102"></label><label>Notitie<input name="note" placeholder="Reden retour"></label><button class="button primary" type="submit">Retour verwerken</button></form></article>
        <article class="panel" id="transferPanel"><div class="panel-head"><div><p class="eyebrow">Stockrooms</p><h3>Voorraad transfer</h3></div></div><p class="warehouse-note">Verplaats voorraad naar een andere stockroom waar je schrijfrechten hebt.</p><form id="transferForm" class="warehouse-form"><label>Artikel<select name="item_id" required></select></label><label>Doel-stockroom<select name="destination_stockroom_id" required></select></label><label>Aantal<input name="quantity" type="number" min="0.001" step="0.001" required></label><label>Notitie<input name="note" placeholder="Optioneel"></label><button class="button primary" type="submit">Voorraad verplaatsen</button></form></article>
      </div>
      <article class="panel warehouse-history"><div class="panel-head"><div><p class="eyebrow">Audit</p><h3>Magazijnhistorie</h3></div><button class="button ghost" type="button" id="refreshWarehouse">Vernieuwen</button></div><div id="warehouseHistory"></div></article>`;
    main.insertBefore(section,footer);
    const style=document.createElement('style');style.textContent=`.warehouse-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.count-workflow{grid-column:1/-1}.warehouse-form{display:grid;gap:9px;margin-top:12px}.warehouse-form label{margin:0}.warehouse-note{color:var(--muted);font-size:11px;line-height:1.5}.warehouse-history{margin-top:20px}.warehouse-op{display:grid;grid-template-columns:1.2fr .8fr .8fr .8fr;gap:10px;padding:11px 0;border-top:1px solid var(--line);align-items:center}.warehouse-op:first-child{border-top:0}.warehouse-op small{display:block;color:var(--muted);margin-top:3px}.warehouse-msg{padding:10px 12px;border-radius:10px;margin-bottom:14px;background:#ecfdf5;color:#065f46}.warehouse-msg.err{background:#fef2f2;color:#991b1b}.count-head,.count-actions{display:flex;gap:8px;align-items:center;justify-content:space-between}.count-actions{justify-content:flex-end;margin-top:14px;flex-wrap:wrap}.count-progress{font-size:11px;font-weight:800;color:var(--green)}.count-lines{margin-top:14px;max-height:55vh;overflow:auto}.count-line{display:grid;grid-template-columns:minmax(150px,1fr) 90px 120px 100px;gap:10px;align-items:center;padding:9px 0;border-top:1px solid var(--line)}.count-line small{display:block;color:var(--muted)}.count-line input{margin:0}.count-difference{font-weight:800}.count-difference.negative-value,.count-difference.positive-value{font-size:12px}@media(max-width:720px){.warehouse-grid{grid-template-columns:1fr}.count-workflow{grid-column:auto}.warehouse-op{grid-template-columns:1fr 1fr}.count-line{grid-template-columns:1fr 92px}.count-line .count-expected{display:none}.count-actions .button{flex:1}.count-head{align-items:flex-start;flex-direction:column}}`;document.head.appendChild(style);
  }

  function message(text,error=false){const el=document.getElementById('warehouseMessage');if(!el)return;el.className=`warehouse-msg${error?' err':''}`;el.textContent=text;setTimeout(()=>{el.className='';el.textContent='';},4500)}
  function typeLabel(type){return ({count:'Voorraadtelling',sales_return:'Verkoopretour',purchase_return:'Inkoopretour',transfer_out:'Transfer uit',transfer_in:'Transfer in'})[type]||type}
  function renderHistory(){const el=document.getElementById('warehouseHistory');if(!el)return;el.innerHTML=history.length?history.map(o=>`<div class="warehouse-op"><div><strong>${esc(o.item_name)}</strong><small>${typeLabel(o.operation_type)} · ${new Date(o.created_at).toLocaleString('nl-NL')}</small></div><div><small>Aantal / verschil</small><strong>${Number(o.quantity||0).toLocaleString('nl-NL')}</strong></div><div><small>Voor → na</small><strong>${o.previous_stock==null?'—':Number(o.previous_stock).toLocaleString('nl-NL')} → ${o.new_stock==null?'—':Number(o.new_stock).toLocaleString('nl-NL')}</strong></div><div><small>Referentie</small><strong>${esc(o.reference||'—')}</strong></div></div>`).join(''):'<div class="empty">Nog geen magazijnmutaties.</div>'}

  function renderCount(){
    const el=document.getElementById('countWorkflow');if(!el)return;
    const active=counts.find(c=>['draft','submitted'].includes(c.status));
    if(!active){el.innerHTML=`<form id="countStartForm" class="warehouse-form"><div class="field-grid"><label>Naam telling<input name="title" value="Voorraadtelling ${new Date().toLocaleDateString('nl-NL')}" required></label><label>Reden / notitie<input name="note" placeholder="Bijv. maandtelling"></label></div><button class="button primary" type="submit">Nieuwe telling starten</button></form>`;return}
    const editable=active.status==='draft',done=Number(active.counted_count||0),total=Number(active.line_count||0);
    el.innerHTML=`<div class="count-head"><div><strong>${esc(active.title)}</strong><small>${editable?'Open telling':'Ingediend voor goedkeuring'} · ${done} van ${total} geteld</small></div><span class="count-progress">${total?Math.round(done/total*100):0}%</span></div>
      ${editable?`<form id="barcodeCountForm" class="warehouse-form"><label>Barcode snel invoeren<input name="barcode" inputmode="numeric" autocomplete="off" placeholder="Scan of typ barcode"></label><label>Geteld aantal<input name="counted_stock" type="number" min="0" step="0.001" required></label><button class="button ghost" type="submit">Aantal opslaan</button></form>`:''}
      <div class="count-lines">${(active.lines||[]).map(line=>{const diff=line.counted_stock==null?null:Number(line.counted_stock)-Number(line.expected_stock);return `<div class="count-line"><div><strong>${esc(line.item_name)}</strong><small>${esc(line.sku||line.barcode||'Geen SKU')}</small></div><span class="count-expected"><small>Administratief</small>${Number(line.expected_stock).toLocaleString('nl-NL')}</span>${editable?`<input aria-label="Geteld aantal ${esc(line.item_name)}" data-count-line="${esc(line.item_id)}" type="number" min="0" step="0.001" value="${line.counted_stock==null?'':esc(line.counted_stock)}" placeholder="Geteld">`:`<span><small>Geteld</small>${Number(line.counted_stock).toLocaleString('nl-NL')}</span>`}<span class="count-difference ${diff<0?'negative-value':diff>0?'positive-value':''}">${diff==null?'Niet geteld':`${diff>0?'+':''}${diff.toLocaleString('nl-NL')} · € ${(diff*Number(line.buy_price||0)).toLocaleString('nl-NL',{minimumFractionDigits:2,maximumFractionDigits:2})}`}</span></div>`}).join('')}</div>
      <div class="count-actions"><button class="button ghost" type="button" data-count-action="cancel" data-count-id="${active.id}">Annuleren</button>${editable?`<button class="button primary" type="button" data-count-action="submit" data-count-id="${active.id}" ${done!==total?'disabled':''}>Indienen ter goedkeuring</button>`:me?.warehousePermissions?.approveCount?`<button class="button primary" type="button" data-count-action="approve" data-count-id="${active.id}">Goedkeuren en voorraad verwerken</button>`:'<span class="warehouse-note">Wacht op goedkeuring door Owner of Admin.</span>'}</div>`;
  }

  function fillForms(){
    document.querySelectorAll('#warehouse select[name="item_id"]').forEach(sel=>{const current=sel.value;sel.innerHTML=itemOptions();if(current)sel.value=current});
    const target=document.querySelector('#transferForm select[name="destination_stockroom_id"]');
    if(target) target.innerHTML=targets.length?targets.map(t=>`<option value="${esc(t.id)}">${esc(t.name)} (${esc(t.role)})</option>`).join(''):'<option value="">Geen andere beschrijfbare stockroom</option>';
  }

  function applyPermissions(){
    const p=me?.warehousePermissions||{};
    document.getElementById('countPanel').hidden=!p.count;
    document.getElementById('transferPanel').hidden=!p.transfer;
    const form=document.getElementById('returnForm');
    const type=document.getElementById('returnType');
    if(form&&type){
      const options=[...type.options];options.forEach(o=>o.hidden=(o.value==='sales'&&!p.salesReturn)||(o.value==='purchase'&&!p.purchaseReturn));
      const first=options.find(o=>!o.hidden);if(first)type.value=first.value;form.closest('#returnPanel').hidden=!first;
    }
  }

  async function refresh(){
    try{
      me=await api('/api/warehouse');state=await api('/api/state');targets=me.targets||[];history=me.history||[];counts=me.counts||[];
      fillForms();applyPermissions();renderHistory();renderCount();
    }catch(e){if(e.message!=='session')message(e.message,true)}
  }

  function defaultPrice(type,itemId){const item=(state.items||[]).find(i=>String(i.id)===String(itemId));return Number(type==='sales'?item?.sell:item?.buy||0).toFixed(2)}

  document.addEventListener('submit',async e=>{
    if(e.target.id==='countStartForm'){e.preventDefault();try{await api('/api/warehouse/count/start',{method:'POST',body:new FormData(e.target)});message('Telling gestart.');await refresh()}catch(err){message(err.message,true)}return}
    if(e.target.id==='barcodeCountForm'){e.preventDefault();const active=counts.find(c=>c.status==='draft'),body=new FormData(e.target);body.set('count_id',active.id);try{await api('/api/warehouse/count/line',{method:'POST',body});e.target.reset();await refresh()}catch(err){message(err.message,true)}return}
    if(!['returnForm','transferForm'].includes(e.target.id))return;
    e.preventDefault();const form=e.target,body=new FormData(form);let url='/api/warehouse/count';
    if(form.id==='returnForm')url='/api/warehouse/return';if(form.id==='transferForm')url='/api/warehouse/transfer';
    try{await api(url,{method:'POST',body});message(form.id==='returnForm'?'Retour verwerkt.':'Transfer verwerkt.');form.reset();await refresh()}catch(err){message(err.message,true)}
  });
  document.addEventListener('change',e=>{if(e.target.matches('#returnType,#returnForm select[name="item_id"]')){const type=document.getElementById('returnType')?.value;const item=document.querySelector('#returnForm select[name="item_id"]')?.value;const price=document.querySelector('#returnForm input[name="price"]');if(type&&item&&price)price.value=defaultPrice(type,item)}});
  document.addEventListener('change',async e=>{const input=e.target.closest('[data-count-line]');if(!input)return;const active=counts.find(c=>c.status==='draft'),body=new FormData();body.set('count_id',active.id);body.set('item_id',input.dataset.countLine);body.set('counted_stock',input.value);try{await api('/api/warehouse/count/line',{method:'POST',body});await refresh()}catch(err){message(err.message,true)}});
  document.addEventListener('click',async e=>{if(e.target.closest('#refreshWarehouse'))refresh();const button=e.target.closest('[data-count-action]');if(!button)return;const action=button.dataset.countAction;if(action==='cancel'&&!confirm('Deze telling annuleren? De voorraad blijft ongewijzigd.'))return;const body=new FormData();body.set('count_id',button.dataset.countId);try{await api(`/api/warehouse/count/${action}`,{method:'POST',body});message(action==='submit'?'Telling ingediend.':action==='approve'?'Telling goedgekeurd en voorraad bijgewerkt.':'Telling geannuleerd.');await refresh()}catch(err){message(err.message,true)}});

  installUI();refresh();
  document.addEventListener('stockroom:refresh',event=>{if(event.detail?.view==='warehouse')refresh()});
})();

