const assert = require('node:assert/strict');
const metrics = require('../dashboard_metrics.js');

const now = Date.parse('2026-08-24T12:00:00Z');
const state = {
  items: [
    {id:'low', stock:5, minStock:5},
    {id:'ok', stock:6, minStock:5},
  ],
  transactions: [
    {type:'incoming', qty:2, price:10, paid:true, done:false, date:'2026-08-23T12:00:00Z'},
    {type:'incoming', qty:3, price:10, paid:false, done:false, date:'2026-08-23T12:00:00Z'},
    {type:'outgoing', qty:1, price:50, done:false, date:'2026-08-10T12:00:00Z'},
    {type:'outgoing', qty:1, price:20, done:false, date:'2026-08-23T12:00:00Z'},
  ],
};

const result = metrics.overview(state, now);
assert.equal(result.expectedTotal, 50);
assert.equal(result.expectedPaidTotal, 20);
assert.equal(result.expectedUnpaidTotal, 30);
assert.equal(result.expectedUnits, 5);
assert.equal(result.outstandingTotal, 70);
assert.equal(result.overdueTotal, 50);
assert.equal(result.recentTotal, 20);
assert.equal(result.revenue, 50);
assert.equal(metrics.isLowStock(state.items[0]), true);
assert.equal(metrics.isLowStock(state.items[1]), false);
assert.equal(metrics.isOlderThanDays({date:'2026-08-17T12:00:00Z'}, 7, now), false);
assert.equal(metrics.overview({items:[], transactions:[{type:'incoming', qty:10, price:10, paid:true, done:true}]}, now).revenue, -100);
console.log('PASS dashboard overview metrics');
