"""Partial purchase-order receipts with reversible stock bookings."""
import json
import uuid

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
        conn.commit()


def rows(stockroom_id, order_id):
    with server.db() as conn:
        receipts=conn.execute("""SELECT r.id::text,r.reference,r.note,r.received_at,r.reversed_at,
                   u.name received_by_name,ru.name reversed_by_name
            FROM purchase_receipts r LEFT JOIN users u ON u.id=r.received_by LEFT JOIN users ru ON ru.id=r.reversed_by
            WHERE r.stockroom_id=%s AND r.order_id=%s ORDER BY r.received_at DESC""",(stockroom_id,order_id)).fetchall()
        for receipt in receipts:
            receipt['lines']=conn.execute("""SELECT item_id,item_name,quantity::float8,previous_stock::float8,new_stock::float8
                FROM purchase_receipt_lines WHERE receipt_id=%s ORDER BY item_name""",(receipt['id'],)).fetchall()
        return receipts


def _requested(raw):
    try:data=json.loads(raw or '[]')
    except json.JSONDecodeError as exc:raise ValueError('Ontvangstregels zijn ongeldig.') from exc
    result={}
    if not isinstance(data,list):raise ValueError('Ontvangstregels zijn ongeldig.')
    for row in data:
        try:line_id=str(row.get('line_id') or '').strip();quantity=float(row.get('quantity') or 0)
        except (AttributeError,TypeError,ValueError):raise ValueError('Controleer de ontvangen aantallen.')
        if line_id and quantity>0:result[line_id]=quantity
    if not result:raise ValueError('Vul minimaal één ontvangen aantal in.')
    return result


def receive(session, values):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om inkoop te ontvangen.')
    order_id=(values.get('order_id') or '').strip();requested=_requested(values.get('lines_json'))
    reference=(values.get('reference') or '').strip()[:120];note=(values.get('note') or '').strip()[:1000]
    with server.db() as conn:
        order=conn.execute("SELECT id,status,relation_name,reference FROM orders WHERE id=%s AND stockroom_id=%s AND order_type='purchase' FOR UPDATE",(order_id,session['stockroom_id'])).fetchone()
        if not order:raise PermissionError('Inkooporder niet gevonden.')
        if order['status'] not in ('ordered','partial'):raise ValueError('Zet de order eerst op Besteld voordat je een ontvangst boekt.')
        lines=conn.execute("""SELECT id::text,item_id,item_name,quantity::float8,fulfilled_quantity::float8,unit_price::float8
            FROM order_lines WHERE order_id=%s ORDER BY created_at,id FOR UPDATE""",(order_id,)).fetchall()
        line_map={line['id']:line for line in lines};room=conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE",(session['stockroom_id'],)).fetchone();state=room['state']
        receipt_id=str(uuid.uuid4());changes=[]
        conn.execute("INSERT INTO purchase_receipts(id,stockroom_id,order_id,reference,note,received_by) VALUES(%s,%s,%s,%s,%s,%s)",(receipt_id,session['stockroom_id'],order_id,reference,note,session['user_id']))
        for line_id,quantity in requested.items():
            line=line_map.get(line_id)
            if not line:raise ValueError('Een orderregel bestaat niet meer.')
            remaining=float(line['quantity'])-float(line['fulfilled_quantity'])
            if quantity>remaining+0.0005:raise ValueError(f"Van {line['item_name']} staan nog {remaining:g} open.")
            item=next((item for item in state.get('items',[]) if str(item.get('id'))==str(line['item_id'])),None)
            if not item:raise ValueError(f"Artikel bestaat niet meer: {line['item_name']}")
            previous=float(item.get('stock') or 0);new_stock=previous+quantity;item['stock']=new_stock;item['buy']=float(line['unit_price'])
            conn.execute("INSERT INTO purchase_receipt_lines(id,receipt_id,order_line_id,item_id,item_name,quantity,previous_stock,new_stock) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",(str(uuid.uuid4()),receipt_id,line_id,line['item_id'],line['item_name'],quantity,previous,new_stock))
            conn.execute("UPDATE order_lines SET fulfilled_quantity=fulfilled_quantity+%s WHERE id=%s",(quantity,line_id))
            state.setdefault('transactions',[]).append({'id':str(uuid.uuid4()),'type':'incoming','itemId':str(line['item_id']),'qty':quantity,'price':float(line['unit_price']),'salePrice':float(item.get('sell') or 0),'party':order['relation_name'],'done':True,'paid':False,'date':server.datetime.now().isoformat(timespec='seconds'),'orderId':str(order_id),'receiptId':receipt_id,'reference':reference})
            changes.append({'itemId':str(line['item_id']),'quantity':quantity})
        inventory_ledger.set_context(conn,'purchase_order_receipt',reference or receipt_id)
        conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s",(json.dumps(state,ensure_ascii=False),session['stockroom_id']))
        remaining=conn.execute("SELECT COALESCE(SUM(quantity-fulfilled_quantity),0)::float8 remaining FROM order_lines WHERE order_id=%s",(order_id,)).fetchone()['remaining']
        status='received' if remaining<=0.0005 else 'partial';conn.execute("UPDATE orders SET status=%s,inventory_booked_at=COALESCE(inventory_booked_at,NOW()),updated_at=NOW() WHERE id=%s",(status,order_id))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase.received',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'orderId':order_id,'receiptId':receipt_id,'reference':reference,'changes':changes,'status':status})))
        conn.commit()
    return {'receiptId':receipt_id,'status':status,'remaining':remaining}


