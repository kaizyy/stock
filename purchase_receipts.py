"""Partial purchase-order receipts with reversible stock bookings."""
import json
import uuid
import base64
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import inventory_ledger
import server


def initialize():
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS purchase_receipts(
            id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
            order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,reference TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',received_by UUID REFERENCES users(id) ON DELETE SET NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),reversed_by UUID REFERENCES users(id) ON DELETE SET NULL,
            reversed_at TIMESTAMPTZ)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS purchase_receipt_lines(
            id UUID PRIMARY KEY,receipt_id UUID NOT NULL REFERENCES purchase_receipts(id) ON DELETE CASCADE,
            order_line_id UUID NOT NULL REFERENCES order_lines(id) ON DELETE RESTRICT,item_id TEXT NOT NULL,
            item_name TEXT NOT NULL,quantity NUMERIC(14,3) NOT NULL CHECK(quantity>0),
            previous_stock NUMERIC(14,3) NOT NULL,new_stock NUMERIC(14,3) NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_receipts_order ON purchase_receipts(order_id,received_at DESC)")
        conn.execute("ALTER TABLE purchase_receipts ADD COLUMN IF NOT EXISTS document_name TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE purchase_receipts ADD COLUMN IF NOT EXISTS document_mime TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE purchase_receipts ADD COLUMN IF NOT EXISTS document_data BYTEA")
        conn.execute("ALTER TABLE purchase_receipts ADD COLUMN IF NOT EXISTS unexpected_items JSONB NOT NULL DEFAULT '[]'::jsonb")
        conn.execute("ALTER TABLE purchase_receipts ADD COLUMN IF NOT EXISTS delivery_complete BOOLEAN NOT NULL DEFAULT FALSE")
        conn.execute("ALTER TABLE purchase_receipt_lines ADD COLUMN IF NOT EXISTS confirmed_quantity NUMERIC(14,3)")
        conn.execute("ALTER TABLE purchase_receipt_lines ADD COLUMN IF NOT EXISTS damaged_quantity NUMERIC(14,3) NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE purchase_receipt_lines ADD COLUMN IF NOT EXISTS usable_quantity NUMERIC(14,3) NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE purchase_receipt_lines ADD COLUMN IF NOT EXISTS discrepancy_code TEXT NOT NULL DEFAULT 'match'")
        conn.execute("ALTER TABLE purchase_receipt_lines ADD COLUMN IF NOT EXISTS discrepancy_note TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS purchase_discrepancy_actions(
            id UUID PRIMARY KEY,receipt_id UUID NOT NULL REFERENCES purchase_receipts(id) ON DELETE CASCADE,
            stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,action_type TEXT NOT NULL CHECK(action_type IN ('claim','redelivery','credit')),
            status TEXT NOT NULL DEFAULT 'open',note TEXT NOT NULL DEFAULT '',created_by UUID REFERENCES users(id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.commit()


def rows(stockroom_id, order_id):
    with server.db() as conn:
        receipts=conn.execute("""SELECT r.id::text,r.reference,r.note,r.received_at,r.reversed_at,r.document_name,(r.document_data IS NOT NULL) has_document,r.unexpected_items,r.delivery_complete,
                   u.name received_by_name,ru.name reversed_by_name
            FROM purchase_receipts r LEFT JOIN users u ON u.id=r.received_by LEFT JOIN users ru ON ru.id=r.reversed_by
            WHERE r.stockroom_id=%s AND r.order_id=%s ORDER BY r.received_at DESC""",(stockroom_id,order_id)).fetchall()
        for receipt in receipts:
            receipt['lines']=conn.execute("""SELECT item_id,item_name,quantity::float8,confirmed_quantity::float8,damaged_quantity::float8,usable_quantity::float8,discrepancy_code,discrepancy_note,previous_stock::float8,new_stock::float8
                FROM purchase_receipt_lines WHERE receipt_id=%s ORDER BY item_name""",(receipt['id'],)).fetchall()
            receipt['actions']=conn.execute("SELECT id::text,action_type,status,note,created_at FROM purchase_discrepancy_actions WHERE receipt_id=%s ORDER BY created_at",(receipt['id'],)).fetchall()
        return receipts


def _requested(raw):
    try:data=json.loads(raw or '[]')
    except json.JSONDecodeError as exc:raise ValueError('Ontvangstregels zijn ongeldig.') from exc
    result={}
    if not isinstance(data,list):raise ValueError('Ontvangstregels zijn ongeldig.')
    for row in data:
        try:line_id=str(row.get('line_id') or '').strip();quantity=float(row.get('quantity') or 0);damaged=float(row.get('damaged_quantity') or 0)
        except (AttributeError,TypeError,ValueError):raise ValueError('Controleer de ontvangen aantallen.')
        if damaged<0 or damaged>quantity:raise ValueError('Beschadigd aantal kan niet hoger zijn dan ontvangen.')
        if line_id and quantity>0:result[line_id]={'quantity':quantity,'damaged':damaged,'note':str(row.get('note') or '').strip()[:500]}
    if not result:raise ValueError('Vul minimaal één ontvangen aantal in.')
    return result


def receive(session, values):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om inkoop te ontvangen.')
    order_id=(values.get('order_id') or '').strip();requested=_requested(values.get('lines_json'))
    reference=(values.get('reference') or '').strip()[:120];note=(values.get('note') or '').strip()[:1000];complete=str(values.get('delivery_complete') or '') in ('1','true','on')
    try:unexpected=json.loads(values.get('unexpected_items_json') or '[]')
    except json.JSONDecodeError:raise ValueError('Onverwachte artikelen zijn ongeldig.')
    unexpected=[{'barcode':str(row.get('barcode') or '')[:100],'quantity':max(0,float(row.get('quantity') or 0)),'note':str(row.get('note') or '')[:300]} for row in unexpected[:25] if isinstance(row,dict)]
    document_name=(values.get('document_name') or '').strip()[:200];document_mime=(values.get('document_mime') or '').strip()[:100];document_data=None
    if values.get('document_base64'):
        try:document_data=base64.b64decode(values['document_base64'],validate=True)
        except Exception as exc:raise ValueError('Pakbonbijlage is ongeldig.') from exc
        if len(document_data)>5*1024*1024:raise ValueError('Pakbonbijlage mag maximaal 5 MB zijn.')
        if document_mime not in ('application/pdf','image/jpeg','image/png'):raise ValueError('Gebruik een PDF-, JPG- of PNG-pakbon.')
    with server.db() as conn:
        order=conn.execute("SELECT id,status,relation_name,reference FROM orders WHERE id=%s AND stockroom_id=%s AND order_type='purchase' FOR UPDATE",(order_id,session['stockroom_id'])).fetchone()
        if not order:raise PermissionError('Inkooporder niet gevonden.')
        if order['status'] not in ('ordered','partial'):raise ValueError('Zet de order eerst op Besteld voordat je een ontvangst boekt.')
        lines=conn.execute("""SELECT id::text,item_id,item_name,quantity::float8,fulfilled_quantity::float8,supplier_cancelled_quantity::float8,unit_price::float8
            FROM order_lines WHERE order_id=%s ORDER BY created_at,id FOR UPDATE""",(order_id,)).fetchall()
        line_map={line['id']:line for line in lines};room=conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE",(session['stockroom_id'],)).fetchone();state=room['state']
        receipt_id=str(uuid.uuid4());changes=[]
        conn.execute("""INSERT INTO purchase_receipts(id,stockroom_id,order_id,reference,note,received_by,document_name,document_mime,document_data,unexpected_items,delivery_complete)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",(receipt_id,session['stockroom_id'],order_id,reference,note,session['user_id'],document_name,document_mime,document_data,json.dumps(unexpected),complete))
        for line_id,received in requested.items():
            line=line_map.get(line_id)
            if not line:raise ValueError('Een orderregel bestaat niet meer.')
            quantity=received['quantity'];damaged=received['damaged'];usable=quantity-damaged;remaining=float(line['quantity'])-float(line['fulfilled_quantity'])-float(line['supplier_cancelled_quantity']);confirmed=None
            response=conn.execute("SELECT available_quantity::float8 FROM supplier_portal_line_responses WHERE order_line_id=%s",(line_id,)).fetchone();confirmed=float(response['available_quantity']) if response else None
            code='over' if quantity>remaining+0.0005 else 'damaged' if damaged>0 else 'short' if complete and quantity<remaining-0.0005 else 'match'
            item=next((item for item in state.get('items',[]) if str(item.get('id'))==str(line['item_id'])),None)
            if not item:raise ValueError(f"Artikel bestaat niet meer: {line['item_name']}")
            previous=float(item.get('stock') or 0);new_stock=previous+usable;item['stock']=new_stock;item['buy']=float(line['unit_price'])
            conn.execute("""INSERT INTO purchase_receipt_lines(id,receipt_id,order_line_id,item_id,item_name,quantity,confirmed_quantity,damaged_quantity,usable_quantity,discrepancy_code,discrepancy_note,previous_stock,new_stock)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",(str(uuid.uuid4()),receipt_id,line_id,line['item_id'],line['item_name'],quantity,confirmed,damaged,usable,code,received['note'],previous,new_stock))
            conn.execute("UPDATE order_lines SET fulfilled_quantity=fulfilled_quantity+%s WHERE id=%s",(quantity,line_id))
            if usable>0:state.setdefault('transactions',[]).append({'id':str(uuid.uuid4()),'type':'incoming','itemId':str(line['item_id']),'qty':usable,'price':float(line['unit_price']),'salePrice':float(item.get('sell') or 0),'party':order['relation_name'],'done':True,'paid':False,'date':server.datetime.now().isoformat(timespec='seconds'),'orderId':str(order_id),'receiptId':receipt_id,'reference':reference})
            changes.append({'itemId':str(line['item_id']),'quantity':quantity,'usable':usable,'damaged':damaged,'discrepancy':code})
        inventory_ledger.set_context(conn,'purchase_order_receipt',reference or receipt_id)
        conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s",(json.dumps(state,ensure_ascii=False),session['stockroom_id']))
        remaining=conn.execute("SELECT COALESCE(SUM(GREATEST(0,quantity-fulfilled_quantity-supplier_cancelled_quantity)),0)::float8 remaining FROM order_lines WHERE order_id=%s",(order_id,)).fetchone()['remaining']
        status='received' if remaining<=0.0005 else 'partial';conn.execute("UPDATE orders SET status=%s,inventory_booked_at=COALESCE(inventory_booked_at,NOW()),updated_at=NOW() WHERE id=%s",(status,order_id))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase.received',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'orderId':order_id,'receiptId':receipt_id,'reference':reference,'changes':changes,'status':status})))
        conn.commit()
    return {'receiptId':receipt_id,'status':status,'remaining':remaining,'discrepancies':sum(1 for change in changes if change['discrepancy']!='match')+len(unexpected)}


def reverse(session, values):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om een ontvangst terug te draaien.')
    receipt_id=(values.get('receipt_id') or '').strip()
    with server.db() as conn:
        receipt=conn.execute("SELECT id,order_id,reference,reversed_at FROM purchase_receipts WHERE id=%s AND stockroom_id=%s FOR UPDATE",(receipt_id,session['stockroom_id'])).fetchone()
        if not receipt:raise ValueError('Ontvangst niet gevonden.')
        if receipt['reversed_at']:raise ValueError('Deze ontvangst is al teruggedraaid.')
        lines=conn.execute("SELECT * FROM purchase_receipt_lines WHERE receipt_id=%s ORDER BY id",(receipt_id,)).fetchall();room=conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE",(session['stockroom_id'],)).fetchone();state=room['state']
        for line in lines:
            item=next((item for item in state.get('items',[]) if str(item.get('id'))==str(line['item_id'])),None);quantity=float(line['quantity']);usable=float(line['usable_quantity'] if line['usable_quantity'] is not None else line['quantity']);stock=float((item or {}).get('stock') or 0)
            if not item or stock+0.0005<usable:raise ValueError(f"Ontvangst kan niet terug: onvoldoende voorraad van {line['item_name']}.")
            item['stock']=stock-usable
            conn.execute("UPDATE order_lines SET fulfilled_quantity=GREATEST(0,fulfilled_quantity-%s) WHERE id=%s",(quantity,line['order_line_id']))
        state['transactions']=[tx for tx in state.get('transactions',[]) if str(tx.get('receiptId') or '')!=receipt_id]
        inventory_ledger.set_context(conn,'purchase_receipt_reversed',receipt['reference'] or receipt_id)
        conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s",(json.dumps(state,ensure_ascii=False),session['stockroom_id']))
        fulfilled=conn.execute("SELECT COALESCE(SUM(fulfilled_quantity),0)::float8 done,COALESCE(SUM(GREATEST(0,quantity-fulfilled_quantity-supplier_cancelled_quantity)),0)::float8 remaining FROM order_lines WHERE order_id=%s",(receipt['order_id'],)).fetchone();status='ordered' if fulfilled['done']<=0.0005 else 'partial' if fulfilled['remaining']>0.0005 else 'received'
        conn.execute("UPDATE purchase_receipts SET reversed_at=NOW(),reversed_by=%s WHERE id=%s",(session['user_id'],receipt_id));conn.execute("UPDATE orders SET status=%s,inventory_booked_at=CASE WHEN %s='ordered' THEN NULL ELSE inventory_booked_at END,updated_at=NOW() WHERE id=%s",(status,status,receipt['order_id']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase.receipt_reversed',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'orderId':str(receipt['order_id']),'receiptId':receipt_id,'status':status})));conn.commit()
    return {'reversed':True,'status':status}


def create_discrepancy_action(session, values):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om een leveranciersactie te starten.')
    receipt_id=(values.get('receipt_id') or '').strip();action=(values.get('action_type') or '').strip();note=(values.get('note') or '').strip()[:1000]
    if action not in ('claim','redelivery','credit'):raise ValueError('Kies claim, nalevering of creditverzoek.')
    with server.db() as conn:
        receipt=conn.execute("SELECT id,order_id FROM purchase_receipts WHERE id=%s AND stockroom_id=%s AND reversed_at IS NULL",(receipt_id,session['stockroom_id'])).fetchone()
        if not receipt:raise ValueError('Ontvangst niet gevonden.')
        action_id=str(uuid.uuid4());conn.execute("INSERT INTO purchase_discrepancy_actions(id,receipt_id,stockroom_id,action_type,note,created_by) VALUES(%s,%s,%s,%s,%s,%s)",(action_id,receipt_id,session['stockroom_id'],action,note,session['user_id']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase.discrepancy_action_created',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':str(receipt['order_id']),'receiptId':receipt_id,'actionId':action_id,'type':action})));conn.commit()
    return {'created':True,'id':action_id,'type':action}


def attachment(stockroom_id, receipt_id):
    with server.db() as conn:row=conn.execute("SELECT document_data,document_name,document_mime FROM purchase_receipts WHERE id=%s AND stockroom_id=%s",(receipt_id,stockroom_id)).fetchone()
    if not row or not row['document_data']:raise PermissionError('Geen pakbonbijlage gevonden.')
    return bytes(row['document_data']),row['document_name'] or 'pakbon',row['document_mime'] or 'application/octet-stream'


def discrepancy_pdf(stockroom_id, receipt_id):
    with server.db() as conn:
        receipt=conn.execute("""SELECT r.reference,r.received_at,r.note,r.unexpected_items,o.order_number,o.reference order_reference,o.relation_name
            FROM purchase_receipts r JOIN orders o ON o.id=r.order_id WHERE r.id=%s AND r.stockroom_id=%s""",(receipt_id,stockroom_id)).fetchone()
        if not receipt:raise PermissionError('Ontvangst niet gevonden.')
        lines=conn.execute("SELECT item_name,quantity::float8,confirmed_quantity::float8,damaged_quantity::float8,usable_quantity::float8,discrepancy_code,discrepancy_note FROM purchase_receipt_lines WHERE receipt_id=%s ORDER BY item_name",(receipt_id,)).fetchall()
    stream=BytesIO();pdf=canvas.Canvas(stream,pagesize=A4);width,height=A4;y=height-55;pdf.setFont('Helvetica-Bold',16);pdf.drawString(45,y,'Afwijkingsrapport goederenontvangst');y-=24;pdf.setFont('Helvetica',10)
    for text in (f"Order: {receipt['order_number'] or receipt['order_reference'] or '-'}",f"Leverancier: {receipt['relation_name'] or '-'}",f"Pakbon: {receipt['reference'] or '-'}",f"Ontvangen: {receipt['received_at'].strftime('%d-%m-%Y %H:%M')}"):pdf.drawString(45,y,text);y-=15
    y-=10
    for line in lines:
        pdf.setFont('Helvetica-Bold',10);pdf.drawString(45,y,str(line['item_name'])[:55]);y-=14;pdf.setFont('Helvetica',9);pdf.drawString(55,y,f"Ontvangen {line['quantity']:g} · bruikbaar {line['usable_quantity']:g} · beschadigd {line['damaged_quantity']:g} · {line['discrepancy_code']}");y-=14
        if line['discrepancy_note']:pdf.drawString(55,y,str(line['discrepancy_note'])[:90]);y-=14
        if y<70:pdf.showPage();y=height-55
    for item in receipt['unexpected_items'] or []:pdf.drawString(45,y,f"Onverwacht artikel: {item.get('barcode') or '-'} · {float(item.get('quantity') or 0):g}");y-=14
    pdf.save();return stream.getvalue(),f"afwijkingsrapport-{receipt_id}.pdf"
