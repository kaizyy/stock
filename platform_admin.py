import json
import os
import uuid
from datetime import date

import server


def platform_admin_emails():
    return {e.strip().lower() for e in os.environ.get('PLATFORM_ADMIN_EMAILS', '').split(',') if e.strip()}


def is_platform_admin(session):
    return bool(session and str(session.get('email') or '').lower() in platform_admin_emails())


def initialize_platform_admin():
    with server.db() as conn:
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_reason TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE stockrooms ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE stockrooms ADD COLUMN IF NOT EXISTS suspended_reason TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS platform_audit_log (id BIGSERIAL PRIMARY KEY,actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,action TEXT NOT NULL,target_type TEXT NOT NULL,target_id TEXT NOT NULL,details JSONB NOT NULL DEFAULT '{}'::jsonb,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_platform_audit_created ON platform_audit_log(created_at DESC)")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_error_log (id BIGSERIAL PRIMARY KEY,level TEXT NOT NULL DEFAULT 'error',component TEXT NOT NULL DEFAULT 'app',message TEXT NOT NULL,stockroom_id UUID REFERENCES stockrooms(id) ON DELETE SET NULL,user_id UUID REFERENCES users(id) ON DELETE SET NULL,details JSONB NOT NULL DEFAULT '{}'::jsonb,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_app_error_created ON app_error_log(created_at DESC)")
        conn.execute("""CREATE TABLE IF NOT EXISTS notification_states (
          user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
          notification_key TEXT NOT NULL,
          read_at TIMESTAMPTZ,dismissed_at TIMESTAMPTZ,snoozed_until TIMESTAMPTZ,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY(user_id,stockroom_id,notification_key))""")
        conn.commit()


def account_status(session):
    if not session:return None
    with server.db() as conn:
        return conn.execute("SELECT u.suspended_at user_suspended_at,u.suspended_reason user_suspended_reason,r.suspended_at stockroom_suspended_at,r.suspended_reason stockroom_suspended_reason FROM users u JOIN stockrooms r ON r.id=%s WHERE u.id=%s",(session['stockroom_id'],session['user_id'])).fetchone()


def enforce_access(session):
    status=account_status(session)
    if not status:return True,''
    if status['user_suspended_at']:return False,'Dit account is door de platformbeheerder geblokkeerd.'
    if status['stockroom_suspended_at'] and not is_platform_admin(session):return False,'Deze stockroom is tijdelijk geblokkeerd.'
    return True,''


def _audit(conn,session,action,target_type,target_id,details=None):
    conn.execute("INSERT INTO platform_audit_log(actor_user_id,action,target_type,target_id,details) VALUES(%s,%s,%s,%s,%s::jsonb)",(session['user_id'],action,target_type,str(target_id),json.dumps(details or {},ensure_ascii=False)))


def platform_overview():
    with server.db() as conn:
        stats=conn.execute("SELECT (SELECT COUNT(*) FROM users) users,(SELECT COUNT(*) FROM stockrooms) stockrooms,(SELECT COUNT(*) FROM sessions WHERE expires_at>NOW()) active_sessions,(SELECT COUNT(*) FROM users WHERE suspended_at IS NOT NULL) suspended_users,(SELECT COUNT(*) FROM stockrooms WHERE suspended_at IS NOT NULL) suspended_stockrooms,(SELECT COUNT(*) FROM app_error_log WHERE created_at>NOW()-INTERVAL '24 hours') errors_24h").fetchone()
        rooms=conn.execute("SELECT r.id::text,r.name,r.created_at,r.updated_at,r.suspended_at,r.suspended_reason,u.email owner_email,u.name owner_name,COUNT(DISTINCT m.user_id) member_count,COUNT(DISTINCT o.id) order_count FROM stockrooms r JOIN users u ON u.id=r.created_by LEFT JOIN memberships m ON m.stockroom_id=r.id LEFT JOIN orders o ON o.stockroom_id=r.id GROUP BY r.id,u.email,u.name ORDER BY r.created_at DESC LIMIT 250").fetchall()
        users=conn.execute("SELECT u.id::text,u.name,u.email,u.created_at,u.email_verified_at,u.suspended_at,u.suspended_reason,COUNT(DISTINCT m.stockroom_id) stockroom_count,MAX(s.created_at) last_session_at FROM users u LEFT JOIN memberships m ON m.user_id=u.id LEFT JOIN sessions s ON s.user_id=u.id GROUP BY u.id ORDER BY u.created_at DESC LIMIT 500").fetchall()
        errors=conn.execute("SELECT id,level,component,message,stockroom_id::text,user_id::text,details,created_at FROM app_error_log ORDER BY created_at DESC LIMIT 100").fetchall()
        audit=conn.execute("SELECT p.id,p.action,p.target_type,p.target_id,p.details,p.created_at,u.email actor_email FROM platform_audit_log p LEFT JOIN users u ON u.id=p.actor_user_id ORDER BY p.created_at DESC LIMIT 100").fetchall()
    return {'stats':stats,'stockrooms':rooms,'users':users,'errors':errors,'audit':audit}


