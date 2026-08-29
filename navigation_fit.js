(()=>{
  const id='navigationFitStyles';
  function installStyles(){
    if(document.getElementById(id))return;
    const style=document.createElement('style');style.id=id;style.textContent=`
      .app-shell{width:100%;max-width:100vw;overflow-x:clip}
      .sidebar{height:100dvh!important;min-height:0!important;padding:20px 16px 14px;overflow:hidden}
      .brand{margin:0 6px 18px;flex:0 0 auto}
      .sidebar nav{display:flex!important;flex-direction:column;gap:4px!important;min-height:0;overflow-y:auto;overscroll-behavior:contain;scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.25) transparent}
      .sidebar .nav-item{min-height:38px;padding:8px 10px!important;border-radius:9px;line-height:1.15}
      .sidebar .nav-item span{font-size:16px}
      .sidebar .nav-item.active,.sidebar .nav-item[aria-current="page"]{background:#fff!important;color:#173f32!important;font-weight:800!important;box-shadow:inset 4px 0 0 #e7c684,0 5px 14px rgba(4,24,16,.18)}
      .sidebar .nav-item.active>span:first-child,.sidebar .nav-item[aria-current="page"]>span:first-child{color:#a96518;transform:scale(1.08)}
      .sidebar .ux-nav-group.active:not(.open){background:rgba(255,255,255,.16)!important;color:#fff!important;box-shadow:inset 4px 0 0 #e7c684}
      .trade-sidebar-submenu .nav-item.active,.settings-sidebar-submenu button.active{background:rgba(231,198,132,.2)!important;color:#fff!important;box-shadow:inset 3px 0 0 #e7c684;font-weight:800!important}
      .sidebar .nav-item,.sidebar .nav-item>span:first-child{transition:background-color .16s ease,color .16s ease,box-shadow .16s ease,transform .16s ease}
      .sidebar-note{flex:0 0 auto;padding:12px 5px 0;margin-top:10px}
      main{width:100%;max-width:100%;overflow-x:hidden;padding:24px clamp(16px,3vw,48px) 48px}
      .topbar{margin-bottom:22px}.section-head{gap:12px;flex-wrap:wrap}
      .section-actions,.top-actions{display:flex;gap:8px;flex-wrap:wrap}
      .table-card{max-width:100%;overflow-x:auto}
      .nav-tight .brand{margin-bottom:10px}.nav-tight .brand-mark{width:30px;height:30px}.nav-tight .brand>span:last-child{font-size:17px}
      .nav-tight .sidebar .nav-item{min-height:32px;padding:6px 9px!important;font-size:12px!important}
      .nav-tight .sidebar .nav-item span{font-size:14px}.nav-tight .sidebar-note{padding-top:8px;margin-top:7px}
      .nav-tight .sidebar-note small{display:none}.nav-tight .settings-sidebar-submenu,.nav-tight .trade-sidebar-submenu{margin-bottom:3px!important}
      @media(max-height:720px) and (min-width:901px){.sidebar-note{display:none}.sidebar{padding-top:12px;padding-bottom:10px}}
      @media(max-width:900px){.sidebar{width:min(300px,88vw);left:min(-300px,-88vw);padding-top:18px;box-shadow:18px 0 45px rgba(12,31,24,.2)}.sidebar.open{left:0}main{padding-top:20px}.topbar{margin-bottom:18px}}
      @media(max-width:580px){main{padding:16px 12px 36px}.topbar h1,.section-head h2{font-size:22px}.top-actions,.section-actions{width:100%}.top-actions .button,.section-actions .button{flex:1 1 auto}.panel,.table-card{border-radius:12px}.filter-row{max-width:100%;overflow-x:auto;padding-bottom:3px}.filter{flex:0 0 auto}}
    `;document.head.appendChild(style);
  }
  function fit(){
    const sidebar=document.querySelector('.sidebar'),nav=sidebar?.querySelector('nav');if(!sidebar||!nav)return;
    const visible=[...nav.querySelectorAll(':scope > .nav-item:not([hidden])')].length;
    document.documentElement.classList.toggle('nav-tight',innerHeight<820||visible>8);
    nav.querySelectorAll('.nav-item').forEach(item=>{const label=item.textContent.replace(/\s+/g,' ').trim();if(label)item.title=label});
    syncActive(nav);
  }
  function syncActive(nav=document.querySelector('.sidebar nav')){
    if(!nav)return;
    nav.querySelectorAll('.nav-item[data-view]').forEach(item=>item.classList.contains('active')?item.setAttribute('aria-current','page'):item.removeAttribute('aria-current'));
    const active=nav.querySelector('.nav-item[data-view].active');
    if(active?.closest('#tradeSidebarSubmenu'))document.getElementById('tradeNavGroup')?.classList.add('active');
  }
  function closeOtherMenus(clicked){
    if(clicked?.closest('#tradeNavGroup'))document.getElementById('settingsSidebarSubmenu')?.classList.remove('open');
    if(clicked?.closest('[data-view="settings"]')){const trade=document.getElementById('tradeSidebarSubmenu');trade?.classList.remove('open');document.getElementById('tradeNavGroup')?.classList.remove('open')}
  }
  installStyles();window.addEventListener('resize',fit,{passive:true});window.addEventListener('load',fit);
  document.addEventListener('click',event=>{closeOtherMenus(event.target);setTimeout(fit,0)});
  new MutationObserver(()=>requestAnimationFrame(fit)).observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['hidden','class']});
  fit();
})();

