import io
import json
import smtplib
import ssl
import urllib.parse
import uuid
from datetime import date, datetime
from email.message import EmailMessage

import server
import documents_v2
import documents_v3

_installed=False
_original_update_order_status=None


def initialize():
    with server.db() as conn:
        conn.execute("ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS paid_amount NUMERIC(14,2) NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS last_reminder_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS reminder_count INTEGER NOT NULL DEFAULT 0")
        conn.execute("""CREATE TABLE IF NOT EXISTS invoice_payments(
          id UUID PRIMARY KEY,order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
          stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
          amount NUMERIC(14,2) NOT NULL CHECK(amount>0),note TEXT NOT NULL DEFAULT '',
          created_by UUID REFERENCES users(id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS credit_sequences(
          stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,year INTEGER NOT NULL,last_value INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(stockroom_id,year))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS credit_notes(
          id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
          order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,credit_number TEXT NOT NULL,
          amount NUMERIC(14,2) NOT NULL CHECK(amount>0),reason TEXT NOT NULL DEFAULT '',created_by UUID REFERENCES users(id) ON DELETE SET NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(stockroom_id,credit_number))""")
        conn.commit()


def _totals(stockroom_id, order_id):
    with server.db() as conn:
        rows=conn.execute("SELECT quantity::float8,unit_price::float8 FROM order_lines WHERE order_id=%s",(order_id,)).fetchall()
        inv=conn.execute("SELECT vat_percent::float8 FROM invoice_documents WHERE order_id=%s AND stockroom_id=%s",(order_id,stockroom_id)).fetchone()
    subtotal=sum(float(r['quantity'])*float(r['unit_price']) for r in rows);vat=float((inv or {}).get('vat_percent') or 0);gross=subtotal*(1+vat/100)
    return round(subtotal,2),round(gross,2)


def _status(row,total,credited):
    outstanding=max(0,round(total-float(row.get('paid_amount') or 0)-credited,2))
    if credited>=total and total>0:return 'credited',outstanding
    if outstanding<=0:return 'paid',0
    if float(row.get('paid_amount') or 0)>0 or credited>0:return 'partial',outstanding
    if row.get('due_date') and row['due_date']<date.today():return 'overdue',outstanding
    if row.get('sent_at'):return 'sent',outstanding
    return 'draft',outstanding


def list_invoices(stockroom_id):
    with server.db() as conn:
        rows=conn.execute("""SELECT i.order_id::text,i.invoice_number,i.invoice_date,i.due_date,i.vat_percent::float8,i.sent_at,
          i.paid_amount::float8,i.paid_at,i.last_reminder_at,i.reminder_count,o.order_number,o.relation_name,o.relation_id::text
          FROM invoice_documents i JOIN orders o ON o.id=i.order_id WHERE i.stockroom_id=%s ORDER BY i.invoice_date DESC,i.created_at DESC""",(stockroom_id,)).fetchall()
        credits=conn.execute("SELECT order_id::text,SUM(amount)::float8 amount FROM credit_notes WHERE stockroom_id=%s GROUP BY order_id",(stockroom_id,)).fetchall()
    cmap={r['order_id']:float(r['amount'] or 0) for r in credits};out=[]
    for r in rows:
        _,total=_totals(stockroom_id,r['order_id']);credited=cmap.get(r['order_id'],0);status,outstanding=_status(r,total,credited);d=dict(r);d.update(total=total,credited=credited,status=status,outstanding=outstanding);out.append(d)
    return out


def record_payment(session,order_id,amount,note=''):
    amount=float(amount)
    if amount<=0:raise ValueError('Bedrag moet groter dan 0 zijn.')
    documents_v3.ensure_invoice(session['stockroom_id'],order_id)
    with server.db() as conn:
        row=conn.execute("SELECT paid_amount::float8 FROM invoice_documents WHERE order_id=%s AND stockroom_id=%s FOR UPDATE",(order_id,session['stockroom_id'])).fetchone()
        if not row:raise PermissionError('Factuur niet gevonden.')
        _,total=_totals(session['stockroom_id'],order_id)
        credited=conn.execute("SELECT COALESCE(SUM(amount),0)::float8 amount FROM credit_notes WHERE order_id=%s AND stockroom_id=%s",(order_id,session['stockroom_id'])).fetchone()['amount']
        max_open=max(0,total-float(row['paid_amount'] or 0)-float(credited or 0))
        if amount>max_open+0.01:raise ValueError('Betaling is hoger dan het openstaande bedrag.')
        conn.execute("INSERT INTO invoice_payments(id,order_id,stockroom_id,amount,note,created_by) VALUES(%s,%s,%s,%s,%s,%s)",(str(uuid.uuid4()),order_id,session['stockroom_id'],amount,(note or '')[:500],session['user_id']))
        conn.execute("UPDATE invoice_documents SET paid_amount=paid_amount+%s,paid_at=CASE WHEN paid_amount+%s>=%s THEN NOW() ELSE paid_at END WHERE order_id=%s AND stockroom_id=%s",(amount,amount,total-float(credited or 0),order_id,session['stockroom_id']));conn.commit()
    return {'saved':True}