def set_suspension(session,target_type,target_id,suspended,reason=''):
    if target_type not in ('user','stockroom'):raise ValueError('Ongeldig doel.')
    table='users' if target_type=='user' else 'stockrooms'
    with server.db() as conn:
        if target_type=='user' and str(target_id)==str(session['user_id']) and suspended:raise ValueError('Je kunt je eigen platformaccount niet blokkeren.')
        row=conn.execute(f"UPDATE {table} SET suspended_at=CASE WHEN %s THEN NOW() ELSE NULL END,suspended_reason=CASE WHEN %s THEN %s ELSE '' END WHERE id=%s RETURNING id::text",(suspended,suspended,reason[:500],target_id)).fetchone()
        if not row:raise ValueError('Doel niet gevonden.')
        if target_type=='user' and suspended:conn.execute('DELETE FROM sessions WHERE user_id=%s',(target_id,))
        if target_type=='stockroom' and suspended:conn.execute('DELETE FROM sessions WHERE active_stockroom_id=%s',(target_id,))
        _audit(conn,session,'suspend' if suspended else 'unsuspend',target_type,target_id,{'reason':reason[:500]});conn.commit()
    return {'updated':True}


def _notification_key(n):
    target=str(n.get('targetId') or '')
    if target:return f"{n.get('type','notice')}:{n.get('targetType','target')}:{target}"
    return f"{n.get('type','notice')}:{n.get('title','')}:{n.get('detail','')}"[:500]


