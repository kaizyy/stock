import json, smtplib, ssl, urllib.parse, uuid
from datetime import date, datetime
from email.message import EmailMessage

import server, documents_v2, financial_workflow

_installed = False
_original_create_order = None


def initialize():
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS debtor_promises(
          id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
          order_key TEXT NOT NULL,amount NUMERIC(14,2) NOT NULL CHECK(amount>0),promise_date DATE NOT NULL,
          note TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'open',created_by UUID REFERENCES users(id) ON DELETE SET NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),completed_at TIMESTAMPTZ)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS debtor_blocks(
          stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
          customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,reason TEXT NOT NULL DEFAULT '',
          blocked_by UUID REFERENCES users(id) ON DELETE SET NULL,blocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY(stockroom_id,customer_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS debtor_dunning_history(
          id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
          order_key TEXT NOT NULL,level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 3),recipient TEXT NOT NULL,
          subject TEXT NOT NULL,created_by UUID REFERENCES users(id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_debtor_promises_room_order ON debtor_promises(stockroom_id,order_key,status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_debtor_history_room_order ON debtor_dunning_history(stockroom_id,order_key,created_at DESC)")
        conn.commit()


def _days_overdue(value):
    if not value:return 0
    return max(0,(date.today()-value).days)


def overview(stockroom_id):
    invoices=[x for x in financial_workflow.list_invoices(stockroom_id) if float(x.get('outstanding') or 0)>0]
    normal_ids=[x['order_id'] for x in invoices if not str(x['order_id']).startswith('quote:')]
    relation_by_order={}; blocks={}; promises={}; histories={}
    with server.db() as conn:
        if normal_ids:
            for row in conn.execute("SELECT id::text,relation_id::text FROM orders WHERE stockroom_id=%s AND id=ANY(%s::uuid[])",(stockroom_id,normal_ids)).fetchall():relation_by_order[row['id']]=row['relation_id']
        for row in conn.execute("SELECT customer_id::text,reason,blocked_at FROM debtor_blocks WHERE stockroom_id=%s",(stockroom_id,)).fetchall():blocks[row['customer_id']]=dict(row)
        for row in conn.execute("SELECT id::text,order_key,amount::float8,promise_date,note,status,created_at,completed_at FROM debtor_promises WHERE stockroom_id=%s ORDER BY promise_date,created_at",(stockroom_id,)).fetchall():promises.setdefault(row['order_key'],[]).append(dict(row))
        for row in conn.execute("SELECT id::text,order_key,level,recipient,subject,created_at FROM debtor_dunning_history WHERE stockroom_id=%s ORDER BY created_at DESC",(stockroom_id,)).fetchall():histories.setdefault(row['order_key'],[]).append(dict(row))
    buckets={k:{'amount':0.0,'count':0} for k in ('not_due','0_30','31_60','61_90','90_plus')}
    rows=[]
    for inv in invoices:
        days=_days_overdue(inv.get('due_date'))
        bucket='not_due' if not inv.get('due_date') or inv['due_date']>=date.today() else '0_30' if days<=30 else '31_60' if days<=60 else '61_90' if days<=90 else '90_plus'
        amount=float(inv.get('outstanding') or 0);buckets[bucket]['amount']=round(buckets[bucket]['amount']+amount,2);buckets[bucket]['count']+=1
        relation_id=relation_by_order.get(inv['order_id']);active=[p for p in promises.get(inv['order_id'],[]) if p['status']=='open']
        row=dict(inv);row.update(days_overdue=days,aging_bucket=bucket,relation_id=relation_id,blocked=bool(relation_id and relation_id in blocks),block=blocks.get(relation_id),promises=promises.get(inv['order_id'],[]),active_promise=active[0] if active else None,dunning_history=histories.get(inv['order_id'],[]))
        rows.append(row)
    return {'buckets':buckets,'invoices':rows,'summary':{'outstanding':round(sum(float(x['outstanding']) for x in rows),2),'overdue':round(sum(float(x['outstanding']) for x in rows if x['days_overdue']>0),2),'overdueCount':sum(1 for x in rows if x['days_overdue']>0),'brokenPromises':sum(1 for x in rows for p in x['promises'] if p['status']=='open' and p['promise_date']<date.today())}}


def save_promise(session, values):
    order_key=(values.get('order_id') or '').strip();promise_date=(values.get('promise_date') or '').strip();amount=float(values.get('amount') or 0)
    if not order_key or not promise_date or amount<=0:raise ValueError('Vul een geldige datum en een bedrag groter dan nul in.')
    invoice=next((x for x in financial_workflow.list_invoices(session['stockroom_id']) if x['order_id']==order_key),None)
    if not invoice or amount>float(invoice.get('outstanding') or 0)+0.01:raise ValueError('De betaalafspraak is hoger dan het openstaande factuurbedrag.')
    try:datetime.strptime(promise_date,'%Y-%m-%d')
    except ValueError:raise ValueError('De datum van de betaalafspraak is ongeldig.')
    promise_id=str(uuid.uuid4())
    with server.db() as conn:
        conn.execute("UPDATE debtor_promises SET status='replaced',completed_at=NOW() WHERE stockroom_id=%s AND order_key=%s AND status='open'",(session['stockroom_id'],order_key))
        conn.execute("INSERT INTO debtor_promises(id,stockroom_id,order_key,amount,promise_date,note,created_by) VALUES(%s,%s,%s,%s,%s::date,%s,%s)",(promise_id,session['stockroom_id'],order_key,amount,promise_date,(values.get('note') or '')[:500],session['user_id']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'debtor.promise_created',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':promise_id,'order_id':order_key,'amount':amount,'date':promise_date})));conn.commit()
    return {'saved':True,'id':promise_id}


def complete_promise(session, values):
    promise_id=(values.get('id') or '').strip();status=(values.get('status') or 'completed').strip()
    if status not in ('completed','cancelled'):raise ValueError('Ongeldige status.')
    with server.db() as conn:
        row=conn.execute("UPDATE debtor_promises SET status=%s,completed_at=NOW() WHERE id=%s AND stockroom_id=%s AND status='open' RETURNING order_key",(status,promise_id,session['stockroom_id'])).fetchone()
        if not row:raise PermissionError('Betaalafspraak niet gevonden of al afgehandeld.')
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'debtor.promise_updated',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':promise_id,'status':status})));conn.commit()
    return {'updated':True}


def set_block(session, values):
    customer_id=(values.get('customer_id') or '').strip();blocked=str(values.get('blocked') or '').lower() in ('1','true','yes')
    if not customer_id:raise ValueError('Klant ontbreekt.')
    with server.db() as conn:
        customer=conn.execute("SELECT name FROM customers WHERE id=%s AND stockroom_id=%s",(customer_id,session['stockroom_id'])).fetchone()
        if not customer:raise PermissionError('Klant niet gevonden.')
        if blocked:conn.execute("INSERT INTO debtor_blocks(stockroom_id,customer_id,reason,blocked_by) VALUES(%s,%s,%s,%s) ON CONFLICT(stockroom_id,customer_id) DO UPDATE SET reason=EXCLUDED.reason,blocked_by=EXCLUDED.blocked_by,blocked_at=NOW()",(session['stockroom_id'],customer_id,(values.get('reason') or 'Betalingsachterstand')[:500],session['user_id']))
        else:conn.execute("DELETE FROM debtor_blocks WHERE stockroom_id=%s AND customer_id=%s",(session['stockroom_id'],customer_id))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'debtor.customer_blocked',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'customer_id':customer_id,'customer':customer['name'],'blocked':blocked})));conn.commit()
    return {'saved':True,'blocked':blocked}


def send_dunning(session, values):
    order_id=(values.get('order_id') or '').strip();level=int(values.get('level') or 1)
    if level not in (1,2,3) or order_id.startswith('quote:'):raise ValueError('Deze factuur kan niet via debiteurenbeheer worden gemaand.')
    inv=next((x for x in financial_workflow.list_invoices(session['stockroom_id']) if x['order_id']==order_id),None)
    if not inv or float(inv.get('outstanding') or 0)<=0:raise ValueError('Deze factuur heeft geen openstaand bedrag.')
    recipient=(inv.get('relation_email') or '').strip()
    if '@' not in recipient:raise ValueError('Geen geldig klant-e-mailadres ingesteld.')
    titles={1:'Betalingsherinnering',2:'Tweede betalingsherinnering',3:'Laatste aanmaning'};title=titles[level]
    with server.db() as conn:
        cfg=conn.execute("SELECT company_name FROM billing_accounts WHERE stockroom_id=%s",(session['stockroom_id'],)).fetchone() or {}
    company=(cfg.get('company_name') or 'Stockroom').strip() or 'Stockroom';subject=f"{title} {inv['invoice_number']} - {company}"
    intro={1:'Volgens onze administratie staat onderstaande factuur nog open.',2:'Ondanks onze eerdere herinnering staat onderstaande factuur nog open.',3:'Dit is onze laatste aanmaning voor onderstaande openstaande factuur.'}[level]
    custom=(values.get('message') or '').strip();body=custom or f"Beste {inv.get('relation_name') or 'klant'},\n\n{intro}\nFactuur: {inv['invoice_number']}\nOpenstaand: € {float(inv['outstanding']):.2f}\nVervaldatum: {inv['due_date'].strftime('%d-%m-%Y')}\n\nMet vriendelijke groet,\n{company}"
    data,name=documents_v2.invoice_pdf(session['stockroom_id'],order_id);msg=EmailMessage();msg['From']=server.SMTP_FROM;msg['To']=recipient;msg['Subject']=subject;msg.set_content(body);msg.add_attachment(data,maintype='application',subtype='pdf',filename=name)
    if not server.SMTP_HOST:raise ValueError('SMTP is niet geconfigureerd.')
    with smtplib.SMTP(server.SMTP_HOST,server.SMTP_PORT,timeout=20) as smtp:
        smtp.ehlo();smtp.starttls(context=ssl.create_default_context());smtp.ehlo()
        if server.SMTP_USERNAME:smtp.login(server.SMTP_USERNAME,server.SMTP_PASSWORD)
        smtp.send_message(msg)
    with server.db() as conn:
        conn.execute("INSERT INTO debtor_dunning_history(id,stockroom_id,order_key,level,recipient,subject,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s)",(str(uuid.uuid4()),session['stockroom_id'],order_id,level,recipient,subject,session['user_id']))
        conn.execute("UPDATE invoice_documents SET last_reminder_at=NOW(),reminder_count=reminder_count+1,sent_at=COALESCE(sent_at,NOW()) WHERE order_id=%s AND stockroom_id=%s",(order_id,session['stockroom_id']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'debtor.dunning_sent',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'order_id':order_id,'level':level,'recipient':recipient})));conn.commit()
    return {'sent':True,'recipient':recipient,'level':level}


