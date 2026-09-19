"""Purchase invoice three-way matching and payment controls."""
import base64
import json
import uuid

import server


def initialize():
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS purchase_invoice_policies(stockroom_id UUID PRIMARY KEY REFERENCES stockrooms(id) ON DELETE CASCADE,
            quantity_tolerance NUMERIC(8,3) NOT NULL DEFAULT 0,price_tolerance_percent NUMERIC(8,3) NOT NULL DEFAULT 1,total_tolerance NUMERIC(14,2) NOT NULL DEFAULT 1,
            updated_by UUID REFERENCES users(id) ON DELETE SET NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS purchase_invoices(id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
            order_id UUID NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,supplier_id UUID REFERENCES suppliers(id) ON DELETE SET NULL,invoice_number TEXT NOT NULL,
            invoice_date DATE NOT NULL,due_date DATE NOT NULL,subtotal NUMERIC(14,2) NOT NULL,vat_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
            shipping_amount NUMERIC(14,2) NOT NULL DEFAULT 0,total_amount NUMERIC(14,2) NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
            dispute_amount NUMERIC(14,2) NOT NULL DEFAULT 0,paid_amount NUMERIC(14,2) NOT NULL DEFAULT 0,document_name TEXT NOT NULL DEFAULT '',document_mime TEXT NOT NULL DEFAULT '',document_data BYTEA,
            match_result JSONB NOT NULL DEFAULT '{}'::jsonb,created_by UUID REFERENCES users(id) ON DELETE SET NULL,approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
            approved_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(stockroom_id,supplier_id,invoice_number))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS purchase_invoice_lines(id UUID PRIMARY KEY,invoice_id UUID NOT NULL REFERENCES purchase_invoices(id) ON DELETE CASCADE,
            order_line_id UUID REFERENCES order_lines(id) ON DELETE SET NULL,item_name TEXT NOT NULL,quantity NUMERIC(14,3) NOT NULL,unit_price NUMERIC(14,4) NOT NULL,line_total NUMERIC(14,2) NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS purchase_invoice_credits(id UUID PRIMARY KEY,invoice_id UUID NOT NULL REFERENCES purchase_invoices(id) ON DELETE CASCADE,
            credit_number TEXT NOT NULL,amount NUMERIC(14,2) NOT NULL CHECK(amount>0),note TEXT NOT NULL DEFAULT '',created_by UUID REFERENCES users(id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(invoice_id,credit_number))""")
        conn.commit()


def policy(stockroom_id):
    with server.db() as conn:row=conn.execute("SELECT quantity_tolerance::float8,price_tolerance_percent::float8,total_tolerance::float8 FROM purchase_invoice_policies WHERE stockroom_id=%s",(stockroom_id,)).fetchone()
    return row or {'quantity_tolerance':0.0,'price_tolerance_percent':1.0,'total_tolerance':1.0}


def save_policy(session, values):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan factuurtoleranties wijzigen.')
    try:q=max(0,float(values.get('quantity_tolerance') or 0));p=max(0,float(values.get('price_tolerance_percent') or 0));t=max(0,float(values.get('total_tolerance') or 0))
    except (TypeError,ValueError):raise ValueError('Controleer de toleranties.')
    with server.db() as conn:conn.execute("""INSERT INTO purchase_invoice_policies(stockroom_id,quantity_tolerance,price_tolerance_percent,total_tolerance,updated_by) VALUES(%s,%s,%s,%s,%s)
        ON CONFLICT(stockroom_id) DO UPDATE SET quantity_tolerance=EXCLUDED.quantity_tolerance,price_tolerance_percent=EXCLUDED.price_tolerance_percent,total_tolerance=EXCLUDED.total_tolerance,updated_by=EXCLUDED.updated_by,updated_at=NOW()""",(session['stockroom_id'],q,p,t,session['user_id']));conn.commit()
    return {'saved':True,**policy(session['stockroom_id'])}


def _lines(raw):
    try:rows=json.loads(raw or '[]')
    except json.JSONDecodeError as exc:raise ValueError('Factuurregels zijn ongeldig.') from exc
    result=[]
    for row in rows[:200]:
        try:line_id=str(row.get('order_line_id') or '').strip();quantity=float(row.get('quantity'));price=float(row.get('unit_price'))
        except (AttributeError,TypeError,ValueError):raise ValueError('Controleer factuuraantallen en prijzen.')
        if not line_id or quantity<0 or price<0:raise ValueError('Controleer factuuraantallen en prijzen.')
        result.append((line_id,quantity,price))
    if not result:raise ValueError('Voeg minimaal één factuurregel toe.')
    return result


def create(session, values):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om een inkoopfactuur vast te leggen.')
    order_id=(values.get('order_id') or '').strip();number=(values.get('invoice_number') or '').strip()[:120];lines=_lines(values.get('lines_json'))
    if not number:raise ValueError('Vul het leveranciersfactuurnummer in.')
    try:vat=round(float(values.get('vat_amount') or 0),2);shipping=round(float(values.get('shipping_amount') or 0),2)
    except (TypeError,ValueError):raise ValueError('Controleer btw en verzendkosten.')
    document=None;name=(values.get('document_name') or '')[:200];mime=(values.get('document_mime') or '')[:100]
    if values.get('document_base64'):
        try:document=base64.b64decode(values['document_base64'],validate=True)
        except Exception as exc:raise ValueError('Factuurbijlage is ongeldig.') from exc
        if len(document)>5*1024*1024 or mime not in ('application/pdf','image/jpeg','image/png'):raise ValueError('Gebruik een PDF, JPG of PNG van maximaal 5 MB.')
    setting=policy(session['stockroom_id'])
    with server.db() as conn:
        order=conn.execute("SELECT id,relation_id,relation_name FROM orders WHERE id=%s AND stockroom_id=%s AND order_type='purchase'",(order_id,session['stockroom_id'])).fetchone()
        if not order:raise PermissionError('Inkooporder niet gevonden.')
        duplicate=conn.execute("SELECT 1 FROM purchase_invoices WHERE stockroom_id=%s AND supplier_id IS NOT DISTINCT FROM %s AND LOWER(invoice_number)=LOWER(%s)",(session['stockroom_id'],order['relation_id'],number)).fetchone()
        if duplicate:raise ValueError('Dit factuurnummer bestaat al voor deze leverancier.')
        expected=conn.execute("""SELECT l.id::text,l.item_name,(l.quantity-l.supplier_cancelled_quantity)::float8 ordered,l.unit_price::float8,
            COALESCE((SELECT SUM(prl.usable_quantity) FROM purchase_receipt_lines prl JOIN purchase_receipts pr ON pr.id=prl.receipt_id WHERE prl.order_line_id=l.id AND pr.reversed_at IS NULL),0)::float8 received
            FROM order_lines l WHERE l.order_id=%s""",(order_id,)).fetchall();by_id={row['id']:row for row in expected};discrepancies=[];subtotal=0
        for line_id,quantity,price in lines:
            source=by_id.get(line_id)
            if not source:raise ValueError('Een factuurregel hoort niet bij deze order.')
            subtotal+=quantity*price;qty_base=float(source['received']);qty_diff=quantity-qty_base;price_diff_pct=0 if not source['unit_price'] else (price-float(source['unit_price']))/float(source['unit_price'])*100
            if abs(qty_diff)>float(setting['quantity_tolerance']):discrepancies.append({'lineId':line_id,'item':source['item_name'],'type':'quantity','expected':qty_base,'actual':quantity,'difference':round(qty_diff,3)})
            if abs(price_diff_pct)>float(setting['price_tolerance_percent']):discrepancies.append({'lineId':line_id,'item':source['item_name'],'type':'price','expected':float(source['unit_price']),'actual':price,'differencePercent':round(price_diff_pct,2)})
        subtotal=round(subtotal,2);total=round(subtotal+vat+shipping,2);order_expected=round(sum(float(row['received'])*float(row['unit_price']) for row in expected)+vat+shipping,2)
        if abs(total-order_expected)>float(setting['total_tolerance']):discrepancies.append({'type':'total','expected':order_expected,'actual':total,'difference':round(total-order_expected,2)})
        status='blocked' if discrepancies else 'matched';invoice_id=str(uuid.uuid4())
        try:inv_date=conn.execute("SELECT %s::date d",(values.get('invoice_date'),)).fetchone()['d'];due=conn.execute("SELECT %s::date d",(values.get('due_date'),)).fetchone()['d']
        except Exception as exc:raise ValueError('Controleer factuur- en vervaldatum.') from exc
        conn.execute("""INSERT INTO purchase_invoices(id,stockroom_id,order_id,supplier_id,invoice_number,invoice_date,due_date,subtotal,vat_amount,shipping_amount,total_amount,status,document_name,document_mime,document_data,match_result,created_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",(invoice_id,session['stockroom_id'],order_id,order['relation_id'],number,inv_date,due,subtotal,vat,shipping,total,status,name,mime,document,json.dumps({'discrepancies':discrepancies,'tolerances':setting,'lines':[{'lineId':line_id,'item':by_id[line_id]['item_name'],'ordered':float(by_id[line_id]['ordered']),'received':float(by_id[line_id]['received']),'invoiced':quantity,'orderPrice':float(by_id[line_id]['unit_price']),'invoicePrice':price} for line_id,quantity,price in lines]}),session['user_id']))
        for line_id,quantity,price in lines:conn.execute("INSERT INTO purchase_invoice_lines(id,invoice_id,order_line_id,item_name,quantity,unit_price,line_total) VALUES(%s,%s,%s,%s,%s,%s,%s)",(str(uuid.uuid4()),invoice_id,line_id,by_id[line_id]['item_name'],quantity,price,round(quantity*price,2)))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase_invoice.matched',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':invoice_id,'orderId':order_id,'invoiceNumber':number,'status':status,'discrepancies':len(discrepancies)})));conn.commit()
    return {'created':True,'id':invoice_id,'status':status,'discrepancies':discrepancies}


