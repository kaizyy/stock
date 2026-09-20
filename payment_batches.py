"""Controlled supplier payment proposals and SEPA pain.001 export."""
import json
import re
import uuid
from datetime import date, datetime, timezone
from xml.etree import ElementTree as ET

import server

ACTIVE = ('draft','approved','exported')


def initialize():
    with server.db() as conn:
        conn.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS iban TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS bic TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS payment_settings(stockroom_id UUID PRIMARY KEY REFERENCES stockrooms(id) ON DELETE CASCADE,
            account_name TEXT NOT NULL DEFAULT '',iban TEXT NOT NULL DEFAULT '',bic TEXT NOT NULL DEFAULT '',updated_by UUID REFERENCES users(id) ON DELETE SET NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS payment_batches(id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
            batch_number TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',execution_date DATE NOT NULL,created_by UUID REFERENCES users(id) ON DELETE SET NULL,
            approved_by UUID REFERENCES users(id) ON DELETE SET NULL,approved_at TIMESTAMPTZ,exported_at TIMESTAMPTZ,processed_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(stockroom_id,batch_number))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS payment_batch_items(id UUID PRIMARY KEY,batch_id UUID NOT NULL REFERENCES payment_batches(id) ON DELETE CASCADE,
            invoice_id UUID NOT NULL REFERENCES purchase_invoices(id) ON DELETE RESTRICT,amount NUMERIC(14,2) NOT NULL CHECK(amount>0),creditor_name TEXT NOT NULL,
            creditor_iban TEXT NOT NULL,creditor_bic TEXT NOT NULL DEFAULT '',reference TEXT NOT NULL,UNIQUE(batch_id,invoice_id))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_batches_room ON payment_batches(stockroom_id,created_at DESC)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_batch_invoice_once ON payment_batch_items(invoice_id)")
        conn.execute("ALTER TABLE payment_batch_items DROP CONSTRAINT IF EXISTS payment_batch_items_invoice_id_fkey")
        conn.execute("ALTER TABLE payment_batch_items ADD CONSTRAINT payment_batch_items_invoice_id_fkey FOREIGN KEY(invoice_id) REFERENCES purchase_invoices(id) ON DELETE CASCADE")
        conn.commit()


def _iban(value):return re.sub(r'\s+','',str(value or '')).upper()


def valid_iban(value):
    value=_iban(value)
    if not re.fullmatch(r'[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}',value):return False
    rearranged=value[4:]+value[:4];digits=''.join(str(ord(ch)-55) if ch.isalpha() else ch for ch in rearranged)
    return int(digits)%97==1


def settings(stockroom_id):
    with server.db() as conn:row=conn.execute("SELECT account_name,iban,bic FROM payment_settings WHERE stockroom_id=%s",(stockroom_id,)).fetchone()
    return row or {'account_name':'','iban':'','bic':''}


def save_settings(session, values):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan bankinstellingen wijzigen.')
    name=(values.get('account_name') or '').strip()[:200];iban=_iban(values.get('iban'));bic=(values.get('bic') or '').strip().upper()[:20]
    if not name or not valid_iban(iban):raise ValueError('Vul een geldige rekeningnaam en IBAN in.')
    with server.db() as conn:
        conn.execute("""INSERT INTO payment_settings(stockroom_id,account_name,iban,bic,updated_by) VALUES(%s,%s,%s,%s,%s)
            ON CONFLICT(stockroom_id) DO UPDATE SET account_name=EXCLUDED.account_name,iban=EXCLUDED.iban,bic=EXCLUDED.bic,updated_by=EXCLUDED.updated_by,updated_at=NOW()""",(session['stockroom_id'],name,iban,bic,session['user_id']));conn.commit()
    return {'saved':True,**settings(session['stockroom_id'])}


def candidates(stockroom_id):
    with server.db() as conn:rows=conn.execute("""SELECT i.id::text,i.invoice_number,i.due_date,i.total_amount::float8,i.paid_amount::float8,i.dispute_amount::float8,i.invoice_iban,s.name supplier_name,s.iban,s.bic,
        COALESCE((SELECT SUM(c.amount) FROM purchase_invoice_credits c WHERE c.invoice_id=i.id),0)::float8 credited
        FROM purchase_invoices i JOIN suppliers s ON s.id=i.supplier_id WHERE i.stockroom_id=%s AND i.status='approved'
        AND NOT EXISTS(SELECT 1 FROM payment_batch_items bi JOIN payment_batches b ON b.id=bi.batch_id WHERE bi.invoice_id=i.id AND b.status IN ('draft','approved','exported','processed')) ORDER BY i.due_date,i.invoice_number""",(stockroom_id,)).fetchall()
    for row in rows:row['amount']=max(0,row['total_amount']-row['paid_amount']-row['dispute_amount']-row['credited']);row['ibanMismatch']=bool(row['invoice_iban'] and _iban(row['invoice_iban'])!=_iban(row['iban']));row['bankReady']=valid_iban(row['iban']) and not row['ibanMismatch']
    return [row for row in rows if row['amount']>0.005]


def _batch_rows(conn, stockroom_id):
    rows=conn.execute("""SELECT b.id::text,b.batch_number,b.status,b.execution_date,b.created_at,b.approved_at,b.exported_at,b.processed_at,
        COALESCE(SUM(i.amount),0)::float8 total,COUNT(i.id)::int item_count FROM payment_batches b LEFT JOIN payment_batch_items i ON i.batch_id=b.id
        WHERE b.stockroom_id=%s GROUP BY b.id ORDER BY b.created_at DESC LIMIT 100""",(stockroom_id,)).fetchall()
    for row in rows:row['items']=conn.execute("SELECT invoice_id::text,amount::float8,creditor_name,creditor_iban,creditor_bic,reference FROM payment_batch_items WHERE batch_id=%s ORDER BY creditor_name,reference",(row['id'],)).fetchall()
    return rows


def overview(stockroom_id):
    with server.db() as conn:batches=_batch_rows(conn,stockroom_id)
    return {'settings':settings(stockroom_id),'candidates':candidates(stockroom_id),'batches':batches}


def create(session, values):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan een betaalvoorstel maken.')
    try:ids=list(dict.fromkeys(str(x) for x in json.loads(values.get('invoice_ids') or '[]')))
    except (TypeError,json.JSONDecodeError):raise ValueError('Factuurselectie is ongeldig.')
    if not ids:raise ValueError('Selecteer minimaal één factuur.')
    try:execution=date.fromisoformat(values.get('execution_date') or '')
    except ValueError:raise ValueError('Kies een geldige uitvoerdatum.')
    available={row['id']:row for row in candidates(session['stockroom_id'])}
    selected=[]
    for invoice_id in ids:
        row=available.get(invoice_id)
        if not row:raise ValueError('Een factuur is niet meer beschikbaar voor betaling.')
        if not row['bankReady']:raise ValueError(f"IBAN ontbreekt of is ongeldig bij {row['supplier_name']}.")
        selected.append(row)
    batch_id=str(uuid.uuid4());number=f"PAY-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{batch_id[:4].upper()}"
    with server.db() as conn:
        conn.execute("INSERT INTO payment_batches(id,stockroom_id,batch_number,execution_date,created_by) VALUES(%s,%s,%s,%s,%s)",(batch_id,session['stockroom_id'],number,execution,session['user_id']))
        for row in selected:conn.execute("INSERT INTO payment_batch_items(id,batch_id,invoice_id,amount,creditor_name,creditor_iban,creditor_bic,reference) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",(str(uuid.uuid4()),batch_id,row['id'],row['amount'],row['supplier_name'],_iban(row['iban']),row['bic'],row['invoice_number']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'payment_batch.created',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':batch_id,'number':number,'count':len(selected)})));conn.commit()
    return {'created':True,'id':batch_id,'batchNumber':number}


def change(session, values, action):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan betaalbatches beheren.')
    batch_id=(values.get('batch_id') or '').strip();target={'approve':'approved','cancel':'cancelled'}.get(action)
    if not target:raise ValueError('Batchactie is ongeldig.')
    with server.db() as conn:
        current=conn.execute("SELECT status FROM payment_batches WHERE id=%s AND stockroom_id=%s FOR UPDATE",(batch_id,session['stockroom_id'])).fetchone()
        if not current or current['status']!='draft':raise ValueError('Alleen een conceptbatch kan worden goedgekeurd of geannuleerd.')
        count=conn.execute("SELECT COUNT(*) n FROM payment_batch_items WHERE batch_id=%s",(batch_id,)).fetchone()['n']
        if target=='approved' and not count:raise ValueError('Een lege batch kan niet worden goedgekeurd.')
        if target=='approved':conn.execute("UPDATE payment_batches SET status='approved',approved_by=%s,approved_at=NOW() WHERE id=%s",(session['user_id'],batch_id))
        else:conn.execute("UPDATE payment_batches SET status='cancelled',approved_by=NULL,approved_at=NULL WHERE id=%s",(batch_id,))
        if target=='cancelled':conn.execute("DELETE FROM payment_batch_items WHERE batch_id=%s",(batch_id,))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",(session['stockroom_id'],session['user_id'],f'payment_batch.{target}',json.dumps({'id':batch_id})));conn.commit()
    return {'updated':True,'status':target}


def sepa(session, batch_id):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Geen rechten om een SEPA-bestand te maken.')
    payer=settings(session['stockroom_id'])
    if not payer['account_name'] or not valid_iban(payer['iban']):raise ValueError('Vul eerst de bankrekening van de organisatie in.')
    with server.db() as conn:
        batch=conn.execute("SELECT id::text,batch_number,status,execution_date FROM payment_batches WHERE id=%s AND stockroom_id=%s FOR UPDATE",(batch_id,session['stockroom_id'])).fetchone()
        if not batch or batch['status'] not in ('approved','exported'):raise ValueError('Keur de betaalbatch eerst goed.')
        items=conn.execute("SELECT * FROM payment_batch_items WHERE batch_id=%s ORDER BY creditor_name,reference",(batch_id,)).fetchall()
        if not items:raise ValueError('De betaalbatch is leeg.')
        ns='urn:iso:std:iso:20022:tech:xsd:pain.001.001.03';ET.register_namespace('',ns);root=ET.Element(f'{{{ns}}}Document');init=ET.SubElement(root,f'{{{ns}}}CstmrCdtTrfInitn');header=ET.SubElement(init,f'{{{ns}}}GrpHdr')
        def node(parent,name,text):child=ET.SubElement(parent,f'{{{ns}}}{name}');child.text=str(text);return child
        node(header,'MsgId',batch['batch_number']);node(header,'CreDtTm',datetime.now(timezone.utc).replace(microsecond=0).isoformat());node(header,'NbOfTxs',len(items));node(header,'CtrlSum',f"{sum(float(x['amount']) for x in items):.2f}");party=node(header,'InitgPty','');node(party,'Nm',payer['account_name'])
        info=node(init,'PmtInf','');node(info,'PmtInfId',batch['batch_number']);node(info,'PmtMtd','TRF');node(info,'BtchBookg','true');node(info,'NbOfTxs',len(items));node(info,'CtrlSum',f"{sum(float(x['amount']) for x in items):.2f}");ptype=node(info,'PmtTpInf','');service=node(ptype,'SvcLvl','');node(service,'Cd','SEPA');node(info,'ReqdExctnDt',batch['execution_date']);debtor=node(info,'Dbtr','');node(debtor,'Nm',payer['account_name']);account=node(info,'DbtrAcct','');account_id=node(account,'Id','');node(account_id,'IBAN',_iban(payer['iban']));agent=node(info,'DbtrAgt','');fin=node(agent,'FinInstnId','')
        if payer['bic']:node(fin,'BIC',payer['bic'])
        else:other=node(fin,'Othr','');node(other,'Id','NOTPROVIDED')
        node(info,'ChrgBr','SLEV')
        for item in items:
            tx=node(info,'CdtTrfTxInf','');pid=node(tx,'PmtId','');node(pid,'EndToEndId',item['reference'][:35] or 'NOTPROVIDED');amount=node(tx,'Amt','');value=node(amount,'InstdAmt',f"{float(item['amount']):.2f}");value.set('Ccy','EUR')
            if item['creditor_bic']:credit_agent=node(tx,'CdtrAgt','');credit_fin=node(credit_agent,'FinInstnId','');node(credit_fin,'BIC',item['creditor_bic'])
            creditor=node(tx,'Cdtr','');node(creditor,'Nm',item['creditor_name'][:70]);credit_account=node(tx,'CdtrAcct','');credit_id=node(credit_account,'Id','');node(credit_id,'IBAN',item['creditor_iban']);remit=node(tx,'RmtInf','');node(remit,'Ustrd',item['reference'][:140])
        conn.execute("UPDATE payment_batches SET status='exported',exported_at=COALESCE(exported_at,NOW()) WHERE id=%s",(batch_id,));conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'payment_batch.exported',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':batch_id})));conn.commit()
    return ET.tostring(root,encoding='utf-8',xml_declaration=True),f"{batch['batch_number']}.xml"


def process(session, values):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan een betaalbatch verwerken.')
    batch_id=(values.get('batch_id') or '').strip()
    with server.db() as conn:
        batch=conn.execute("SELECT status FROM payment_batches WHERE id=%s AND stockroom_id=%s FOR UPDATE",(batch_id,session['stockroom_id'])).fetchone()
        if not batch or batch['status']!='exported':raise ValueError('Exporteer de goedgekeurde batch voordat u deze als verwerkt markeert.')
        items=conn.execute("SELECT invoice_id,amount::float8 FROM payment_batch_items WHERE batch_id=%s",(batch_id,)).fetchall()
        for item in items:
            invoice=conn.execute("SELECT status,total_amount::float8,paid_amount::float8,dispute_amount::float8 FROM purchase_invoices WHERE id=%s FOR UPDATE",(item['invoice_id'],)).fetchone();credited=conn.execute("SELECT COALESCE(SUM(amount),0)::float8 n FROM purchase_invoice_credits WHERE invoice_id=%s",(item['invoice_id'],)).fetchone()['n'];open_amount=max(0,invoice['total_amount']-invoice['paid_amount']-invoice['dispute_amount']-credited)
            if invoice['status']!='approved' or abs(open_amount-item['amount'])>0.005:raise ValueError('Een factuur is sinds het voorstel gewijzigd; maak een nieuwe batch.')
            conn.execute("UPDATE purchase_invoices SET paid_amount=paid_amount+%s,status='paid',updated_at=NOW() WHERE id=%s",(item['amount'],item['invoice_id']))
        conn.execute("UPDATE payment_batches SET status='processed',processed_at=NOW() WHERE id=%s",(batch_id,));conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'payment_batch.processed',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':batch_id,'count':len(items)})));conn.commit()
    return {'processed':True,'count':len(items)}
