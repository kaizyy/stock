(() => {
  const style = document.createElement('style');
  style.textContent = `
    .role-dashboard-note{display:none;margin:-8px 0 18px;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:#fff;color:var(--muted);font-size:12px}
    .role-dashboard-note.show{display:block}
    body[data-dashboard-role="buyer"] .revenue-metric,
    body[data-dashboard-role="buyer"] .outstanding-metric,
    body[data-dashboard-role="buyer"] .status-card.warning,
    body[data-dashboard-role="buyer"] .chart-grid section:nth-child(2),
    body[data-dashboard-role="buyer"] .chart-legend,
    body[data-dashboard-role="buyer"] [data-view="outgoing"],
    body[data-dashboard-role="buyer"] #outgoing,
    body[data-dashboard-role="buyer"] #inventoryTable tr > :nth-child(5),
    body[data-dashboard-role="buyer"] #inventoryTable tr > :nth-child(6),
    body[data-dashboard-role="buyer"] #inventory thead tr > :nth-child(5),
    body[data-dashboard-role="buyer"] #inventory thead tr > :nth-child(6){display:none!important}

    body[data-dashboard-role="seller"] .inventory-metric .inventory-primary,
    body[data-dashboard-role="seller"] .inventory-metric .metric-breakdown > div:first-child,
    body[data-dashboard-role="seller"] .inventory-metric .metric-breakdown > div:nth-child(2),
    body[data-dashboard-role="seller"] .expected-metric,
    body[data-dashboard-role="seller"] .status-card.info,
    body[data-dashboard-role="seller"] [data-view="incoming"],
    body[data-dashboard-role="seller"] #incoming,
    body[data-dashboard-role="seller"] #inventoryTable tr > :nth-child(4),
    body[data-dashboard-role="seller"] #inventoryTable tr > :nth-child(6),
    body[data-dashboard-role="seller"] #inventory thead tr > :nth-child(4),
    body[data-dashboard-role="seller"] #inventory thead tr > :nth-child(6){display:none!important}

    body[data-dashboard-role="viewer"] .inventory-metric .inventory-primary,
    body[data-dashboard-role="viewer"] .inventory-metric .metric-breakdown,
    body[data-dashboard-role="viewer"] .revenue-metric,
    body[data-dashboard-role="viewer"] .outstanding-metric,
    body[data-dashboard-role="viewer"] .chart-grid section:nth-child(2),
    body[data-dashboard-role="viewer"] .chart-legend,
    body[data-dashboard-role="viewer"] .status-card.warning,
    body[data-dashboard-role="viewer"] #inventoryTable tr > :nth-child(4),
    body[data-dashboard-role="viewer"] #inventoryTable tr > :nth-child(5),
    body[data-dashboard-role="viewer"] #inventoryTable tr > :nth-child(6),
    body[data-dashboard-role="viewer"] #inventory thead tr > :nth-child(4),
    body[data-dashboard-role="viewer"] #inventory thead tr > :nth-child(5),
    body[data-dashboard-role="viewer"] #inventory thead tr > :nth-child(6){display:none!important}

    body[data-dashboard-role="buyer"] .summary-grid{grid-template-columns:1.35fr 1fr}
    body[data-dashboard-role="seller"] .summary-grid{grid-template-columns:1.35fr 1fr}
    body[data-dashboard-role="viewer"] .summary-grid{grid-template-columns:1.35fr 1fr}
    body[data-dashboard-role="buyer"] .chart-grid,
    body[data-dashboard-role="viewer"] .chart-grid{grid-template-columns:1fr}
    @media(max-width:900px){body[data-dashboard-role] .summary-grid{grid-template-columns:1fr 1fr}}
    @media(max-width:620px){body[data-dashboard-role] .summary-grid{grid-template-columns:1fr}}
  `;
  document.head.appendChild(style);

  const overview = document.getElementById('overview');
  if (!overview) return;

  const note = document.createElement('div');
  note.id = 'roleDashboardNote';
  note.className = 'role-dashboard-note';
  overview.prepend(note);

  const roleText = {
    buyer: 'Inkoper-weergave: je ziet voorraad en inkomende processen. Verkoop- en omzetinformatie is verborgen.',
    seller: 'Verkoper-weergave: je ziet voorraad en uitgaande processen. Inkoopinformatie is verborgen.',
    viewer: 'Viewer-weergave: alleen-lezen. Financiële KPI’s en prijsinformatie zijn verborgen.'
  };

  function forceOverviewIfBlocked(role) {
    const active = document.querySelector('.view.active-view')?.id;
    if ((role === 'buyer' && active === 'outgoing') || (role === 'seller' && active === 'incoming')) {
      if (typeof showView === 'function') showView('overview');
      else {
        document.querySelectorAll('.view').forEach(v => v.classList.toggle('active-view', v.id === 'overview'));
      }
    }
  }

  function filterActivity(role) {
    const list = document.getElementById('activityList');
    if (!list) return;
    const rows = [...list.querySelectorAll('.activity')];
    rows.forEach(row => {
      const text = row.textContent.toLowerCase();
      if (role === 'buyer') row.hidden = text.includes('verkocht');
      else if (role === 'seller') row.hidden = text.includes('ingekocht');
      else if (role === 'viewer') row.hidden = false;
      else row.hidden = false;
    });
  }

  async function applyDashboardRole() {
    try {
      const response = await fetch('/api/me', {cache:'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      const role = data?.stockroom?.role || 'viewer';
      document.body.dataset.dashboardRole = role;
      if (roleText[role]) {
        note.textContent = roleText[role];
        note.classList.add('show');
      } else {
        note.classList.remove('show');
      }
      forceOverviewIfBlocked(role);
      filterActivity(role);
      setTimeout(() => filterActivity(role), 250);
    } catch {}
  }

  const originalRender = window.render;
  if (typeof originalRender === 'function') {
    window.render = function(...args) {
      const result = originalRender.apply(this, args);
      const role = document.body.dataset.dashboardRole;
      if (role) setTimeout(() => filterActivity(role), 0);
      return result;
    };
  }

  document.addEventListener('click', event => {
    const target = event.target.closest('[data-view],[data-go]');
    if (!target) return;
    const role = document.body.dataset.dashboardRole;
    const view = target.dataset.view || target.dataset.go;
    if ((role === 'buyer' && view === 'outgoing') || (role === 'seller' && view === 'incoming')) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);

  applyDashboardRole();
})();
