"""Alternative supplier proposals for confirmed purchase shortages."""
import json
import uuid

import purchase_approvals
import purchase_intelligence
import server


def initialize():
    with server.db() as conn:
        conn.execute("ALTER TABLE order_lines ADD COLUMN IF NOT EXISTS supplier_cancelled_quantity NUMERIC(14,3) NOT NULL DEFAULT 0 CHECK(supplier_cancelled_quantity>=0 AND supplier_cancelled_quantity<=quantity)")
        conn.execute("ALTER TABLE order_lines ADD COLUMN IF NOT EXISTS source_order_line_id UUID REFERENCES order_lines(id) ON DELETE SET NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_lines_source ON order_lines(source_order_line_id)")
        conn.commit()


def _wait_days(weekdays):
    today=server.date.today().isoweekday();days=[int(day) for day in str(weekdays or '').split(',') if day.isdigit() and 1<=int(day)<=7]
    return min(((day-today)%7 for day in days),default=0)


def overview(session, order_id):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om alternatieve leveranciers te bekijken.')
    intelligence=purchase_intelligence.overview(session['stockroom_id']);by_item={str(row['itemId']):row for row in intelligence['recommendations']};metrics={str(row['id']):row for row in intelligence['suppliers'] if row['id']}
    with server.db() as conn:
        order=conn.execute("SELECT id::text,relation_id::text,relation_name,COALESCE(order_number,reference,'Inkooporder') number,confirmed_delivery_date FROM orders WHERE id=%s AND stockroom_id=%s AND order_type='purchase'",(order_id,session['stockroom_id'])).fetchone()
        if not order:raise PermissionError('Inkooporder niet gevonden.')
        lines=conn.execute("""SELECT l.id::text,l.item_id,l.item_name,l.sku,l.quantity::float8,l.fulfilled_quantity::float8,l.supplier_cancelled_quantity::float8,
            r.availability,r.available_quantity::float8 FROM order_lines l JOIN supplier_portal_line_responses r ON r.order_line_id=l.id
            WHERE l.order_id=%s AND r.availability IN ('partial','unavailable') ORDER BY l.created_at""",(order_id,)).fetchall()
        suppliers=conn.execute("SELECT id::text,name,lead_time_days,ordering_weekdays FROM suppliers WHERE stockroom_id=%s",(session['stockroom_id'],)).fetchall()
        open_supplements=conn.execute("""SELECT source_order_line_id::text,COALESCE(SUM(quantity-fulfilled_quantity-supplier_cancelled_quantity),0)::float8 quantity
            FROM order_lines l JOIN orders o ON o.id=l.order_id WHERE o.stockroom_id=%s AND source_order_line_id IS NOT NULL
            AND o.status IN ('draft','pending_approval','approved','ordered','partial') GROUP BY source_order_line_id""",(session['stockroom_id'],)).fetchall()
    suppliers_by_id={row['id']:row for row in suppliers};open_by_line={row['source_order_line_id']:float(row['quantity']) for row in open_supplements};result=[]
    for line in lines:
        shortage=max(0,float(line['quantity'])-float(line['supplier_cancelled_quantity'])-max(float(line['fulfilled_quantity']),float(line['available_quantity'] or 0))-open_by_line.get(line['id'],0))
        if shortage<=0.0005:continue
        choices=[]
        for option in (by_item.get(str(line['item_id'])) or {}).get('alternatives',[]):
            supplier_id=str(option.get('supplierId') or '')
            if not supplier_id or supplier_id==str(order['relation_id'] or ''):continue
            supplier=suppliers_by_id.get(supplier_id)
            if not supplier:continue
            metric=metrics.get(supplier_id) or {};lead=max(1,int(supplier['lead_time_days'] or round(float(metric.get('avgLeadDays') or 14))));arrival=lead+_wait_days(supplier['ordering_weekdays'])
            current_days=(order['confirmed_delivery_date']-server.date.today()).days if order['confirmed_delivery_date'] else None
            choices.append({'supplierId':supplier_id,'supplierName':supplier['name'],'price':float(option.get('latestPrice') or 0),'leadDays':arrival,'daysEarlier':max(0,current_days-arrival) if current_days is not None else None,'score':option.get('score') or metric.get('score') or 0,'returnRate':metric.get('returnRate'),'confidence':metric.get('confidence')})
        choices.sort(key=lambda row:(-int(row['score']),row['leadDays'],row['price']))
        result.append({'lineId':line['id'],'itemId':line['item_id'],'itemName':line['item_name'],'sku':line['sku'],'shortage':round(shortage,3),'availability':line['availability'],'alternatives':choices})
    return {'order':order,'shortages':result}


