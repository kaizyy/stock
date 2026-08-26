import io
from datetime import datetime

import server
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


def _load(stockroom_id, order_id):
    with server.db() as conn:
        order=conn.execute("""SELECT o.*,s.name stockroom_name,b.company_name,b.address,b.postal_code,b.city,b.country,b.vat_number,b.chamber_number,b.invoice_email
          FROM orders o JOIN stockrooms s ON s.id=o.stockroom_id LEFT JOIN billing_accounts b ON b.stockroom_id=o.stockroom_id
          WHERE o.id=%s AND o.stockroom_id=%s""",(order_id,stockroom_id)).fetchone()
        if not order: raise PermissionError('Order niet gevonden.')
        lines=conn.execute("SELECT item_name,sku,quantity::float8,unit_price::float8,fulfilled_quantity::float8 FROM order_lines WHERE order_id=%s ORDER BY created_at,id",(order_id,)).fetchall()
        relation=None
        if order['relation_id']:
            table='suppliers' if order['order_type']=='purchase' else 'customers'
            relation=conn.execute(f"SELECT name,contact_name,email,phone,address FROM {table} WHERE id=%s AND stockroom_id=%s",(order['relation_id'],stockroom_id)).fetchone()
    return order,lines,relation or {}


def _company(order):
    name=order.get('company_name') or order.get('stockroom_name') or 'Stockroom'
    parts=[name,order.get('address') or '',f"{order.get('postal_code') or ''} {order.get('city') or ''}".strip(),order.get('country') or '']
    return [p for p in parts if p]


def _header(c,title,order,relation):
    c.setFont('Helvetica-Bold',20);c.drawString(18*mm,278*mm,title)
    c.setFont('Helvetica-Bold',11);y=267*mm
    for p in _company(order):c.drawString(18*mm,y,str(p)[:70]);y-=5*mm
    c.setFont('Helvetica',8)
    extras=[]
    if order.get('vat_number'):extras.append('BTW '+str(order['vat_number']))
    if order.get('chamber_number'):extras.append('KvK '+str(order['chamber_number']))
    if order.get('invoice_email'):extras.append(str(order['invoice_email']))
    if extras:c.drawString(18*mm,y,' · '.join(extras)[:100])
    c.setFont('Helvetica-Bold',10);c.drawString(118*mm,267*mm,'Aan')
    c.setFont('Helvetica',9);ry=261*mm
    for p in [relation.get('name') or order.get('relation_name') or '—',relation.get('contact_name') or '',relation.get('address') or '',relation.get('email') or '']:
        if p:c.drawString(118*mm,ry,str(p)[:55]);ry-=5*mm
    return 242*mm


def _meta(c,y,order):
    c.setFont('Helvetica',9)
    fields=[('Ordernummer',order.get('order_number') or '—'),('Referentie',order.get('reference') or '—'),('Datum',str(order.get('order_date') or '')),('Status',order.get('status') or '')]
    x=18*mm
    for label,value in fields:
        c.setFont('Helvetica-Bold',8);c.drawString(x,y,label);c.setFont('Helvetica',9);c.drawString(x,y-5*mm,str(value)[:28]);x+=45*mm
    return y-13*mm


def _table(c,y,lines,show_prices=True,qty_key='quantity'):
    c.setFont('Helvetica-Bold',8);c.drawString(18*mm,y,'Artikel');c.drawString(103*mm,y,'SKU');c.drawRightString(140*mm,y,'Aantal')
    if show_prices:c.drawRightString(166*mm,y,'Prijs');c.drawRightString(194*mm,y,'Totaal')
    y-=5*mm;c.line(18*mm,y+2*mm,194*mm,y+2*mm);c.setFont('Helvetica',8);total=0
    for line in lines:
        if y<28*mm:
            c.showPage();y=270*mm;c.setFont('Helvetica',8)
        qty=float(line.get(qty_key) or line.get('quantity') or 0);price=float(line.get('unit_price') or 0);amount=qty*price;total+=amount
        c.drawString(18*mm,y,str(line.get('item_name') or '')[:48]);c.drawString(103*mm,y,str(line.get('sku') or '')[:18]);c.drawRightString(140*mm,y,f'{qty:g}')
        if show_prices:c.drawRightString(166*mm,y,f'€ {price:.2f}');c.drawRightString(194*mm,y,f'€ {amount:.2f}')
        y-=6*mm
    return y,total


def invoice_pdf(stockroom_id,order_id):
    order,lines,relation=_load(stockroom_id,order_id)
    if order['order_type']!='sales':raise ValueError('Facturen zijn alleen beschikbaar voor verkooporders.')
    buf=io.BytesIO();c=canvas.Canvas(buf,pagesize=A4);y=_header(c,'FACTUUR',order,relation);y=_meta(c,y,order);y,total=_table(c,y,lines,True)
    c.setFont('Helvetica-Bold',12);c.drawRightString(194*mm,max(y-6*mm,22*mm),f'Totaal: € {total:.2f}')
    c.setFont('Helvetica',7);c.drawString(18*mm,14*mm,'Gegenereerd door Stockroom · '+datetime.now().strftime('%d-%m-%Y %H:%M'))
    c.save();return buf.getvalue(),f"factuur-{order.get('order_number') or order_id}.pdf"


def packing_slip_pdf(stockroom_id,order_id):
    order,lines,relation=_load(stockroom_id,order_id);buf=io.BytesIO();c=canvas.Canvas(buf,pagesize=A4);y=_header(c,'PAKBON',order,relation);y=_meta(c,y,order);y,_=_table(c,y,lines,False)
    c.setFont('Helvetica',8);c.drawString(18*mm,max(y-8*mm,20*mm),'Controle: ____  Verpakt door: ____________________')
    c.save();return buf.getvalue(),f"pakbon-{order.get('order_number') or order_id}.pdf"


def return_pdf(stockroom_id,order_id):
    order,lines,relation=_load(stockroom_id,order_id);buf=io.BytesIO();c=canvas.Canvas(buf,pagesize=A4);y=_header(c,'RETOURDOCUMENT',order,relation);y=_meta(c,y,order);y,_=_table(c,y,lines,False)
    c.setFont('Helvetica',8);y=max(y-8*mm,28*mm);c.drawString(18*mm,y,'Reden retour: ________________________________________________________________');c.drawString(18*mm,y-7*mm,'Ontvangen/verwerkt door: ____________________  Datum: ____________________')
    c.save();return buf.getvalue(),f"retour-{order.get('order_number') or order_id}.pdf"
