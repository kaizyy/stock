(() => {
  const euro = new Intl.NumberFormat('nl-NL', { style: 'currency', currency: 'EUR' });
  const integer = new Intl.NumberFormat('nl-NL');
  let period = '30d';
  let timer = null;

  function sinceFor(value) {
    const now = new Date();
    if (value === '7d') return new Date(now.getTime() - 7 * 86400000);
    if (value === '30d') return new Date(now.getTime() - 30 * 86400000);
    if (value === 'year') return new Date(now.getFullYear(), 0, 1);
    return null;
  }

  function inPeriod(t) {
    const since = sinceFor(period);
    if (!since) return true;
    const date = new Date(t.date);
    return !Number.isNaN(date.getTime()) && date >= since;
  }

  function ensureAnalyticsView() {
    let view = document.getElementById('analytics');
    if (!view) {
      view = document.createElement('section');
      view.id = 'analytics';
      view.className = 'view';
      view.innerHTML = '<div class="section-head"><div><p class="eyebrow">Inzichten en prestaties</p><h2>Analytics</h2></div></div><div id="analyticsContent"></div>';
      const outgoing = document.getElementById('outgoing');
      outgoing?.insertAdjacentElement('afterend', view);
    }

    const nav = document.querySelector('.sidebar nav');
    if (nav && !nav.querySelector('[data-view="analytics"]')) {
      const button = document.createElement('button');
      button.className = 'nav-item';
      button.dataset.view = 'analytics';
      button.innerHTML = '<span>◫</span>Analytics';
      const inventoryButton = nav.querySelector('[data-view="inventory"]');
      if (inventoryButton) inventoryButton.insertAdjacentElement('beforebegin', button);
      else nav.appendChild(button);
    }

    const content = view.querySelector('#analyticsContent');
    const revenueMetric = document.querySelector('#overview .revenue-metric');
    const chartPanel = document.querySelector('#overview .chart-panel');
    if (revenueMetric && !content.querySelector('.analytics-existing-metrics')) {
      const row = document.createElement('div');
      row.className = 'analytics-existing-metrics';
      content.appendChild(row);
      row.appendChild(revenueMetric);
    }
    if (chartPanel && chartPanel.parentElement !== content) content.appendChild(chartPanel);
    return { view, content };
  }

  function injectPanel() {
    if (document.getElementById('salesAnalyticsPanel')) return;
    const { content } = ensureAnalyticsView();
    if (!content) return;
    const panel = document.createElement('article');
    panel.id = 'salesAnalyticsPanel';
    panel.className = 'panel analytics-panel';
    panel.innerHTML = `
      <div class="panel-head analytics-head">
        <div><p class="eyebrow">Verkoopanalyse</p><h3>Analytics & winst</h3></div>
        <div class="analytics-period" role="group" aria-label="Analyseperiode">
          <button type="button" data-analytics-period="7d">7 dagen</button>
          <button type="button" data-analytics-period="30d" class="active">30 dagen</button>
          <button type="button" data-analytics-period="year">Dit jaar</button>
          <button type="button" data-analytics-period="all">Alles</button>
        </div>
      </div>
      <div class="analytics-kpis">
        <div><small>Verkoopomzet</small><strong id="analyticsRevenue">€ 0,00</strong></div>
        <div><small>Brutowinst</small><strong id="analyticsProfit">€ 0,00</strong></div>
        <div><small>Gem. marge</small><strong id="analyticsMargin">0%</strong></div>
        <div><small>Verkochte stuks</small><strong id="analyticsUnits">0</strong></div>
        <div><small>Gem. verkoopprijs</small><strong id="analyticsAverage">€ 0,00</strong></div>
      </div>
      <div class="analytics-top"><h4>Top 5 artikelen</h4><div id="analyticsTopItems"></div></div>`;
    content.insertAdjacentElement('afterbegin', panel);

    const style = document.createElement('style');
    style.textContent = `
      #analyticsContent{display:grid;gap:24px}.analytics-existing-metrics{display:grid;grid-template-columns:minmax(0,360px);gap:16px}.analytics-existing-metrics .metric{height:100%}.analytics-panel{margin-top:0}.analytics-head{gap:16px;align-items:flex-start}.analytics-period{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}.analytics-period button{border:1px solid #e3e7e4;background:#fff;border-radius:999px;padding:7px 11px;font:inherit;font-size:12px;cursor:pointer}.analytics-period button.active{background:#17211b;color:#fff;border-color:#17211b}.analytics-kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin:18px 0}.analytics-kpis>div{padding:15px;border:1px solid #edf0ed;border-radius:14px;background:#fafbfa}.analytics-kpis small{display:block;color:#6d756f;margin-bottom:7px}.analytics-kpis strong{font-size:20px}.analytics-top h4{margin:4px 0 12px}.analytics-row{display:grid;grid-template-columns:minmax(120px,1fr) 90px 110px 110px;gap:12px;padding:10px 0;border-top:1px solid #edf0ed;align-items:center}.analytics-empty{padding:14px 0;color:#727a74}@media(max-width:900px){.analytics-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.analytics-row{grid-template-columns:1fr 70px}.analytics-row .analytics-secondary{display:none}}@media(max-width:520px){.analytics-kpis{grid-template-columns:1fr}.analytics-head{display:block}.analytics-period{justify-content:flex-start;margin-top:12px}.analytics-existing-metrics{grid-template-columns:1fr}}
    `;
    document.head.appendChild(style);
  }

  async function refresh() {
    ensureAnalyticsView();
    injectPanel();
    if (!document.getElementById('salesAnalyticsPanel')) return;
    try {
      const response = await fetch('/api/state', { cache: 'no-store' });
      if (!response.ok) throw new Error('state');
      const state = await response.json();
      const items = new Map((state.items || []).map(i => [i.id, i]));
      const sales = (state.transactions || []).filter(t => t.type === 'outgoing' && inPeriod(t));
      const units = sales.reduce((s,t) => s + Number(t.qty || 0), 0);
      const revenue = sales.reduce((s,t) => s + Number(t.qty || 0) * Number(t.price || 0), 0);
      const cost = sales.reduce((s,t) => {
        const item = items.get(t.itemId);
        const unitCost = Number(t.buyPrice ?? t.costPrice ?? item?.buy ?? 0);
        return s + Number(t.qty || 0) * unitCost;
      }, 0);
      const profit = revenue - cost;
      const margin = revenue > 0 ? (profit / revenue) * 100 : 0;
      const average = units > 0 ? revenue / units : 0;
      document.getElementById('analyticsRevenue').textContent = euro.format(revenue);
      document.getElementById('analyticsProfit').textContent = euro.format(profit);
      document.getElementById('analyticsMargin').textContent = `${margin.toLocaleString('nl-NL',{maximumFractionDigits:1})}%`;
      document.getElementById('analyticsUnits').textContent = integer.format(units);
      document.getElementById('analyticsAverage').textContent = euro.format(average);

      const grouped = new Map();
      sales.forEach(t => {
        const qty = Number(t.qty || 0), saleRevenue = qty * Number(t.price || 0);
        const item = items.get(t.itemId);
        const unitCost = Number(t.buyPrice ?? t.costPrice ?? item?.buy ?? 0);
        const current = grouped.get(t.itemId) || { name: item?.name || 'Onbekend item', units: 0, revenue: 0, profit: 0 };
        current.units += qty;
        current.revenue += saleRevenue;
        current.profit += saleRevenue - qty * unitCost;
        grouped.set(t.itemId, current);
      });
      const top = [...grouped.values()].sort((a,b) => b.units - a.units || b.revenue - a.revenue).slice(0,5);
      document.getElementById('analyticsTopItems').innerHTML = top.length ? top.map((x,i) => `<div class="analytics-row"><strong>${i+1}. ${x.name}</strong><span>${integer.format(x.units)} stuks</span><span class="analytics-secondary">${euro.format(x.revenue)} omzet</span><span class="analytics-secondary">${euro.format(x.profit)} winst</span></div>`).join('') : '<div class="analytics-empty">Geen verkopen in deze periode.</div>';
    } catch {
      const top = document.getElementById('analyticsTopItems');
      if (top) top.innerHTML = '<div class="analytics-empty">Analytics konden niet worden geladen.</div>';
    }
  }

  document.addEventListener('click', e => {
    const periodButton = e.target.closest('[data-analytics-period]');
    if (periodButton) {
      period = periodButton.dataset.analyticsPeriod;
      document.querySelectorAll('[data-analytics-period]').forEach(b => b.classList.toggle('active', b === periodButton));
      refresh();
      return;
    }
    if (e.target.closest('#saveTransaction,[data-toggle],[data-delete-transaction],[data-delete-order]')) {
      clearTimeout(timer);
      timer = setTimeout(refresh, 150);
    }
  });
  document.addEventListener('submit', e => {
    if (e.target?.id === 'transactionForm') {
      clearTimeout(timer);
      timer = setTimeout(refresh, 150);
    }
  });
  window.addEventListener('load', refresh);
  setInterval(refresh, 60000);
})();
