(()=>{
  document.addEventListener('click',event=>{
    const button=event.target.closest('#actionCenter [data-action-view]');
    const title=button?.closest('.action-card')?.querySelector('strong')?.textContent||'';
    if(!button||!title.startsWith('Kasstroomprognose'))return;
    event.preventDefault();event.stopImmediatePropagation();
    document.querySelector('[data-view="analytics"]')?.click();
    setTimeout(()=>document.getElementById('cashflowPanel')?.scrollIntoView({behavior:'smooth',block:'start'}),300);
  },true);
})();
