import io,json,smtplib,ssl,urllib.parse,uuid
from datetime import date,timedelta
from email.message import EmailMessage
import server,order_management,business_tools,documents_v3

_installed=False
def initialize():
    with server.db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS quotes(id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,quote_number TEXT NOT NULL,relation_id UUID,relation_name TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'draft',reference TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',quote_date DATE NOT NULL DEFAULT CURRENT_DATE,valid_until DATE NOT NULL,created_by UUID REFERENCES users(id) ON DELETE SET NULL,converted_order_id UUID REFERENCES orders(id) ON DELETE SET NULL,sent_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(stockroom_id,quote_number))""")
        c.execute("""CREATE TABLE IF NOT EXISTS quote_lines(id UUID PRIMARY KEY,quote_id UUID NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,item_id TEXT NOT NULL,item_name TEXT NOT NULL,sku TEXT NOT NULL DEFAULT '',quantity NUMERIC(14,3) NOT NULL CHECK(quantity>0),unit_price NUMERIC(14,4) NOT NULL CHECK(unit_price>=0))""");c.commit()
        c.execute("CREATE TABLE IF NOT EXISTS quote_sequences(stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,year INTEGER NOT NULL,last_value INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(stockroom_id,year))");c.commit()
def rows(room):
    with server.db() as c:
        out=c.execute("SELECT q.*,q.id::text,converted_order_id::text FROM quotes q WHERE stockroom_id=%s ORDER BY created_at DESC",(room,)).fetchall()
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
def convert(session,qid):
    with server.db() as c:
        q=c.execute("SELECT * FROM quotes WHERE id=%s AND stockroom_id=%s FOR UPDATE",(qid,session['stockroom_id'])).fetchone()
        if not q:raise PermissionError('Offerte niet gevonden.')
        if q['converted_order_id']:return {'converted':True,'order_id':str(q['converted_order_id'])}
        lines=c.execute("SELECT item_id,item_name,sku,quantity::float8 quantity,unit_price::float8 unit_price FROM quote_lines WHERE quote_id=%s",(qid,)).fetchall()
        oid=order_management.create_order(session,{'order_type':'sales','status':'draft','relation_id':str(q['relation_id'] or ''),'relation_name':q['relation_name'],'reference':q['quote_number'],'notes':q['notes'],'lines_json':json.dumps(lines)});business_tools.assign_order_number(oid,session['stockroom_id'],'sales');inv=documents_v3.ensure_invoice(session['stockroom_id'],oid)
        c.execute("UPDATE quotes SET status='converted',converted_order_id=%s,updated_at=NOW() WHERE id=%s",(oid,qid));c.commit()
    return {'converted':True,'order_id':oid,'invoice_number':inv['invoice_number']}
def pdf(room,qid):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    q=next((x for x in rows(room) if str(x['id'])==str(qid)),None)
    if not q:raise PermissionError('Offerte niet gevonden.')
    b=io.BytesIO();c=canvas.Canvas(b,pagesize=A4);c.setFont('Helvetica-Bold',20);c.drawString(20*mm,280*mm,'OFFERTE');c.setFont('Helvetica',10);y=265*mm
    for label,val in [('Offertenummer',q['quote_number']),('Klant',q['relation_name'] or '—'),('Geldig tot',str(q['valid_until']))]:c.drawString(20*mm,y,f'{label}: {val}');y-=7*mm
    total=0;y-=5*mm
    for l in q['lines']:amount=float(l['quantity'])*float(l['unit_price']);total+=amount;c.drawString(20*mm,y,f"{float(l['quantity']):g} × {l['item_name']}");c.drawRightString(190*mm,y,f'€ {amount:.2f}');y-=7*mm
    c.setFont('Helvetica-Bold',12);c.drawRightString(190*mm,y-5*mm,f'Totaal excl. btw: € {total:.2f}');c.save();return b.getvalue(),f"offerte-{q['quote_number']}.pdf"
def mail(session,qid):
    with server.db() as c:q=c.execute("SELECT q.*,c.email FROM quotes q LEFT JOIN customers c ON c.id=q.relation_id WHERE q.id=%s AND q.stockroom_id=%s",(qid,session['stockroom_id'])).fetchone()
    if not q or '@' not in (q.get('email') or ''):raise ValueError('Geen geldig klant-e-mailadres ingesteld.')
    data,name=pdf(session['stockroom_id'],qid);m=EmailMessage();m['From']=server.SMTP_FROM;m['To']=q['email'];m['Subject']=f"Offerte {q['quote_number']}";m.set_content('In de bijlage vindt u onze offerte.');m.add_attachment(data,maintype='application',subtype='pdf',filename=name)
    if not server.SMTP_HOST:raise ValueError('SMTP is niet geconfigureerd.')
    with smtplib.SMTP(server.SMTP_HOST,server.SMTP_PORT,timeout=20) as s:s.ehlo();s.starttls(context=ssl.create_default_context());s.ehlo();s.login(server.SMTP_USERNAME,server.SMTP_PASSWORD) if server.SMTP_USERNAME else None;s.send_message(m)
    with server.db() as c:c.execute("UPDATE quotes SET status='sent',sent_at=NOW(),updated_at=NOW() WHERE id=%s",(qid,));c.commit()
    return {'sent':True}
def install():
    global _installed
    if _installed:return
    _installed=True;initialize();og=server.StockroomHandler.do_GET;op=server.StockroomHandler.do_POST
    def get(self):
        p=urllib.parse.urlparse(self.path)
        if p.path in ('/api/quotes','/api/quotes/pdf'):
            s=self.require_session(api=True)
            if not s:return
            if p.path=='/api/quotes':self.send_json(200,{'quotes':rows(s['stockroom_id'])})
            else:data,name=pdf(s['stockroom_id'],urllib.parse.parse_qs(p.query).get('id',[''])[0]);self.send_pdf(data,name)
            return
        return og(self)
    def post(self):
        path=urllib.parse.urlparse(self.path).path
        if path in ('/api/quotes','/api/quotes/convert','/api/quotes/mail'):
            if not self.enforce_origin():return
            s=self.require_session(api=True)
            if not s:return
            f=self.form_data() or {};v={k:(x[0] if isinstance(x,list) and x else x) for k,x in f.items()}
            try:self.send_json(200,create(s,v) if path=='/api/quotes' else convert(s,v.get('id')) if path.endswith('convert') else mail(s,v.get('id')))
            except (ValueError,PermissionError) as e:self.send_json(400,{'error':str(e)})
            return
        return op(self)
    server.StockroomHandler.do_GET=get;server.StockroomHandler.do_POST=post

