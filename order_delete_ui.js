(() => {
  function decorate() {
    document.querySelectorAll('.order-card').forEach(card => {
      if (card.querySelector('[data-delete-order]')) return;
      const status = card.querySelector('[data-order-status]');
      if (!status) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'delete-order-button';
      button.dataset.deleteOrder = status.dataset.orderStatus;
      button.dataset.orderType = status.dataset.orderType;
      button.textContent = 'Verwijderen';
      card.appendChild(button);
    });
  }

  const style = document.createElement('style');
  style.textContent = '.delete-order-button{margin-top:8px;border:1px solid #eadfd9;background:#fff7f3;color:#a55239;border-radius:8px;padding:7px 10px;font:700 10px DM Sans;cursor:pointer}.delete-order-button:hover{background:#f9e5dc}';
  document.head.appendChild(style);

  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-delete-order]');
    if (!button) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const type = button.dataset.orderType;
    const label = type === 'purchase' ? 'inkooporder' : 'verkooporder';
    if (!confirm(`Deze ${label} permanent verwijderen? Als de order al voorraad heeft geboekt, wordt die voorraadboeking eerst teruggedraaid.`)) return;
    button.disabled = true;
    try {
      const body = new FormData();
      body.set('order_id', button.dataset.deleteOrder);
      body.set('order_type', type);
      const response = await fetch('/api/orders/delete', { method: 'POST', body });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || 'Order kon niet worden verwijderd.');
      button.closest('.order-card')?.remove();
      const toast = document.getElementById('toast');
      if (toast) {
        toast.textContent = data.inventoryReversed ? 'Order verwijderd en voorraadboeking teruggedraaid.' : 'Order verwijderd.';
        toast.classList.add('show');
        setTimeout(() => toast.classList.remove('show'), 2600);
      }
    } catch (err) {
      alert(err.message);
      button.disabled = false;
    }
  }, true);

  new MutationObserver(decorate).observe(document.body, { childList: true, subtree: true });
  window.addEventListener('load', decorate);
  decorate();
})();