def create_credit(session,order_id,amount,reason=''):
    amount=float(amount)
    if amount<=0:raise ValueError('Creditbedrag moet groter dan 0 zijn.')
    inv=documents_v3.ensure_invoice(session['stockroom_id'],order_id);_,total=_totals(session['stockroom_id'],order_id)
    with server.db() as conn:
        existing=conn.execute("SELECT COALESCE(SUM(amount),0)::float8 amount FROM credit_notes WHERE order_id=%s AND stockroom_id=%s",(order_id,session['stockroom_id'])).fetchone()['amount']
        if float(existing or 0)+amount>total+0.01:raise ValueError('Totale credit mag niet hoger zijn dan het factuurbedrag.')
        year=date.today().year;seq=conn.execute("""INSERT INTO credit_sequences(stockroom_id,year,last_value) VALUES(%s,%s,1)
          ON CONFLICT(stockroom_id,year) DO UPDATE SET last_value=credit_sequences.last_value+1 RETURNING last_value""",(session['stockroom_id'],year)).fetchone()
        number=f"CR-{year}-{int(seq['last_value']):06d}";cid=str(uuid.uuid4())
        conn.execute("INSERT INTO credit_notes(id,stockroom_id,order_id,credit_number,amount,reason,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s)",(cid,session['stockroom_id'],order_id,number,amount,(reason or '')[:1000],session['user_id']));conn.commit()
    return {'created':True,'id':cid,'credit_number':number,'invoice_number':inv['invoice_number']}


def credit_pdf(stockroom_id,credit_id):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    with server.db() as conn:
        cnote=conn.execute("""SELECT c.*,i.invoice_number,o.order_number,o.relation_name,b.company_name,s.name stockroom_name,b.vat_number,b.chamber_number
          FROM credit_notes c JOIN invoice_documents i ON i.order_id=c.order_id JOIN orders o ON o.id=c.order_id JOIN stockrooms s ON s.id=c.stockroom_id LEFT JOIN billing_accounts b ON b.stockroom_id=c.stockroom_id
          WHERE c.id=%s AND c.stockroom_id=%s""",(credit_id,stockroom_id)).fetchone()
    if not cnote:raise PermissionError('Creditnota niet gevonden.')
    buf=io.BytesIO();c=canvas.Canvas(buf,pagesize=A4);w,h=A4;name=(cnote.get('company_name') or cnote.get('stockroom_name') or 'Stockroom')
    c.setFillColor(colors.HexColor('#111827'));c.rect(0,h-36*mm,w,36*mm,stroke=0,fill=1);c.setFillColor(colors.white);c.setFont('Helvetica-Bold',18);c.drawString(18*mm,h-17*mm,name[:55]);c.setFont('Helvetica-Bold',20);c.drawRightString(w-18*mm,h-17*mm,'CREDITNOTA')
    c.setFillColor(colors.black);c.setFont('Helvetica-Bold',10);y=h-55*mm
    for label,val in [('Creditnummer',cnote['credit_number']),('Factuurnummer',cnote['invoice_number']),('Ordernummer',cnote['order_number'] or '—'),('Relatie',cnote['relation_name'] or '—'),('Datum',cnote['created_at'].strftime('%d-%m-%Y'))]:
        c.drawString(18*mm,y,label);c.setFont('Helvetica',10);c.drawString(65*mm,y,str(val));c.setFont('Helvetica-Bold',10);y-=7*mm
    y-=8*mm;c.setFont('Helvetica-Bold',11);c.drawString(18*mm,y,'Reden');c.setFont('Helvetica',10);c.drawString(18*mm,y-7*mm,(cnote['reason'] or 'Correctie')[:90]);y-=25*mm
    c.setFont('Helvetica-Bold',14);c.drawRightString(w-18*mm,y,f"Creditbedrag: € {float(cnote['amount']):.2f}")
    c.setFont('Helvetica',7);c.drawString(18*mm,14*mm,'Creditnota gekoppeld aan oorspronkelijke factuur. Voorraad wordt hierdoor niet gewijzigd.')
    c.save();return buf.getvalue(),f"creditnota-{cnote['credit_number']}.pdf"


