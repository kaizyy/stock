(() => {
  const esc = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const euro = new Intl.NumberFormat('nl-NL', {style:'currency', currency:'EUR'});
  let currentOrder = '';

  async function api(url, options={}) {
    const response = await fetch(url, {cache:'no-store', ...options});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'Actie mislukt.');
    return data;
  }
  function ensureUI() {
    if (document.getElementById('returnDialog')) return;
    const dialog = document.createElement('dialog');
    dialog.id = 'returnDialog';
    dialog.innerHTML = '<div class="return-shell"><div class="dialog-head"><div><p class="eyebrow">Gekoppelde historie</p><h2>Retouren</h2></div><button type="button" class="icon-button" data-close-return>×</button></div><div id="returnContent"></div></div>';
    document.body.appendChild(dialog);
    const style = document.createElement('style');
    style.textContent = '.return-shell{width:min(760px,92vw);max-height:86vh;overflow:auto}.return-grid{display:grid;grid-template-columns:1fr 110px;gap:8px;align-items:center}.return-grid input,.return-form input,.return-form textarea{border:1px solid var(--line);border-radius:9px;padding:9px;font:inherit;width:100%;box-sizing:border-box}.return-form{display:grid;gap:10px}.return-card{border:1px solid var(--line);border-radius:12px;padding:12px;margin-top:10px}.return-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:8px}.return-muted{display:block;color:var(--muted);font-size:12px}@media(max-width:600px){.return-grid{grid-template-columns:1fr 85px}.return-shell{width:88vw}}';
    document.head.appendChild(style);
  }
  function decorate() {
    document.querySelectorAll('.order-card[data-order-card-id]').forEach(card => {
      if (card.querySelector('[data-open-returns]')) return;
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'crm-edit';
      button.dataset.openReturns = card.dataset.orderCardId; button.textContent = 'Retouren';
      card.appendChild(button);
    });
  }
  const statusLabel = status => ({registered:'Aangemeld', processed:'Afgehandeld', cancelled:'Geannuleerd'}[status] || status);
  async function render() {
    const root = document.getElementById('returnContent');
    root.innerHTML = '<p>Retourgegevens laden…</p>';
    try {
      const data = await api(`/api/orders/returns?order_id=${encodeURIComponent(currentOrder)}`);
      const available = data.lines.filter(line => Number(line.available_quantity) > 0);
      const form = available.length ? `<form class="return-form" id="returnForm"><input name="reference" placeholder="Retourreferentie (optioneel)"><textarea name="reason" placeholder="Reden van retour" rows="2"></textarea><div class="return-grid">${available.map(line => `<label>${esc(line.item_name)}<span class="return-muted">Geleverd ${Number(line.fulfilled_quantity)}, nog mogelijk ${Number(line.available_quantity)}</span></label><input type="number" min="0" max="${Number(line.available_quantity)}" step="0.001" value="0" data-return-line="${esc(line.id)}">`).join('')}</div><button class="button primary">Retour aanmelden</button></form>` : '<p class="return-muted">Er zijn geen geleverde aantallen meer beschikbaar voor een nieuwe retour.</p>';
      const history = data.returns.map(item => `<article class="return-card"><strong>${statusLabel(item.status)} · ${new Date(item.created_at).toLocaleDateString('nl-NL')}</strong><div>${item.lines.map(line => `${Number(line.quantity)}× ${esc(line.item_name)}`).join(' · ')}</div>${item.reference ? `<span class="return-muted">${esc(item.reference)}</span>` : ''}${item.reason ? `<div>${esc(item.reason)}</div>` : ''}${item.return_type === 'sales' && Number(item.credit_amount) > 0 ? `<div>Creditvoorstel: ${euro.format(item.credit_amount)}</div>` : ''}<div class="return-actions">${item.status === 'registered' ? `<button type="button" class="button primary" data-return-action="process" data-id="${item.id}">Verwerken</button><button type="button" class="button ghost" data-return-action="cancel" data-id="${item.id}">Annuleren</button>` : ''}${item.status === 'processed' && !item.credit_note_id ? `<button type="button" class="button ghost" data-return-action="reverse" data-id="${item.id}">Terugdraaien</button>${item.return_type === 'sales' ? `<button type="button" class="button primary" data-return-action="credit" data-id="${item.id}">Creditnota maken</button>` : ''}` : ''}${item.credit_note_id ? '<span class="return-muted">Creditnota aangemaakt</span>' : ''}</div></article>`).join('');
      root.innerHTML = form + history;
    } catch (error) { root.innerHTML = `<p class="crm-msg err">${esc(error.message)}</p>`; }
  }
  async function open(orderId) { ensureUI(); currentOrder = orderId; document.getElementById('returnDialog').showModal(); await render(); }
  document.addEventListener('submit', async event => {
    if (event.target.id !== 'returnForm') return;
    event.preventDefault();
    const body = new FormData(event.target);
    const lines = [...event.target.querySelectorAll('[data-return-line]')].map(input => ({line_id:input.dataset.returnLine, quantity:Number(input.value)})).filter(line => line.quantity > 0);
    body.set('order_id', currentOrder); body.set('lines_json', JSON.stringify(lines));
    try { await api('/api/orders/returns', {method:'POST', body}); await render(); } catch (error) { alert(error.message); }
  });
  document.addEventListener('click', async event => {
    const opener = event.target.closest('[data-open-returns]');
    if (opener) { open(opener.dataset.openReturns); return; }
    if (event.target.closest('[data-close-return]')) { document.getElementById('returnDialog').close(); return; }
    const action = event.target.closest('[data-return-action]');
    if (!action) return;
    const wording = {process:'verwerken en de voorraad aanpassen', cancel:'annuleren', reverse:'terugdraaien', credit:'omzetten in een creditnota'}[action.dataset.returnAction];
    if (!confirm(`Deze retour ${wording}?`)) return;
    const body = new FormData(); body.set('return_id', action.dataset.id);
    try {
      await api(`/api/orders/returns/${action.dataset.returnAction}`, {method:'POST', body});
      await render(); document.dispatchEvent(new CustomEvent('orders:refresh'));
      document.dispatchEvent(new CustomEvent('stockroom:refresh', {detail:{view:'orders'}}));
    } catch (error) { alert(error.message); }
  });
  ensureUI(); decorate();
  new MutationObserver(decorate).observe(document.body, {childList:true, subtree:true});
})();
