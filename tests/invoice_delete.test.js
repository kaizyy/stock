const fs=require('node:fs');const assert=require('node:assert/strict');
const ui=fs.readFileSync('invoice_delete_ui.js','utf8');
assert.match(ui,/data-fin-delete/);
assert.match(ui,/api\/finance\/delete/);
assert.match(ui,/api\/finance\/trash/);
assert.match(ui,/api\/finance\/restore/);
assert.match(ui,/betalingen en creditnota's blijven behouden/);
assert.match(ui,/Factuurprullenbak/);