def _protect_blocked_customers():
    global _original_create_order
    import order_management
    if _original_create_order:return
    _original_create_order=order_management.create_order
    def wrapped(session,values):
        customer_id=(values.get('relation_id') or '').strip()
        if values.get('order_type')=='sales' and customer_id:
            with server.db() as conn:
                blocked=conn.execute("SELECT reason FROM debtor_blocks WHERE stockroom_id=%s AND customer_id=%s",(session['stockroom_id'],customer_id)).fetchone()
            if blocked:raise ValueError(f"Deze klant is geblokkeerd voor nieuwe verkooporders: {blocked['reason'] or 'betalingsachterstand'}")
        return _original_create_order(session,values)
    order_management.create_order=wrapped


def install():
    global _installed
    if _installed:return
    _installed=True;initialize();_protect_blocked_customers();old_get=server.StockroomHandler.do_GET;old_post=server.StockroomHandler.do_POST
    def do_GET(self):
        if urllib.parse.urlparse(self.path).path=='/api/debtors':
            s=self.require_session(api=True)
            if s:self.send_json(200,overview(s['stockroom_id']))
            return
        return old_get(self)
    def do_POST(self):
        path=urllib.parse.urlparse(self.path).path
        if path in ('/api/debtors/promise','/api/debtors/promise/status','/api/debtors/block','/api/debtors/dunning'):
            if not self.enforce_origin():return
            s=self.require_session(api=True)
            if not s:return
            if s.get('role') not in ('owner','admin','member','seller'):self.send_json(403,{'error':'Geen rechten voor debiteurenbeheer.'});return
            if path=='/api/debtors/block' and s.get('role') not in ('owner','admin'):self.send_json(403,{'error':'Alleen een eigenaar of beheerder kan klanten blokkeren.'});return
            f=self.form_data() or {};v={k:(x[0] if isinstance(x,list) and x else x) for k,x in f.items()}
            try:
                result=save_promise(s,v) if path.endswith('/promise') else complete_promise(s,v) if path.endswith('/status') else set_block(s,v) if path.endswith('/block') else send_dunning(s,v)
                self.send_json(200,result)
            except PermissionError as e:self.send_json(403,{'error':str(e)})
            except (ValueError,TypeError) as e:self.send_json(400,{'error':str(e)})
            return
        return old_post(self)
    server.StockroomHandler.do_GET=do_GET;server.StockroomHandler.do_POST=do_POST
