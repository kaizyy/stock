(() => {
  const esc = v => String(v ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  let me = null, state = {items:[]}, targets = [], history = [];

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
        <article class="panel" id="countPanel"><div class="panel-head"><div><p class="eyebrow">Inventarisatie</p><h3>Voorraadtelling</h3></div></div><p class="warehouse-note">Voer het werkelijk getelde aantal in. Het verschil wordt automatisch gelogd.</p><form id="countForm" class="warehouse-form"><label>Artikel<select name="item_id" required></select></label><label>Werkelijk aantal<input name="actual_quantity" type="number" min="0" step="0.001" required></label><label>Notitie<input name="note" placeholder="Bijv. jaarlijkse telling"></label><button class="button primary" type="submit">Telling verwerken</button></form></article>
        <article class="panel" id="returnPanel"><div class="panel-head"><div><p class="eyebrow">Correctie</p><h3>Retour boeken</h3></div></div><p class="warehouse-note">Verkoopretour boekt terug op voorraad; inkoopretour boekt voorraad af.</p><form id="returnForm" class="warehouse-form"><label>Type<select name="return_type" id="returnType"><option value="sales">Verkoopretour</option><option value="purchase">Inkoopretour</option></select></label><label>Artikel<select name="item_id" required></select></label><div class="field-grid"><label>Aantal<input name="quantity" type="number" min="0.001" step="0.001" required></label><label>Prijs per stuk<input name="price" type="number" min="0" step="0.01" required></label></div><label>Klant / leverancier<input name="party" placeholder="Optioneel"></label><label>Referentie<input name="reference" placeholder="Bijv. retour RMA-102"></label><label>Notitie<input name="note" placeholder="Reden retour"></label><button class="button primary" type="submit">Retour verwerken</button></form></article>
        <article class="panel" id="transferPanel"><div class="panel-head"><div><p class="eyebrow">Stockrooms</p><h3>Voorraad transfer</h3></div></div><p class="warehouse-note">Verplaats voorraad naar een andere stockroom waar je schrijfrechten hebt.</p><form id="transferForm" class="warehouse-form"><label>Artikel<select name="item_id" required></select></label><label>Doel-stockroom<select name="destination_stockroom_id" required></select></label><label>Aantal<input name="quantity" type="number" min="0.001" step="0.001" required></label><label>Notitie<input name="note" placeholder="Optioneel"></label><button class="button primary" type="submit">Voorraad verplaatsen</button></form></article>
      </div>
      <article class="panel warehouse-history"><div class="panel-head"><div><p class="eyebrow">Audit</p><h3>Magazijnhistorie</h3></div><button class="button ghost" type="button" id="refreshWarehouse">Vernieuwen</button></div><div id="warehouseHistory"></div></article>`;
    main.insertBefore(section,footer);
    const style=document.createElement('style');style.textContent=`.warehouse-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.warehouse-form{display:grid;gap:9px;margin-top:12px}.warehouse-form label{margin:0}.warehouse-note{color:var(--muted);font-size:11px;line-height:1.5}.warehouse-history{margin-top:20px}.warehouse-op{display:grid;grid-template-columns:1.2fr .8fr .8fr .8fr;gap:10px;padding:11px 0;border-top:1px solid var(--line);align-items:center}.warehouse-op:first-child{border-top:0}.warehouse-op small{display:block;color:var(--muted);margin-top:3px}.warehouse-msg{padding:10px 12px;border-radius:10px;margin-bottom:14px;background:#ecfdf5;color:#065f46}.warehouse-msg.err{background:#fef2f2;color:#991b1b}@media(max-width:1050px){.warehouse-grid{grid-template-columns:1fr 1fr}.warehouse-grid>article:last-child{grid-column:1/-1}}@media(max-width:720px){.warehouse-grid{grid-template-columns:1fr}.warehouse-grid>article:last-child{grid-column:auto}.warehouse-op{grid-template-columns:1fr 1fr}}`;document.head.appendChild(style);
  }

  function message(text,error=false){const el=document.getElementById('warehouseMessage');if(!el)return;el.className=`warehouse-msg${error?' err':''}`;el.textContent=text;setTimeout(()=>{el.className='';el.textContent='';},4500)}
  function typeLabel(type){return ({count:'Voorraadtelling',sales_return:'Verkoopretour',purchase_return:'Inkoopretour',transfer_out:'Transfer uit',transfer_in:'Transfer in'})[type]||type}
  function renderHistory(){const el=document.getElementById('warehouseHistory');if(!el)return;el.innerHTML=history.length?history.map(o=>`<div class="warehouse-op"><div><strong>${esc(o.item_name)}</strong><small>${typeLabel(o.operation_type)} · ${new Date(o.created_at).toLocaleString('nl-NL')}</small></div><div><small>Aantal / verschil</small><strong>${Number(o.quantity||0).toLocaleString('nl-NL')}</strong></div><div><small>Voor → na</small><strong>${o.previous_stock==null?'—':Number(o.previous_stock).toLocaleString('nl-NL')} → ${o.new_stock==null?'—':Number(o.new_stock).toLocaleString('nl-NL')}</strong></div><div><small>Referentie</small><strong>${esc(o.reference||'—')}</strong></div></div>`).join(''):'<div class="empty">Nog geen magazijnmutaties.</div>'}

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
      me=await api('/api/warehouse');state=await api('/api/state');targets=me.targets||[];history=me.history||[];
      fillForms();applyPermissions();renderHistory();
    }catch(e){if(e.message!=='session')message(e.message,true)}
  }

  function defaultPrice(type,itemId){const item=(state.items||[]).find(i=>String(i.id)===String(itemId));return Number(type==='sales'?item?.sell:item?.buy||0).toFixed(2)}

  document.addEventListener('submit',async e=>{
    if(!['countForm','returnForm','transferForm'].includes(e.target.id))return;
    e.preventDefault();const form=e.target,body=new FormData(form);let url='/api/warehouse/count';
    if(form.id==='returnForm')url='/api/warehouse/return';if(form.id==='transferForm')url='/api/warehouse/transfer';
    try{await api(url,{method:'POST',body});message(form.id==='countForm'?'Telling verwerkt.':form.id==='returnForm'?'Retour verwerkt.':'Transfer verwerkt.');form.reset();await refresh()}catch(err){message(err.message,true)}
  });
  document.addEventListener('change',e=>{if(e.target.matches('#returnType,#returnForm select[name="item_id"]')){const type=document.getElementById('returnType')?.value;const item=document.querySelector('#returnForm select[name="item_id"]')?.value;const price=document.querySelector('#returnForm input[name="price"]');if(type&&item&&price)price.value=defaultPrice(type,item)}});
  document.addEventListener('click',e=>{if(e.target.closest('#refreshWarehouse'))refresh()});

  installUI();refresh();
})();
