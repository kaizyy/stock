const fs = require('node:fs');
const assert = require('node:assert/strict');

const source = fs.readFileSync('crm_orders.js', 'utf8');
const install = source.lastIndexOf('installUI();');
const cancelBinding = source.lastIndexOf("getElementById('orderDialog').addEventListener('cancel'");
const refresh = source.lastIndexOf('refresh();');

assert.ok(install >= 0, 'de CRM-interface moet worden geïnstalleerd');
assert.ok(cancelBinding > install, 'het ordervenster moet bestaan voordat gebeurtenissen worden gekoppeld');
assert.ok(refresh > cancelBinding, 'gegevens worden pas geladen nadat de interface volledig klaarstaat');