def rows(stockroom_id):
    with server.db() as conn:data=conn.execute("""SELECT i.id::text,i.order_id::text,i.invoice_number,i.invoice_date,i.due_date,i.subtotal::float8,i.vat_amount::float8,i.shipping_amount::float8,i.total_amount::float8,i.dispute_amount::float8,i.paid_amount::float8,i.status,i.match_result,i.document_name,o.order_number,o.relation_name,
        COALESCE((SELECT SUM(amount) FROM purchase_invoice_credits c WHERE c.invoice_id=i.id),0)::float8 credited FROM purchase_invoices i JOIN orders o ON o.id=i.order_id WHERE i.stockroom_id=%s ORDER BY i.invoice_date DESC,i.created_at DESC""",(stockroom_id,)).fetchall()
    for row in data:row['outstanding']=max(0,float(row['total_amount'])-float(row['paid_amount'])-float(row['credited'])-float(row['dispute_amount']));row['paymentBlocked']=row['status'] not in ('approved','partially_disputed')
    return data


def attachment(stockroom_id, invoice_id):
    with server.db() as conn:row=conn.execute("SELECT document_data,document_name,document_mime FROM purchase_invoices WHERE id=%s AND stockroom_id=%s",(invoice_id,stockroom_id)).fetchone()
    if not row or not row['document_data']:raise ValueError('Factuurbijlage niet gevonden.')
    return bytes(row['document_data']),row['document_name'] or 'inkoopfactuur',row['document_mime'] or 'application/octet-stream'


