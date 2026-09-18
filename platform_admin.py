import json
import os
import uuid

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
            if role in ('owner','admin'):
                approvals=conn.execute("SELECT id::text,COALESCE(order_number,reference,'Inkooporder') number,relation_name,approval_reason FROM orders WHERE stockroom_id=%s AND order_type='purchase' AND status='pending_approval' ORDER BY created_at",(stockroom_id,)).fetchall()
                for order in approvals:actions.append({'key':f"purchase-approval:{order['id']}",'severity':'warning','title':'Inkooporder wacht op goedkeuring','detail':f"{order['number']} · {order['relation_name'] or 'Geen leverancier'} · {order['approval_reason']}",'targetView':'orders','actionLabel':'Order beoordelen'})
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
    rank={'g�z��$z{-���jםeference": f"TEST-{uuid.uuid4()}",
            "relation_name": "Relatie",
            "lines_json": json.dumps([{ "item_id": self.item_id, "item_name": "Testitem", "sku": "T-1", "quantity": qty, "unit_price": price }]),
        })

    def get_state(self):
        with server.db() as conn:
            return conn.execute("SELECT state FROM stockrooms WHERE id=%s", (self.room_id,)).fetchone()["state"]

    def test_sales_completed_books_once(self):
        order_id = self.create_order("sales", 3, 10)
        order_management.update_order_status(self.session, "sales", {"order_id": order_id, "status": "completed"})
        state = self.get_state()
        self.assertEqual(state["items"][0]["stock"], 7)
        self.assertEqual(len([t for t in state["transactions"] if t.get("orderId") == order_id]), 1)
        order_management.update_order_status(self.session, "sales", {"order_id": order_id, "status": "paid"})
        state = self.get_state()
        self.assertEqual(state["items"][0]["stock"], 7)
        self.assertEqual(len([t for t in state["transactions"] if t.get("orderId") == order_id]), 1)

    def test_sales_completed_rejects_insufficient_stock(self):
        with self.assertRaises(ValueError):
            self.create_order("sales", 11, 10)
        state = self.get_state()
        self.assertEqual(state["items"][0]["stock"], 10)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) count FROM orders WHERE stockroom_id=%s",(self.room_id,)).fetchone()["count"],0)

    def test_purchase_received_books_once_and_cannot_go_back(self):
        order_id = self.create_order("purchase", 5, 3.5)
        order_management.update_order_status(self.session, "purchase", {"order_id": order_id, "status": "received"})
        state = self.get_state()
        self.assertEqual(state["items"][0]["stock"], 15)
        self.assertEqual(state["items"][0]["buy"], 3.5)
        self.assertEqual(len([t for t in state["transactions"] if t.get("orderId") == order_id]), 1)
        with self.assertRaises(ValueError):
            order_management.update_order_status(self.session, "purchase", {"order_id": order_id, "status": "ordered"})
        state = self.get_state()
        self.assertEqual(state["items"][0]["stock"], 15)

    def test_purchase_advice_groups_supplier_and_prevents_duplicate_draft(self):
        supplier_id = order_management.save_relation(self.session, "supplier", {"name":"Supply BV", "email":"inkoop@example.test"})
        with server.db() as conn:
            state = conn.execute("SELECT state FROM stockrooms WHERE id=%s", (self.room_id,)).fetchone()["state"]
            state["items"][0]["supplier"] = "Supply BV"
            conn.execute("UPDATE stockrooms SET state=%s::jsonb WHERE id=%s", (json.dumps(state), self.room_id))
            conn.commit()
        result = order_management.create_purchase_advice_drafts(self.session, {"lines_json":json.dumps([
            {"item_id":self.item_id, "quantity":8}
        ])})
        self.assertEqual(len(result["created"]), 1)
        order = order_management.order_rows(str(self.room_id), "purchase")[0]
        self.assertEqual(order["relation_id"], supplier_id)
        self.assertEqual(order["status"], "draft")
        self.assertEqual(order["lines"][0]["quantity"], 8)
        self.assertIsNotNone(order["expected_delivery_date"])
        self.assertEqual(order["advice_details"]["source"],"purchase_advice")
        self.assertEqual(order_management.open_purchase_quantities(str(self.room_id))[self.item_id], 8)
        with self.assertRaisesRegex(ValueError, "voldoende open"):
            order_management.create_purchase_advice_drafts(self.session, {"lines_json":json.dumps([
                {"item_id":self.item_id, "quantity":8}
            ])})

    def test_purchase_advice_requires_reason_for_supplier_override(self):
        first=order_management.save_relation(self.session,"supplier",{"name":"Beste leverancier"})
        second=order_management.save_relation(self.session,"supplier",{"name":"Alternatief"})
        historical=order_management.create_order(self.session,{"order_type":"purchase","relation_id":first,"relation_name":"Beste leverancier","lines_json":json.dumps([{"item_id":self.item_id,"item_name":"Testitem","sku":"T-1","quantity":1,"unit_price":3}])})
        order_management.update_order_status(self.session,"purchase",{"order_id":historical,"status":"ordered"})
        line_id=order_management.order_rows(str(self.room_id),"purchase")[0]["lines"][0]["id"]
        purchase_receipts.receive(self.session,{"order_id":historical,"lines_json":json.dumps([{"line_id":line_id,"quantity":1}])})
        with self.assertRaisesRegex(ValueError,"reden"):
            order_management.create_purchase_advice_drafts(self.session,{"lines_json":json.dumps([{"item_id":self.item_id,"quantity":2,"supplier_id":second}])})
        result=order_management.create_purchase_advice_drafts(self.session,{"lines_json":json.dumps([{"item_id":self.item_id,"quantity":2,"supplier_id":second,"override_reason":"Contractuele afspraak"}])})
        self.assertEqual(result["created"][0]["supplier"],"Alternatief")

    def test_purchase_budget_requires_and_records_approval(self):
        purchase_approvals.save_policy(self.session,{"approval_threshold":"0","monthly_budget":"5","price_warning_requires_approval":"1"})
        result=order_management.create_purchase_advice_drafts(self.session,{"lines_json":json.dumps([{"item_id":self.item_id,"quantity":2}])})
        self.assertTrue(result["created"][0]["approvalRequired"])
        order=order_management.order_rows(str(self.room_id),"purchase")[0]
        self.assertEqual(order["status"],"pending_approval");self.assertIn("Maandbudget",order["approval_reason"])
        purchase_approvals.decide(self.session,{"order_id":order["id"],"reason":"Budget gecontroleerd"},"approve")
        smtp=MagicMock();smtp.__enter__.return_value=smtp
        with patch.object(server,"SMTP_HOST","smtp.example.test"),patch.object(server,"SMTP_PORT",587),patch.object(purchase_approvals.smtplib,"SMTP",return_value=smtp):
            sent=purchase_approvals.send_order(self.session,{"order_id":order["id"],"recipient":"supplier@example.test"})
        self.assertTrue(sent["sent"]);smtp.send_message.assert_called_once()
        delivery=(date.today()+timedelta(days=10)).isoformat()
        confirmation=purchase_approvals.confirm_delivery(self.session,{"order_id":order["id"],"confirmed_delivery_date":delivery,"confirmation_reference":"BEV-42"})
        self.assertTrue(confirmation["confirmed"])
        approved=order_management.order_rows(str(self.room_id),"purchase")[0]
        self.assertEqual(approved["status"],"ordered");self.assertEqual(approved["approval_status"],"approved");self.assertEqual(approved["purchase_sent_to"],"supplier@example.test");self.assertEqual(str(approved["confirmed_delivery_date"]),delivery)

    def test_partial_purchase_receipts_update_stock_status_and_can_reverse(self):
        order_id = self.create_order("purchase", 5, 3.5)
        order_management.update_order_status(self.session, "purchase", {"order_id":order_id, "status":"ordered"})
        line_id = order_management.order_rows(str(self.room_id), "purchase")[0]["lines"][0]["id"]
        first = purchase_receipts.receive(self.session, {"order_id":order_id,"reference":"PB-1",
            "lines_json":json.dumps([{"line_id":line_id,"quantity":2}])})
        self.assertEqual(first["status"], "partial")
        self.assertEqual(self.get_state()["items"][0]["stock"], 12)
        second = purchase_receipts.receive(self.session, {"order_id":order_id,"reference":"PB-2",
            "lines_json":json.dumps([{"line_id":line_id,"quantity":3}])})
        self.assertEqual(second["status"], "received")
        self.assertEqual(self.get_state()["items"][0]["stock"], 15)
        self.assertEqual(len(purchase_receipts.rows(str(self.room_id),order_id)),2)
        reversed_result=purchase_receipts.reverse(self.session,{"receipt_id":second["receiptId"]})
        self.assertEqual(reversed_result["status"],"partial")
        self.assertEqual(self.get_state()["items"][0]["stock"],12)
        purchase_receipts.reverse(self.session,{"receipt_id":first["receiptId"]})
        self.assertEqual(self.get_state()["items"][0]["stock"],10)
        self.assertEqual(order_management.order_rows(str(self.room_id),"purchase")[0]["status"],"ordered")

    def test_receipt_reversal_blocks_when_received_stock_was_used(self):
        order_id=self.create_order("purchase",2,3.5);order_management.update_order_status(self.session,"purchase",{"order_id":order_id,"status":"ordered"})
        line_id=order_management.order_rows(str(self.room_id),"purchase")[0]["lines"][0]["id"]
        receipt=purchase_receipts.receive(self.session,{"order_id":order_id,"lines_json":json.dumps([{"line_id":line_id,"quantity":2}])})
        with server.db() as conn:
            state=conn.execute("SELECT state FROM stockrooms WHERE id=%s",(self.room_id,)).fetchone()["state"];state["items"][0]["stock"]=1
            conn.execute("UPDATE stockrooms SET state=%s::jsonb WHERE id=%s",(json.dumps(state),self.room_id));conn.commit()
        with self.assertRaisesRegex(ValueError,"onvoldoende voorraad"):
            purchase_receipts.reverse(self.session,{"receipt_id":receipt["receiptId"]})

    def test_sales_return_is_bounded_processed_and_reversible(self):
        order_id=self.create_order("sales",3,10)
        order_management.update_order_status(self.session,"sales",{"order_id":order_id,"status":"completed"})
        line_id=order_management.order_rows(str(self.room_id),"sales")[0]["lines"][0]["id"]
        result=order_returns.create(self.session,{"order_id":order_id,"reason_code":"defective","reason":"Klantretour","lines_json":json.dumps([{"line_id":line_id,"quantity":2}])})
        self.assertRegex(result["rmaNumber"],r"^RMA-\d{4}-\d{6}$")
        label,filename=order_returns.label_pdf(self.session,result["id"])
        self.assertTrue(label.startswith(b"%PDF"));self.assertTrue(filename.startswith("RMA-"))
        self.assertEqual(self.get_state()["items"][0]["stock"],7)
        order_returns.process(self.session,{"return_id":result["id"]})
        self.assertEqual(self.get_state()["items"][0]["stock"],9)
        report=order_returns.analytics(str(self.room_id))
        self.assertEqual(report["summary"]["return_count"],1);self.assertEqual(report["reasons"][0]["label"],"defective")
        self.assertEqual(report["items"][0]["quantity"],2)
        with self.assertRaisesRegex(ValueError,"hoger dan geleverd"):
            order_returns.create(self.session,{"order_id":order_id,"lines_json":json.dumps([{"line_id":line_id,"quantity":2}])})
        order_returns.change(self.session,{"return_id":result["id"]},"reverse")
        self.assertEqual(self.get_state()["items"][0]["stock"],7)
        order_returns.change(self.session,{"return_id":result["id"]},"cancel")

    def test_purchase_return_uses_only_received_quantity(self):
        order_id=self.create_order("purchase",5,3.5)
        order_management.update_order_status(self.session,"purchase",{"order_id":order_id,"status":"ordered"})
        line_id=order_management.order_rows(str(self.room_id),"purchase")[0]["lines"][0]["id"]
        purchase_receipts.receive(self.session,{"order_id":order_id,"lines_json":json.dumps([{"line_id":line_id,"quantity":3}])})
        result=order_returns.create(self.session,{"order_id":order_id,"lines_json":json.dumps([{"line_id":line_id,"quantity":2}])})
        order_returns.process(self.session,{"return_id":result["id"]})
        self.assertEqual(self.get_state()["items"][0]["stock"],11)
        details=order_returns.overview(str(self.room_id),order_id)["returns"][0]
        self.assertEqual(details["claim_status"],"open");self.assertEqual(details["expected_refund"],7)
        order_returns.update_claim(self.session,{"return_id":result["id"],"claim_reference":"CLAIM-42","expected_refund":"7.00"})
        partial=order_returns.record_refund(self.session,{"return_id":result["id"],"amount":"2.00","note":"deelbetaling"})
        self.assertEqual(partial["claimStatus"],"partial")
        settled=order_returns.record_refund(self.session,{"return_id":result["id"],"amount":"5.00","note":"slotbetaling"})
        self.assertEqual(settled["claimStatus"],"settled")
        intelligence=purchase_intelligence.overview(str(self.room_id))
        self.assertEqual(intelligence["suppliers"][0]["name"],"Relatie")
        self.assertAlmostEqual(intelligence["suppliers"][0]["returnRate"],66.7,places=1)
        self.assertEqual(intelligence["recommendations"][0]["recommended"]["latestPrice"],3.5)
        with self.assertRaisesRegex(ValueError,"hoger dan geleverd"):
            order_returns.create(self.session,{"order_id":order_id,"lines_json":json.dumps([{"line_id":line_id,"quantity":2}])})
        with self.assertRaisesRegex(ValueError,"terugbetaling"):
            order_returns.change(self.session,{"return_id":result["id"]},"reverse")
        self.assertEqual(self.get_state()["items"][0]["stock"],11)

    def test_decimal_purchase_and_sale_preserve_stock_and_reservations(self):
        purchase_id = self.create_order("purchase", 0.1)
        order_management.update_order_status(self.session, "purchase", {"order_id": purchase_id, "status": "received"})
        self.assertAlmostEqual(self.get_state()["items"][0]["stock"], 10.1)

        sales_id = self.create_order("sales", 0.2)
        reservation = financial_workflow.reservation_overview(str(self.room_id))[0]
        self.assertAlmostEqual(reservation["reserved"], 0.2)
        self.assertAlmostEqual(reservation["available"], 9.9)
        order_management.update_order_status(self.session, "sales", {"order_id": sales_id, "status": "completed"})
        state = self.get_state()
        self.assertAlmostEqual(state["items"][0]["stock"], 9.9)
        self.assertAlmostEqual(next(t for t in state["transactions"] if t.get("orderId") == sales_id)["qty"], 0.2)
        self.assertAlmostEqual(financial_workflow.reservation_overview(str(self.room_id))[0]["available"], 9.9)

    def test_decimal_quote_invoice_payment_creates_sales_order_without_double_booking(self):
        quote = sales_workflow.create(self.session, {
            "relation_name": "Relatie",
            "lines_json": json.dumps([{"item_id": self.item_id, "item_name": "Testitem", "sku": "T-1", "quantity": 0.1, "unit_price": 10}]),
        })
        converted = sales_workflow.convert(self.session, quote["id"])
        self.assertTrue(converted["invoiced"])
        reservation = financial_workflow.reservation_overview(str(self.room_id))[0]
        self.assertAlmostEqual(reservation["reserved"], 0.1)
        self.assertAlmostEqual(reservation["available"], 9.9)

        payment = sales_workflow.pay_quote_invoice(self.session, quote["id"], 1.21)
        self.assertTrue(payment["converted"])
        order_id = payment["order_id"]
        reservation = financial_workflow.reservation_overview(str(self.room_id))[0]
        self.assertAlmostEqual(reservation["reserved"], 0.1)
        with server.db() as conn:
            line = conn.execute("SELECT quantity::float8 quantity FROM order_lines WHERE order_id=%s", (order_id,)).fetchone()
            invoice = conn.execute("SELECT paid_amount::float8 paid_amount FROM invoice_documents WHERE order_id=%s", (order_id,)).fetchone()
        self.assertAlmostEqual(line["quantity"], 0.1)
        self.assertAlmostEqual(invoice["paid_amount"], 1.21)
        order_management.update_order_status(self.session, "sales", {"order_id": order_id, "status": "completed"})
        self.assertAlmostEqual(self.get_state()["items"][0]["stock"], 9.9)
        self.assertAlmostEqual(financial_workflow.reservation_overview(str(self.room_id))[0]["reserved"], 0)


if __name__ == "__main__":
    unittest.main()
