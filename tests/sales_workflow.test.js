const fs=require('node:fs'),assert=require('node:assert/strict');
const source=fs.readFileSync('sales_workflow_ui.js','utf8');
assert.match(source,/Nieuwe offerte/);assert.match(source,/api\/quotes\/convert/);assert.match(source,/Naar factuur/);assert.match(source,/volledige betaling wordt automatisch een verkooporder aangemaakt/);
assert.match(source,/api\/quotes\/delete/);assert.match(source,/data-quote-delete/);
assert.match(source,/window\.addEventListener\('load',install\);install\(\)/);
