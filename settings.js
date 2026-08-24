(() => {
  const sidebarNav = document.querySelector('.sidebar nav');
  const main = document.querySelector('main');
  const footer = document.querySelector('.site-footer');
  if (!sidebarNav || !main || !footer) return;

  const oldMembersLink = sidebarNav.querySelector('a[href="/members"]');
  if (oldMembersLink) oldMembersLink.remove();

  const settingsButton = document.createElement('button');
  settingsButton.className = 'nav-item';
  settingsButton.dataset.view = 'settings';
  settingsButton.innerHTML = '<span>⚙</span>Instellingen';
  sidebarNav.appendChild(settingsButton);

  const style = document.createElement('style');
  style.textContent = `
    .settings-grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(300px,.8fr);gap:24px;align-items:start}
    .settings-panel{background:var(--white);border:1px solid var(--line);border-radius:18px;padding:24px;box-shadow:var(--shadow);margin-bottom:24px}
    .settings-panel h3{margin:0 0 6px}.settings-panel>p{color:var(--muted);margin:0 0 20px}
    .settings-form{display:grid;grid-template-columns:minmax(0,1fr) minmax(180px,.55fr) auto;gap:12px;align-items:end}
    .settings-form label,.danger-form label{display:grid;gap:7px;font-weight:700;font-size:13px;color:var(--ink)}
    .settings-form input,.settings-form select,.danger-form input,.existing-role-select{width:100%;border:1px solid var(--line);border-radius:10px;padding:9px 10px;background:#fff;font:inherit;color:var(--ink)}
    .settings-form .button{margin:0;white-space:nowrap}.settings-table-wrap{overflow-x:auto}.settings-table{width:100%;border-collapse:collapse}
    .settings-table th,.settings-table td{text-align:left;padding:12px 10px;border-bottom:1px solid var(--line);vertical-align:middle}.settings-table th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
    .role-chip{display:inline-flex;padding:5px 9px;border-radius:999px;background:var(--paper);font-size:12px;font-weight:700}.remove-member{border:0;background:transparent;color:#9b2c2c;cursor:pointer;font-weight:700;padding:8px}
    .member-actions{display:flex;gap:8px;align-items:center;justify-content:flex-end}.account-summary{display:grid;gap:8px;margin-bottom:18px}.account-summary strong{font-size:18px}.account-summary span{color:var(--muted)}
    .danger-zone{border-color:#efcaca;background:#fffafa}.danger-zone h3{color:#8d2525}.danger-warning{padding:13px 14px;border-radius:10px;background:#fff0f0;color:#7b2323;font-size:13px;line-height:1.5;margin-bottom:16px!important}
    .danger-form{display:grid;gap:12px}.danger-button{border:0;border-radius:10px;padding:12px 14px;background:#8d2525;color:#fff;font:inherit;font-weight:700;cursor:pointer}
    .settings-message{display:none;margin:0 0 16px;padding:11px 13px;border-radius:10px;font-size:13px}.settings-message.show{display:block}.settings-message.ok{background:#ecfdf5;color:#065f46}.settings-message.error{background:#fef2f2;color:#991b1b}
    body[data-stockroom-role="viewer"] #quickAddBtn,
    body[data-stockroom-role="viewer"] [data-add],
    body[data-stockroom-role="viewer"] #archiveBtn,
    body[data-stockroom-role="viewer"] .delete-item,
    body[data-stockroom-role="viewer"] .restore-item,
    body[data-stockroom-role="viewer"] .purge-item,
    body[data-stockroom-role="viewer"] .transaction-buttons,
    body[data-stockroom-role="viewer"] .transaction-controls button{display:none!important}
    body[data-stockroom-role="buyer"] [data-add="outgoing"],
    body[data-stockroom-role="buyer"] [data-add="item"],
    body[data-stockroom-role="buyer"] #archiveBtn,
    body[data-stockroom-role="buyer"] .delete-item,
    body[data-stockroom-role="buyer"] .restore-item,
    body[data-stockroom-role="buyer"] .purge-item,
    body[data-stockroom-role="buyer"] #outgoingCards .transaction-controls button{display:none!important}
    body[data-stockroom-role="seller"] [data-add="incoming"],
    body[data-stockroom-role="seller"] [data-add="item"],
    body[data-stockroom-role="seller"] #archiveBtn,
    body[data-stockroom-role="seller"] .delete-item,
    body[data-stockroom-role="seller"] .restore-item,
    body[data-stockroom-role="seller"] .purge-item,
    body[data-stockroom-role="seller"] #incomingCards .transaction-controls button{display:none!important}
    @media(max-width:900px){.settings-grid{grid-template-columns:1fr}.settings-form{grid-template-columns:1fr}}
  `;
  document.head.appendChild(style);

  const section = document.createElement('section');
  section.id = 'settings';
  section.className = 'view';
  section.innerHTML = `
    <div class="section-head"><div><p class="eyebrow">Beheer</p><h2>Instellingen</h2></div></div>
    <div id="settingsMessage" class="settings-message" role="status"></div>
    <div class="settings-grid">
      <div id="memberManagementColumn">
        <article class="settings-panel" id="memberManagementPanel">
          <h3>Gebruikers & rollen</h3>
          <p>Beheer wie toegang heeft tot deze stockroom en welke rol iemand heeft.</p>
          <form id="addMemberForm" class="settings-form" hidden>
            <label>E-mailadres<input name="email" type="email" required placeholder="naam@bedrijf.nl"></label>
            <label>Rol<select name="role" id="memberRoleSelect"></select></label>
            <button class="button primary" type="submit">Koppelen</button>
          </form>
          <div class="settings-table-wrap">
            <table class="settings-table"><thead><tr><th>Naam</th><th>E-mail</th><th>Rol</th><th></th></tr></thead><tbody id="settingsMembers"><tr><td colspan="4">Laden…</td></tr></tbody></table>
          </div>
        </article>
      </div>
      <div>
        <article class="settings-panel">
          <h3>Mijn account</h3>
          <div class="account-summary"><strong id="settingsUserName">—</strong><span id="settingsUserEmail">—</span><span id="settingsStockroomName">—</span></div>
          <a class="button ghost" href="/account/security" style="text-decoration:none;text-align:center">E-mail en actieve sessies</a>
          <a class="button ghost" href="/logout" style="text-decoration:none;text-align:center">Uitloggen</a>
        </article>
        <article class="settings-panel danger-zone">
          <h3>Account permanent verwijderen</h3>
          <p class="danger-warning">Dit verwijdert je account permanent. Stockrooms waarvan jij eigenaar bent worden inclusief voorraad, transacties en memberships verwijderd. Dit kan niet ongedaan worden gemaakt.</p>
          <form class="danger-form" method="post" action="/account/delete">
            <label>Huidig wachtwoord<input name="password" type="password" autocomplete="current-password" required></label>
            <label>Typ VERWIJDEREN ter bevestiging<input name="confirm" autocomplete="off" required></label>
            <button class="danger-button" type="submit">Account permanent verwijderen</button>
          </form>
        </article>
      </div>
    </div>`;
  main.insertBefore(section, footer);

  const roleLabels = {owner:'Owner',admin:'Admin',member:'Gebruiker',buyer:'Inkoper',seller:'Verkoper',viewer:'Viewer'};
  let currentRole = null;

  const message = (text, type='ok') => {
    const el = document.getElementById('settingsMessage');
    el.textContent = text;
    el.className = `settings-message show ${type}`;
  };

  function applyRoleUI(role) {
    currentRole = role;
    document.body.dataset.stockroomRole = role;
    const canManageMembers = role === 'owner' || role === 'admin';
    document.getElementById('memberManagementColumn').hidden = !canManageMembers;

    const quick = document.getElementById('quickAddBtn');
    if (!quick) return;
    if (role === 'viewer') {
      quick.hidden = true;
    } else if (role === 'buyer') {
      quick.hidden = false;
      quick.innerHTML = '<span>＋</span> Nieuwe inkoop';
      quick.onclick = () => window.openDialog?.('incoming');
    } else if (role === 'seller') {
      quick.hidden = false;
      quick.innerHTML = '<span>＋</span> Nieuwe verkoop';
      quick.onclick = () => window.openDialog?.('outgoing');
    } else {
      quick.hidden = false;
      quick.innerHTML = '<span>＋</span> Nieuwe transactie';
    }
  }

  async function loadRole() {
    try {
      const response = await fetch('/api/me', {cache:'no-store'});
      if (response.status === 401) { location.href = '/login'; return; }
      if (!response.ok) return;
      const data = await response.json();
      applyRoleUI(data.stockroom.role);
    } catch {}
  }

  function openSettings() {
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('active-view', v.id === 'settings'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === 'settings'));
    const title = document.getElementById('pageTitle');
    if (title) title.textContent = 'Instellingen';
    document.querySelector('.sidebar')?.classList.remove('open');
    window.scrollTo({top:0, behavior:'smooth'});
    history.replaceState(null, '', '#settings');
    loadSettings();
  }

  settingsButton.addEventListener('click', openSettings);

  async function loadSettings() {
    try {
      const response = await fetch('/api/members', {cache:'no-store'});
      if (response.status === 401) { location.href = '/login'; return; }
      if (!response.ok) throw new Error();
      const data = await response.json();
      applyRoleUI(data.stockroom.role);
      document.getElementById('settingsUserName').textContent = data.user.name;
      document.getElementById('settingsUserEmail').textContent = data.user.email;
      document.getElementById('settingsStockroomName').textContent = `${data.stockroom.name} · ${roleLabels[data.stockroom.role] || data.stockroom.role}`;

      const form = document.getElementById('addMemberForm');
      form.hidden = !data.canManage;
      const select = document.getElementById('memberRoleSelect');
      select.innerHTML = data.roles.map(r => `<option value="${r.value}">${escapeHtml(r.label)}</option>`).join('');

      const tbody = document.getElementById('settingsMembers');
      tbody.innerHTML = data.members.map(m => {
        const protectedMember = m.role === 'owner' || m.user_id === data.user.id || (data.stockroom.role === 'admin' && m.role === 'admin');
        const editable = data.canManage && !protectedMember;
        const removable = editable;
        const roleCell = editable
          ? `<select class="existing-role-select" data-change-role="${m.user_id}" data-original-role="${m.role}">${data.roles.map(r => `<option value="${r.value}" ${r.value === m.role ? 'selected' : ''}>${escapeHtml(r.label)}</option>`).join('')}</select>`
          : `<span class="role-chip">${roleLabels[m.role] || escapeHtml(m.role)}</span>`;
        return `<tr><td><strong>${escapeHtml(m.name)}</strong></td><td>${escapeHtml(m.email)}</td><td>${roleCell}</td><td><div class="member-actions">${removable ? `<button class="remove-member" data-remove-member="${m.user_id}">Verwijderen</button>` : ''}</div></td></tr>`;
      }).join('') || '<tr><td colspan="4">Geen gebruikers gevonden.</td></tr>';
    } catch {
      message('Instellingen konden niet worden geladen.', 'error');
    }
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  }

  document.getElementById('addMemberForm').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      const response = await fetch('/members/add', {method:'POST', body:new FormData(form)});
      if (!response.ok) {
        message(response.status === 404 ? 'Geen geregistreerde gebruiker gevonden met dit e-mailadres.' : 'Gebruiker kon niet worden gekoppeld.', 'error');
        return;
      }
      form.reset();
      message('Gebruiker is gekoppeld aan deze stockroom.');
      await loadSettings();
    } catch {
      message('Gebruiker kon niet worden gekoppeld.', 'error');
    }
  });

  document.getElementById('settingsMembers').addEventListener('change', async event => {
    const select = event.target.closest('[data-change-role]');
    if (!select) return;
    const body = new FormData();
    body.set('user_id', select.dataset.changeRole);
    body.set('role', select.value);
    select.disabled = true;
    try {
      const response = await fetch('/members/role', {method:'POST', body});
      if (!response.ok) {
        select.value = select.dataset.originalRole;
        message('Rol kon niet worden gewijzigd.', 'error');
        return;
      }
      select.dataset.originalRole = select.value;
      message(`Rol gewijzigd naar ${roleLabels[select.value] || select.value}.`);
      await loadSettings();
    } catch {
      select.value = select.dataset.originalRole;
      message('Rol kon niet worden gewijzigd.', 'error');
    } finally {
      select.disabled = false;
    }
  });

  document.getElementById('settingsMembers').addEventListener('click', async event => {
    const button = event.target.closest('[data-remove-member]');
    if (!button) return;
    const body = new FormData();
    body.set('user_id', button.dataset.removeMember);
    try {
      const response = await fetch('/members/remove', {method:'POST', body});
      if (!response.ok) { message('Gebruiker kon niet worden verwijderd.', 'error'); return; }
      message('Gebruiker is verwijderd uit deze stockroom.');
      await loadSettings();
    } catch {
      message('Gebruiker kon niet worden verwijderd.', 'error');
    }
  });

  loadRole();
  if (location.hash === '#settings') openSettings();
})();
