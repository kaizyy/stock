import io
import json
from datetime import datetime

import server
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


def initialize_business_tools():
    with server.db() as conn:
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_number TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_stockroom_number ON orders(stockroom_id,order_number) WHERE order_number IS NOT NULL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS order_sequences (
                stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
                order_type TEXT NOT NULL CHECK (order_type IN ('purchase','sales')),
                year INTEGER NOT NULL,
                last_value INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(stockroom_id,order_type,year)
            )
        """)
        rows = conn.execute("SELECT id::text,stockroom_id::text,order_type,EXTRACT(YEAR FROM order_date)::int AS year FROM orders WHERE order_number IS NULL ORDER BY created_at,id").fetchall()
        for row in rows:
            number = next_order_number(conn, row['stockroom_id'], row['order_type'], int(row['year']))
            conn.execute("UPDATE orders SET order_number=%s WHERE id=%s", (number, row['id']))
        conn.commit()


def next_order_number(conn, stockroom_id, order_type, year=None):
    year = int(year or datetime.now().year)
    row = conn.execute("""
        INSERT INTO order_sequences(stockroom_id,order_type,year,last_value)
        VALUES(%s,%s,%s,1)
        ON CONFLICT(stockroom_id,order_type,year)
        DO UPDATE SET last_value=order_sequences.last_value+1
        RETURNING last_value
    """, (stockroom_id, order_type, year)).fetchone()
    prefix = 'PO' if order_type == 'purchase' else 'SO'
    return f"{prefix}-{year}-{int(row['last_value']):06d}"


def assign_order_number(order_id, stockroom_id, order_type):
    with server.db() as conn:
        row = conn.execute("SELECT order_date,order_number FROM orders WHERE id=%s AND stockroom_id=%s FOR UPDATE", (order_id, stockroom_id)).fetchone()
        if not row:
            raise PermissionError('Order niet gevonden.')
        if row['order_number']:
            return row['order_number']
        number = next_order_number(conn, stockroom_id, order_type, row['order_date'].year)
        conn.execute("UPDATE orders SET order_number=%s WHERE id=%s", (number, order_id))
        conn.commit()
        return number


def enrich_orders(stockroom_id, rows):
    if not rows:
        return rows
    ids = [row['id'] for row in rows]
    with server.db() as conn:
        mapped = {r['id']: r['order_number'] for r in conn.execute("SELECT id::text AS id,order_number FROM orders WHERE stockroom_id=%s AND id=ANY(%s::uuid[])", (stockroom_id, ids)).fetchall()}
    for row in rows:
        row['order_number'] = mapped.get(row['id'])
    return rows


def search_all(stockroom_id, query):
    q = (query or '').strip().lower()
    if len(q) < 2:
        return []
    results = []
    with server.db() as conn:
        room = conn.execute("SELECT state FROM stockrooms WHERE id=%s", (stockroom_id,)).fetchone()
        state = room['state'] if room else {}
        for item in state.get('items', []):
            hay = ' '.join(str(item.get(k, '')) for k in ('name','sku','barcode','supplier','location')).lower()
            if q in hay:
                results.append({'type':'item','id':str(item.get('id')),'title':item.get('name') or 'Artikel','subtitle':f"SKU {item.get('sku','—')} · Locatie {item.get('location') or '—'}"})
        for table, kind in [('suppliers','supplier'),('customers','customer')]:
            rows = conn.execute(f"SELECT id::text,name,email,phone FROM {table} WHERE stockroom_id=%s AND (lower(name) LIKE %s OR lower(email) LIKE %s OR lower(phone) LIKE %s) LIMIT 20", (stockroom_id,f'%{q}%',f'%{q}%',f'%{q}%')).fetchall()
            for r in rows:
                results.append({'type':kind,'id':r['id'],'title':r['name'],'subtitle':' · '.join(x for x in (r['email'],r['phone']) if x)})
        rows = conn.execute("""SELECT id::text,order_type,order_number,reference,relation_name,status FROM orders
                             WHERE stockroom_id=%s AND (lower(COALESCE(order_number,'')) LIKE %s OR lower(reference) LIKE %s OR lower(relation_name) LIKE %s)
                             ORDER BY created_at DESC LIMIT 30""", (stockroom_id,f'%{q}%',f'%{q}%',f'%{q}%')).fetchall()
        for r in rows:
            results.append({'type':'order','id':r['id'],'order_type':r['order_type'],'title':r['order_number'] or r['reference'] or 'Order','subtitle':f"{r['relation_name'] or 'Geen relatie'} · {r['status']}"})
    return results[:50]


def _pdf_header(c, title, stockroom_name):
    c.setFont('Helvetica-Bold', 18)
    c.drawString(20*mm, 280*mm, 'Stockroom')
    c.setFont('Helvetica-Bold', 13)
    c.drawString(20*mm, 270*mm, title)
    c.setFont('Helvetica', 9)
    c.drawRightString(190*mm, 280*mm, stockroom_name or '')


def order_pdf(stockroom_id, order_id):
    with server.db() as conn:
        order = conn.execute("""SELECT o.*,s.name AS stockroom_name FROM orders o JOIN stockrooms s ON s.id=o.stockroom_id
                                WHERE o.id=%s AND o.stockroom_id=%s""", (order_id, stockroom_id)).fetchone()
        if not order:
            raise PermissionError('Order niet gevonden.')
        lines = conn.execute("SELECT item_name,sku,quantity::float8,unit_price::float8 FROM order_lines WHERE order_id=%s ORDER BY created_at,id", (order_id,)).fetchall()
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4)
    title = 'Inkooporder' if order['order_type']=='purchase' else 'Verkooporder'
    _pdf_header(c, title, order['stockroom_name'])
    c.setFont('Helvetica', 10)
    y=258*mm
    for label,value in [('Ordernummer',order['order_number'] or '—'),('Referentie',order['reference'] or '—'),('Relatie',order['relation_name'] or '—'),('Datum',str(order['order_date'])),('Status',order['status'])]:
        c.drawString(20*mm,y,f'{label}: {value}'); y-=6*mm
    y-=4*mm; c.setFont('Helvetica-Bold',9)
    c.drawString(20*mm,y,'Artikel'); c.drawString(105*mm,y,'Aantal'); c.drawString(135*mm,y,'Prijs'); c.drawRightString(190*mm,y,'Totaal'); y-=5*mm
    c.setFont('Helvetica',9); total=0
    for line in lines:
        if y<25*mm: c.showPage(); _pdf_header(c,title,order['stockroom_name']); y=255*mm; c.setFont('Helvetica',9)
        line_total=float(line['quantity'])*float(line['unit_price']); total+=line_total
        c.drawString(20*mm,y,(line['item_name'] or '')[:45]); c.drawString(105*mm,y,f"{float(line['quantity']):g}"); c.drawString(135*mm,y,f"€ {float(line['unit_price']):.2f}"); c.drawRightString(190*mm,y,f"€ {line_total:.2f}"); y-=6*mm
    y-=4*mm; c.setFont('Helvetica-Bold',11); c.drawRightString(190*mm,y,f'Totaal: € {total:.2f}')
    c.save(); return buf.getvalue(), f"{order['order_number'] or order_id}.pdf"


def inventory_pdf(stockroom_id):
    with server.db() as conn:
        row = conn.execute("SELECT name,state FROM stockrooms WHERE id=%s", (stockroom_id,)).fetchone()
    if not row: raise PermissionError('Stockroom niet gevonden.')
    items=[i for i in row['state'].get('items',[]) if not i.get('archived')]
    buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=A4); _pdf_header(c,'Voorraadlijst',row['name']); y=258*mm
    c.setFont('Helvetica-Bold',9); c.drawString(20*mm,y,'Artikel'); c.drawString(90*mm,y,'SKU'); c.drawString(125*mm,y,'Locatie'); c.drawRightString(190*mm,y,'Voorraad'); y-=6*mm
    c.setFont('Helvetica',9)
    for item in items:
        if y<20*mm: c.showPage(); _pdf_header(c,'Voorraadlijst',row['name']); y=255*mm; c.setFont('Helvetica',9)
        c.drawString(20*mm,y,str(item.get('name',''))[:38]); c.drawString(90*mm,y,str(item.get('sku',''))[:18]); c.drawString(125*mm,y,str(item.get('location','—'))[:24]); c.drawRightString(190*mm,y,f"{float(item.get('stock') or 0):g}"); y-=6*mm
    c.save(); return buf.getvalue(),'voorraadlijst.pdf'
