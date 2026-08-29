import io, json, smtplib, ssl, urllib.parse, uuid
from datetime import date
from email.message import EmailMessage
import server, documents_v2, documents_v3

_installed=False
_original_update_order_status=None

def initialize():
    with server.db() as conn:
        conn.execute("ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS paid_amount NUMERIC(14,2) NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS last_reminder_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS reminder_count INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS deleted_by UUID REFERENCES users(id) ON DELETE SET NULL")
        conn.execute("CREATE TABLE IF NOT EXISTS invoice_payments(id UUID PRIMARY KEY,order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,amount NUMERIC(14,2) NOT NULL CHECK(amount>0),note TEXT NOT NULL DEFAULT '',created_by UUID REFERENCES users(id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        conn.execute("CREATE TABLE IF NOT EXISTS credit_sequences(stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,year INTEGER NOT NULL,last_value INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(stockroom_id,year))")
        conn.execute("CREATE TABLE IF NOT EXISTS credit_notes(id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,credit_number TEXT NOT NULL,amount NUMERIC(14,2) NOT NULL CHECK(amount>0),reason TEXT NOT NULL DEFAULT '',created_by UUID REFERENCES users(id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(stockroom_id,credit_number))")
        conn.commit()

def list_invoices(stockroom_id):
    with server.db() as conn:
        rows=conn.execute("""
        WITH line_totals AS (
          SELECT order_id,COALESCE(SUM(quantity*unit_price),0)::float8 subtotal FROM order_lines GROUP BY order_id
        ), credits AS (
          SELECT order_id,COALESCE(SUM(amount),0)::float8 credited FROM credit_notes WHERE stockroom_id=%s GROUP BY order_id
        )
        SELECT i.order_id::text,i.invoice_number,i.invoice_date,i.due_date,i.vat_percent::float8,i.sent_at,
               i.paid_amount::float8,i.paid_at,i.last_reminder_at,i.reminder_count,
               o.order_number,o.relation_name,
               ROUND((COALESCE(lt.subtotal,0)*(1+i.vat_percent/100))::numeric,2)::float8 total,
               COALESCE(c.credited,0)::float8 credited
        FROM invoice_documents i
        JOIN orders o ON o.id=i.order_id
        LEFT JOIN line_totals lt ON lt.order_id=i.order_id
        LEFT JOIN credits c ON c.order_id=i.order_id
        WHERE i.stockroom_id=%s AND i.deleted_at IS NULL
        ORDER BY i.invoice_date DESC,i.created_at DESC
        """,(stockroom_id,stockroom_id)).fetchall()
    out=[]
    for r in rows:
        d=dict(r); total=float(d.get('total') or 0); credited=float(d.get('credited') or 0); paid=float(d.get('paid_amount') or 0)
        outstanding=max(0,round(total-paid-credited,2))
        if credited>=total and total>0:status='credited'
        elif outstanding<=0:status='paid'
        elif paid>0 or credited>0:status='partial'
        elif d.get('due_date') and d['due_date']<date.today():status='overdue'
        elif d.get('sent_at'):status='sent'
        else:status='draft'
        d.update(outstanding=outstanding,status=status);out.append(d)
    return out

def _invoice_total(conn,stockroom_id,order_id):
    r=conn.execute("SELECT COALESCE(SUM(l.quantity*l.unit_price),0)::float8 subtotal,i.vat_percent::float8 FROM invoice_documents i LEFT JOIN order_lines l ON l.order_id=i.order_id WHERE i.order_id=%s AND i.stockroom_id=%s AND i.deleted_at IS NULL GROUP BY i.vat_percent",(order_id,stockroom_id)).fetchone()
    if not r:return 0
    return round(float(r['subtotal'] or 0)*(1+float(r['vat_percent'] or 0)/100),2)

def record_payment(session,order_id,amount,note=''):
    amount=float(amount)
    if amount<=0:raise ValueError('Bedrag moet groter dan 0 zijn.')
    documents_v3.ensure_invoice(session['stockroom_id'],order_id)
    with server.db() as conn:
        row=conn.execute("SELECT paid_amount::float8 FROM invoice_documents WHERE order_id=%s AND stockroom_id=%s AND deleted_at IS NULL FOR UPDATE",(order_id,session['stockroom_id'])).fetchone()
        if not row:raise PermissionError('Factuur niet gevonden.')
        total=_invoice_total(conn,session['stockroom_id'],order_id)
        credited=float(conn.execute("SELECT COALESCE(SUM(amount),0)::float8 amount FROM credit_notes WHERE order_id=%s AND stockroom_id=%s",(order_id,session['stockroom_id'])).fetchone()['amount'] or 0)
        open_amt=max(0,total-float(row['paid_amount'] or 0)-credited)
        if amount>open_amt+0.01:raise ValueError('Betaling is hoger dan het openstaande bedrag.')
        conn.execute("INSERT INTO invoice_payments(id,order_id,stockroom_id,amount,note,created_by) VALUES(%s,%s,%s,%s,%s,%s)",(str(uuid.uuid4()),order_id,session['stockroom_id'],amount,(note or '')[:500],session['user_id']))
        conn.execute("UPDATE invoice_documents SET paid_amount=paid_amount+%s,paid_at=CASE WHEN paid_amount+%s>=%s THEN NOW() ELSE paid_at END WHERE order_id=%s AND stockroom_id=%s",(amount,amount,total-credited,order_id,session['stockroom_id']))
        conn.commit()
    return {'saved':True}

def create_credit(session,order_id,amount,reason=''):
    amount=float(amount)
    if amount<=0:raise ValueError('Creditbedrag moet groter dan 0 zijn.')
    inv=documents_v3.ensure_invoice(session['stockroom_id'],order_id)
    with server.db() as conn:
        total=_invoice_total(conn,session['stockroom_id'],order_id)
        existing=float(conn.execute("SELECT COALESCE(SUM(amount),0)::float8 amount FROM credit_notes WHERE order_id=%s AND stockroom_id=%s",(order_id,session['stockroom_id'])).fetchone()['amount'] or 0)
        if existing+amount>total+0.01:raise ValueError('Totale credit mag niet hoger zijn dan het factuurbedrag.')
        year=date.today().year
        seq=conn.execute("INSERT INTO credit_sequences(stockroom_id,year,last_value) VALUES(%s,%s,1) ON CONFLICT(stockroom_id,year) DO UPDATE SET last_value=credit_sequences.last_value+1 RETURNING last_value",(session['stockroom_id'],year)).fetchone()
        number=f"CR-{year}-{int(seq['last_value']):06d}";cid=str(uuid.uuid4())
        conn.execute("INSERT INTO credit_notes(id,stockroom_id,order_id,credit_number,amount,reason,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s)",(cid,session['stockroom_id'],order_id,number,amount,(reason or '')[:1000],session['user_id']))
        conn.commit()
    return {'created':True,'id':cid,'credit_number':number,'invoice_number':inv['invoice_number']}

def credit_pdf(stockroom_id,credit_id):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    with server.db() as conn:
        n=conn.execute("SELECT c.*,i.invoice_number,o.order_number,o.relation_name,b.company_name,s.name stockroom_name FROM credit_notes c JOIN invoice_documents i ON i.order_id=c.order_id JOIN orders o ON o.id=c.order_id JOIN stockrooms s ON s.id=c.stockroom_id LEFT JOIN billing_accounts b ON b.stockroom_id=c.stockroom_id WHERE c.id=%s AND c.stockroom_id=%s",(credit_id,stockroom_id)).fetchone()
    if not n:raise PermissionError('Creditnota niet gevonden.')
    buf=io.BytesIO();c=canvas.Canvas(buf,pagesize=A4);w,h=A4;name=(n.get('company_name') or n.get('stockroom_name') or 'Stockroom')
    c.setFillColor(colors.HexColor('#111827'));c.rect(0,h-36*mm,w,36*mm,stroke=0,fill=1);c.setFillColor(colors.white);c.setFont('Helvetica-Bold',18);c.drawString(18*mm,h-17*mm,name[:55]);c.setFont('Helvetica-Bold',20);c.drawRightString(w-18*mm,h-17*mm,'CREDITNOTA')
    c.setFillColor(colors.black);c.setFont('Helvetica-Bold',10);y=h-55*mm
    for label,val in [('Creditnummer',n['credit_number']),('Factuurnummer',n['invoice_number']),('Ordernummer',n['order_number'] or '—'),('Relatie',n['relation_name'] or '—'),('Datum',n['created_at'].strftime('%d-%m-%Y'))]:c.drawString(18*mm,y,label);c.setFont('Helvetica',10);c.drawString(65*mm,y,str(val));c.setFont('Helvetica-Bold',10);y-=7*mm
    y-=8*mm;c.drawString(18*mm,y,'Reden');c.setFont('Helvetica',10);c.drawString(18*mm,y-7*mm,(n['reason'] or 'Correctie')[:90]);c.setFont('Helvetica-Bold',14);c.drawRightString(w-18*mm,y-30*mm,f"Creditbedrag: € {float(n['amount']):.2f}");c.save()
    return buf.getvalue(),f"creditnota-{n['credit_number']}.pdf"

def send_reminder(session,order_id):
    inv=next((x for x in list_invoices(session['stockroom_id']) if x['order_id']==str(order_id)),None)
    if not inv:raise PermissionError('Factuur niet gevonden.')
    if inv['outstanding']<=0:raise ValueError('Deze factuur heeft geen openstaand bedrag.')
    with server.db() as conn:
        rel=conn.execute("SELECT c.email,c.name FROM orders o LEFT JOIN customers c ON c.id=o.relation_id AND c.stockroom_id=o.stockroom_id WHERE o.id=%s AND o.stockroom_id=%s",(order_id,session['stockroom_id'])).fetchone() or {}
        cfg=conn.execute("SELECT company_name FROM billing_accounts WHERE stockroom_id=%s",(session['stockroom_id'],)).fetchone() or {}
    recipient=(rel.get('email') or '').strip()
    if '@' not in recipient:raise ValueError('Geen geldig klant-e-mailadres ingesteld.')
    data,name=documents_v2.invoice_pdf(session['stockroom_id'],order_id);company=(cfg.get('company_name') or 'Stockroom').strip() or 'Stockroom'
    msg=EmailMessage();msg['From']=server.SMTP_FROM;msg['To']=recipient;msg['Subject']=f"Betalingsherinnering {inv['invoice_number']} - {company}";msg.set_content(f"Beste {rel.get('name') or 'klant'},\n\nFactuur {inv['invoice_number']} staat nog open voor € {inv['outstanding']:.2f}.\n\nMet vriendelijke groet,\n{company}");msg.add_attachment(data,maintype='application',subtype='pdf',filename=name)
    if not server.SMTP_HOST:raise ValueError('SMTP is niet geconfigureerd.')
    with smtplib.SMTP(server.SMTP_HOST,server.SMTP_PORT,timeout=20) as smtp:
        smtp.ehlo();smtp.starttls(context=ssl.create_default_context());smtp.ehlo()
        if server.SMTP_USERNAME:smtp.login(server.SMTP_USERNAME,server.SMTP_PASSWORD)
        smtp.send_message(msg)
    with server.db() as conn:conn.execute("UPDATE invoice_documents SET last_reminder_at=NOW(),reminder_count=reminder_count+1,sent_at=COALESCE(sent_at,NOW()) WHERE order_id=%s AND stockroom_id=%s",(order_id,session['stockroom_id']));conn.commit()
    return {'sent':True,'recipient':recipient}

def delete_invoice(session,order_id):
    order_id=(order_id or '').strip()
    if not order_id:raise ValueError('Factuur is ongeldig.')
    with server.db() as conn:
        invoice=conn.execute("SELECT invoice_number FROM invoice_documents WHERE order_id=%s AND stockroom_id=%s AND deleted_at IS NULL FOR UPDATE",(order_id,session['stockroom_id'])).fetchone()
        if not invoice:raise PermissionError('Factuur niet gevonden.')
        conn.execute("UPDATE invoice_documents SET deleted_at=NOW(),deleted_by=%s WHERE order_id=%s AND stockroom_id=%s",(session['user_id'],order_id,session['stockroom_id']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",(session['stockroom_id'],session['user_id'],'invoice.trashed',json.dumps({'order_id':order_id,'invoice_number':invoice['invoice_number'],'orderPreserved':True,'financialHistoryPreserved':True})))
        conn.commit()
    return {'trashed':True,'order_id':order_id,'invoice_number':invoice['invoice_number']}

def list_deleted_invoices(stockroom_id):
    with server.db() as conn:
        return conn.execute("""SELECT i.order_id::text,i.invoice_number,i.invoice_date,i.deleted_at,o.order_number,o.relation_name,
          (SELECT COUNT(*) FROM invoice_payments p WHERE p.order_id=i.order_id AND p.stockroom_id=i.stockroom_id) payments,
          (SELECT COUNT(*) FROM credit_notes c WHERE c.order_id=i.order_id AND c.stockroom_id=i.stockroom_id) credits
          FROM invoice_documents i JOIN orders o ON o.id=i.order_id
          WHERE i.stockroom_id=%s AND i.deleted_at IS NOT NULL ORDER BY i.deleted_at DESC""",(stockroom_id,)).fetchall()

def restore_invoice(session,order_id):
    order_id=(order_id or '').strip()
    if not order_id:raise ValueError('Factuur is ongeldig.')
    with server.db() as conn:
        invoice=conn.execute("UPDATE invoice_documents SET deleted_at=NULL,deleted_by=NULL WHERE order_id=%s AND stockroom_id=%s AND deleted_at IS NOT NULL RETURNING invoice_number",(order_id,session['stockroom_id'])).fetchone()
        if not invoice:raise PermissionError('Factuur staat niet in de prullenbak.')
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",(session['stockroom_id'],session['user_id'],'invoice.restored',json.dumps({'order_id':order_id,'invoice_number':invoice['invoice_number']})))
        conn.commit()
    return {'restored':True,'order_id':order_id,'invoice_number':invoice['invoice_number']}

def _wrap_order_status():
    global _original_update_order_status
    import order_management
    if _original_update_order_status:return
    _original_update_order_status=order_management.update_order_status
    def wrapped(session,order_type,values):
        result=_original_update_order_status(session,order_type,values)
        if order_type=='sales' and values.get('status') in ('completed','paid'):
            with server.db() as conn:
                trashed=conn.execute("SELECT 1 FROM invoice_documents WHERE order_id=%s AND stockroom_id=%s AND deleted_at IS NOT NULL",(values.get('order_id'),session['stockroom_id'])).fetchone()
            if trashed:return result
            documents_v3.ensure_invoice(session['stockroom_id'],values.get('order_id'))
            if values.get('status')=='paid':
                with server.db() as conn:
                    total=_invoice_total(conn,session['stockroom_id'],values.get('order_id'))
                    conn.execute("UPDATE invoice_documents SET paid_amount=GREATEST(paid_amount,%s),paid_at=COALESCE(paid_at,NOW()),sent_at=COALESCE(sent_at,NOW()) WHERE order_id=%s AND stockroom_id=%s",(total,values.get('order_id'),session['stockroom_id']));conn.commit()
        return result
    order_management.update_order_status=wrapped

def install():
    global _installed
    if _installed:return
    _installed=True;initialize();_wrap_order_status();old_get=server.StockroomHandler.do_GET;old_post=server.StockroomHandler.do_POST
    def do_GET(self):
        p=urllib.parse.urlparse(self.path)
        if p.path=='/api/finance/invoices':
            s=self.require_session(api=True)
            if s:self.send_json(200,{'invoices':list_invoices(s['stockroom_id'])})
            return
        if p.path=='/api/finance/trash':
            s=self.require_session(api=True)
            if s:self.send_json(200,{'invoices':list_deleted_invoices(s['stockroom_id'])})
            return
        if p.path=='/api/finance/credit.pdf':
            s=self.require_session(api=True)
            if not s:return
            try:data,name=credit_pdf(s['stockroom_id'],urllib.parse.parse_qs(p.query).get('id',[''])[0]);self.send_response(200);self.send_header('Content-Type','application/pdf');self.send_header('Content-Disposition',f'inline; filename="{name}"');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            except PermissionError as e:self.send_json(404,{'error':str(e)})
            return
        return old_get(self)
    def do_POST(self):
        path=urllib.parse.urlparse(self.path).path
        if path in ('/api/finance/payment','/api/finance/credit','/api/finance/reminder','/api/finance/delete','/api/finance/restore'):
            if not self.enforce_origin():return
            s=self.require_session(api=True)
            if not s:return
            if s.get('role') not in ('owner','admin','member','seller'):self.send_json(403,{'error':'Geen rechten.'});return
            f=self.form_data() or {};v={k:(x[0] if isinstance(x,list) and x else x) for k,x in f.items()}
            try:
                res=record_payment(s,v.get('order_id'),v.get('amount'),v.get('note') or '') if path.endswith('/payment') else create_credit(s,v.get('order_id'),v.get('amount'),v.get('reason') or '') if path.endswith('/credit') else delete_invoice(s,v.get('order_id')) if path.endswith('/delete') else restore_invoice(s,v.get('order_id')) if path.endswith('/restore') else send_reminder(s,v.get('order_id'))
                self.send_json(200,res)
            except PermissionError as e:self.send_json(403,{'error':str(e)})
            except ValueError as e:self.send_json(400,{'error':str(e)})
            return
        return old_post(self)
    server.StockroomHandler.do_GET=do_GET;server.StockroomHandler.do_POST=do_POST

