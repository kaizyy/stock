(() => {
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const quantity = value => value == null ? '—' : Number(value).toLocaleString('nl-NL', {maximumFractionDigits:3});

  function summarize(rows) {
    return {differences:rows.filter(row => row.status === 'difference').length,
      checked:rows.filter(row => row.status === 'ok' || row.status === 'difference').length,
      unavailable:rows.filter(row => row.status === 'unavailable').length};
  }

  if (typeof document === 'undefined') {
    if (typeof module !== 'undefined') module.exports = {summarize};
    return;
  }

  function render(rows) {
    const summary = summarize(rows);
    const status = document.getElementById('reconciliationSummary');
    const body = document.getElementById('reconciliationTable');
    if (!status || !body) return;
    status.textContent = `${summary.differences} mogelijke afwijking${summary.differences === 1 ? '' : 'en'} · ${summary.checked} gecontroleerd · ${summary.unavailable} zonder betrouwbare basis`;
    status.classList.toggle('reconciliation-warning', summary.differences > 0);
    body.innerHTML = rows.length ? rows.map(row => {
      const label = row.status === 'difference' ? 'Mogelijk verschil' : row.status === 'ok' ? 'In balans' : 'Niet controleerbaar';
      const date = row.counted_at && !Number.isNaN(Date.parse(row.counted_at)) ? new Date(row.counted_at).toLocaleDateString('nl-NL') : 'Geen telling';
      const difference = row.difference == null ? '—' : `${row.difference > 0 ? '+' : ''}${quantity(row.difference)}`;
      return `<tr class="reconciliation-${escapeHtml(row.status)}"><td><strong>${escapeHtml(row.name)}</strong><small>${escapeHtml(row.sku)}</small></td><td>${quantity(row.actual)}</td><td>${quantity(row.expected)}</td><td><strong>${difference}</strong></td><td><span class="pill ${row.status === 'difference' ? 'warn' : row.status === 'ok' ? 'good' : ''}">${label}</span><small>${date}</small></td></tr>`;
    }).join('') : '<tr><td colspan="5" class="empty">Geen voorraadartikelen.</td></tr>';
  }

  async function refresh() {
    const button = document.getElementById('refreshReconciliation');
    const status = document.getElementById('reconciliationSummary');
    if (!button || !status) return;
    button.disabled = true;
    status.textContent = 'Voorraad controleren…';
    try {
      const response = await fetch('/api/inventory/reconciliation', {cache:'no-store'});
      if (!response.ok) throw new Error('Voorraadcontrole kon niet worden geladen.');
      const data = await response.json();
      render(data.items || []);
    } catch (error) {
      status.textContent = error.message || 'Voorraadcontrole kon niet worden geladen.';
      status.classList.add('reconciliation-warning');
    } finally { button.disabled = false; }
  }

  document.addEventListener('click', event => {
    if (event.target.closest('#refreshReconciliation')) refresh();
  });
})();

