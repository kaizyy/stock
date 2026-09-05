import io,json,smtplib,ssl,urllib.parse,uuid
from datetime import date,timedelta
from email.message import EmailMessage
import server,order_management,business_tools,documents_v3

_installed=False
def initialize():
    with server.db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS quotes(id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,quote_number TEXT NOT NULL,relation_id UUID,relation_name TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'draft',reference TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',quote_date DATE NOT NULL DEFAULT CURRENT_DATE,valid_until DATE NOT NULL,created_by UUID REFERENCES users(id) ON DELETE SET NULL,converted_order_id UUID REFERENCES orders(id) ON DELETE SET NULL,sent_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(stockroom_id,quote_number))""")
        for sql in ("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS invoice_number TEXT","ALTER TABLE quotes ADD COLUMN IF NOT EXISTS invoice_date DATE","ALTER TABLE quotes ADD COLUMN IF NOT EXISTS due_date DATE","ALTER TABLE quotes ADD COLUMN IF NOT EXISTS invoice_vat_percent NUMERIC(6,3) NOT NULL DEFAULT 21","ALTER TABLE quotes ADD COLUMN IF NOT EXISTS invoice_paid_amount NUMERIC(14,2) NOT NULL DEFAULT 0","ALTER TABLE quotes ADD COLUMN IF NOT EXISTS invoice_paid_at TIMESTAMPTZ"):
            c.execute(sql)
        c.execute("""CREATE TABLE IF NOT EXISTS quote_lines(id UUID PRIMARY KEY,quote_id UUID NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,item_id TEXT NOT NULL,item_name TEXT NOT NULL,sku TEXT NOT NULL DEFAULT '',quantity NUMERIC(14,3) NOT NULL CHECK(quantity>0),unit_price NUMERIC(14,4) NOT NULL CHECK(unit_price>=0))""");c.commit()
        c.execute("CREATE TABLE IF NOT EXISTS quote_sequences(stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,year INTEGER NOT NULL,last_value INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(stockroom_id,year))");c.commit()
def rows(room):
    with server.db() as c:
        out=c.execute("""SELECT q.id::text id,q.stockroom_id::text stockroom_id,q.quote_number,q.relation_id::text relation_id,
          q.relation_name,q.status,q.reference,q.notes,q.quote_date,q.valid_until,q.created_by::text created_by,
          q.converted_order_id::text converted_order_id,q.sent_at,q.created_at,q.updated_at,q.invoice_number,
          q.invoice_date,q.due_date,q.invoice_vat_percent::float8,q.invoice_paid_amount::float8,q.invoice_paid_at
          FROM quotes q WHERE q.stockroom_id=%s ORDER BY q.created_at DESC""",(room,)).fetchall()
        for q in out:q['lines']=c.execute("SELECT item_id,item_name,sku,quantity::float8,unit_price::float8 FROM quote_lines WHERE quote_id=%s",(q['id'],)).fetchall();q['status']='expired' if q['status'] in ('draft','sent') and q['valid_until']<date.today() else q['status']
    return out
def create(session,v):
    lines=order_management._parse_lines(v.get('lines_json'));rid=(v.get('relation_id') or '').strip() or None;name=(v.get('relation_name') or '').strip();qid=str(uuid.uuid4())
    with server.db() as c:
        if rid:
            rel=c.execute("SELECT name FROM customers WHERE id=%s AND stockroom_id=%s",(rid,session['stockroom_id'])).fetchone()
            if not rel:raise PermissionError('Klant niet gevonden.')
            name=rel['name']
        year=date.today().year;seq=c.execute("INSERT INTO quote_sequences(stockroom_id,year,last_value) VALUES(%s,%s,1) ON CONFLICT(stockroom_id,year) DO UPDATE SET last_value=quote_sequences.last_value+1 RETURNING last_value",(session['stockroom_id'],year)).fetchone()['last_value'];number=f'OFF-{year}-{int(seq):06d}'
        c.execute("INSERT INTO quotes(id,stockroom_id,quote_number,relation_id,relation_name,reference,notes,valid_until,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",(qid,session['stockroom_id'],number,rid,name,(v.get('reference') or '')[:120],(v.get('notes') or '')[:5000],date.today()+timedelta(days=30),session['user_id']))
        for i,n,s,q,p in lines:c.execute("INSERT INTO quote_lines(id,quote_id,item_id,item_name,sku,quantity,unit_price) VALUES(%s,%s,%s,%s,%s,%s,%s)",(str(uuid.uuid4()),qid,i,n,s,q,p))
        c.commit()
    return {'created':True,'id':qid,'quote_number':number}
def delete_quote(session,qid):
    with server.db() as c:
        q=c.execute("SELECT quote_number,converted_order_id FROM quotes WHERE id=%s AND stockroom_id=%s FOR UPDATE",(qid,session['stockroom_id'])).fetchone()
        if not q:raise PermissionError('Offerte niet gevonden.')
        c.execute("DELETE FROM quote_reservations WHERE quote_id=%s AND stockroom_id=%s",(qid,session['stockroom_id']))
        c.execute("DELETE FROM quotes WHERE id=%s AND stockroom_id=%s",(qid,session['stockroom_id']))
        c.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'quote.deleted',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':qid,'quote_number':q['quote_number'],'converted_order_preserved':bool(q['converted_order_id'])})))
        c.commit()
    return {'deleted':True,'id':qid}
def convert(session,qid):
    with server.db() as c:
        q=c.execute("SELECT * FROM quotes WHERE id=%s AND stockroom_id=%s FOR UPDATE",(qid,session['stockroom_id'])).fetchone()
        if not q:raise PermissionError('Offerte niet gevonden.')
        if q['converted_order_id']:return {'converted':True,'order_id':str(q['converted_order_id']),'invoice_number':q.get('invoice_number')}
        if q.get('invoice_number'):return {'invoiced':True,'invoice_number':q['invoice_number']}
        lines=c.execute("SELECT item_id,item_name,sku,quantity::float8 quantity,unit_price::float8 unit_price FROM quote_lines WHERE quote_id=%s",(qid,)).fetchall()
        room=c.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE",(session['stockroom_id'],)).fetchone();state=(room or {}).get('state') or {'items':[]}
        for line in lines:
            item=next((x for x in state.get('items',[]) if str(x.get('id'))==str(line['item_id'])),None);stock=float((item or {}).get('stock') or 0)
            reserved=c.execute("SELECT COALESCE((SELECT SUM(quantity) FROM inventory_reservations WHERE stockroom_id=%s AND item_id=%s),0)::float8+COALESCE((SELECT SUM(quantity) FROM quote_reservations WHERE stockroom_id=%s AND item_id=%s AND quote_id<>%s),0)::float8 quantity",(session['stockroom_id'],line['item_id'],session['stockroom_id'],line['item_id'],qid)).fetchone()['quantity']
            if not item or stock-float(reserved or 0)<float(line['quantity']):raise ValueError(f"Onvoldoende vrije voorraad voor {line['item_name']}: {max(0,stock-float(reserved or 0)):g} beschikbaar.")
            c.execute("INSERT INTO quote_reservations(quote_id,stockroom_id,item_id,quantity) VALUES(%s,%s,%s,%s) ON CONFLICT(quote_id,item_id) DO UPDATE SET quantity=EXCLUDED.quantity",(qid,session['stockroom_id'],line['item_id'],line['quantity']))
        cfg=c.execute("SELECT payment_term_days,default_vat_percent::float8 FROM billing_accounts WHERE stockroom_id=%s",(session['stockroom_id'],)).fetchone() or {'payment_term_days':14,'default_vat_percent':21};year=date.today().year
        seq=c.execute("INSERT INTO invoice_sequences(stockroom_id,year,last_value) VALUES(%s,%s,1) ON CONFLICT(stockroom_id,year) DO UPDATE SET last_value=invoice_sequences.last_value+1 RETURNING last_value",(session['stockroom_id'],year)).fetchone();number=f"INV-{year}-{int(seq['last_value']):06d}";today=date.today();due=today+timedelta(days=int(cfg['payment_term_days'] or 0))
        c.execute("UPDATE quotes SET status='invoiced',invoice_number=%s,invoice_date=%s,due_date=%s,invoice_vat_percent=%s,updated_at=NOW() WHERE id=%s",(number,today,due,float(cfg['default_vat_percent'] or 0),qid));c.commit()
    return {'invoiced':True,'invoice_number':number}

def quote_invoices(room):
    with server.db() as c:
        data=c.execute("""SELECT q.id::text,invoice_number,invoice_date,due_date,invoice_vat_percent::float8 vat_percent,invoice_paid_amount::float8 paid_amount,invoice_paid_at paid_at,sent_at,q.relation_name,quote_number,COALESCE(cu.email,'') relation_email,COALESCE(SUM(l.quantity*l.unit_price),0)::float8 subtotal FROM quotes q JOIN quote_lines l ON l.quote_id=q.id LEFT JOIN customers cu ON cu.id=q.relation_id AND cu.stockroom_id=q.stockroom_id WHERE q.stockroom_id=%s AND q.invoice_number IS NOT NULL AND q.converted_order_id IS NULL GROUP BY q.id,cu.email""",(room,)).fetchall()
    out=[]
    for r in data:
        d=dict(r);d['order_id']='quote:'+d.pop('id');d['order_number']=d.pop('quote_number');d['total']=round(float(d.pop('subtotal'))*(1+float(d['vat_percent'])/100),2);d['credited']=0;d['outstanding']=max(0,round(d['total']-float(d['paid_amount'] or 0),2));d['reminder_count']=0;d['source']='quote';d['status']='paid' if d['outstanding']<=0 else 'partial' if d['paid_amount'] else 'overdue' if d['due_date']<date.today() else 'sent' if d['sent_at'] else 'draft';out.append(d)
    return out

def pay_quote_invoice(session,qid,amount,note=''):
    amount=float(amount)
    with server.db() as c:
        q=c.execute("SELECT * FROM quotes WHERE id=%s AND stockroom_id=%s AND invoice_number IS NOT NULL AND converted_order_id IS NULL FOR UPDATE",(qid,session['stockroom_id'])).fetchone()
        if not q:raise PermissionError('Factuur niet gevonden.')
        subtotal=float(c.execute("SELECT COALESCE(SUM(quantity*unit_price),0)::float8 total FROM quote_lines WHERE quote_id=%s",(qid,)).fetchone()['total']);total=round(subtotal*(1+float(q['invoice_vat_percent'])/100),2);open_amount=max(0,total-float(q['invoice_paid_amount'] or 0))
        if amount<=0:raise ValueError('Bedrag moet groter dan 0 zijn.')
        if amount>open_amount+0.01:raise ValueError('Betaling is hoger dan het openstaande bedrag.')
        paid=float(q['invoice_paid_amount'] or 0)+amount
        c.execute("UPDATE quotes SET invoice_paid_amount=%s,invoice_paid_at=CASE WHEN %s>=%s THEN NOW() ELSE invoice_paid_at END,status=CASE WHEN %s>=%s THEN 'paid' ELSE 'partial' END,updated_at=NOW() WHERE id=%s",(paid,paid,total,paid,total,qid));c.commit()
    if paid+0.01<total:return {'saved':True,'converted':False}
    lines=[]
    with server.db() as c:
        q=c.execute("SELECT * FROM quotes WHERE id=%s AND stockroom_id=%s FOR UPDATE",(qid,session['stockroom_id'])).fetchone();lines=c.execute("SELECT item_id,item_name,sku,quantity::float8 quantity,unit_price::float8 unit_price FROM quote_lines WHERE quote_id=%s",(qid,)).fetchall();c.execute("DELETE FROM quote_reservations WHERE quote_id=%s",(qid,));c.commit()
    try:
        oid=order_management.create_order(session,{'order_type':'sales','status':'draft','relation_id':str(q['relation_id'] or ''),'relation_name':q['relation_name'],'reference':q['quote_number'],'notes':q['notes'],'lines_json':json.dumps(lines)});business_tools.assign_order_number(oid,session['stockroom_id'],'sales')
        with server.db() as c:
            c.execute("INSERT INTO invoice_documents(order_id,stockroom_id,invoice_number,invoice_date,due_date,vat_percent,paid_amount,paid_at,sent_at) VALUES(%s,%s,%s,%s,%s,%s,%s,NOW(),%s)",(oid,session['stockroom_id'],q['invoice_number'],q['invoice_date'],q['due_date'],q['invoice_vat_percent'],total,q['sent_at']))
            c.execute("UPDATE quotes SET status='converted',converted_order_id=%s,updated_at=NOW() WHERE id=%s",(oid,qid));c.commit()
        return {'saved':True,'converted':True,'order_id':oid}
    except Exception:
        with server.db() as c:
            for l in lines:c.execute("INSERT INTO quote_reservations(quote_id,stockroom_id,item_id,quantity) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",(qid,session['stockroom_id'],l['item_id'],l['quantity']))
            c.commit()
        raise
def pdf(room,qid,invoice=False):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    q=next((x for x in rows(room) if str(x['id'])==str(qid)),None)
    if not q:raise PermissionError('Offerte niet gevonden.')
    if invoice and not q.get('invoice_number'):raise ValueError('Factuur is nog niet aangemaakt.')
    b=io.BytesIO();c=canvas.Canvas(b,pagesize=A4);c.setFont('Helvetica-Bold',20);c.drawString(20*mm,280*mm,'FACTUUR' if invoice else 'OFFERTE');c.setFont('Helvetica',10);y=265*mm
    meta=[('Factuurnummer',q['invoice_number']),('Klant',q['relation_name'] or '—'),('Vervaldatum',str(q['due_date']))] if invoice else [('Offertenummer',q['quote_number']),('Klant',q['relation_name'] or '—'),('Geldig tot',str(q['valid_until']))]
    for label,val in meta:c.drawString(20*mm,y,f'{label}: {val}');y-=7*mm
    total=0;y-=5*mm
    for l in q['lines']:amount=float(l['quantity'])*float(l['unit_price']);total+=amount;c.drawString(20*mm,y,f"{float(l['quantity']):g} × {l['item_name']}");c.drawRightString(190*mm,y,f'€ {amount:.2f}');y-=7*mm
    vat=float(q.get('invoice_vat_percent') or 0) if invoice else 0;grand=total*(1+vat/100);c.setFont('Helvetica-Bold',12);c.drawRightString(190*mm,y-5*mm,f'Totaal excl. btw: € {total:.2f}');
    if invoice:c.drawRightString(190*mm,y-12*mm,f'Totaal incl. btw: € {grand:.2f}')
    c.save();return b.getvalue(),f"{'factuur-'+q['invoice_number'] if invoice else 'offerte-'+q['quote_number']}.pdf"
def mail(session,qid):
    with server.db() as c:q=c.execute("SELECT q.*,c.email FROM quotes q LEFT JOIN customers c ON c.id=q.relation_id WHERE q.id=%s AND q.stockroom_id=%s",(qid,session['stockroom_id'])).fetchone()
    if not q or '@' not in (q.get('email') or ''):raise ValueError('Geen geldig klant-e-mailadres ingesteld.')
    data,name=pdf(session['stockroom_id'],qid);m=EmailMessage();m['From']=server.SMTP_FROM;m['To']=q['email'];m['Subject']=f"Offerte {q['quote_number']}";m.set_content('In de bijlage vindt u onze offerte.');m.add_attachment(data,maintype='application',subtype='pdf',filename=name)
    if not server.SMTP_HOST:raise ValueError('SMTP is niet geconfigureerd.')
    with smtplib.SMTP(server.SMTP_HOST,server.SMTP_PORT,timeout=20) as s:s.ehlo();s.starttls(context=ssl.create_default_context());s.ehlo();s.login(server.SMTP_USERNAME,server.SMTP_PASSWORD) if server.SMTP_USERNAME else None;s.send_message(m)
    with server.db() as c:c.execute("UPDATE quotes SET status='sent',sent_at=NOW(),updated_at=NOW() WHERE id=%s",(qid,));c.commit()
    return {'sent':True}
def mail_invoice(session,qid,recipient='',message=''):
    with server.db() as c:q=c.execute("SELECT q.*,cu.email FROM quotes q LEFT JOIN customers cu ON cu.id=q.relation_id AND cu.stockroom_id=q.stockroom_id WHERE q.id=%s AND q.stockroom_id=%s AND q.invoice_number IS NOT NULL",(qid,session['stockroom_id'])).fetchone()
    if not q:raise PermissionError('Factuur niet gevonden.')
    recipient=(recipient or q.get('email') or '').strip()
    if '@' not in recipient:raise ValueError('Geen geldig klant-e-mailadres ingesteld.')
    data,name=pdf(session['stockroom_id'],qid,True);m=EmailMessage();m['From']=server.SMTP_FROM;m['To']=recipient;m['Subject']=f"Factuur {q['invoice_number']}";m.set_content((message or 'In de bijlage vindt u onze factuur.').strip());m.add_attachment(data,maintype='application',subtype='pdf',filename=name)
    if not server.SMTP_HOST:raise ValueError('SMTP is niet geconfigureerd.')
    with smtplib.SMTP(server.SMTP_HOST,server.SMTP_PORT,timeout=20) as s:s.ehlo();s.starttls(context=ssl.create_default_context());s.ehlo();s.login(server.SMTP_USERNAME,server.SMTP_PASSWORD) if server.SMTP_USERNAME else None;s.send_message(m)
    with server.db() as c:c.execute("UPDATE quotes SET sent_at=COALESCE(sent_at,NOW()),updated_at=NOW() WHERE id=%s",(qid,));c.commit()
    return {'sent':True,'recipient':recipient}
def install():
    global _installed
    if _installed:return
    _installed=True;initialize();og=server.StockroomHandler.do_GET;op=server.StockroomHandler.do_POST
    def get(self):
        p=urllib.parse.urlparse(self.path)
        if p.path in ('/api/quotes','/api/quotes/pdf','/api/quotes/invoice.pdf'):
            s=self.require_session(api=True)
            if not s:return
            if p.path=='/api/quotes':self.send_json(200,{'quotes':rows(s['stockroom_id'])})
            else:data,name=pdf(s['stockroom_id'],urllib.parse.parse_qs(p.query).get('id',[''])[0],p.path.endswith('invoice.pdf'));self.send_pdf(data,name)
            return
        return og(self)
    def post(self):
        path=urllib.parse.urlparse(self.path).path
        if path in ('/api/quotes','/api/quotes/convert','/api/quotes/mail','/api/quotes/delete'):
            if not self.enforce_origin():return
            s=self.require_session(api=True)
            if not s:return
            f=self.form_data() or {};v={k:(x[0] if isinstance(x,list) and x else x) for k,x in f.items()}
            try:self.send_json(200,create(s,v) if path=='/api/quotes' else convert(s,v.get('id')) if path.endswith('convert') else delete_quote(s,v.get('id')) if path.endswith('delete') else mail(s,v.get('id')))
            except (ValueError,PermissionError) as e:self.send_json(400,{'error':str(e)})
            return
        return op(self)
    server.StockroomHandler.do_GET=get;server.StockroomHandler.do_POST=post