def stockroom_notifications(stockroom_id,user_id=None):
    notifications=[]
    with server.db() as conn:
        row=conn.execute('SELECT state FROM stockrooms WHERE id=%s',(stockroom_id,)).fetchone();state=(row or {}).get('state') or {'items':[],'transactions':[]}
        for item in state.get('items',[]):
            if item.get('archived'):continue
            stock=float(item.get('stock') or 0);minimum=float(item.get('minStock') or 0)
            if minimum>0 and stock<=minimum:
                iid=str(item.get('id') or '');notifications.append({'type':'low_stock','severity':'warning','title':f"Lage voorraad: {item.get('name','Artikel')}",'detail':f'{stock:g} op voorraad · minimum {minimum:g}','targetView':'inventory','targetType':'item','targetId':iid,'itemId':iid})
        for tx in state.get('transactions',[]):
            tid=str(tx.get('id') or '')
            if tx.get('type')=='outgoing' and not tx.get('done'):notifications.append({'type':'unpaid','severity':'warning','title':'Openstaande verkoop','detail':f"{tx.get('party') or tx.get('itemName') or 'Verkoop'} · nog niet afgerond",'targetView':'outgoing','targetType':'transaction','targetId':tid,'transactionId':tid})
            elif tx.get('type')=='incoming' and not tx.get('done'):notifications.append({'type':'delivery','severity':'info','title':'Levering nog niet ontvangen','detail':f"{tx.get('party') or tx.get('itemName') or 'Inkoop'}",'targetView':'incoming','targetType':'transaction','targetId':tid,'transactionId':tid})
        pending=conn.execute("SELECT id::text,order_type,status,order_number,reference,relation_name,order_date FROM orders WHERE stockroom_id=%s AND status NOT IN ('received','completed','paid','cancelled') ORDER BY order_date LIMIT 100",(stockroom_id,)).fetchall()
        for order in pending:notifications.append({'type':'order','severity':'info','title':f"Open order {order.get('order_number') or order.get('reference') or ''}",'detail':f"{order['relation_name'] or 'Geen relatie'} · {order['status']}",'targetView':'orders','targetType':'order','targetId':order['id'],'orderId':order['id'],'orderType':order['order_type'],'status':order['status']})
        returns_ready=conn.execute("SELECT to_regclass('public.order_returns') IS NOT NULL AS ready").fetchone()['ready']
        if returns_ready:
            open_returns=conn.execute("SELECT r.id::text,r.rma_number,r.return_type,o.relation_name FROM order_returns r JOIN orders o ON o.id=r.order_id WHERE r.stockroom_id=%s AND r.status='registered' ORDER BY r.created_at",(stockroom_id,)).fetchall()
            for result in open_returns:notifications.append({'type':'return','severity':'warning','title':f"Retour {result['rma_number'] or ''} verwerken",'detail':result['relation_name'] or 'Geen relatie','targetView':'orders','targetType':'return','targetId':result['id'],'returnType':result['return_type']})
            claims=conn.execute("SELECT r.id::text,r.rma_number,r.expected_refund::float8,r.received_refund::float8,o.relation_name FROM order_returns r JOIN orders o ON o.id=r.order_id WHERE r.stockroom_id=%s AND r.claim_status IN ('open','partial') ORDER BY r.claimed_at",(stockroom_id,)).fetchall()
            for claim in claims:notifications.append({'type':'supplier_claim','severity':'warning','title':f"Leveranciersclaim {claim['rma_number'] or ''} staat open",'detail':f"{claim['relation_name'] or 'Geen leverancier'} · nog € {float(claim['expected_refund'])-float(claim['received_refund']):.2f} te ontvangen",'targetView':'orders','targetType':'return','targetId':claim['id']})
        errors=conn.execute("SELECT component,message,created_at FROM app_error_log WHERE stockroom_id=%s AND created_at>NOW()-INTERVAL '7 days' ORDER BY created_at DESC LIMIT 20",(stockroom_id,)).fetchall()
        for error in errors:notifications.append({'type':'system','severity':'danger','title':f"Systeemmelding: {error['component']}",'detail':error['message'][:250],'createdAt':error['created_at']})
        states={}
        if user_id:
            rows=conn.execute("SELECT notification_key,read_at,dismissed_at,snoozed_until FROM notification_states WHERE user_id=%s AND stockroom_id=%s",(user_id,stockroom_id)).fetchall();states={r['notification_key']:r for r in rows}
        visible=[]
        for n in notifications:
            key=_notification_key(n);n['key']=key;s=states.get(key)
            if s and s['dismissed_at']:continue
            if s and s['snoozed_until'] and s['snoozed_until']>__import__('datetime').datetime.now(__import__('datetime').timezone.utc):continue
            n['read']=bool(s and s['read_at']);visible.append(n)
    return visible[:100]


