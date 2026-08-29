const fs=require('node:fs'),assert=require('node:assert/strict');
const source=fs.readFileSync('sales_workflow_ui.js','utf8');
assert.match(source,/Nieuwe offerte/);assert.match(source,/api\/quotes\/convert/);assert.match(source,/Naar order \+ factuur/);assert.match(source,/voorraad wordt direct gereserveerd/);

