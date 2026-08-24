(() => {
  const roleLabels = {owner:'Owner',admin:'Admin',member:'Gebruiker',buyer:'Inkoper',seller:'Verkoper',viewer:'Viewer'};
  const esc = v => String(v ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const settings = document.getElementById('settings');
  if (!settings) return;

  const css = document.createElement('style');
  css.textContent = `
    .feature-panel{background:var(--white);border:1px solid var(--line);border-radius:18px;padding:24px;box-shadow:var(--shadow);margin-bottom:24px}
    .feature-panel h3{margin:0 0 6px}.feature-panel>p{margin:0 0 18px;color:var(--muted)}
    .feature-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.feature-row input,.feature-row select{border:1px solid var(--line);border-radius:10px;padding:10px 12px;font:inherit;background:#fff}
    .feature-row input{min-width:180px;flex:1}.feature-row .button{margin:0}
    .room-list,.invite-list,.audit-list{display:grid;gap:10px;margin-top:16px}.room-item,.invite-item,.audit-item{display:flex;justify-content:space-between;gap:16px;align-items:center;padding:12px 14px;background:var(--paper);border-radius:12px}
    .room-item strong,.invite-item strong,.audit-item strong{display:block}.room-item small,.invite-item small,.audit-item small{color:var(--muted)}
    .room-item form{margin:0}.room-item button{border:1px solid var(--line);background:#fff;border-radius:9px;padding:8px 10px;cursor:pointer}.room-item.active button{font-weight:700}
    .inventory-admin-table{width:100%;border-collapse:collapse}.inventory-admin-table th,.inventory-admin-table td{text-align:left;padding:10px;border-bottom:1px solid var(--line);vertical-align:middle}.inventory-admin-table th{font-size:12px;color:var(--muted);text-transform:uppercase}.inventory-admin-table input{width:100%;min-width:90px;border:1px solid var(--line);border-radius:8px;padding:8px;font:inherit}.inventory-actions{display:flex;gap:7px;flex-wrap:wrap}.inventory-actions button{border:1px solid var(--line);background:#fff;border-radius:8px;padding:7px 9px;cursor:pointer}
    .low-stock-box{display:none;margin:0 0 20px;padding:14px 16px;border:1px solid #f0d6a4;background:#fff9ec;border-radius:14px}.low-stock-box.show{display:block}.low-stock-box strong{display:block;margin-bottom:6px}.low-stock-box span{color:#7a5a16;font-size:13px}
    .feature-message{display:none;margin:0 0 16px;padding:11px 13px;border-radius:10px;font-size:13px}.feature-message.show{display:block}.feature-message.ok{background:#ecfdf5;color:#065f46}.feature-message.error{background:#fef2f2;color:#991b1b}
    @media(max-width:760px){.room-item,.invite-item,.audit-item{align-items:flex-start;flex-direction:column}.inventory-admin-table{min-width:780px}.feature-table-wrap{overflow:auto}}
  `;
  document.head.appendChild(css);

  const host = settings.querySelector('.settings-grid > div:first-child') || settings;
  const message = document.createElement('div');
  message.id = 'featureMessage';
  message.className = 'feature-message';
  settings.querySelector('.section-head')?.after(message);
  const say = (text, type='ok') => { message.textContent = text; message.className = `feature-message show ${type}`; };

  const roomsPanel = document.createElement('article');
  roomsPanel.className = 'feature-panel';
  roomsPanel.innerHTML = `<h3>Stockrooms</h3><p>Wissel tussen stockrooms of maak een extra, volledig afgescheiden stockroom aan.</p><div id="roomList" class="room-list">Laden…</div><form id="createRoomForm" class="feature-row" style="margin-top:16px"><input name="name" required maxlength="120" placeholder="Nieuwe stockroomnaam"><button class="button primary" type="submit">Stockroom aanmaken</button></form>`;
  host.prepend(roomsPanel);

  const invitePanel = document.createElement('article');
  invitePanel.className = 'feature-panel';
  invitePanel.id = 'invitePanel';
  invitePanel.hidden = true;
  invitePanel.innerHTML = `<h3>Uitnodigingen</h3><p>Nodig iemand per e-mail uit. Een nieuwe gebruiker wordt direct aan deze stockroom gekoppeld en krijgt géén eigen stockroom.</p><form id="inviteForm" class="feature-row"><input name="email" type="email" required placeholder="naam@bedrijf.nl"><select name="role" id="inviteRole"><option value="member">Gebruiker</option><option value="buyer">Inkoper</option><option value="seller">Verkoper</option><option value="viewer">Viewer</option></select><button class="button primary" type="submit">Uitnodigen</button></form><div id="inviteList" class="invite-list"></div>`;
  host.appendChild(invitePanel);

  const inventoryPanel = document.createElement('article');
  inventoryPanel.className = 'feature-panel';
  inventoryPanel.id = 'inventoryAdminPanel';
  inventoryPanel.hidden = true;
  inventoryPanel.innerHTML = `<h3>Voorraadinstellingen</h3><p>Stel categorie, standaardleverancier en minimumvoorraad in. Voorraadcorrecties worden in het auditlog geregistreerd.</p><div class="feature-table-wrap"><table class="inventory-admin-table"><thead><tr><th>Artikel</th><th>Categorie</th><th>Leverancier</th><th>Minimum</th><th>Correctie</th><th>Acties</th></tr></thead><tbody id="inventoryAdminBody"><tr><td colspan="6">Laden…</td></tr></tbody></table></div>`;
  host.appendChild(inventoryPanel);

  const auditPanel = document.createElement('article');
  auditPanel.className = 'feature-panel';
  auditPanel.id = 'auditPanel';
  auditPanel.hidden = true;
  auditPanel.innerHTML = `<h3>Auditlog</h3><p>De laatste wijzigingen in deze stockroom, inclusief gebruiker en tijdstip.</p><div id="auditList" class="audit-list">Laden…</div>`;
  host.appendChild(auditPanel);

  const overview = document.getElementById('overview');
  if (overview) {
    const low = document.createElement('div');
    low.id = 'lowStockBox';
    low.className = 'low-stock-box';
    const first = overview.querySelector('.section-head');
    if (first) first.after(low); else overview.prepend(low);
  }

  let me = null;

  async function fetchJSON(url, options) {
    const r = await fetch(url, options);
    if (r.status === 401) { location.href = '/login'; throw new Error('session'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || 'Actie mislukt.');
    return data;
  }

  async function loadMe() {
    me = await fetchJSON('/api/me', {cache:'no-store'});
    invitePanel.hidden = !me.permissions.manageMembers;
    inventoryPanel.hidden = !me.permissions.manageItems;
    auditPanel.hidden = !me.permissions.audit;
    document.getElementById('createRoomForm').hidden = !me.permissions.createStockroom;
    if (me.permissions.assignAdmin && !document.querySelector('#inviteRole option[value="admin"]')) {
      document.getElementById('inviteRole').insertAdjacentHTML('beforeend','<option value="admin">Admin</option>');
    }
  }

  async function loadRooms() {
    const data = await fetchJSON('/api/stockrooms', {cache:'no-store'});
    document.getElementById('roomList').innerHTML = data.stockrooms.map(r => `<div class="room-item ${r.stockroom_id === data.active ? 'active':''}"><div><strong>${esc(r.stockroom_name)}</strong><small>${esc(roleLabels[r.role] || r.role)}${r.stockroom_id === data.active ? ' · actief':''}</small></div>${r.stockroom_id === data.active ? '<span>✓</span>' : `<form method="post" action="/switch-stockroom"><input type="hidden" name="stockroom_id" value="${esc(r.stockroom_id)}"><button type="submit">Openen</button></form>`}</div>`).join('') || '<div>Geen stockrooms gevonden.</div>';
  }

  async function loadInvites() {
    if (!me?.permissions.manageMembers) return;
    const data = await fetchJSON('/api/invitations', {cache:'no-store'});
    document.getElementById('inviteList').innerHTML = data.invitations.map(i => `<div class="invite-item"><div><strong>${esc(i.email)}</strong><small>${esc(roleLabels[i.role] || i.role)} · ${i.accepted_at ? 'Geaccepteerd' : 'Openstaand'}</small></div><small>${new Date(i.created_at).toLocaleString('nl-NL')}</small></div>`).join('') || '<small>Nog geen uitnodigingen.</small>';
  }

  async function loadInventoryAdmin() {
    const state = await fetchJSON('/api/state', {cache:'no-store'});
    const items = state.items.filter(i => !i.archived);
    const low = items.filter(i => Number(i.minStock || 0) > 0 && Number(i.stock || 0) <= Number(i.minStock || 0));
    const box = document.getElementById('lowStockBox');
    if (box) {
      if (low.length) { box.classList.add('show'); box.innerHTML = `<strong>⚠ Lage voorraad</strong><span>${low.map(i => `${esc(i.name)}: ${Number(i.stock || 0)} / minimum ${Number(i.minStock || 0)}`).join(' · ')}</span>`; }
      else { box.classList.remove('show'); box.innerHTML = ''; }
    }
    if (!me?.permissions.manageItems) return;
    document.getElementById('inventoryAdminBody').innerHTML = items.map(i => `<tr data-item-row="${esc(i.id)}"><td><strong>${esc(i.name)}</strong><br><small>${esc(i.sku)}</small></td><td><input data-field="category" value="${esc(i.category || '')}" placeholder="Categorie"></td><td><input data-field="supplier" value="${esc(i.supplier || '')}" placeholder="Leverancier"></td><td><input data-field="min_stock" type="number" min="0" step="1" value="${Number(i.minStock || 0)}"></td><td><input data-field="delta" type="number" step="1" placeholder="+5 / -2"><input data-field="reason" style="margin-top:6px" placeholder="Reden"></td><td><div class="inventory-actions"><button data-save-meta="${esc(i.id)}">Opslaan</button><button data-correct="${esc(i.id)}">Corrigeer</button></div><small>Voorraad: ${Number(i.stock || 0)}</small></td></tr>`).join('') || '<tr><td colspan="6">Geen artikelen.</td></tr>';
  }

  async function loadAudit() {
    if (!me?.permissions.audit) return;
    const data = await fetchJSON('/api/audit', {cache:'no-store'});
    document.getElementById('auditList').innerHTML = data.entries.map(e => `<div class="audit-item"><div><strong>${esc(e.action)}</strong><small>${esc(e.user_name || e.email || 'Systeem')} · ${esc(formatDetails(e.details))}</small></div><small>${new Date(e.created_at).toLocaleString('nl-NL')}</small></div>`).join('') || '<small>Nog geen auditregels.</small>';
  }

  function formatDetails(d) {
    if (!d) return '';
    if (d.item) return `${d.item}${d.reason ? ` · ${d.reason}`:''}${d.delta ? ` · ${d.delta > 0 ? '+':''}${d.delta}`:''}`;
    if (d.email) return `${d.email}${d.role ? ` · ${roleLabels[d.role] || d.role}`:''}`;
    if (d.name) return d.name;
    if (d.from && d.to) return `${roleLabels[d.from] || d.from} → ${roleLabels[d.to] || d.to}`;
    if (Array.isArray(d.items) && d.items.length) return `Artikelen: ${d.items.join(', ')}`;
    return '';
  }

  async function refreshFeatures() {
    try {
      await loadMe();
      await Promise.all([loadRooms(), loadInventoryAdmin(), loadInvites(), loadAudit()]);
    } catch (e) {
      if (e.message !== 'session') say(e.message || 'Beheerfuncties konden niet worden geladen.', 'error');
    }
  }

  document.getElementById('createRoomForm').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      await fetchJSON('/api/stockrooms/create', {method:'POST',body:new FormData(e.currentTarget)});
      say('Nieuwe stockroom is aangemaakt en geopend.');
      location.reload();
    } catch (err) { say(err.message, 'error'); }
  });

  document.getElementById('inviteForm').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      await fetchJSON('/api/invitations', {method:'POST',body:new FormData(e.currentTarget)});
      e.currentTarget.reset();
      say('Uitnodiging is per e-mail verstuurd.');
      await loadInvites(); await loadAudit();
    } catch (err) { say(err.message, 'error'); }
  });

  document.getElementById('inventoryAdminBody').addEventListener('click', async e => {
    const save = e.target.closest('[data-save-meta]');
    const correct = e.target.closest('[data-correct]');
    if (!save && !correct) return;
    const id = (save || correct).dataset.saveMeta || (save || correct).dataset.correct;
    const row = e.target.closest('[data-item-row]');
    try {
      if (save) {
        const body = new FormData(); body.set('item_id', id); body.set('category', row.querySelector('[data-field="category"]').value); body.set('supplier', row.querySelector('[data-field="supplier"]').value); body.set('min_stock', row.querySelector('[data-field="min_stock"]').value || '0');
        await fetchJSON('/api/inventory/meta', {method:'POST',body});
        say('Artikelinstellingen opgeslagen.');
      } else {
        const delta = row.querySelector('[data-field="delta"]').value; const reason = row.querySelector('[data-field="reason"]').value.trim();
        if (!delta || Number(delta) === 0 || !reason) { say('Vul een correctie en reden in.', 'error'); return; }
        const body = new FormData(); body.set('item_id', id); body.set('delta', delta); body.set('reason', reason);
        await fetchJSON('/api/inventory/correct', {method:'POST',body});
        say('Voorraadcorrectie opgeslagen.');
      }
      await loadInventoryAdmin(); await loadAudit();
      if (typeof loadState === 'function') await loadState(); else location.reload();
    } catch (err) { say(err.message, 'error'); }
  });

  document.querySelector('[data-view="settings"]')?.addEventListener('click', () => setTimeout(refreshFeatures, 0));
  window.addEventListener('hashchange', () => { if (location.hash === '#settings') refreshFeatures(); });
  setTimeout(refreshFeatures, 100);
})();
