(() => {
  const esc = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  let rows = [];

  async function api(url, options={}) {
    const response = await fetch(url, {cache:'no-store', ...options});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'Actie mislukt.');
    return data;
  }

  function ensurePanel() {
    const forecast = document.querySelector('.forecast-panel');
    if (!forecast || document.getElementById('purchaseAdvicePanel')) return;
    forecast.insertAdjacentHTML('beforeend', `<div id="purchaseAdvicePanel" class="purchase-advice-panel"><div class="purchase-advice-head"><div><strong>Concept-inkooporders</strong><small>Selecteer adviezen; orders worden automatisch per leverancier gegroepeerd.</small></div><button class="button ghost" type="button" id="refreshPurchaseAdvice">Vernieuwen</button></div><div id="purchaseAdviceRows"></div><div class="purchase-advice-actions"><span id="purchaseAdviceStatus" role="status"></span><button class="button primary" type="button" id="createPurchaseDrafts">Maak conceptorders</button></div></div>`);
    const style = document.createElement('style');
    style.textContent = `.purchase-advice-panel{border-top:1px solid var(--line);margin:20px -20px -20px;padding:18px 20px}.purchase-advice-head,.purchase-advice-actions{display:flex;align-items:center;justify-content:space-between;gap:12px}.purchase-advice-head strong,.purchase-advice-head small{display:block}.purchase-advice-head small{color:var(--muted);margin-top:3px;font-size:10px}.purchase-advice-row{display:grid;grid-template-columns:24px minmax(160px,1fr) minmax(120px,.6fr) 110px;gap:10px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}.purchase-advice-row:first-child{margin-top:12px}.purchase-advice-row strong,.purchase-advice-row small{display:block}.purchase-advice-row small{color:var(--muted);font-size:9px}.purchase-advice-row input[type=number]{padding:8px}.purchase-advice-actions{margin-top:12px}.purchase-advice-actions span{font-size:11px;color:var(--muted)}@media(max-width:650px){.purchase-advice-row{grid-template-columns:24px 1fr 90px}.purchase-advice-supplier{grid-column:2/-1}.purchase-advice-head{align-items:flex-start}.purchase-advice-actions{align-items:stretch;flex-direction:column}.purchase-advice-actions .button{width:100%}}`;
    document.head.appendChild(style);
  }

  function render() {
    ensurePanel();
    const target = document.getElementById('purchaseAdviceRows');
    const button = document.getElementById('createPurchaseDrafts');
    if (!target || !button) return;
    target.innerHTML = rows.length ? rows.map(row => `<div class="purchase-advice-row"><input type="checkbox" data-advice-select="${esc(row.itemId)}" checked><span><strong>${esc(row.name)}</strong><small>${esc(row.sku || 'Geen SKU')} · ${row.priceChange===null?'geen prijstrend':`${row.priceChange>0?'+':''}${row.priceChange}%`}${row.priceWarning?' · prijscontrole nodig':''}</small></span><span class="purchase-advice-supplier"><small>Geadviseerde leverancier</small><select data-advice-supplier="${esc(row.itemId)}">${row.alternatives.length?row.alternatives.map(option=>`<option value="${esc(option.supplierId||'')}" ${String(option.supplierId||'')===String(row.supplierId||'')?'selected':''}>${esc(option.supplierName)} · € ${Number(option.latestPrice).toFixed(2)} · ${option.score}/100</option>`).join(''):'<option value="">Niet gekoppeld</option>'}</select><input data-advice-reason="${esc(row.itemId)}" placeholder="Reden bij afwijkend advies"></span><input aria-label="Aantal ${esc(row.name)}" data-advice-quantity="${esc(row.itemId)}" type="number" min="0.001" step="0.001" value="${row.recommended}"></div>`).join('') : '<div class="empty">Geen artikelen met besteladvies.</div>';
    button.disabled = !rows.length;
  }

  async function refresh() {
    ensurePanel();
    const status = document.getElementById('purchaseAdviceStatus');
    if (status) status.textContent = 'Besteladvies laden…';
    try {
      const [state, reservationData, openData, intelligence, supplierData] = await Promise.all([api('/api/state'), api('/api/finance/reservations').catch(() => ({items:[]})), api('/api/purchase-advice/open'),api('/api/purchase-intelligence').catch(()=>({recommendations:[]})),api('/api/suppliers').catch(()=>({items:[]}))]);
      const items = new Map((state.items || []).map(item => [String(item.id), item]));
      const advice=new Map((intelligence.recommendations||[]).map(row=>[String(row.itemId),row]));
      rows = window.StockroomForecast.calculate(state, reservationData.items || []).map(row => {const openQuantity=Number(openData.items?.[String(row.itemId)]||0);return {...row,openQuantity,recommended:Math.max(0,row.recommended-openQuantity)}}).filter(row => row.recommended > 0).map(row => {const found=advice.get(String(row.itemId)),best=found?.recommended,known=found?.alternatives||[],missing=(supplierData.items||[]).filter(s=>!known.some(option=>String(option.supplierId||'')===String(s.id))).map(s=>({supplierId:s.id,supplierName:s.name,latestPrice:items.get(String(row.itemId))?.buy||0,score:0,noHistory:true}));return {...row,supplier:best?.supplierName||items.get(String(row.itemId))?.supplier||'',supplierId:best?.supplierId||'',alternatives:[...known,...missing],priceChange:best?.priceChange??null,priceWarning:Number(best?.priceChange||0)>=10}});
      render(); if (status) status.textContent = rows.length ? `${rows.length} adviesregels klaar voor controle.` : 'Voorraad is op peil.';
    } catch (error) { if (status) status.textContent = error.message; }
  }

  document.addEventListener('click', async event => {
    if (event.target.closest('#refreshPurchaseAdvice')) { refresh(); return; }
    const button = event.target.closest('#createPurchaseDrafts'); if (!button) return;
    const selected = rows.filter(row => document.querySelector(`[data-advice-select="${CSS.escape(String(row.itemId))}"]`)?.checked).map(row => ({item_id:row.itemId, quantity:Number(document.querySelector(`[data-advice-quantity="${CSS.escape(String(row.itemId))}"]`)?.value)+row.openQuantity,supplier_id:document.querySelector(`[data-advice-supplier="${CSS.escape(String(row.itemId))}"]`)?.value||'',override_reason:document.querySelector(`[data-advice-reason="${CSS.escape(String(row.itemId))}"]`)?.value||''}));
    if (!selected.length) { document.getElementById('purchaseAdviceStatus').textContent = 'Selecteer minimaal één artikel.'; return; }
    button.disabled = true;
    try {
      const body = new FormData(); body.set('lines_json', JSON.stringify(selected));
      const result = await api('/api/purchase-advice/drafts', {method:'POST', body});
      document.getElementById('purchaseAdviceStatus').textContent = `${result.created.length} conceptorder${result.created.length===1?'':'s'} gemaakt${result.skipped?` · ${result.skipped} al voldoende besteld`:''}.`;
      await refresh();
    } catch (error) { document.getElementById('purchaseAdviceStatus').textContent = error.message; }
    finally { button.disabled = false; }
  });

  document.addEventListener('stockroom:refresh', event => { if (event.detail?.view === 'inventory') refresh(); });
  ensurePanel(); refresh();
})();
import('/purchase_intelligence_ui.js?v=20260918-1').catch(() => {});