def decide(session, values, decision):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan inkoopfacturen beoordelen.')
    invoice_id=(values.get('invoice_id') or '').strip();status={'approve':'approved','reject':'rejected','dispute':'partially_disputed'}.get(decision)
    if not status:raise ValueError('Beslissing is ongeldig.')
    try:dispute=max(0,float(values.get('dispute_amount') or 0)) if decision=='dispute' else 0
    except (TypeError,ValueError):raise ValueError('Betwist bedrag is ongeldig.')
    with server.db() as conn:
        row=conn.execute("UPDATE purchase_invoices SET status=%s,dispute_amount=%s,approved_by=%s,approved_at=NOW(),updated_at=NOW() WHERE id=%s AND stockroom_id=%s RETURNING invoice_number",(status,dispute,session['user_id'],invoice_id,session['stockroom_id'])).fetchone()
        if not row:raise ValueError('Inkoopfactuur niet gevonden.')
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",(session['stockroom_id'],session['user_id'],f'purchase_invoice.{status}',json.dumps({'id':invoice_id,'disputeAmount':dispute})));conn.commit()
    return {'updated':True,'status':status}


def payment(session, values):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan betalingen registreren.')
    invoice_id=(values.get('invoice_id') or '').strip()
    try:amount=round(float(values.get('amount') or 0),2)
    except (TypeError,ValueError):raise ValueError('Betaalbedrag is ongeldig.')
    with server.db() as conn:
        row=conn.execute("SELECT total_amount::float8,paid_amount::float8,dispute_amount::float8,status FROM purchase_invoices WHERE id=%s AND stockroom_id=%s FOR UPDATE",(invoice_id,session['stockroom_id'])).fetchone()
        if not row or row['status'] not in ('approved','partially_disputed'):raise ValueError('Betaling is geblokkeerd totdat de factuur is goedgekeurd.')
        credited=float(conn.execute("SELECT COALESCE(SUM(amount),0)::float8 n FROM purchase_invoice_credits WHERE invoice_id=%s",(invoice_id,)).fetchone()['n']);payable=max(0,float(row['total_amount'])-credited-float(row['dispute_amount'])-float(row['paid_amount']))
        if amount<=0 or amount>payable+0.005:raise ValueError(f'Er kan maximaal € {payable:.2f} worden betaald.')
        conn.execute("UPDATE purchase_invoices SET paid_amount=paid_amount+%s,status=CASE WHEN paid_amount+%s>=%s THEN 'paid' ELSE status END,updated_at=NOW() WHERE id=%s",(amount,amount,float(row['total_amount'])-credited-float(row['dispute_amount']),invoice_id))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase_invoice.payment',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':invoice_id,'amount':amount})));conn.commit()
    return {'paid':True,'amount':amount}


