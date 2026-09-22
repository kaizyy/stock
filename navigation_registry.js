(()=>{
  const entries=[
    {id:'overview',label:'Overzicht',area:'dashboard',view:'overview',trigger:'[data-view="overview"]',features:['action-center','stock-summary','recent-activity','dashboard-alerts']},
    {id:'analytics',label:'Analytics',area:'analytics',view:'analytics',trigger:'[data-view="analytics"]',features:['inventory-analysis','cash-bank-analysis','cashflow-forecast','profit-margins','tax-report','budget-planning','return-analysis']},
    {id:'inventory',label:'Voorraad',area:'inventory',view:'inventory',trigger:'[data-view="inventory"]',features:['items','inventory-forecast','purchase-advice','inventory-movements','reservations','inventory-reconciliation','supplier-intelligence']},
    {id:'warehouse',label:'Magazijn',area:'inventory',view:'warehouse',trigger:'[data-view="warehouse"]',features:['goods-receipts','stock-counts','returns','stock-transfers']},
    {id:'incoming',label:'Inkomend',area:'purchase',view:'incoming',trigger:'[data-view="incoming"]',features:['manual-purchases']},
    {id:'outgoing',label:'Uitgaand',area:'sales',view:'outgoing',trigger:'[data-view="outgoing"]',features:['manual-sales']},
    {id:'relations',label:'Relaties',area:'relations',view:'relations',trigger:'[data-view="relations"]',features:['suppliers','customers']},
    {id:'orders',label:'Orders',area:'orders',view:'orders',trigger:'[data-view="orders"]',features:['purchase-orders','sales-orders','order-approval','supplier-followup','delivery-confirmation']},
    {id:'quotes',label:'Offertes',area:'sales',view:'quotes',trigger:'[data-view="quotes"]',features:['quotes','quote-mail','quote-conversion']},
    {id:'finance',label:'Facturen',area:'finance',view:'finance',trigger:'[data-view="finance"]',features:['sales-invoices','invoice-trash','debtors','purchase-invoices','payment-batches','bank-reconciliation']},
    {id:'settings',label:'Instellingen',area:'management',view:'settings',trigger:'[data-view="settings"]',features:['account','organization','documents','members-roles','inventory-settings','notifications-settings','security-integrations','audit-log','billing']},
    {id:'notifications',label:'Meldingen',area:'management',view:'notifications',trigger:'[data-view="notifications"]',features:['notification-center']},
    {id:'platform-admin',label:'Platformbeheer',area:'management',view:'platformAdmin',trigger:'[data-view="platformAdmin"]',features:['platform-status','subscriptions','errors','backup-restore']},
    {id:'members',label:'Gebruikers',area:'management',route:'/members',trigger:'a[href="/members"]',features:['member-management','invitations']}
  ];
  const featureTargets={
    'action-center':'#actionCenter','inventory-forecast':'#forecastTable','inventory-movements':'#movementPanel','reservations':'#movementReservations','inventory-reconciliation':'#reconciliationTable',
    'suppliers':'#supplierPanel','customers':'#customerPanel','purchase-orders':'#purchasePanel','sales-orders':'#salesPanel','quotes':'#quoteList','sales-invoices':'#financeList',
    'debtors':'#debtorPanel','purchase-invoices':'#purchaseInvoicePanel','payment-batches':'#paymentBatchPanel','bank-reconciliation':'#bankReconciliationPanel',
    'stock-counts':'#countPanel','returns':'#returnPanel','stock-transfers':'#transferPanel','security-integrations':'#securityIntegrationsPanel','notification-center':'#notificationList','platform-status':'#platformContent',
    'cashflow-forecast':'#cashflowPanel','profit-margins':'#profitReportPanel','tax-report':'#taxReportPanel','budget-planning':'#budgetPlanningPanel','return-analysis':'#returnAnalytics'
  };
  const byId=new Map(entries.map(entry=>[entry.id,entry]));
  function all(){return entries.map(entry=>({...entry,features:[...entry.features]}))}
  function get(id){const entry=byId.get(id);return entry?{...entry,features:[...entry.features]}:null}
  function resolveFeature(feature){const entry=entries.find(item=>item.features.includes(feature));return entry?{entry:get(entry.id),target:featureTargets[feature]||null}:null}
  function auditDom(){const missingPages=[],missingFeatures=[];for(const entry of entries){const target=entry.view?document.getElementById(entry.view):document.querySelector(entry.route?`a[href="${entry.route}"]`:entry.trigger);const trigger=document.querySelector(entry.trigger);if(target&&!trigger)missingPages.push({id:entry.id,reason:'trigger'});if(trigger&&!target)missingPages.push({id:entry.id,reason:'target'});for(const feature of entry.features){const selector=featureTargets[feature];if(selector&&document.querySelector(entry.trigger)&&!document.querySelector(selector))missingFeatures.push({page:entry.id,feature,selector})}}const result={ok:missingPages.length===0,missingPages,missingFeatures,checkedAt:new Date().toISOString()};document.dispatchEvent(new CustomEvent('stockroom:navigation-audit',{detail:result}));return result}
  window.StockroomNavigationRegistry=Object.freeze({version:1,all,get,resolveFeature,auditDom,areas:Object.freeze(['dashboard','sales','purchase','relations','orders','inventory','finance','analytics','management'])});
  const run=()=>setTimeout(auditDom,800);document.readyState==='loading'?window.addEventListener('load',run,{once:true}):run();
})();
