(() => {
  const body = document.getElementById('inventoryAdminBody');
  if (!body) return;

  body.addEventListener('click', async event => {
    const button = event.target.closest('[data-correct]');
    if (!button) return;

    event.preventDefault();
    event.stopImmediatePropagation();

    const row = button.closest('[data-item-row]');
    if (!row) return;

    const deltaInput = row.querySelector('[data-field="delta"]');
    const reasonInput = row.querySelector('[data-field="reason"]');
    const delta = (deltaInput?.value ?? '').replace(',', '.');
    const numericDelta = Number(delta);
    const reason = reasonInput?.value.trim() || 'Handmatige correctie';
    const message = document.getElementById('featureMessage');

    const show = (text, type='ok') => {
      if (!message) return;
      message.textContent = text;
      message.className = `feature-message show ${type}`;
    };

    if (!delta || numericDelta === 0 || !Number.isFinite(numericDelta) || Math.abs(numericDelta * 10 - Math.round(numericDelta * 10)) > 1e-9) {
      show('Vul een voorraadcorrectie per 0,1 in, bijvoorbeeld +0,1 of -0,1.', 'error');
      return;
    }

    const form = new FormData();
    form.set('item_id', button.dataset.correct);
    form.set('delta', delta);
    form.set('reason', reason);

    try {
      const response = await fetch('/api/inventory/correct', {method:'POST', body:form});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || 'Voorraadcorrectie mislukt.');
      show('Voorraadcorrectie opgeslagen.');
      location.reload();
    } catch (error) {
      show(error.message || 'Voorraadcorrectie mislukt.', 'error');
    }
  }, true);
})();