def send_reminder(session,order_id):
    rows=[x for x in list_invoices(session['stockroom_id']) if x['order_id']==str(order_id)]
    if not rows:raise PermissionError('Factuur niet gevonden.')
    inv=rows[0]
    if inv['outstanding']<=0:raise ValueError('Deze factuur heeft geen openstaand bedrag.')
    with server.db() as conn:
        rel=conn.execute("SELECT c.email,c.name FROM orders o LEFT JOIN customers c ON c.id=o.relation_id AND c.stockroom_id=o.stockroom_id WHERE o.id=%s AND o.stockroom_id=%s",(order_id,session['stockroom_id'])).fetchone() or {}
        cfg=conn.execute("SELECT company_name FROM billing_accounts WHERE stockroom_id=%s",(session['stockroom_id'],)).fetchone() or {}
    recipient=(rel.get('email') or '').strip()
    if '@' not in recipient:raise ValueError('Geen geldig klant-e-mailadres ingesteld.')
    data,name=documents_v2.invoice_pdf(session['stockroom_id'],order_id);company=(cfg.get('company_name') or 'Stockroom').strip() or 'Stockroom'
    msg=EmailMessage();msg['From']=server.SMTP_FROM;msg['To']=recipient;msg['Subject']=f"Betalingsherinnering {inv['invoice_number']} - {company}";msg.set_content(f"Beste {rel.get('name') or 'klant'},\n\nVolgens onze administratie staat factuur {inv['invoice_number']} nog open voor € {inv['outstanding']:.2f}. De vervaldatum was {inv['due_date']}.\n\nWilt u deze betaling controleren?\n\nMet vriendelijke groet,\n{company}");msg.add_attachment(data,maintype='application',subtype='pdf',filename=name)
    if not server.SMTP_HOST:raise ValueError('SMTP is niet geconfigureerd.')
    context=ssl.create_default_context()
    with smtplib.SMTP(server.SMTP_HOST,server.SMTP_PORT,timeout=20) as smtp:
        smtp.ehlo();smtp.starttls(context=context);smtp.ehlo()
        if server.SMTP_USERNAME:smtp.login(server.SMTP_USERNAME,server.SMTP_PASSWORD)
        smtp.send_message(msg)
    with server.db() as conn:conn.execute("UPDATE invoice_documents SET last_reminder_at=NOW(),reminder_count=reminder_count+1,sent_at=COALESCE(sent_at,NOW()) WHERE order_id=%s AND stockroom_id=%s",(order_id,session['stockroom_id']));conn.commit()
    return {'sent':True,'recipient':recipient}


def mark_sent(stockroom_id,order_id):
    documents_v3.ensure_invoice(stockroom_id,order_id)
    with server.db() as conn:conn.execute("UPDATE invoice_documents SET sent_at=COALESCE(sent_at,NOW()) WHERE order_id=%s AND stockroom_id=%s",(order_id,stockroom_id));conn.commit()


def _wrap_order_status():
    global _original_update_order_status
    import order_management
    if _original_update_order_status:return
    _original_update_order_status=order_management.update_order_status
    def wrapped(session,order_type,values):
        result=_original_update_order_status(session,order_type,values)
        if order_type=='sales' and values.get('status') in ('completed','paid'):
            documents_v3.ensure_invoice(session['stockroom_id'],values.get('order_id'))
            if values.get('status')=='paid':
                _,total=_totals(session['stockroom_id'],values.get('order_id'))
                with server.db() as conn:conn.execute("UPDATE invoice_documents SET paid_amount=GREATEST(paid_amount,%s),paid_at=COALESCE(paid_at,NOW()),sent_at=COALESCE(sent_at,NOW()) WHERE order_id=%s AND stockroom_id=%s",(total,values.get('order_id'),session['stockroom_id']));conn.commit()
        return result
    order_management.update_order_status=wrapped


def install():
    global _installed
    if _installed:return
    _installed=True;initialize();_wrap_order_status()
    old_get=server.StockroomHandler.do_GET;old_post=server.StockroomHandler.do_POST
    def do_GET(self):
        p=urllib.parse.urlparse(self.path)
        if p.path=='/api/finance/invoices':
            s=self.require_session(api=True)
            if s:self.send_json(200,{'invoices':list_invoices(s['stockroom_id'])})
            return
        if p.path=='/api/finance/credit.pdf':
            s=self.require_session(api=True)
            if not s:return
            cid=urllib.parse.parse_qs(p.query).get('id',[''])[0]
            try:data,name=credit_pdf(s['stockroom_id'],cid);self.send_response(200);self.send_header('Content-Type','application/pdf');self.send_header('Content-Disposition',f'inline; filename="{name}"');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            except PermissionError as e:self.send_json(404,{'error':str(e)})
            return
        return old_get(self)
    def do_POST(self):
        path=urllib.parse.urlparse(self.path).path
        if path in ('/api/finance/payment','/api/finance/credit','/api/finance/reminder'):
            if not self.enforce_origin():return
            s=self.require_session(api=True)
            if not s:return
            if s.get('role') not in ('owner','admin','member','seller'):
                self.send_json(403,{'error':'Geen rechten.'});return
            f=self.form_data() or {};v={k:(x[0] if isinstance(x,list) and x else x) for k,x in f.items()}
            try:
                if path.endswith('/payment'):res=record_payment(s,v.get('order_id'),v.get('amount'),v.get('note') or '')
                elif path.endswith('/credit'):res=create_credit(s,v.get('order_id'),v.get('amount'),v.get('reason') or '')
                else:res=send_reminder(s,v.get('order_id'))
                self.send_json(200,res)
            except PermissionError as e:self.send_json(403,{'error':str(e)})
            except ValueError as e:self.send_json(400,{'error':str(e)})
            return
        return old_post(self)
    server.StockroomHandler.do_GET=do_GET;server.StockroomHandler.do_POST=do_POST
