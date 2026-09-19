"""Token-based supplier portal for purchase-order confirmations."""
import html
import json
import secrets

import server


def initialize():
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS supplier_portal_links(
            order_id UUID PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
            stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,expires_at TIMESTAMPTZ NOT NULL,revoked_at TIMESTAMPTZ,
            created_by UUID REFERENCES users(id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),last_used_at TIMESTAMPTZ)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS supplier_portal_line_responses(
            order_line_id UUID PRIMARY KEY REFERENCES order_lines(id) ON DELETE CASCADE,
            availability TEXT NOT NULL CHECK(availability IN ('full','partial','unavailable')),
            available_quantity NUMERIC(14,3) NOT NULL DEFAULT 0 CHECK(available_quantity>=0),note TEXT NOT NULL DEFAULT '',updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.commit()


def issue(session, order_id, base_url, days=30):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om een leverancierslink te maken.')
    try:days=max(1,min(90,int(days or 30)))
    except (TypeError,ValueError):days=30
    raw=secrets.token_urlsafe(32)
    with server.db() as conn:
        order=conn.execute("SELECT id FROM orders WHERE id=%s AND stockroom_id=%s AND order_type='purchase' AND status IN ('approved','ordered','partial')",(order_id,session['stockroom_id'])).fetchone()
        if not order:raise PermissionError('Inkooporder niet gevonden.')
        conn.execute("""INSERT INTO supplier_portal_links(order_id,stockroom_id,token_hash,expires_at,created_by)
            VALUES(%s,%s,%s,NOW()+(%s*INTERVAL '1 day'),%s) ON CONFLICT(order_id) DO UPDATE SET token_hash=EXCLUDED.token_hash,
            expires_at=EXCLUDED.expires_at,revoked_at=NULL,created_by=EXCLUDED.created_by,created_at=NOW()""",(order_id,session['stockroom_id'],server.token_digest(raw),days,session['user_id']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase.portal_link_created',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':order_id,'expiresInDays':days})));conn.commit()
    return {'url':f"{base_url.rstrip('/')}/supplier-order?token={raw}",'expiresInDays':days}


def revoke(session, order_id):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om de leverancierslink in te trekken.')
    with server.db() as conn:
        row=conn.execute("UPDATE supplier_portal_links SET revoked_at=NOW() WHERE order_id=%s AND stockroom_id=%s AND revoked_at IS NULL RETURNING order_id",(order_id,session['stockroom_id'])).fetchone()
        if not row:raise ValueError('Er is geen actieve leverancierslink.')
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase.portal_link_revoked',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':order_id})));conn.commit()
    return {'revoked':True}


def lookup(raw):
    if not raw:return None
    with server.db() as conn:
        order=conn.execute("""SELECT o.id::text,o.stockroom_id::text,o.order_number,o.reference,o.relation_name,o.status,o.order_date,
            o.expected_delivery_date,o.confirmed_delivery_date,o.confirmation_reference,o.confirmation_note,b.company_name,p.expires_at
            FROM supplier_portal_links p JOIN orders o ON o.id=p.order_id LEFT JOIN billing_accounts b ON b.stockroom_id=o.stockroom_id
            WHERE p.token_hash=%s AND p.revoked_at IS NULL AND p.expires_at>NOW() AND o.status IN ('approved','ordered','partial')""",(server.token_digest(raw),)).fetchone()
        if not order:return None
        lines=conn.execute("""SELECT l.id::text,l.item_name,l.sku,l.quantity::float8,r.availability,r.available_quantity::float8,r.note
            FROM order_lines l LEFT JOIN supplier_portal_line_responses r ON r.order_line_id=l.id WHERE l.order_id=%s ORDER BY l.created_at""",(order['id'],)).fetchall()
    return {'order':order,'lines':lines}


