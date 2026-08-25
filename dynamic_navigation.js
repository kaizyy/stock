(() => {
  const titles = {
    overview: 'Overzicht',
    analytics: 'Analytics',
    inventory: 'Voorraad',
    incoming: 'Inkomend',
    outgoing: 'Uitgaand',
    settings: 'Instellingen',
    relations: 'Relaties',
    orders: 'Orders',
    warehouse: 'Magazijn'
  };

  function activateView(id) {
    const target = document.getElementById(id);
    if (!target || !target.classList.contains('view')) return false;

    document.querySelectorAll('.view').forEach(view => {
      view.classList.toggle('active-view', view.id === id);
    });
    document.querySelectorAll('.nav-item[data-view]').forEach(item => {
      item.classList.toggle('active', item.dataset.view === id);
    });

    const title = document.getElementById('pageTitle');
    if (title) {
      if (id === 'overview') {
        const hour = new Date().getHours();
        title.textContent = hour >= 5 && hour < 12 ? 'Goedemorgen' : hour >= 12 && hour < 18 ? 'Goedemiddag' : hour >= 18 && hour < 23 ? 'Goedenavond' : 'Goedenacht';
      } else {
        title.textContent = titles[id] || id.charAt(0).toUpperCase() + id.slice(1);
      }
    }

    document.querySelector('.sidebar')?.classList.remove('open');
    window.scrollTo({ top: 0, behavior: 'smooth' });
    return true;
  }

  document.addEventListener('click', event => {
    const trigger = event.target.closest('.nav-item[data-view], [data-go]');
    if (!trigger) return;
    const id = trigger.dataset.view || trigger.dataset.go;
    if (!id || !document.getElementById(id)) return;
    event.preventDefault();
    activateView(id);
  });
})();
