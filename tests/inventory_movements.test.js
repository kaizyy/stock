const assert = require('node:assert/strict');
const {entriesFor} = require('../inventory_movements.js');

const transactions = [
  {id:'incoming', itemId:'a', type:'incoming', qty:0.1, done:true, date:'2026-09-01T10:00:00Z'},
  {id:'pending', itemId:'a', type:'incoming', qty:5, done:false, date:'2026-09-05T10:00:00Z'},
  {id:'sale', itemId:'a', type:'outgoing', qty:0.2, date:'2026-09-02T10:00:00Z'},
  {id:'correction', itemId:'a', type:'adjustment', qty:-0.1, reason:'Telling', date:'2026-09-03T10:00:00Z'},
  {id:'return', itemId:'a', type:'outgoing', qty:-0.1, isReturn:true, date:'2026-09-04T10:00:00Z'},
  {id:'other', itemId:'b', type:'outgoing', qty:9, date:'2026-09-05T10:00:00Z'},
];
const operations = [
  {id:'count', item_id:'a', operation_type:'count', quantity:0.1, previous_stock:9.9, new_stock:10, created_at:'2026-09-06T10:00:00Z'},
  {id:'transfer', item_id:'a', operation_type:'transfer_out', quantity:0.2, previous_stock:10, new_stock:9.8, created_at:'2026-09-07T10:00:00Z'},
  {id:'return-op', item_id:'a', operation_type:'sales_return', quantity:0.1, created_at:'2026-09-04T10:00:00Z'},
];
const rows = entriesFor('a', transactions, operations);
assert.equal(rows.length, 6, 'open levering en dubbele retourregistratie horen niet in de mutaties');
assert.deepEqual(rows.map(row => row.id), ['warehouse:transfer', 'warehouse:count', 'tx:return', 'tx:correction', 'tx:sale', 'tx:incoming']);
assert.deepEqual(rows.map(row => Math.round(row.delta * 10) / 10), [-0.2, 0.1, 0.1, -0.1, -0.2, 0.1]);
assert.equal(rows.find(row => row.id === 'tx:correction').detail, 'Telling');
assert.equal(entriesFor('b', transactions, operations).length, 1, 'andere artikelen blijven buiten beeld');
console.log('PASS inventory movements per item');

