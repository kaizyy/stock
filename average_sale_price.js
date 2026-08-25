(() => {
  const euro = new Intl.NumberFormat('nl-NL', { style: 'currency', currency: 'EUR' });
  let refreshTimer = null;

  async function refreshAverageSalePrice() {
    const valueEl = document.getElementById('averageSalePriceValue');
    const countEl = document.getElementById('averageSalePriceCount');
    if (!valueEl || !countEl) return;

    try {
      const response = await fetch('/api/state', { cache: 'no-store' });
      if (!response.ok) throw new Error('state');
      const state = await response.json();
      const sales = (state.transactions || []).filter(t => t.type === 'outgoing');
      const totalUnits = sales.reduce((sum, t) => sum + Number(t.qty || 0), 0);
      const totalRevenue = sales.reduce((sum, t) => sum + (Number(t.qty || 0) * Number(t.price || 0)), 0);
      const average = totalUnits > 0 ? totalRevenue / totalUnits : 0;

      valueEl.textContent = euro.format(average);
      countEl.textContent = totalUnits > 0
        ? `Gebaseerd op ${totalUnits} verkocht${totalUnits === 1 ? ' item' : 'e items'}`
        : 'Nog geen verkochte items';
    } catch {
      valueEl.textContent = '—';
      countEl.textContent = 'Kon niet worden berekend';
    }
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refreshAverageSalePrice, 120);
  }

  window.addEventListener('load', refreshAverageSalePrice);
  document.addEventListener('click', event => {
    if (event.target.closest('#saveTransaction,[data-toggle],[data-delete-transaction],.edit-transaction')) {
      scheduleRefresh();
    }
  });
  document.addEventListener('submit', event => {
    if (event.target?.id === 'transactionForm') scheduleRefresh();
  });
  window.addEventListener('hashchange', scheduleRefresh);
  setInterval(refreshAverageSalePrice, 60_000);
})();
