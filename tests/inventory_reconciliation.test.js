const assert = require('node:assert/strict');
const {summarize} = require('../inventory_reconciliation_ui.js');
assert.deepEqual(summarize([
  {status:'ok'}, {status:'difference'}, {status:'difference'}, {status:'unavailable'},
]), {differences:2, checked:3, unavailable:1});
console.log('PASS inventory reconciliation summary');

