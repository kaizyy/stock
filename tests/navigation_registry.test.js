const assert=require('assert');
const fs=require('fs');
const path=require('path');
const vm=require('vm');

const root=path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'navigation_registry.js'),'utf8');
const document={readyState:'loading',getElementById:()=>null,querySelector:()=>null,dispatchEvent:()=>{}};
const window={addEventListener:()=>{}};
vm.runInNewContext(source,{window,document,CustomEvent:function(){},Map,Object,Date,setTimeout:()=>0});
const registry=window.StockroomNavigationRegistry;
assert(registry,'Navigatieregister ontbreekt');
const entries=registry.all();
assert.strictEqual(new Set(entries.map(x=>x.id)).size,entries.length,'Dubbele pagina-id in navigatieregister');
for(const view of ['overview','analytics','inventory','warehouse','incoming','outgoing','relations','orders','quotes','finance','settings','notifications','platformAdmin']){
  assert(entries.some(x=>x.view===view),`Pagina ${view} ontbreekt in navigatieregister`);
}
for(const feature of ['suppliers','customers','purchase-orders','sales-orders','quotes','sales-invoices','debtors','purchase-invoices','payment-batches','bank-reconciliation','inventory-forecast','inventory-movements','reservations','stock-counts','returns','cashflow-forecast','profit-margins','tax-report','budget-planning','security-integrations']){
  assert(registry.resolveFeature(feature),`Functie ${feature} heeft geen navigatie-eigenaar`);
}
assert(entries.every(entry=>entry.trigger&&entry.features.length),'Iedere pagina moet een ingang en minimaal één functie hebben');
console.log('Navigatieregister dekt alle bestaande hoofdpagina’s en kritieke functies.');
