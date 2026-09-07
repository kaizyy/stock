const assert=require('node:assert/strict');
const forecast=require('../inventory_forecast.js');
const now=new Date('2026-09-07T12:00:00Z').getTime();
const state={items:[
  {id:'fast',name:'Snel',sku:'S-1',stock:20,minStock:5},
  {id:'slow',name:'Stabiel',sku:'S-2',stock:50,minStock:5},
  {id:'new',name:'Nieuw',sku:'N-1',stock:2,minStock:10},
  {id:'old',name:'Archief',stock:0,archived:true}
],transactions:[
  {type:'outgoing',itemId:'fast',qty:90,date:'2026-08-20T12:00:00Z'},
  {type:'outgoing',itemId:'fast',qty:999,date:'2026-01-01T12:00:00Z'},
  {type:'incoming',itemId:'fast',qty:15,done:false,date:'2026-09-06T12:00:00Z'}
]};
const rows=forecast.calculate(state,[{item_id:'fast',reserved:5}],now);
assert.equal(rows.length,3,'gearchiveerde artikelen horen niet in het advies');
const fast=rows.find(row=>row.itemId==='fast');
assert.equal(fast.available,15,'reserveringen moeten van vrije voorraad af');
assert.equal(fast.incoming,15,'open inkomende leveringen moeten worden meegerekend');
assert.equal(fast.sold,90,'alleen verkopen binnen 90 dagen tellen mee');
assert.equal(fast.recommended,19,'advies moet verbruik, levertijd, buffer, vrije voorraad en onderweg combineren');
const fresh=rows.find(row=>row.itemId==='new');
assert.equal(fresh.recommended,8,'minimumvoorraad geeft ook zonder verkoophistorie een advies');
assert.equal(rows.find(row=>row.itemId==='slow').recommended,0,'voldoende voorraad geeft geen onnodig advies');
console.log('PASS inventory forecast and reorder advice');
