const fs = require('node:fs');
const assert = require('node:assert/strict');

const orders = fs.readFileSync('crm_orders.js', 'utf8');
const documents = fs.readFileSync('documents_v3_ui.js', 'utf8');

assert.match(orders, /data-relation-email=/, 'orders moeten het e-mailadres van de relatie beschikbaar maken');
assert.match(documents, /card\.dataset\.relationEmail/, 'documentacties moeten het relatie-e-mailadres lezen');
assert.match(documents, /prompt\('E-mailadres ontvanger:',defaultRecipient\)/, 'het ontvangersveld moet vooraf worden ingevuld');
assert.match(documents, /m\.dataset\.recipient\|\|''/, 'de mailactie moet het geselecteerde relatie-e-mailadres doorgeven');