def action_center(stockroom_id, role):
    actions=[]
    can_manage = role in ('owner','admin','member')
    can_purchase = can_manage or role == 'buyer'
    can_sales = can_manage or role == 'seller'
    with server.db() as conn:
        row=conn.execute('SELECT state FROM stockrooms WHERE id=%s',(stockroom_id,)).fetchone();state=(row or {}).get('state') or {'items':[]}
        for item in state.get('items',[]):
            if item.get('archived'):continue
            stock=float(item.get('stock') or 0);minimum=float(item.get('minStock') or 0)
            if minimum>0 and stock<=minimum:
                actions.append({'key':f"stock:{item.get('id')}",'severity':'danger' if stock<=0 else 'warning','title':f"Besteladvies: {item.get('name') or 'Artikel'}",'detail':f"{stock:g} beschikbaar · minimum {minimum:g}",'targetView':'inventory','actionLabel':'Besteladvies openen'})
        returns_ready=conn.execute("SELECT to_regclass('public.order_returns') IS NOT NULL AS ready").fetchone()['ready']
        if returns_ready and (can_purchase or can_sales):
            open_returns=conn.execute("""SELECT r.id::text,r.rma_number,r.return_type,r.created_at,o.relation_name
                FROM order_returns r JOIN orders o ON o.id=r.order_id WHERE r.stockroom_id=%s AND r.status='registered'
                ORDER BY r.created_at""",(stockroom_id,)).fetchall()
            for result in open_returns:
                if (result['return_type']=='purchase' and not can_purchase) or (result['return_type']=='sales' and not can_sales):continue
                actions.append({'key':f"return:{result['id']}",'severity':'warning','title':f"Retour {result['rma_number'] or ''} wacht op verwerking",'detail':f"{result['relation_name'] or 'Geen relatie'} · aangemeld {result['created_at'].strftime('%d-%m-%Y')}",'targetView':'orders','actionLabel':'Retour bekijken'})
            if can_purchase:
                claims=conn.execute("""SELECT r.id::text,r.rma_number,r.expected_refund::float8,r.received_refund::float8,o.relation_name
                    FROM order_returns r JOIN orders o ON o.id=r.order_id WHERE r.stockroom_id=%s AND r.claim_status IN ('open','partial') ORDER BY r.claimed_at""",(stockroom_id,)).fetchall()
                for claim in claims:actions.append({'key':f"supplier-claim:{claim['id']}",'severity':'warning','title':f"Leveranciersclaim {claim['rma_number'] or ''} staat open",'detail':f"{claim['relation_name'] or 'Geen leverancier'} · nog € {float(claim['expected_refund'])-float(claim['received_refund']):.2f} te ontvangen",'targetView':'orders','actionLabel':'Claim bekijken'})
        if role in ('owner','admin'):
            counts=conn.execute("SELECT id::text,title,submitted_at FROM inventory_counts WHERE stockroom_id=%s AND status='submitted' ORDER BY submitted_at",(stockroom_id,)).fetchall()
            for count in counts:actions.append({'key':f"count:{count['id']}",'severity':'warning','title':'Voorraadtelling wacht op goedkeuring','detail':count['title'],'targetView':'warehouse','actionLabel':'Telling beoordelen'})
        if can_purchase:
            drafts=conn.execute("SELECT id::text,COALESCE(order_number,reference,'') number,relation_name,created_at FROM orders WHERE stockroom_id=%s AND order_type='purchase' AND status='draft' ORDER BY created_at",(stockroom_id,)).fetchall()
            for order in drafts:actions.append({'key':f"purchase-draft:{order['id']}",'severity':'info','title':'Concept-inkooporder controleren','detail':f"{order['number'] or 'Concept'} · {order['relation_name'] or 'Geen leverancier'}",'targetView':'orders','actionLabel':'Order openen'})
            late=conn.execute("SELECT id::text,COALESCE(order_number,reference,'') number,relation_name,order_date FROM orders WHERE stockroom_id=%s AND order_type='purchase' AND status IN ('ordered','partial') AND order_date<CURRENT_DATE-14 ORDER BY order_date",(stockroom_id,)).fetchall()
            for order in late:actions.append({'key':f"late-delivery:{order['id']}",'severity':'warning','title':'Levering mogelijk te laat','detail':f"{order['number'] or 'Inkooporder'} · besteld op {order['order_date'].strftime('%d-%m-%Y')}",'targetView':'orders','actionLabel':'Levering bekijken'})
            unconfirmed=conn.execute("SELECT id::text,COALESCE(order_number,reference,'Inkooporder') number,relation_name,purchase_sent_at FROM orders WHERE stockroom_id=%s AND order_type='purchase' AND status IN ('ordered','partial') AND purchase_sent_at<NOW()-INTERVAL '3 days' AND supplier_confirmed_at IS NULL ORDER BY purchase_sent_at",(stockroom_id,)).fetchall()
            for order in unconfirmed:actions.append({'key':f"supplier-confirmation:{order['id']}",'severity':'warning','title':'Bestelbevestiging ontbreekt','detail':f"{order['number']} · {order['relation_name'] or 'Geen leverancier'} · langer dan 3 dagen verzonden",'targetView':'orders','actionLabel':'Bevestiging registreren'})
            delayed=conn.execute("SELECT id::text,COALESCE(order_number,reference,'Inkooporder') number,relation_name,confirmed_delivery_date FROM orders WHERE stockroom_id=%s AND order_type='purchase' AND status IN ('ordered','partial') AND confirmed_delivery_date<CURRENT_DATE ORDER BY confirmed_delivery_date",(stockroom_id,)).fetchall()
            for order in delayed:actions.append({'key':f"confirmed-delay:{order['id']}",'severity':'danger','title':'Bevestigde leverdatum verstreken','detail':f"{order['number']} · {order['relation_name'] or 'Geen leverancier'} · verwacht {order['confirmed_delivery_date'].strftime('%d-%m-%Y')}",'targetView':'orders','actionLabel':'Levering opvolgen'})
            shortages=conn.execute("""SELECT o.id::text,COALESCE(o.order_number,o.reference,'Inkooporder') number,l.item_name,
                GREATEST(0,l.quantity-l.supplier_cancelled_quantity-GREATEST(l.fulfilled_quantity,r.available_quantity)-COALESCE((SELECT SUM(sl.quantity-sl.fulfilled_quantity-sl.supplier_cancelled_quantity) FROM order_lines sl JOIN orders so ON so.id=sl.order_id WHERE sl.source_order_line_id=l.id AND so.status IN ('draft','pending_approval','approved','ordered','partial')),0))::float8 shortage
                FROM orders o JOIN order_lines l ON l.order_id=o.id JOIN supplier_portal_line_responses r ON r.order_line_id=l.id
                WHERE o.stockroom_id=%s AND r.availability IN ('partial','unavailable') ORDER BY o.created_at""",(stockroom_id,)).fetchall()
            for shortage in shortages:
                if shortage['shortage']>0.0005:actions.append({'key':f"supplier-shortage:{shortage['id']}:{shortage['item_name']}",'severity':'danger','title':'Leverancierstekort oplossen','detail':f"{shortage['number']} · {shortage['item_name']} · {shortage['shortage']:g} tekort",'targetView':'orders','actionLabel':'Alternatieven bekijken'})
            receipts_ready=conn.execute("SELECT to_regclass('public.purchase_discrepancy_actions') IS NOT NULL AS ready").fetchone()['ready']
            if receipts_ready:
                receipt_issues=conn.execute("""SELECT DISTINCT r.id::text,COALESCE(o.order_number,o.reference,'Inkooporder') number,r.reference
                    FROM purchase_receipts r JOIN orders o ON o.id=r.order_id LEFT JOIN purchase_receipt_lines l ON l.receipt_id=r.id
                    WHERE r.stockroom_id=%s AND r.reversed_at IS NULL AND (l.discrepancy_code<>'match' OR jsonb_array_length(r.unexpected_items)>0)
                    AND NOT EXISTS(SELECT 1 FROM purchase_discrepancy_actions a WHERE a.receipt_id=r.id) ORDER BY r.id::text""",(stockroom_id,)).fetchall()
                for issue in receipt_issues:actions.append({'key':f"receipt-discrepancy:{issue['id']}",'severity':'warning','title':'Pakbonafwijking vraagt actie','detail':f"{issue['number']} · {issue['reference'] or 'ontvangst zonder pakbonnummer'}",'targetView':'orders','actionLabel':'Afwijking behandelen'})
            if role in ('owner','admin'):
                approvals=conn.execute("SELECT id::text,COALESCE(order_number,reference,'Inkooporder') number,relation_name,approval_reason FROM orders WHERE stockroom_id=%s AND order_type='purchase' AND status='pending_approval' ORDER BY created_at",(stockroom_id,)).fetchall()
                for order in approvals:actions.append({'key':f"purchase-approval:{order['id']}",'severity':'warning','title':'Inkooporder wacht op goedkeuring','detail':f"{order['number']} · {order['relation_name'] or 'Geen leverancier'} · {order['approval_reason']}",'targetView':'orders','actionLabel':'Order beoordelen'})
            invoice_ready=conn.execute("SELECT to_regclass('public.purchase_invoices') IS NOT NULL AS ready").fetchone()['ready']
            if invoice_ready:
                purchase_invoices=conn.execute("""SELECT i.id::text,i.invoice_number,i.due_date,i.status,i.total_amount::float8,i.paid_amount::float8,o.relation_name,
                    COALESCE((SELECT SUM(c.amount) FROM purchase_invoice_credits c WHERE c.invoice_id=i.id),0)::float8 credited
                    FROM purchase_invoices i JOIN orders o ON o.id=i.order_id WHERE i.stockroom_id=%s AND i.status NOT IN ('paid','rejected') ORDER BY i.due_date""",(stockroom_id,)).fetchall()
                for invoice in purchase_invoices:
                    outstanding=max(0,invoice['total_amount']-invoice['paid_amount']-invoice['credited'])
                    if invoice['status'] in ('blocked','matched'):
                        actions.append({'key':f"purchase-invoice-review:{invoice['id']}",'severity':'danger' if invoice['status']=='blocked' else 'warning','title':f"Inkoopfactuur {invoice['invoice_number']} wacht op controle",'detail':f"{invoice['relation_name'] or 'Geen leverancier'} · € {outstanding:.2f} open",'targetView':'finance','actionLabel':'Factuur controleren'})
                    elif invoice['due_date']<date.today() and outstanding>0.005:
                        actions.append({'key':f"purchase-invoice-due:{invoice['id']}",'severity':'danger','title':f"Inkoopfactuur {invoice['invoice_number']} is vervallen",'detail':f"{invoice['relation_name'] or 'Geen leverancier'} · € {outstanding:.2f} open",'targetView':'finance','actionLabel':'Betaling bekijken'})
        if can_sales:
            invoices=conn.execute("""SELECT i.order_id::text id,i.invoice_number,i.due_date,o.relation_name
                FROM invoice_documents i JOIN orders o ON o.id=i.order_id
                WHERE i.stockroom_id=%s AND i.deleted_at IS NULL AND i.due_date<CURRENT_DATE AND i.paid_at IS NULL ORDER BY i.due_date""",(stockroom_id,)).fetchall()
            for invoice in invoices:actions.append({'key':f"invoice:{invoice['id']}",'severity':'danger','title':f"Factuur {invoice['invoice_number']} is verlopen",'detail':f"{invoice['relation_name'] or 'Geen klant'} · vervallen {invoice['due_date'].strftime('%d-%m-%Y')}",'targetView':'finance','actionLabel':'Factuur openen'})
            quote_invoices=conn.execute("SELECT id::text,invoice_number,due_date,relation_name FROM quotes WHERE stockroom_id=%s AND invoice_number IS NOT NULL AND converted_order_id IS NULL AND due_date<CURRENT_DATE AND invoice_paid_at IS NULL ORDER BY due_date",(stockroom_id,)).fetchall()
            for invoice in quote_invoices:actions.append({'key':f"quote-invoice:{invoice['id']}",'severity':'danger','title':f"Factuur {invoice['invoice_number']} is verlopen",'detail':f"{invoice['relation_name'] or 'Geen klant'} · vervallen {invoice['due_date'].strftime('%d-%m-%Y')}",'targetView':'finance','actionLabel':'Factuur openen'})
            quotes=conn.execute("SELECT id::text,quote_number,relation_name,sent_at FROM quotes WHERE stockroom_id=%s AND status='sent' AND converted_order_id IS NULL AND sent_at<NOW()-INTERVAL '7 days' ORDER BY sent_at",(stockroom_id,)).fetchall()
            for quote in quotes:actions.append({'key':f"quote-followup:{quote['id']}",'severity':'info','title':f"Offerte {quote['quote_number']} opvolgen",'detail':f"{quote['relation_name'] or 'Geen klant'} · langer dan 7 dagen verzonden",'targetView':'quotes','actionLabel':'Offerte openen'})
        if can_manage:
            reservations=conn.execute("""SELECT 'order' kind,r.order_id::text id,COALESCE(o.order_number,o.reference,'Verkooporder') label,MIN(r.created_at) created_at
                FROM inventory_reservations r JOIN orders o ON o.id=r.order_id WHERE r.stockroom_id=%s AND r.created_at<NOW()-INTERVAL '14 days' GROUP BY r.order_id,o.order_number,o.reference
                UNION ALL SELECT 'quote',r.quote_id::text,q.quote_number,MIN(r.created_at) FROM quote_reservations r JOIN quotes q ON q.id=r.quote_id WHERE r.stockroom_id=%s AND r.created_at<NOW()-INTERVAL '14 days' GROUP BY r.quote_id,q.quote_number""",(stockroom_id,stockroom_id)).fetchall()
            for reservation in reservations:actions.append({'key':f"reservation:{reservation['kind']}:{reservation['id']}",'severity':'warning','title':'Reservering staat lang open','detail':f"{reservation['label']} · ouder dan 14 dagen",'targetView':'inventory','actionLabel':'Reservering bekijken'})
        batches_ready=conn.execute("SELECT to_regclass('public.payment_batches') IS NOT NULL AS ready").fetchone()['ready']
        if batches_ready and role in ('owner','admin'):
            pending_batches=conn.execute("SELECT id::text,batch_number,status,execution_date FROM payment_batches WHERE stockroom_id=%s AND status IN ('draft','exported') ORDER BY execution_date",(stockroom_id,)).fetchall()
            for batch in pending_batches:actions.append({'key':f"payment-batch:{batch['id']}",'severity':'warning','title':f"Betaalbatch {batch['batch_number']} vraagt actie",'detail':'Wacht op goedkeuring' if batch['status']=='draft' else 'SEPA geëxporteerd · bevestig verwerking na uitvoering door de bank','targetView':'finance','actionLabel':'Betaalbatch bekijken'})
        bank_ready=conn.execute("SELECT to_regclass('public.bank_transactions') IS NOT NULL AS ready").fetchone()['ready']
        if bank_ready and role in ('owner','admin'):
            unmatched=conn.execute("SELECT COUNT(*) n,COALESCE(SUM(ABS(amount)),0)::float8 total FROM bank_transactions WHERE stockroom_id=%s AND status='unmatched'",(stockroom_id,)).fetchone()
            if unmatched['n']:actions.append({'key':'bank-unmatched','severity':'warning','title':f"{unmatched['n']} bankmutatie(s) vragen controle",'detail':f"Totaal te beoordelen: € {unmatched['total']:.2f}",'targetView':'finance','actionLabel':'Bankmutaties koppelen'})
    rank={'danger':0,'warning':1,'info':2}
    actions.sort(key=lambda action:(rank.get(action['severity'],3),action['title']))
    return actions[:100]


