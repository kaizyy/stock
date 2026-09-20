"""CAMT.053/MT940 imports and controlled invoice reconciliation."""
import base64
import hashlib
import json
import re
import uuid
from datetime import datetime
from xml.etree import ElementTree as ET

import server
import financial_workflow
import purchase_invoices
import payment_batches


def initialize():
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS bank_imports(id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,file_hash TEXT NOT NULL,format TEXT NOT NULL,transaction_count INTEGER NOT NULL DEFAULT 0,created_by UUID REFERENCES users(id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(stockroom_id,file_hash))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bank_transactions(id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,import_id UUID NOT NULL REFERENCES bank_imports(id) ON DELETE CASCADE,
            external_id TEXT NOT NULL,booking_date DATE NOT NULL,amount NUMERIC(14,2) NOT NULL,currency TEXT NOT NULL DEFAULT 'EUR',counterparty_name TEXT NOT NULL DEFAULT '',counterparty_iban TEXT NOT NULL DEFAULT '',reference TEXT NOT NULL DEFAULT '',description TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'unmatched',suggestion JSONB NOT NULL DEFAULT '{}'::jsonb,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(stockroom_id,external_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bank_allocations(id UUID PRIMARY KEY,transaction_id UUID NOT NULL REFERENCES bank_transactions(id) ON DELETE CASCADE,target_type TEXT NOT NULL,target_id TEXT NOT NULL DEFAULT '',amount NUMERIC(14,2) NOT NULL CHECK(amount>0),note TEXT NOT NULL DEFAULT '',created_by UUID REFERENCES users(id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_transactions_room_status ON bank_transactions(stockroom_id,status,booking_date DESC)");conn.commit()


def _local(element):return element.tag.rsplit('}',1)[-1]
def _child_text(element, name):
    found=next((x for x in element.iter() if _local(x)==name and (x.text or '').strip()),None);return (found.text or '').strip() if found is not None else ''


def parse_camt(data):
    try:root=ET.fromstring(data)
    except ET.ParseError as exc:raise ValueError('CAMT-bestand is geen geldige XML.') from exc
    rows=[]
    for entry in (x for x in root.iter() if _local(x)=='Ntry'):
        amount_node=next((x for x in entry if _local(x)=='Amt'),None);raw=float((amount_node.text or '0').replace(',','.')) if amount_node is not None else 0
        if _child_text(entry,'CdtDbtInd')=='DBIT':raw=-raw
        date=_child_text(entry,'Dt') or _child_text(entry,'DtTm')[:10];details=next((x for x in entry.iter() if _local(x)=='TxDtls'),entry)
        reference=_child_text(details,'EndToEndId');description=' '.join(x.text.strip() for x in details.iter() if _local(x) in ('Ustrd','AddtlTxInf') and (x.text or '').strip())
        account=_child_text(details,'IBAN');name=_child_text(details,'Nm');external=_child_text(entry,'AcctSvcrRef') or _child_text(details,'TxId')
        rows.append({'external_id':external,'booking_date':date,'amount':round(raw,2),'currency':amount_node.attrib.get('Ccy','EUR') if amount_node is not None else 'EUR','counterparty_name':name,'counterparty_iban':account.replace(' ',''),'reference':reference,'description':description})
    if not rows:raise ValueError('Geen bankmutaties gevonden in het CAMT-bestand.')
    return rows


def parse_mt940(data):
    text=data.decode('utf-8','replace');rows=[];current=None
    for line in text.splitlines():
        if line.startswith(':61:'):
            raw=line[4:];found=re.match(r'(\d{6})(\d{4})?([CD])(?:R)?([0-9,]+)(.*)',raw)
            if not found:continue
            day=found.group(1);amount=float(found.group(4).replace(',','.'))*(-1 if found.group(3)=='D' else 1);current={'external_id':'','booking_date':f"20{day[:2]}-{day[2:4]}-{day[4:6]}",'amount':round(amount,2),'currency':'EUR','counterparty_name':'','counterparty_iban':'','reference':'','description':found.group(5).strip()};rows.append(current)
        elif line.startswith(':86:') and current:
            info=line[4:].strip();current['description']=info;current['reference']=next((m.group(1) for p in (r'(?:EREF|REMI|KENMERK)\+?([^+]+)',r'(?:FACTUUR|INVOICE)\s*[:#]?\s*([A-Z0-9._/-]+)') if (m:=re.search(p,info,re.I))),info[:140]);iban=re.search(r'\b([A-Z]{2}\d{2}[A-Z0-9]{11,30})\b',info.replace(' ',''));current['counterparty_iban']=iban.group(1) if iban else ''
    if not rows:raise ValueError('Geen bankmutaties gevonden in het MT940-bestand.')
    return rows


def _external(row):
    return row['external_id'] or hashlib.sha256('|'.join(str(row.get(k,'')) for k in ('booking_date','amount','counterparty_iban','reference','description')).encode()).hexdigest()


def _candidates(stockroom_id, amount, text):
    text=(text or '').lower();options=[]
    if amount>0:
        for inv in financial_workflow.list_invoices(stockroom_id):
            if inv['outstanding']<=0:continue
            score=(80 if str(inv['invoice_number']).lower() in text else 0)+(20 if abs(inv['outstanding']-amount)<0.01 else 0)
            if score:options.append({'type':'sales_invoice','id':inv['order_id'],'number':inv['invoice_number'],'amount':min(amount,inv['outstanding']),'open':inv['outstanding'],'score':score})
    else:
        outgoing=abs(amount)
        for inv in purchase_invoices.rows(stockroom_id):
            if inv['outstanding']<=0 or inv['paymentBlocked']:continue
            score=(80 if str(inv['invoice_number']).lower() in text else 0)+(20 if abs(inv['outstanding']-outgoing)<0.01 else 0)
            if score:options.append({'type':'purchase_invoice','id':inv['id'],'number':inv['invoice_number'],'amount':min(outgoing,inv['outstanding']),'open':inv['outstanding'],'score':score})
        for batch in payment_batches.overview(stockroom_id)['batches']:
            if batch['status']!='exported':continue
            score=(90 if batch['batch_number'].lower() in text else 0)+(10 if abs(batch['total']-outgoing)<0.01 else 0)
            if score:options.append({'type':'payment_batch','id':batch['id'],'number':batch['batch_number'],'amount':outgoing,'open':batch['total'],'score':score})
    return sorted(options,key=lambda x:x['score'],reverse=True)[:10]


def _apply(session, transaction_id, allocations, automatic=False):
    with server.db() as conn:tx=conn.execute("SELECT * FROM bank_transactions WHERE id=%s AND stockroom_id=%s FOR UPDATE",(transaction_id,session['stockroom_id'])).fetchone()
    if not tx or tx['status']!='unmatched':raise ValueError('Bankmutatie is al verwerkt of niet gevonden.')
    available=abs(float(tx['amount']));used=0
    for item in allocations:
        kind=item.get('type');target=str(item.get('id') or '');amount=round(float(item.get('amount') or 0),2)
        if amount<=0 or used+amount>available+0.01:raise ValueError('Toewijzingen zijn hoger dan de bankmutatie.')
        if kind=='sales_invoice' and tx['amount']>0:financial_workflow.record_payment(session,target,amount,f"Bankimport {tx['reference'] or tx['external_id']}")
        elif kind=='purchase_invoice' and tx['amount']<0:purchase_invoices.payment(session,{'invoice_id':target,'amount':amount})
        elif kind=='payment_batch' and tx['amount']<0:
            payment_batches.process(session,{'batch_id':target});amount=available
        elif kind not in ('bank_fee','ignore'):raise ValueError('Deze toewijzing past niet bij de richting van de betaling.')
        with server.db() as conn:conn.execute("INSERT INTO bank_allocations(id,transaction_id,target_type,target_id,amount,note,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s)",(str(uuid.uuid4()),transaction_id,kind,target,amount,'Automatisch gekoppeld' if automatic else (item.get('note') or '')[:500],session['user_id']));conn.commit()
        used+=amount
    status='matched' if used>=available-0.01 else 'partial'
    with server.db() as conn:conn.execute("UPDATE bank_transactions SET status=%s WHERE id=%s AND stockroom_id=%s",(status,transaction_id,session['stockroom_id']));conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'bank_transaction.reconciled',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':transaction_id,'automatic':automatic,'allocations':allocations})));conn.commit()
    return {'matched':True,'status':status}


def import_file(session, values):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan bankafschriften importeren.')
    try:data=base64.b64decode(values.get('file_base64') or '',validate=True)
    except Exception as exc:raise ValueError('Bankbestand is ongeldig.') from exc
    if not data or len(data)>5*1024*1024:raise ValueError('Gebruik een bankbestand van maximaal 5 MB.')
    filename=(values.get('filename') or 'bankafschrift')[:200];digest=hashlib.sha256(data).hexdigest();kind='camt053' if data.lstrip().startswith(b'<') else 'mt940';parsed=parse_camt(data) if kind=='camt053' else parse_mt940(data);import_id=str(uuid.uuid4());created=[];duplicates=0
    with server.db() as conn:
        if conn.execute("SELECT 1 FROM bank_imports WHERE stockroom_id=%s AND file_hash=%s",(session['stockroom_id'],digest)).fetchone():raise ValueError('Dit bankbestand is al geïmporteerd.')
        conn.execute("INSERT INTO bank_imports(id,stockroom_id,filename,file_hash,format,transaction_count,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s)",(import_id,session['stockroom_id'],filename,digest,kind,len(parsed),session['user_id']))
        for row in parsed:
            external=_external(row);text=f"{row['reference']} {row['description']} {row['counterparty_name']}";suggestions=_candidates(session['stockroom_id'],row['amount'],text);txid=str(uuid.uuid4())
            inserted=conn.execute("""INSERT INTO bank_transactions(id,stockroom_id,import_id,external_id,booking_date,amount,currency,counterparty_name,counterparty_iban,reference,description,suggestion)
                VALUES(%s,%s,%s,%s,%s::date,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(stockroom_id,external_id) DO NOTHING RETURNING id""",(txid,session['stockroom_id'],import_id,external,row['booking_date'],row['amount'],row['currency'],row['counterparty_name'],row['counterparty_iban'],row['reference'],row['description'],json.dumps({'candidates':suggestions}))).fetchone()
            if inserted:created.append((txid,suggestions))
            else:duplicates+=1
        conn.commit()
    auto=0
    for txid,suggestions in created:
        if suggestions and suggestions[0]['score']>=100 and (len(suggestions)==1 or suggestions[1]['score']<100):
            try:_apply(session,txid,[suggestions[0]],True);auto+=1
            except (ValueError,PermissionError):pass
    return {'imported':len(created),'duplicates':duplicates,'automaticallyMatched':auto,'format':kind}


def overview(stockroom_id):
    with server.db() as conn:
        rows=conn.execute("""SELECT t.id::text,t.booking_date,t.amount::float8,t.currency,t.counterparty_name,t.counterparty_iban,t.reference,t.description,t.status,t.suggestion,i.filename
            FROM bank_transactions t JOIN bank_imports i ON i.id=t.import_id WHERE t.stockroom_id=%s ORDER BY t.booking_date DESC,t.created_at DESC LIMIT 500""",(stockroom_id,)).fetchall()
        for row in rows:row['allocations']=conn.execute("SELECT target_type,target_id,amount::float8,note FROM bank_allocations WHERE transaction_id=%s ORDER BY created_at",(row['id'],)).fetchall()
    return {'transactions':rows}


def reconcile(session, values):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan bankmutaties verwerken.')
    try:allocations=json.loads(values.get('allocations') or '[]')
    except json.JSONDecodeError as exc:raise ValueError('Toewijzingen zijn ongeldig.') from exc
    if not allocations:raise ValueError('Voeg minimaal één toewijzing toe.')
    return _apply(session,(values.get('transaction_id') or '').strip(),allocations)
