const fs = require('node:fs');
const assert = require('node:assert/strict');

const html = fs.readFileSync('index.html', 'utf8');
const app = fs.readFileSync('app.js', 'utf8');

const cancelButtons = [...html.matchAll(/<button[^>]*class="[^"]*cancel-transaction[^"]*"[^>]*>/g)].map(match => match[0]);
assert.equal(cancelButtons.length, 2, 'sluiten en annuleren moeten aparte annuleerknoppen zijn');
for (const button of cancelButtons) assert.match(button, /type="button"/, 'een annuleerknop mag het formulier niet indienen');
assert.match(html, /id="saveTransaction" type="submit"/, 'alleen Opslaan dient het formulier in');
assert.match(app, /function cancelTransaction\(\)/, 'annuleren heeft een expliciet pad');
assert.match(app, /addEventListener\('cancel'/, 'Escape annuleert via hetzelfde veilige pad');