def page(raw, message='', error=''):
    data=lookup(raw)
    if not data:return server.result_page('Link niet beschikbaar','Deze leverancierslink is ongeldig, verlopen of ingetrokken.')
    order=data['order'];esc=html.escape;number=order['order_number'] or order['reference'] or 'Inkooporder'
    rows=''.join(f"""<tr><td><strong>{esc(line['item_name'])}</strong><br><small>{esc(line['sku'] or '')}</small></td><td>{line['quantity']:g}</td><td><select name="availability_{line['id']}"><option value="full" {'selected' if (line['availability'] or 'full')=='full' else ''}>Volledig leverbaar</option><option value="partial" {'selected' if line['availability']=='partial' else ''}>Deels leverbaar</option><option value="unavailable" {'selected' if line['availability']=='unavailable' else ''}>Niet leverbaar</option></select></td><td><input name="quantity_{line['id']}" type="number" min="0" max="{line['quantity']}" step="0.001" value="{line['available_quantity'] if line['availability'] else line['quantity']}"></td><td><input name="note_{line['id']}" value="{esc(line['note'] or '')}" placeholder="Opmerking"></td></tr>""" for line in data['lines'])
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(number)}</title><style>body{{font:16px system-ui;background:#f4f6f4;color:#15251d;margin:0}}main{{max-width:950px;margin:auto;padding:24px}}section{{background:white;border-radius:14px;padding:22px;box-shadow:0 4px 20px #0001}}h1{{margin-top:0}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #ddd}}input,select,textarea,button{{box-sizing:border-box;padding:10px;border:1px solid #aaa;border-radius:8px;max-width:100%}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:18px 0}}label{{display:grid;gap:5px}}button,.button{{background:#176b45;color:white;text-decoration:none;display:inline-block;border:0;cursor:pointer}}.message{{padding:10px;background:#e8f7ee}}.error{{padding:10px;background:#fde8e8}}small{{color:#65736b}}@media(max-width:700px){{table,tbody,tr,td{{display:block}}thead{{display:none}}td{{border:0;padding:6px}}.grid{{grid-template-columns:1fr}}}}</style></head><body><main><section><small>{esc(order['company_name'] or 'Stockroom')}</small><h1>{esc(number)}</h1><p>Leverancier: {esc(order['relation_name'] or '')} · besteldatum {order['order_date'].strftime('%d-%m-%Y')}</p>{f'<p class="message">{esc(message)}</p>' if message else ''}{f'<p class="error">{esc(error)}</p>' if error else ''}<p><a class="button" href="/supplier-order.pdf?token={esc(raw)}">Inkooporder als PDF</a></p><form method="post" action="/supplier-order/respond"><input type="hidden" name="token" value="{esc(raw)}"><div class="grid"><label>Bevestigde leverdatum<input required name="confirmed_delivery_date" type="date" value="{order['confirmed_delivery_date'] or order['expected_delivery_date'] or ''}"></label><label>Uw referentie<input name="confirmation_reference" maxlength="120" value="{esc(order['confirmation_reference'] or '')}"></label></div><label>Algemene opmerking<textarea name="confirmation_note" maxlength="1000">{esc(order['confirmation_note'] or '')}</textarea></label><h2>Leverbaarheid per regel</h2><div style="overflow:auto"><table><thead><tr><th>Artikel</th><th>Besteld</th><th>Status</th><th>Leverbaar</th><th>Opmerking</th></tr></thead><tbody>{rows}</tbody></table></div><p><button type="submit">Bevestiging versturen</button></p></form><small>Deze beveiligde link verloopt op {order['expires_at'].strftime('%d-%m-%Y')}.</small></section></main></body></html>"""


def submit(raw, values):
    data=lookup(raw)
    if not data:raise PermissionError('Deze leverancierslink is ongeldig, verlopen of ingetrokken.')
    delivery=(values.get('confirmed_delivery_date') or '').strip();reference=(values.get('confirmation_reference') or '').strip()[:120];note=(values.get('confirmation_note') or '').strip()[:1000]
    if not delivery:raise ValueError('Vul de verwachte leverdatum in.')
    with server.db() as conn:
        try:confirmed=conn.execute("SELECT %s::date value",(delivery,)).fetchone()['value']
        except Exception as exc:raise ValueError('De verwachte leverdatum is ongeldig.') from exc
        for line in data['lines']:
            availability=(values.get(f"availability_{line['id']}") or 'full').strip()
            if availability not in ('full','partial','unavailable'):raise ValueError('Een leverbaarheidsstatus is ongeldig.')
            try:quantity=float(values.get(f"quantity_{line['id']}") or 0)
            except (TypeError,ValueError):raise ValueError('Een leverbaar aantal is ongeldig.')
            if availability=='full':quantity=float(line['quantity'])
            if availability=='unavailable':quantity=0
            if quantity<0 or quantity>float(line['quantity']):raise ValueError('Een leverbaar aantal valt buiten het bestelde aantal.')
            conn.execute("""INSERT INTO supplier_portal_line_responses(order_line_id,availability,available_quantity,note) VALUES(%s,%s,%s,%s)
                ON CONFLICT(order_line_id) DO UPDATE SET availability=EXCLUDED.availability,available_quantity=EXCLUDED.available_quantity,note=EXCLUDED.note,updated_at=NOW()""",(line['id'],availability,quantity,(values.get(f"note_{line['id']}") or '').strip()[:500]))
        conn.execute("UPDATE orders SET supplier_confirmed_at=NOW(),confirmed_delivery_date=%s,confirmation_reference=%s,confirmation_note=%s,updated_at=NOW() WHERE id=%s",(confirmed,reference,note,data['order']['id']))
        conn.execute("UPDATE supplier_portal_links SET last_used_at=NOW() WHERE order_id=%s",(data['order']['id'],))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,NULL,'purchase.supplier_portal_response',%s::jsonb)",(data['order']['stockroom_id'],json.dumps({'id':data['order']['id'],'confirmedDeliveryDate':str(confirmed),'reference':reference})));conn.commit()
    return {'confirmed':True}