def reverse(session, values):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om een ontvangst terug te draaien.')
    receipt_id=(values.get('receipt_id') or '').strip()
    with server.db() as conn:
        receipt=conn.execute("SELECT id,order_id,reference,reversed_at FROM purchase_receipts WHERE id=%s AND stockroom_id=%s FOR UPDATE",(receipt_id,session['stockroom_id'])).fetchone()
        if not receipt:raise ValueError('Ontvangst niet gevonden.')
        if receipt['reversed_at']:raise ValueError('Deze ontvangst is al teruggedraaid.')
        lines=conn.execute("SELECT * FROM purchase_receipt_lines WHERE receipt_id=%s ORDER BY id",(receipt_id,)).fetchall();room=conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE",(session['stockroom_id'],)).fetchone();state=room['state']
        for line in lines:
            item=next((item for item in state.get('items',[]) if str(item.get('id'))==str(line['item_id'])),None);quantity=float(line['quantity']);stock=float((item or {}).get('stock') or 0)
            if not item or stock+0.0005<quantity:raise ValueError(f"Ontvangst kan niet terug: onvoldoende voorraad van {line['item_name']}.")
            item['stock']=stock-quantity
            conn.execute("UPDATE order_lines SET fulfilled_quantity=GREATEST(0,fulfilled_quantity-%s) WHERE id=%s",(quantity,line['order_line_id']))
        state['transactions']=[tx for tx in state.get('transactions',[]) if str(tx.get('receiptId') or '')!=receipt_id]
        inventory_ledger.set_context(conn,'purchase_receipt_reversed',receipt['reference'] or receipt_id)
        conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s",(json.dumps(state,ensure_ascii=False),session['stockroom_id']))
        fulfilled=conn.execute("SELECT COALESCE(SUM(fulfilled_quantity),0)::float8 done,COALESCE(SUM(quantity-fulfilled_quantity),0)::float8 remaining FROM order_lines WHERE order_id=%s",(receipt['order_id'],)).fetchone();status='ordered' if fulfilled['done']<=0.0005 else 'partial' if fulfilled['remaining']>0.0005 else 'received'
        conn.execute("UPDATE purchase_receipts SET reversed_at=NOW(),reversed_by=%s WHERE id=%s",(session['user_id'],receipt_id));conn.execute("UPDATE orders SET status=%s,inventory_booked_at=CASE WHEN %s='ordered' THEN NULL ELSE inventory_booked_at END,updated_at=NOW() WHERE id=%s",(status,status,receipt['order_id']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase.receipt_reversed',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'orderId':str(receipt['order_id']),'receiptId':receipt_id,'status':status})));conn.commit()
    return {'reversed':True,'status':status}