def create(session, values):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om een aanvullende inkooporder te maken.')
    line_id=(values.get('line_id') or '').strip();supplier_id=(values.get('supplier_id') or '').strip()
    with server.db() as conn:
        original=conn.execute("""SELECT l.id::text,l.order_id::text,l.item_id,l.item_name,l.sku,l.quantity::float8,l.fulfilled_quantity::float8,l.supplier_cancelled_quantity::float8,
            r.availability,r.available_quantity::float8,o.relation_id::text original_supplier_id,o.relation_name,o.confirmed_delivery_date
            FROM order_lines l JOIN orders o ON o.id=l.order_id JOIN supplier_portal_line_responses r ON r.order_line_id=l.id
            WHERE l.id=%s AND o.stockroom_id=%s AND o.order_type='purchase' AND r.availability IN ('partial','unavailable') FOR UPDATE OF l,o""",(line_id,session['stockroom_id'])).fetchone()
        if not original:raise ValueError('Er is geen actueel leverancierstekort voor deze orderregel.')
        supplier=conn.execute("SELECT id::text,name,lead_time_days,ordering_weekdays FROM suppliers WHERE id=%s AND stockroom_id=%s",(supplier_id,session['stockroom_id'])).fetchone()
        if not supplier or supplier['id']==original['original_supplier_id']:raise ValueError('Kies een andere leverancier uit deze stockroom.')
        existing=conn.execute("""SELECT COALESCE(SUM(l.quantity-l.fulfilled_quantity-l.supplier_cancelled_quantity),0)::float8 quantity FROM order_lines l JOIN orders o ON o.id=l.order_id
            WHERE l.source_order_line_id=%s AND o.status IN ('draft','pending_approval','approved','ordered','partial')""",(line_id,)).fetchone()['quantity']
        available=max(float(original['fulfilled_quantity']),float(original['available_quantity'] or 0));shortage=max(0,float(original['quantity'])-float(original['supplier_cancelled_quantity'])-available-float(existing or 0))
        if shortage<=0.0005:raise ValueError('Dit tekort is al volledig opgevangen.')
        intelligence=purchase_intelligence.overview(session['stockroom_id']);recommendation=next((row for row in intelligence['recommendations'] if str(row['itemId'])==str(original['item_id'])),None) or {}
        option=next((row for row in recommendation.get('alternatives',[]) if str(row.get('supplierId') or '')==supplier_id),None)
        if not option:raise ValueError('Voor deze leverancier is nog geen inkoopprijs van dit artikel bekend.')
        wait=_wait_days(supplier['ordering_weekdays']);lead=max(1,int(supplier['lead_time_days'] or 14));order_id=str(uuid.uuid4());details={'source':'supplier_shortage','sourceOrderId':original['order_id'],'sourceOrderLineId':line_id,'shortage':shortage}
        conn.execute("""INSERT INTO orders(id,stockroom_id,order_type,relation_id,relation_name,status,reference,notes,order_date,expected_delivery_date,advice_details,created_by)
            VALUES(%s,%s,'purchase',%s,%s,'draft',%s,%s,CURRENT_DATE,CURRENT_DATE+(%s*INTERVAL '1 day'),%s::jsonb,%s)""",(order_id,session['stockroom_id'],supplier_id,supplier['name'],f"Aanvulling tekort {original['item_name']}",f"Automatisch alternatief voor tekort bij {original['relation_name'] or 'oorspronkelijke leverancier'}.",lead+wait,json.dumps(details),session['user_id']))
        conn.execute("""INSERT INTO order_lines(id,order_id,item_id,item_name,sku,quantity,unit_price,source_order_line_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",(str(uuid.uuid4()),order_id,original['item_id'],original['item_name'],original['sku'],shortage,float(option.get('latestPrice') or 0),line_id))
        conn.execute("UPDATE order_lines SET supplier_cancelled_quantity=supplier_cancelled_quantity+%s WHERE id=%s",(shortage,line_id))
        approval=purchase_approvals.evaluate(conn,session['stockroom_id'],order_id)
        if approval['required']:conn.execute("UPDATE orders SET status='pending_approval',approval_status='pending',approval_reason=%s WHERE id=%s",('; '.join(approval['reasons']),order_id))
        totals=conn.execute("SELECT COALESCE(SUM(quantity-supplier_cancelled_quantity),0)::float8 effective,COALESCE(SUM(fulfilled_quantity),0)::float8 fulfilled FROM order_lines WHERE order_id=%s",(original['order_id'],)).fetchone();remaining=float(totals['effective'])-float(totals['fulfilled'])
        original_status='cancelled' if float(totals['effective'])<=0.0005 else 'received' if remaining<=0.0005 else 'partial' if float(totals['fulfilled'])>0 else 'ordered'
        conn.execute("UPDATE orders SET status=%s,updated_at=NOW() WHERE id=%s",(original_status,original['order_id']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase.shortage_reordered',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':original['order_id'],'lineId':line_id,'supplementOrderId':order_id,'supplierId':supplier_id,'quantity':shortage})));conn.commit()
    return {'created':True,'orderId':order_id,'quantity':round(shortage,3),'supplier':supplier['name'],'approvalRequired':approval['required']}
