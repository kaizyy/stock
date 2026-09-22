const assert=require('assert');
const fs=require('fs');
const path=require('path');
const source=fs.readFileSync(path.resolve(__dirname,'..','finance_navigation.js'),'utf8');
for(const marker of ['sales-invoices','debtors','purchase-invoices','payment-batches','bank-reconciliation','Verkoopfacturen','Debiteuren','Inkoopfacturen','Betaalvoorstellen','Bankmutaties','finance-subview-hidden','data-debtor-open','max-width:900px'])assert(source.includes(marker),`Ontbrekend financieel navigatiekenmerk: ${marker}`);
assert(source.includes("source.classList.add('finance-split-source')"),'Oude gecombineerde factuuringang wordt niet vervangen');
console.log('Financiële navigatie bevat alle vijf bestaande werkprocessen.');
