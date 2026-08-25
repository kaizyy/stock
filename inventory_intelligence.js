(() => {
  const esc = v => String(v ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  let me = null;
  let state = null;
  let refreshTimer = null;

  async function fetchJSON(url, options) {
    const r = await fetch(url, options);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || 'Laden mislukt.');
    return data;
  }

  function salesRate(itemId) {
    const since = Date.now() - 30 * 86400000;
    const units = (state?.transactions || []).filter(t => t.type === 'outgoing' && t.itemId === itemId && new Date(t.date).getTime() >= since).reduce((s,t) => s + Number(t.qty || 0), 0);
    return units / 30;
  }

  function advice(item) {
    const rate = salesRate(item.id);
    const stock = Number(item.stock || 0);
    const min = Math.max(0, Number(item.minStock || 0));
    const daysLeft = rate > 0 ? stock / rate : null;
    const target = Math.ceil(rate * 30 + min);
    const reorder = Math.max(0, target - stock);
    return { rate, daysLeft, reorder };
  }

  function injectUI() {
    const settings = document.getElementById('settings');
    if (settings && !document.getElementById('barcodePanel')) {
      const host = settings.querySelector('.settings-grid > div:first-child') || settings;
      const panel = document.createElement('article');
      panel.id = 'barcodePanel'; panel.className = 'feature-panel';
      panel.innerHTML = `<h3>Barcodes & besteladvies</h3><p>Beheer EAN/barcodes en bekijk voorraadprognoses op basis van de verkopen van de laatste 30 dagen.</p><div id="barcodeTableWrap">Laden…</div>`;
      host.appendChild(panel);
    }
    const inventory = document.getElementById('inventory');
    const actions = inventory?.querySelector('.section-actions');
    if (actions && !document.getElementById('scanInventoryBtn')) {
      const button = document.createElement('button'); button.id='scanInventoryBtn'; button.className='button ghost'; button.type='button'; button.textContent='▣ Barcode scannen'; actions.prepend(button);
    }
    const select = document.getElementById('itemSelect');
    if (select && !document.getElementById('scanTransactionBtn')) {
      const button = document.createElement('button'); button.id='scanTransactionBtn'; button.type='button'; button.className='button ghost'; button.style.marginTop='8px'; button.textContent='▣ Scan barcode'; select.parentElement.appendChild(button);
    }
    if (!document.getElementById('barcodeScannerDialog')) {
      const dialog = document.createElement('dialog'); dialog.id='barcodeScannerDialog';
      dialog.innerHTML = `<div style="padding:20px;min-width:min(520px,90vw)"><div class="dialog-head"><div><p class="eyebrow">Barcode</p><h2>Scanner</h2></div><button class="icon-button" data-close-scanner aria-label="Sluiten">×</button></div><video id="barcodeVideo" autoplay playsinline style="width:100%;max-height:320px;background:#111;border-radius:12px"></video><p id="barcodeScannerStatus" class="note">Camera wordt gestart…</p><div class="feature-row"><input id="barcodeManual" inputmode="numeric" placeholder="Barcode handmatig invoeren"><button class="button primary" id="barcodeManualSubmit" type="button">Zoeken</button></div></div>`;
      document.body.appendChild(dialog);
    }
    if (!document.getElementById('inventoryIntelligenceStyle')) {
      const style=document.createElement('style'); style.id='inventoryIntelligenceStyle'; style.textContent=`.barcode-table{width:100%;border-collapse:collapse}.barcode-table th,.barcode-table td{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}.barcode-table input{width:100%;min-width:120px;border:1px solid var(--line);border-radius:8px;padding:8px;font:inherit}.forecast-good{color:#087f5b}.forecast-warn{color:#b7791f}.forecast-danger{color:#b42318}.reorder-chip{display:inline-block;padding:4px 8px;border-radius:999px;background:#fff3d6}.barcode-actions{display:flex;gap:6px;flex-wrap:wrap}@media(max-width:760px){#barcodeTableWrap{overflow:auto}.barcode-table{min-width:850px}}`; document.head.appendChild(style);
    }
  }

  async function saveBarcode(itemId, barcode) {
    const latest = await fetchJSON('/api/state', {cache:'no-store'});
    const duplicate = (latest.items || []).find(i => String(i.barcode || '').trim() === barcode && i.id !== itemId);
    if (barcode && duplicate) throw new Error(`Barcode is al gekoppeld aan ${duplicate.name}.`);
    const item = (latest.items || []).find(i => i.id === itemId);
    if (!item) throw new Error('Artikel niet gevonden.');
    item.barcode = barcode;
    const r = await fetch('/api/state',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(latest)});
    if (!r.ok) { const e = await r.json().catch(()=>({})); throw new Error(e.error || 'Barcode opslaan mislukt.'); }
  }

  function renderPanel() {
    const wrap=document.getElementById('barcodeTableWrap'); if (!wrap || !state) return;
    const items=(state.items||[]).filter(i=>!i.archived);
    wrap.innerHTML = items.length ? `<table class="barcode-table"><thead><tr><th>Artikel</th><th>Barcode/EAN</th><th>Leverancier</th><th>Verkoop/dag</th><th>Voorraadduur</th><th>Besteladvies</th><th></th></tr></thead><tbody>${items.map(i=>{const a=advice(i);const cls=a.daysLeft===null?'':a.daysLeft<7?'forecast-danger':a.daysLeft<21?'forecast-warn':'forecast-good';const days=a.daysLeft===null?'Geen verkoopdata':`${Math.max(0,a.daysLeft).toLocaleString('nl-NL',{maximumFractionDigits:1})} dagen`;return `<tr data-barcode-row="${esc(i.id)}"><td><strong>${esc(i.name)}</strong><br><small>${esc(i.sku)}</small></td><td><input data-barcode-input value="${esc(i.barcode||'')}" placeholder="EAN / UPC"></td><td>${esc(i.supplier||'—')}</td><td>${a.rate.toLocaleString('nl-NL',{maximumFractionDigits:2})}</td><td class="${cls}">${days}</td><td>${a.reorder>0?`<span class="reorder-chip">Bestel ${a.reorder}</span>`:'Voldoende voorraad'}</td><td><button class="button ghost" data-save-barcode="${esc(i.id)}" type="button">Opslaan</button></td></tr>`}).join('')}</tbody></table>` : '<div class="empty">Nog geen artikelen.</div>';
  }

  function improveLowStock() {
    const box=document.getElementById('lowStockBox'); if(!box||!state) return;
    const low=(state.items||[]).filter(i=>!i.archived && Number(i.minStock||0)>0 && Number(i.stock||0)<=Number(i.minStock||0));
    if(!low.length) return;
    box.classList.add('show');
    box.innerHTML=`<strong>⚠ Lage voorraad & besteladvies</strong><span>${low.map(i=>{const a=advice(i);return `${esc(i.name)}: ${Number(i.stock||0)} op voorraad${i.supplier?` · ${esc(i.supplier)}`:''}${a.reorder>0?` · advies bestel ${a.reorder}`:''}`}).join('<br>')}</span>`;
  }

  async function refresh() {
    injectUI();
    try { me = await fetchJSON('/api/me',{cache:'no-store'}); state = await fetchJSON('/api/state',{cache:'no-store'}); const panel=document.getElementById('barcodePanel'); if(panel) panel.hidden=!me.permissions.manageItems; renderPanel(); improveLowStock(); } catch {}
  }

  let stream=null, scanTimer=null, scanTarget='inventory';
  function stopScanner(){ if(scanTimer) clearInterval(scanTimer); scanTimer=null; if(stream){stream.getTracks().forEach(t=>t.stop());stream=null;} const v=document.getElementById('barcodeVideo'); if(v) v.srcObject=null; }
  function useBarcode(value){ const barcode=String(value||'').trim(); if(!barcode||!state) return; const found=(state.items||[]).find(i=>String(i.barcode||'').trim()===barcode); const status=document.getElementById('barcodeScannerStatus'); if(!found){ if(status) status.textContent=`Geen artikel gevonden voor ${barcode}.`; return; } if(scanTarget==='transaction'){ const sel=document.getElementById('itemSelect'); if(sel){sel.value=found.id;sel.dispatchEvent(new Event('change'));} if(status) status.textContent=`${found.name} geselecteerd.`; setTimeout(()=>document.getElementById('barcodeScannerDialog')?.close(),350); } else { if(status) status.textContent=`Gevonden: ${found.name} · voorraad ${Number(found.stock||0)}.`; document.querySelector(`[data-item-row="${CSS.escape(found.id)}"]`)?.scrollIntoView({behavior:'smooth',block:'center'}); } }
  async function openScanner(target){ scanTarget=target; const d=document.getElementById('barcodeScannerDialog'); d.showModal(); document.getElementById('barcodeScannerStatus').textContent='Camera wordt gestart…'; try { if(!('BarcodeDetector' in window)) throw new Error('BarcodeDetector niet beschikbaar'); const detector=new BarcodeDetector({formats:['ean_13','ean_8','upc_a','upc_e','code_128','code_39','itf']}); stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'}},audio:false}); const video=document.getElementById('barcodeVideo'); video.srcObject=stream; scanTimer=setInterval(async()=>{try{const codes=await detector.detect(video);if(codes[0]?.rawValue){useBarcode(codes[0].rawValue);stopScanner();}}catch{}},350); document.getElementById('barcodeScannerStatus').textContent='Richt de camera op de barcode.'; } catch { document.getElementById('barcodeScannerStatus').textContent='Camerascanner niet beschikbaar. Voer de barcode hieronder handmatig in.'; } }

  document.addEventListener('click', async e=>{
    if(e.target.closest('#scanInventoryBtn')) return openScanner('inventory');
    if(e.target.closest('#scanTransactionBtn')) return openScanner('transaction');
    if(e.target.closest('[data-close-scanner]')){stopScanner();document.getElementById('barcodeScannerDialog')?.close();return;}
    if(e.target.closest('#barcodeManualSubmit')) return useBarcode(document.getElementById('barcodeManual').value);
    const save=e.target.closest('[data-save-barcode]'); if(save){const row=e.target.closest('[data-barcode-row]');try{await saveBarcode(save.dataset.saveBarcode,row.querySelector('[data-barcode-input]').value.trim());await refresh();}catch(err){alert(err.message);}return;}
    if(e.target.closest('#saveTransaction,[data-toggle],[data-delete-transaction],[data-correct],[data-save-meta]')){clearTimeout(refreshTimer);refreshTimer=setTimeout(refresh,180);}
  });
  document.getElementById('barcodeScannerDialog')?.addEventListener('close',stopScanner);
  window.addEventListener('load',refresh); setInterval(refresh,60000);
})();
