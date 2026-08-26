(() => {
  const titles={overview:'Overzicht',analytics:'Analytics',inventory:'Voorraad',incoming:'Inkomend',outgoing:'Uitgaand',settings:'Instellingen',relations:'Relaties',orders:'Orders',warehouse:'Magazijn',notifications:'Meldingen',platformAdmin:'Platformbeheer'};
  function activate(id){
    const target=document.getElementById(id);
    if(!target||!target.classList.contains('view')) return;
    document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active-view',v===target));
    document.querySelectorAll('.nav-item[data-view]').forEach(n=>n.classList.toggle('active',n.dataset.view===id));
    const title=document.getElementById('pageTitle');
    if(title){
      if(id==='overview'){
        const h=new Date().getHours();
        title.textContent=h>=5&&h<12?'Goedemorgen':h>=12&&h<18?'Goedemiddag':h>=18&&h<23?'Goedenavond':'Goedenacht';
      }else title.textContent=titles[id]||id.charAt(0).toUpperCase()+id.slice(1);
    }
    document.querySelector('.sidebar')?.classList.remove('open');
    window.scrollTo(0,0);
  }
  document.addEventListener('click',e=>{
    const menu=e.target.closest('.mobile-menu');
    if(menu){
      e.preventDefault();e.stopImmediatePropagation();
      document.querySelector('.sidebar')?.classList.toggle('open');
      return;
    }
    const trigger=e.target.closest('.nav-item[data-view],[data-go]');
    if(!trigger) return;
    const id=trigger.dataset.view||trigger.dataset.go;
    if(!id||!document.getElementById(id)) return;
    e.preventDefault();e.stopImmediatePropagation();
    activate(id);
  },true);
})();
