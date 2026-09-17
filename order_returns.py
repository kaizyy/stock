"""Returns linked to delivered purchase and sales orders."""
import json
import io
import uuid
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

import financial_workflow
import inventory_ledger
import order_management
import server


def initialize():
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS order_returns(
            id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
            order_id UUID NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,return_type TEXT NOT NULL CHECK(return_type IN ('sales','purchase')),
            status TEXT NOT NULL CHECK(status IN ('registered','processed','cancelled')),reference TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',
            credit_amount NUMERIC(14,2) NOT NULL DEFAULT 0,credit_note_id UUID,created_by UUID REFERENCES users(id) ON DELETE SET NULL,
            processed_by UUID REFERENCES users(id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),processed_at TIMESTAMPTZ,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS order_return_lines(
            id UUID PRIMARY KEY,return_id UUID NOT NULL REFERENCES order_returns(id) ON DELETE CASCADE,
            order_line_id UUID NOT NULL REFERENCES order_lines(id) ON DELETE RESTRICT,item_id TEXT NOT NULL,item_name TEXT NOT NULL,
            quantity NUMERIC(14,3) NOT NULL CHECK(quantity>0),unit_price NUMERIC(14,4) NOT NULL CHECK(unit_price>=0))""")
        conn.execute("ALTER TABLE order_returns ADD COLUMN IF NOT EXISTS rma_number TEXT")
        conn.execute("ALTER TABLE order_returns ADD COLUMN IF NOT EXISTS expected_refund NUMERIC(14,2) NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE order_returns ADD COLUMN IF NOT EXISTS received_refund NUMERIC(14,2) NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE order_returns ADD COLUMN IF NOT EXISTS claim_status TEXT NOT NULL DEFAULT 'none'")
        conn.execute("ALTER TABLE order_returns ADD COLUMN IF NOT EXISTS claim_reference TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE order_returns ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE order_returns ADD COLUMN IF NOT EXISTS settled_at TIMESTAMPTZ")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_order_returns_rma ON order_returns(stockroom_id,rma_number) WHERE rma_number IS NOT NULL")
        conn.execute("""CREATE TABLE IF NOT EXISTS return_sequences(
            stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,year INTEGER NOT NULL,
            last_value INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(stockroom_id,year))""")
        for row in conn.execute("SELECT id::text,stockroom_id::text FROM order_returns WHERE rma_number IS NULL ORDER BY created_at,id").fetchall():
            conn.execute("UPDATE order_returns SET rma_number=%s WHERE id=%s",(_next_rma(conn,row['stockroom_id']),row['id']))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_returns_order ON order_returns(order_id,created_at DESC)")
        conn.commit()


def _lines(conn, order_id):
    return conn.execute("""SELECT l.id::text,l.item_id,l.item_name,l.sku,l.quantity::float8,l.fulfilled_quantity::float8,l.unit_price::float8,
               COALESCE((SELECT SUM(rl.quantity) FROM order_return_lines rl JOIN order_returns r ON r.id=rl.return_id WHERE rl.order_line_id=l.id AND r.status<>'cancelled'),0)::float8 returned_quantity
        FROM order_lines l WHERE l.order_id=%s ORDER BY l.created_at,l.id""",(order_id,)).fetchall()


def overview(stockroom_id, order_id):
    with server.db() as conn:
        order=conn.execute("SELECT id,order_type FROM orders WHERE id=%s AND stockroom_id=%s",(order_id,stockroom_id)).fetchone()
        if not order:raise PermissionError('Order niet gevonden.')
        lines=_lines(conn,order_id)
        for line in lines:line['available_quantity']=max(0,float(line['fulfilled_quantity'])-float(line['returned_quantity']))
        returns=conn.execute("""SELECT r.id::text,r.rma_number,r.return_type,r.status,r.reference,r.reason,r.credit_amount::float8,r.credit_note_id::text,
                   r.expected_refund::float8,r.received_refund::float8,r.claim_status,r.claim_reference,r.claimed_at,r.settled_at,r.created_at,r.processed_at,
                   u.name created_by_name,p.name processed_by_name
            FROM order_returns r LEFT JOIN users u ON u.id=r.created_by LEFT JOIN users p ON p.id=r.processed_by
            WHERE r.stockroom_id=%s AND r.order_id=%s ORDER BY r.created_at DESC""",(stockroom_id,order_id)).fetchall()
        for result in returns:result['lines']=conn.execute("SELECT item_id,item_name,quantity::float8,unit_price::float8 FROM order_return_lines WHERE return_id=%s ORDER BY item_name",(result['id'],)).fetchall()
    return {'orderType':order['order_type'],'lines':lines,'returns':returns}


def _parse(raw):
    try:data=json.loads(raw or '[]')
    except json.JSONDecodeError as exc:raise ValueError('Retourregels zijn ongeldig.') from exc
    result={}
    if not isinstance(data,list):raise ValueError('Retourregels zijn ongeldig.')
    for row in data:
        try:line_id=str(row.get('line_id') or '').strip();quantity=float(row.get('quantity') or 0)
        except (AttributeError,TypeError,ValueError):raise ValueError('Controleer de retouraantallen.')
        if line_id and quantity>0:result[line_id]=quantity
    if not result:raise ValueError('Selecteer minimaal één retourregel.')
    return result


def _require_access(session, return_type, write=True):
    capability = ('write_' if write else 'read_') + return_type
    if not order_management.allowed(session.get('role'), capability):
        raise PermissionError('Geen rechten voor deze retour.')


def _next_rma(conn, stockroom_id):
    year=datetime.now().year
    row=conn.execute("""INSERT INTO return_sequences(stockroom_id,year,last_value) VALUES(%s,%s,1)
        ON CONFLICT(stockroom_id,year) DO UPDATE SET last_value=return_sequences.last_value+1 RETURNING last_value""",(stockroom_id,year)).fetchone()
    return f"RMA-{year}-{int(row['last_value']):06d}"


def create(session, values):
    order_id=(values.get('order_id') or '').strip();requested=_parse(values.get('lines_json'));reference=(values.get('reference') or '').strip()[:120];reason=(values.get('reason') or '').strip()[:1000]
    with server.db() as conn:
        order=conn.execute("SELECT id,order_type,status FROM orders WHERE id=%s AND stockroom_id=%s FOR UPDATE",(order_id,session['stockroom_id'])).fetchone()
        if not order:raise PermissionError('Order niet gevonden.')
        _require_access(session,order['order_type'])
        lines={line['id']:line for line in _lines(conn,order_id)};selected=[]
        for line_id,quantity in requested.items():
            line=lines.get(line_id);available=float(line['fulfilled_quantity'])-float(line['returned_quantity']) if line else 0
            if not line or quantity>available+0.0005:raise ValueError(f"Retouraantal is hoger dan geleverd voor {(line or {}).get('item_name','deze regel')}.")
            selected.append((line,quantity))
        return_id=str(uuid.uuid4());rma_number=_next_rma(conn,session['stockroom_id']);subtotal=sum(quantity*float(line['unit_price']) for line,quantity in selected)
        vat=conn.execute("SELECT vat_percent::float8 FROM invoice_documents WHERE order_id=%s AND stockroom_id=%s AND deleted_at IS NULL",(order_id,session['stockroom_id'])).fetchone();credit=round(subtotal*(1+float((vat or {}).get('vat_percent') or 0)/100),2) if order['order_type']=='sales' else 0
        expected_refund=round(subtotal,2) if order['order_type']=='purchase' else 0
        conn.execute("INSERT INTO order_returns(id,stockroom_id,order_id,rma_number,return_type,status,reference,reason,credit_amount,expected_refund,created_by) VALUES(%s,%s,%s,%s,%s,'registered',%s,%s,%s,%s,%s)",(return_id,session['stockroom_id'],order_id,rma_number,order['order_type'],reference,reason,credit,expected_refund,session['user_id']))
        for line,quantity in selected:conn.execute("INSERT INTO order_return_lines(id,return_id,order_line_id,item_id,item_name,quantity,unit_price) VALUES(%s,%s,%s,%s,%s,%s,%s)",(str(uuid.uuid4()),return_id,line['id'],line['item_id'],line['item_name'],quantity,line['unit_price']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'return.registered',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'returnId':return_id,'orderId':order_id,'type':order['order_type'],'creditSuggestion':credit})));conn.commit()
    return {'id':return_id,'rmaNumber':rma_number,'creditAmount':credit,'expectedRefund':expected_refund}


def label_pdf(session, return_id):
    with server.db() as conn:
        result=conn.execute("""SELECT r.id::text,r.rma_number,r.return_type,r.status,r.reference,r.reason,r.created_at,
                   o.order_number,o.relation_name,o.relation_id,s.name stockroom_name
            FROM order_returns r JOIN orders o ON o.id=r.order_id JOIN stockrooms s ON s.id=r.stockroom_id
            WHERE r.id=%s AND r.stockroom_id=%s""",(return_id,session['stockroom_id'])).fetchone()
        if not result:raise PermissionError('Retour niet gevonden.')
        _require_access(session,result['return_type'],False)
        table='customers' if result['return_type']=='sales' else 'suppliers'
        relation=conn.execute(f"SELECT address,email,phone FROM {table} WHERE id=%s AND stockroom_id=%s",(result['relation_id'],session['stockroom_id'])).fetchone() if result['relation_id'] else None
        lines=conn.execute("SELECT item_name,quantity::float8 FROM order_return_lines WHERE return_id=%s ORDER BY item_name",(return_id,)).fetchall()
    buf=io.BytesIO();pdf=canvas.Canvas(buf,pagesize=A4);pdf.setTitle(result['rma_number'] or 'Retourlabel')
    pdf.setFont('Helvetica-Bold',22);pdf.drawString(18*mm,276*mm,'RETOURLABEL');pdf.setFont('Helvetica-Bold',16);pdf.drawRightString(192*mm,276*mm,result['rma_number'] or '')
    pdf.setFont('Helvetica-Bold',11);pdf.drawString(18*mm,258*mm,'Retour naar / verwerken bij');pdf.setFont('Helvetica',11)
    y=250*mm
    for value in (result['stockroom_name'],):pdf.drawString(18*mm,y,str(value or ''));y-=7*mm
    pdf.setFont('Helvetica-Bold',11);pdf.drawString(110*mm,258*mm,'Afzender / relatie');pdf.setFont('Helvetica',10);y2=250*mm
    for value in (result['relation_name'],(relation or {}).get('address'),(relation or {}).get('email'),(relation or {}).get('phone')):
        if value:pdf.drawString(110*mm,y2,str(value)[:48]);y2-=6*mm
    pdf.line(18*mm,220*mm,192*mm,220*mm);pdf.setFont('Helvetica',10);y=210*mm
    for label,value in [('RMA-nummer',result['rma_number']),('Order',result['order_number']),('Aangemeld',result['created_at'].strftime('%d-%m-%Y')),('Referentie',result['reference'] or '—')]:pdf.drawString(18*mm,y,f'{label}: {value or "—"}');y-=7*mm
    y=190*mm;pdf.setFont('Helvetica-Bold',11);pdf.drawString(18*mm,y,'Inhoud retour');y-=8*mm;pdf.setFont('Helvetica',10)
    for line in lines:pdf.drawString(18*mm,y,f"{float(line['quantity']):g} × {line['item_name'][:70]}");y-=7*mm
    if result['reason']:y-=4*mm;pdf.setFont('Helvetica-Bold',10);pdf.drawString(18*mm,y,'Reden');y-=6*mm;pdf.setFont('Helvetica',9);pdf.drawString(18*mm,y,result['reason'][:110])
    pdf.setFont('Helvetica',8);pdf.drawString(18*mm,16*mm,'Bewaar dit label bij de retourzending zodat de retour aan de juiste order wordt gekoppeld.')
    pdf.save();return buf.getvalue(),f"{result['rma_number'] or return_id}.pdf"


def process(session, values):
    return_id=(values.get('return_id') or '').strip()
    with server.db() as conn:
        result=conn.execute("SELECT r.*,o.relation_name FROM order_returns r JOIN orders o ON o.id=r.order_id WHERE r.id=%s AND r.stockroom_id=%s FOR UPDATE",(return_id,session['stockroom_id'])).fetchone()
        if not result or result['status']!='registered':raise ValueError('Alleen een aangemelde retour kan worden verwerkt.')
        _require_access(session,result['return_type'])
        lines=conn.execute("SELECT * FROM order_return_lines WHERE return_id=%s ORDER BY id",(return_id,)).fetchall();room=conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE",(session['stockroom_id'],)).fetchone();state=room['state']
        for line in lines:
            item=next((item for item in state.get('items',[]) if str(item.get('id'))==str(line['item_id'])),None);quantity=float(line['quantity'])
            if not item:raise ValueError(f"Artikel bestaat niet meer: {line['item_name']}")
            stock=float(item.get('stock') or 0)
            if result['return_type']=='purchase' and stock+0.0005<quantity:raise ValueError(f"Onvoldoende voorraad voor inkoopretour van {line['item_name']}.")
            item['stock']=stock+quantity if result['return_type']=='sales' else stock-quantity
            state.setdefault('transactions',[]).append({'id':str(uuid.uuid4()),'type':'outgoing' if result['return_type']=='sales' else 'incoming','itemId':str(line['item_id']),'qty':-quantity,'price':float(line['unit_price']),'party':result['relation_name'] or 'Retour','done':True,'paid':False,'date':server.datetime.now().isoformat(timespec='seconds'),'orderId':str(result['order_id']),'returnId':return_id,'isReturn':True,'reference':result['reference']})
        inventory_ledger.set_context(conn,'linked_sales_return' if result['return_type']=='sales' else 'linked_purchase_return',result['reference'] or return_id)
        conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s",(json.dumps(state,ensure_ascii=False),session['stockroom_id']))
        claim_sql=",claim_status='open',claimed_at=NOW()" if result['return_type']=='purchase' else ''
        conn.execute(f"UPDATE order_returns SET status='processed',processed_by=%s,processed_at=NOW(),updated_at=NOW(){claim_sql} WHERE id=%s",(session['user_id'],return_id));conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'return.processed',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'returnId':return_id,'orderId':str(result['order_id']),'type':result['return_type']})));conn.commit()
    return {'processed':True,'creditAmount':float(result['credit_amount'])}


def change(session, values, action):
    return_id=(values.get('return_id') or '').strip()
    with server.db() as conn:
        result=conn.execute("SELECT * FROM order_returns WHERE id=%s AND stockroom_id=%s FOR UPDATE",(return_id,session['stockroom_id'])).fetchone()
        if not result:raise ValueError('Retour niet gevonden.')
        _require_access(session,result['return_type'])
        if action=='cancel':
            if result['status']!='registered':raise ValueError('Alleen een aangemelde retour kan worden geannuleerd.')
            conn.execute("UPDATE order_returns SET status='cancelled',updated_at=NOW() WHERE id=%s",(return_id,));event='return.cancelled'
        else:
            if result['status']!='processed':raise ValueError('Alleen een verwerkte retour kan worden teruggedraaid.')
            if result['credit_note_id']:raise ValueError('Draai eerst de gekoppelde creditnota terug.')
            if result['return_type']=='purchase' and float(result['received_refund'] or 0)>0:raise ValueError('De retour kan niet terug zolang er een terugbetaling op de leveranciersclaim staat.')
            lines=conn.execute("SELECT * FROM order_return_lines WHERE return_id=%s",(return_id,)).fetchall();room=conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE",(session['stockroom_id'],)).fetchone();state=room['state']
            for line in lines:
                item=next((item for item in state.get('items',[]) if str(item.get('id'))==str(line['item_id'])),None);quantity=float(line['quantity']);stock=float((item or {}).get('stock') or 0)
                if not item or (result['return_type']=='sales' and stock+0.0005<quantity):raise ValueError(f"Retour kan niet terug: onvoldoende voorraad van {line['item_name']}.")
                item['stock']=stock-quantity if result['return_type']=='sales' else stock+quantity
            state['transactions']=[tx for tx in state.get('transactions',[]) if str(tx.get('returnId') or '')!=return_id];inventory_ledger.set_context(conn,'linked_return_reversed',result['reference'] or return_id);conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s",(json.dumps(state,ensure_ascii=False),session['stockroom_id']));conn.execute("UPDATE order_returns SET status='registered',processed_by=NULL,processed_at=NULL,claim_status='none',claimed_at=NULL,settled_at=NULL,updated_at=NOW() WHERE id=%s",(return_id,));event='return.reversed'
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",(session['stockroom_id'],session['user_id'],event,json.dumps({'returnId':return_id,'orderId':str(result['order_id'])})));conn.commit()
    return {'updated':True}


def create_credit(session, values):
    return_id=(values.get('return_id') or '').strip()
    with server.db() as conn:result=conn.execute("SELECT id,order_id,status,return_type,credit_amount,credit_note_id,reason FROM order_returns WHERE id=%s AND stockroom_id=%s",(return_id,session['stockroom_id'])).fetchone()
    if not result or result['return_type']!='sales' or result['status']!='processed':raise ValueError('Verwerk de verkoopretour eerst.')
    _require_access(session,'sales')
    if result['credit_note_id']:raise ValueError('Voor deze retour bestaat al een creditnota.')
    credit=financial_workflow.create_credit(session,str(result['order_id']),float(result['credit_amount']),f"Retour {return_id}: {result['reason']}")
    with server.db() as conn:conn.execute("UPDATE order_returns SET credit_note_id=%s,updated_at=NOW() WHERE id=%s AND credit_note_id IS NULL",(credit['id'],return_id));conn.commit()
    return credit


def update_claim(session, values):
    return_id=(values.get('return_id') or '').strip();reference=(values.get('claim_reference') or '').strip()[:120]
    try:expected=round(float(values.get('expected_refund') or 0),2)
    except (TypeError,ValueError):raise ValueError('Controleer het verwachte terugbetalingsbedrag.')
    if expected<0:raise ValueError('Het verwachte bedrag kan niet negatief zijn.')
    with server.db() as conn:
        result=conn.execute("SELECT id,return_type,status,received_refund::float8 FROM order_returns WHERE id=%s AND stockroom_id=%s FOR UPDATE",(return_id,session['stockroom_id'])).fetchone()
        if not result or result['return_type']!='purchase' or result['status']!='processed':raise ValueError('Alleen een verwerkte inkoopretour heeft een leveranciersclaim.')
        _require_access(session,'purchase')
        if expected+0.005<float(result['received_refund']):raise ValueError('Het verwachte bedrag is lager dan wat al ontvangen is.')
        status='settled' if expected<=float(result['received_refund'])+0.005 else ('partial' if float(result['received_refund'])>0 else 'open')
        conn.execute("UPDATE order_returns SET expected_refund=%s,claim_reference=%s,claim_status=%s,settled_at=CASE WHEN %s='settled' THEN NOW() ELSE NULL END,updated_at=NOW() WHERE id=%s",(expected,reference,status,status,return_id))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'return.claim_updated',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'returnId':return_id,'expectedRefund':expected,'reference':reference,'status':status})));conn.commit()
    return {'updated':True,'claimStatus':status}


def record_refund(session, values):
    return_id=(values.get('return_id') or '').strip();note=(values.get('note') or '').strip()[:500]
    try:amount=round(float(values.get('amount') or 0),2)
    except (TypeError,ValueError):raise ValueError('Controleer het ontvangen bedrag.')
    if amount<=0:raise ValueError('Vul een ontvangen bedrag groter dan nul in.')
    with server.db() as conn:
        result=conn.execute("SELECT id,return_type,status,claim_status,expected_refund::float8,received_refund::float8 FROM order_returns WHERE id=%s AND stockroom_id=%s FOR UPDATE",(return_id,session['stockroom_id'])).fetchone()
        if not result or result['return_type']!='purchase' or result['status']!='processed' or result['claim_status'] not in ('open','partial'):raise ValueError('Deze leveranciersclaim staat niet open.')
        _require_access(session,'purchase')
        total=round(float(result['received_refund'])+amount,2);expected=float(result['expected_refund'])
        if total>expected+0.005:raise ValueError('De terugbetaling is hoger dan het verwachte claimbedrag.')
        status='settled' if total>=expected-0.005 else 'partial'
        conn.execute("UPDATE order_returns SET received_refund=%s,claim_status=%s,settled_at=CASE WHEN %s='settled' THEN NOW() ELSE NULL END,updated_at=NOW() WHERE id=%s",(total,status,status,return_id))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'return.refund_received',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'returnId':return_id,'amount':amount,'totalReceived':total,'note':note,'status':status})));conn.commit()
    return {'receivedRefund':total,'claimStatus':status}