def credit(session, values):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om een leverancierscredit te koppelen.')
    invoice_id=(values.get('invoice_id') or '').strip();number=(values.get('credit_number') or '').strip()[:120]
    try:amount=round(float(values.get('amount') or 0),2)
    except (TypeError,ValueError):raise ValueError('Creditbedrag is ongeldig.')
    if not number or amount<=0:raise ValueError('Vul creditnummer en bedrag in.')
    with server.db() as conn:
        inv=conn.execute("SELECT total_amount::float8 FROM purchase_invoices WHERE id=%s AND stockroom_id=%s",(invoice_id,session['stockroom_id'])).fetchone()
        if not inv:raise ValueError('Inkoopfactuur niet gevonden.')
        existing=float(conn.execute("SELECT COALESCE(SUM(amount),0)::float8 n FROM purchase_invoice_credits WHERE invoice_id=%s",(invoice_id,)).fetchone()['n']);
        if existing+amount>float(inv['total_amount'])+0.005:raise ValueError('Credit is hoger dan het factuurtotaal.')
        conn.execute("INSERT INTO purchase_invoice_credits(id,invoice_id,credit_number,amount,note,created_by) VALUES(%s,%s,%s,%s,%s,%s)",(str(uuid.uuid4()),invoice_id,number,amount,(values.get('note') or '')[:1000],session['user_id']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase_invoice.credit',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':invoice_id,'creditNumber':number,'amount':amount})));conn.commit()
    return {'created':True,'amount':amount}
