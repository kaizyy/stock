import base64
import io
import json
import smtplib
import ssl
import urllib.parse
from datetime import date, timedelta
from email.message import EmailMessage

import server

_installed=False


def initialize():
    with server.db() as conn:
        conn.execute("ALTER TABLE billing_accounts ADD COLUMN IF NOT EXISTS iban TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE billing_accounts ADD COLUMN IF NOT EXISTS bic TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE billing_accounts ADD COLUMN IF NOT EXISTS payment_term_days INTEGER NOT NULL DEFAULT 14")
        conn.execute("ALTER TABLE billing_accounts ADD COLUMN IF NOT EXISTS default_vat_percent NUMERIC(6,3) NOT NULL DEFAULT 21")
        conn.execute("ALTER TABLE billing_accounts ADD COLUMN IF NOT EXISTS invoice_footer TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE billing_accounts ADD COLUMN IF NOT EXISTS accent_hex TEXT NOT NULL DEFAULT '#111827'")
        conn.execute("ALTER TABLE billing_accounts ADD COLUMN IF NOT EXISTS logo_data TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS invoice_sequences(
          stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
          year INTEGER NOT NULL,last_value INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(stockroom_id,year))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS invoice_documents(
          order_id UUID PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
          stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
          invoice_number TEXT NOT NULL,
          invoice_date DATE NOT NULL DEFAULT CURRENT_DATE,
          due_date DATE NOT NULL,
          vat_percent NUMERIC(6,3) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(stockroom_id,invoice_number))""")
        conn.commit()


def settings(stockroom_id):
    with server.db() as conn:
        r=conn.execute("SELECT company_name,address,postal_code,city,country,vat_number,chamber_number,invoice_email,iban,bic,payment_term_days,default_vat_percent::float8,invoice_footer,accent_hex,logo_data<>'' logo_configured FROM billing_accounts WHERE stockroom_id=%s",(stockroom_id,)).fetchone()
    return dict(r or {})


def branding(stockroom_id):
    with server.db() as conn:
        r=conn.execute("SELECT logo_data FROM billing_accounts WHERE stockroom_id=%s",(stockroom_id,)).fetchone()
    return {'logo_data':(r or {}).get('logo_data') or ''}


def save_settings(stockroom_id, values):
    term=max(0,min(365,int(values.get('payment_term_days') or 14)))
    vat=float(values.get('default_vat_percent') or 0)
    if vat<0 or vat>100:raise ValueError('BTW-percentage is ongeldig.')
    accent=(values.get('accent_hex') or '#111827').strip()
    if len(accent)!=7 or not accent.startswith('#'):
        raise ValueError('Huisstijlkleur is ongeldig.')
    int(accent[1:],16)
    logo=(values.get('logo_data') or '').strip()
    if logo and not (logo.startswith('data:image/png;base64,') or logo.startswith('data:image/jpeg;base64,')):
        raise ValueError('Logo moet PNG of JPEG zijn.')
    if len(logo)>400000:raise ValueError('Logo is te groot. Gebruik een afbeelding kleiner dan ongeveer 250 KB.')
    with server.db() as conn:
        conn.execute("""UPDATE billing_accounts SET iban=%s,bic=%s,payment_term_days=%s,default_vat_percent=%s,
          invoice_footer=%s,accent_hex=%s,logo_data=CASE WHEN %s<>'' THEN %s ELSE logo_data END,updated_at=NOW() WHERE stockroom_id=%s""",
          ((values.get('iban') or '').strip()[:100],(values.get('bic') or '').strip()[:50],term,vat,(values.get('invoice_footer') or '').strip()[:1000],accent,logo,logo,stockroom_id))
        conn.commit()
    return {'saved':True}


def remove_logo(stockroom_id):
    with server.db() as conn:conn.execute("UPDATE billing_accounts SET logo_data='',updated_at=NOW() WHERE stockroom_id=%s",(stockroom_id,));conn.commit()
    return {'removed':True}


def ensure_invoice(stockroom_id, order_id):
    with server.db() as conn:
        existing=conn.execute("SELECT invoice_number,invoice_date,due_date,vat_percent::float8,deleted_at FROM invoice_documents WHERE order_id=%s AND stockroom_id=%s",(order_id,stockroom_id)).fetchone()
        if existing:
            if existing.get('deleted_at'):raise ValueError('Deze factuur staat in de prullenbak. Herstel de factuur eerst.')
            return {k:v for k,v in dict(existing).items() if k!='deleted_at'}
        order=conn.execute("SELECT order_type FROM orders WHERE id=%s AND stockroom_id=%s",(order_id,stockroom_id)).fetchone()
        if not order:raise PermissionError('Order niet gevonden.')
        if order['order_type']!='sales':raise ValueError('Facturen zijn alleen beschikbaar voor verkooporders.')
        cfg=conn.execute("SELECT payment_term_days,default_vat_percent::float8 FROM billing_accounts WHERE stockroom_id=%s",(stockroom_id,)).fetchone() or {'payment_term_days':14,'default_vat_percent':21}
        year=date.today().year
        seq=conn.execute("""INSERT INTO invoice_sequences(stockroom_id,year,last_value) VALUES(%s,%s,1)
          ON CONFLICT(stockroom_id,year) DO UPDATE SET last_value=invoice_sequences.last_value+1 RETURNING last_value""",(stockroom_id,year)).fetchone()
        number=f"INV-{year}-{int(seq['last_value']):06d}"
        inv_date=date.today();due=inv_date+timedelta(days=int(cfg['payment_term_days'] or 0))
        row=conn.execute("INSERT INTO invoice_documents(order_id,stockroom_id,invoice_number,invoice_date,due_date,vat_percent) VALUES(%s,%s,%s,%s,%s,%s) RETURNING invoice_number,invoice_date,due_date,vat_percent::float8",(order_id,stockroom_id,number,inv_date,due,float(cfg['default_vat_percent'] or 0))).fetchone();conn.commit();return dict(row)


def email_document(session, values):
    import documents_v2
    oid=(values.get('order_id') or '').strip();kind=(values.get('kind') or 'invoice').strip()
    with server.db() as conn:
        order=conn.execute("SELECT order_type,relation_id,relation_name,order_number FROM orders WHERE id=%s AND stockroom_id=%s",(oid,session['stockroom_id'])).fetchone()
        if not order:raise PermissionError('Order niet gevonden.')
        table='customers' if order['order_type']=='sales' else 'suppliers'
        rel=conn.execute(f"SELECT email,name FROM {table} WHERE id=%s AND stockroom_id=%s",(order['relation_id'],session['stockroom_id'])).fetchone() if order['relation_id'] else None
        cfg=conn.execute("SELECT company_name FROM billing_accounts WHERE stockroom_id=%s",(session['stockroom_id'],)).fetchone() or {}
    recipient=(values.get('recipient') or (rel or {}).get('email') or '').strip()
    if '@' not in recipient:raise ValueError('Geen geldig e-mailadres voor deze relatie ingesteld.')
    if kind=='invoice':data,name=documents_v2.invoice_pdf(session['stockroom_id'],oid);label='factuur'
    elif kind=='packing':data,name=documents_v2.packing_slip_pdf(session['stockroom_id'],oid);label='pakbon'
    elif kind=='return':data,name=documents_v2.return_pdf(session['stockroom_id'],oid);label='retourdocument'
    else:raise ValueError('Documenttype is ongeldig.')
    msg=EmailMessage();company=(cfg.get('company_name') or '').strip() or 'Stockroom';msg['From']=server.SMTP_FROM;msg['To']=recipient;msg['Subject']=f"{label.capitalize()} {order.get('order_number') or ''} - {company}".strip();msg.set_content((values.get('message') or f"Beste {(rel or {}).get('name') or order.get('relation_name') or 'klant'},\n\nIn de bijlage vindt u het {label}.\n\nMet vriendelijke groet,\n{company}").strip());msg.add_attachment(data,maintype='application',subtype='pdf',filename=name)
    if not server.SMTP_HOST:raise ValueError('SMTP is niet geconfigureerd.')
    context=ssl.create_default_context()
    with smtplib.SMTP(server.SMTP_HOST,server.SMTP_PORT,timeout=20) as smtp:
        smtp.ehlo();smtp.starttls(context=context);smtp.ehlo()
        if server.SMTP_USERNAME:smtp.login(server.SMTP_USERNAME,server.SMTP_PASSWORD)
        smtp.send_message(msg)
    if kind=='invoice':
        with server.db() as conn:conn.execute("UPDATE invoice_documents SET sent_at=COALESCE(sent_at,NOW()) WHERE order_id=%s AND stockroom_id=%s AND deleted_at IS NULL",(oid,session['stockroom_id']));conn.commit()
    return {'sent':True,'recipient':recipient}


def install():
    global _installed
    if _installed:return
    _installed=True;initialize()
    old_get=server.StockroomHandler.do_GET;old_post=server.StockroomHandler.do_POST
    def do_GET(self):
        p=urllib.parse.urlparse(self.path)
        if p.path in ('/api/documents/settings','/api/documents/branding'):
            s=self.require_session(api=True)
            if s:self.send_json(200,branding(s['stockroom_id']) if p.path.endswith('/branding') else settings(s['stockroom_id']))
            return
        return old_get(self)
    def do_POST(self):
        path=urllib.parse.urlparse(self.path).path
        if path in ('/api/documents/settings','/api/documents/logo/remove','/api/documents/email'):
            if not self.enforce_origin():return
            s=self.require_session(api=True)
            if not s:return
            if s.get('role') not in ('owner','admin','member','seller','buyer'):
                self.send_json(403,{'error':'Geen rechten.'});return
            max_form_bytes=550000 if path=='/api/documents/settings' else 32768
            f=self.form_data(max_bytes=max_form_bytes) or {};v={k:(x[0] if isinstance(x,list) and x else x) for k,x in f.items()}
            try:
                if path=='/api/documents/settings':res=save_settings(s['stockroom_id'],v)
                elif path.endswith('/logo/remove'):res=remove_logo(s['stockroom_id'])
                else:res=email_document(s,v)
                self.send_json(200,res)
            except PermissionError as e:self.send_json(403,{'error':str(e)})
            except ValueError as e:self.send_json(400,{'error':str(e)})
            return
        return old_post(self)
    server.StockroomHandler.do_GET=do_GET;server.StockroomHandler.do_POST=do_POST

