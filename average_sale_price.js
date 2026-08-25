(() => {
  const euro = new Intl.NumberFormat('nl-NL', { style: 'currency', currency: 'EUR' });
  let refreshTimer = null;

  function ensureCard() {
    const grid = document.querySelector('#overview .summary-grid');
    if (!grid || document.getElementById('averageSalePriceValue')) return;
    const card = document.createElement('article');
    card.className = 'metric average-sale-price-metric';
    card.innerHTML = '<div class="metric-icon green">€</div><p>Gemiddelde verkoopprijs</p><h2 id="averageSalePriceValue">€ 0,00</h2><span id="averageSalePriceCount">Nog geen verkochte items</span>';
    grid.appendChild(card);
  }

  async function refreshAverageSalePrice() {
    ensureCard();
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
    refreshTimer = setTimeout(refreshAverageSalePrice, 180);
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
