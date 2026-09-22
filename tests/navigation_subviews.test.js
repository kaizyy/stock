const assert=require('assert');
const fs=require('fs');
const path=require('path');
const source=fs.readFileSync(path.resolve(__dirname,'..','navigation_subviews.js'),'utf8');
for(const marker of ['customers','suppliers','sales-orders','purchase-orders','customerPanel','supplierPanel','salesPanel','purchasePanel','crm-subview-hidden','max-width:900px'])assert(source.includes(marker),`Ontbrekend CRM-navigatiekenmerk: ${marker}`);
assert(source.includes("source('relations')?.classList.add('crm-split-source')"),'Oude gecombineerde relatie-ingang wordt niet vervangen');
assert(source.includes("source('orders')?.classList.add('crm-split-source')"),'Oude gecombineerde orderingang wordt niet vervangen');
console.log('Gesplitste relatie- en ordernavigatie is volledig geregistreerd.');