def update_notification_state(session,key,action):
    key=(key or '')[:500]
    if not key:raise ValueError('Melding ontbreekt.')
    if action not in ('read','unread','dismiss','snooze'):raise ValueError('Ongeldige meldingactie.')
    with server.db() as conn:
        conn.execute("INSERT INTO notification_states(user_id,stockroom_id,notification_key) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",(session['user_id'],session['stockroom_id'],key))
        if action=='read':conn.execute("UPDATE notification_states SET read_at=NOW(),updated_at=NOW() WHERE user_id=%s AND stockroom_id=%s AND notification_key=%s",(session['user_id'],session['stockroom_id'],key))
        elif action=='unread':conn.execute("UPDATE notification_states SET read_at=NULL,updated_at=NOW() WHERE user_id=%s AND stockroom_id=%s AND notification_key=%s",(session['user_id'],session['stockroom_id'],key))
        elif action=='dismiss':conn.execute("UPDATE notification_states SET dismissed_at=NOW(),updated_at=NOW() WHERE user_id=%s AND stockroom_id=%s AND notification_key=%s",(session['user_id'],session['stockroom_id'],key))
        else:conn.execute("UPDATE notification_states SET snoozed_until=NOW()+INTERVAL '1 day',updated_at=NOW() WHERE user_id=%s AND stockroom_id=%s AND notification_key=%s",(session['user_id'],session['stockroom_id'],key))
        conn.commit()
    return {'updated':True}


def record_error(component,message,stockroom_id=None,user_id=None,details=None,level='error'):
    try:
        with server.db() as conn:
            conn.execute("INSERT INTO app_error_log(level,component,message,stockroom_id,user_id,details) VALUES(%s,%s,%s,%s,%s,%s::jsonb)",(level[:20],component[:100],str(message)[:2000],stockroom_id,user_id,json.dumps(details or {},ensure_ascii=False)));conn.commit()
    except Exception:pass
