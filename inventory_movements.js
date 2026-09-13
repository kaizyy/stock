(() => {
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const amount = value => Number(value || 0).toLocaleString('nl-NL', {maximumFractionDigits: 3});
  let selectedId = null;
  let currentState = {items: [], transactions: []};
  let reservations = [];
  let warehouseHistory = [];
  let loading = false;
  let warehouseError = false;

  function entriesFor(itemId, transactions, operations) {
    const entries = [];
    for (const transaction of transactions || []) {
      if (String(transaction.itemId) !== String(itemId)) continue;
      const qty = Number(transaction.qty || 0);
      let delta;
      let label;
      if (transaction.type === 'adjustment') {
        delta = qty;
        label = 'Voorraadcorrectie';
      } else if (transaction.type === 'incoming') {
        if (!transaction.done) continue;
        delta = qty;
        label = transaction.isReturn ? 'Inkoopretour' : 'Inkoop ontvangen';
      } else if (transaction.type === 'outgoing') {
        delta = -qty;
        label = transaction.isReturn ? 'Verkoopretour' : 'Verkoop geboekt';
      } else continue;
      entries.push({id: `tx:${transaction.id}`, date: transaction.date, label, delta,
        detail: transaction.reason || transaction.reference || transaction.party || (transaction.orderId ? `Order ${transaction.orderId}` : '')});
    }
    for (const operation of operations || []) {
      if (String(operation.item_id) !== String(itemId) || !['count', 'transfer_out', 'transfer_in'].includes(operation.operation_type)) continue;
      const delta = operation.previous_stock == null || operation.new_stock == null
        ? Number(operation.quantity || 0) * (operation.operation_type === 'transfer_out' ? -1 : 1)
        : Number(operation.new_stock) - Number(operation.previous_stock);
      entries.push({id: `warehouse:${operation.id}`, date: operation.created_at,
        label: {count:'Voorraadtelling', transfer_out:'Transfer uit', transfer_in:'Transfer in'}[operation.operation_type],
        delta, detail: operation.note || operation.reference || '',
        balance: operation.new_stock});
    }
    return entries.sort((a, b) => (Date.parse(b.date || 0) || 0) - (Date.parse(a.date || 0) || 0));
  }

  function csvCell(value, protectFormula = true) {
    let text = String(value ?? '');
    if (protectFormula && /^[\s]*[=+\-@\t\r]/.test(text)) text = `'${text}`;
    return `"${text.replace(/"/g, '""')}"`;
  }

  function csvFor(item, entries) {
    const header = ['Artikel', 'SKU', 'Datum', 'Soort', 'Verschil', 'Referentie / toelichting'];
    const rows = entries.map(entry => [item.name, item.sku || '', entry.date || '', entry.label,
      Number(entry.delta).toLocaleString('nl-NL', {useGrouping:false, maximumFractionDigits:3}), entry.detail || '']);
    return '\uFEFF' + [header, ...rows].map(row => row.map((cell, index) => csvCell(cell, index !== 4)).join(';')).join('\r\n') + '\r\n';
  }

  function csvFilename(item) {
    const identifier = String(item.sku || item.name || 'artikel').normalize('NFKD')
      .replace(/[^a-zA-Z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60) || 'artikel';
    return `voorraadmutaties-${identifier}.csv`;
  }

  if (typeof document === 'undefined') {
    if (typeof module !== 'undefined') module.exports = {entriesFor, csvFor, csvFilename};
    return;
  }

  function render() {
    const panel = document.getElementById('movementPanel');
    if (!panel) return;
    panel.hidden = !selectedId;
    if (!selectedId) return;
    const item = (currentState.items || []).find(row => String(row.id) === String(selectedId));
    if (!item) { selectedId = null; panel.hidden = true; return; }
    document.getElementById('movementTitle').textContent = `${item.name} · ${item.sku || 'geen SKU'}`;
    const reservation = reservations.find(row => String(row.item_id) === String(selectedId));
    const reserved = Number(reservation?.reserved || 0);
    const available = Number(item.stock || 0) - reserved;
    document.getElementById('movementSummary').textContent = `${amount(item.stock)} op voorraad · ${amount(reserved)} gereserveerd · ${amount(available)} vrij`;
    const sources = reservation?.sources || [];
    document.getElementById('movementReservations').innerHTML = sources.length
      ? sources.map(source => `<li><span>${escapeHtml(source.label || (source.type === 'invoice' ? 'Factuur' : 'Verkooporder'))}</span><strong>${amount(source.quantity)} gereserveerd</strong></li>`).join('')
      : '<li>Geen actieve reserveringen.</li>';
    const entries = entriesFor(selectedId, currentState.transactions, warehouseHistory);
    document.getElementById('downloadMovements').disabled = loading || warehouseError;
    const note = warehouseError ? '<p class="movement-note">Magazijnmutaties konden niet worden geladen. Vernieuw om opnieuw te proberen.</p>' : '';
    document.getElementById('movementEntries').innerHTML = `${note}${loading ? '<p class="movement-note">Mutaties laden…</p>' : ''}${entries.length
      ? entries.map(entry => `<li><div><strong>${escapeHtml(entry.label)}</strong><small>${entry.date && !Number.isNaN(Date.parse(entry.date)) ? new Date(entry.date).toLocaleString('nl-NL') : 'Datum onbekend'}${entry.detail ? ` · ${escapeHtml(entry.detail)}` : ''}</small></div><span class="${entry.delta < 0 ? 'negative-value' : 'positive-value'}">${entry.delta > 0 ? '+' : ''}${amount(entry.delta)}</span></li>`).join('')
      : '<li>Geen geboekte mutaties voor dit artikel.</li>'}`;
  }

  async function loadWarehouseHistory() {
    loading = true; warehouseError = false; render();
    try {
      const response = await fetch('/api/warehouse', {cache:'no-store'});
      if (!response.ok) throw new Error('Magazijnmutaties laden mislukt.');
      const data = await response.json();
      warehouseHistory = data.history || [];
    } catch { warehouseError = true; }
    loading = false; render();
  }

  function downloadCsv() {
    if (!selectedId || loading || warehouseError) return;
    const item = (currentState.items || []).find(row => String(row.id) === String(selectedId));
    if (!item) return;
    const blob = new Blob([csvFor(item, entriesFor(selectedId, currentState.transactions, warehouseHistory))],
      {type:'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = csvFilename(item);
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  document.addEventListener('click', event => {
    const button = event.target.closest('[data-show-movements]');
    if (button) {
      selectedId = selectedId === button.dataset.showMovements ? null : button.dataset.showMovements;
      render();
      if (selectedId) { document.getElementById('movementPanel')?.scrollIntoView({behavior:'smooth', block:'nearest'}); loadWarehouseHistory(); }
    }
    if (event.target.closest('#closeMovements')) { selectedId = null; render(); }
    if (event.target.closest('#refreshMovements') && selectedId) loadWarehouseHistory();
    if (event.target.closest('#downloadMovements')) downloadCsv();
  });

  window.StockroomMovements = {entriesFor, renderState(state, activeReservations) {
    currentState = state || {items:[], transactions:[]};
    reservations = activeReservations || [];
    render();
  }};
})();

