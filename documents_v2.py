import io
from datetime import datetime

import server
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
DARK = colors.HexColor('#1F2937')
MID = colors.HexColor('#6B7280')
LIGHT = colors.HexColor('#F3F4F6')
LINE = colors.HexColor('#E5E7EB')
ACCENT = colors.HexColor('#111827')


def _load(stockroom_id, order_id):
    with server.db() as conn:
        order = conn.execute("""SELECT o.*,s.name stockroom_name,b.company_name,b.address,b.postal_code,b.city,b.country,b.vat_number,b.chamber_number,b.invoice_email
          FROM orders o JOIN stockrooms s ON s.id=o.stockroom_id LEFT JOIN billing_accounts b ON b.stockroom_id=o.stockroom_id
          WHERE o.id=%s AND o.stockroom_id=%s""", (order_id, stockroom_id)).fetchone()
        if not order:
            raise PermissionError('Order niet gevonden.')
        lines = conn.execute("SELECT item_name,sku,quantity::float8,unit_price::float8,fulfilled_quantity::float8 FROM order_lines WHERE order_id=%s ORDER BY created_at,id", (order_id,)).fetchall()
        relation = None
        if order['relation_id']:
            table = 'suppliers' if order['order_type'] == 'purchase' else 'customers'
            relation = conn.execute(f"SELECT name,contact_name,email,phone,address FROM {table} WHERE id=%s AND stockroom_id=%s", (order['relation_id'], stockroom_id)).fetchone()
    return order, lines, relation or {}


def _company_name(order):
    return (order.get('company_name') or '').strip() or (order.get('stockroom_name') or '').strip() or 'Stockroom'


def _company_lines(order):
    lines = [_company_name(order)]
    for value in [order.get('address') or '', f"{order.get('postal_code') or ''} {order.get('city') or ''}".strip(), order.get('country') or '']:
        if str(value).strip():
            lines.append(str(value).strip())
    return lines


def _relation_lines(order, relation):
    lines = [relation.get('name') or order.get('relation_name') or '—']
    for value in [relation.get('contact_name') or '', relation.get('address') or '', relation.get('email') or '', relation.get('phone') or '']:
        if str(value).strip():
            lines.append(str(value).strip())
    return lines


def _money(value):
    return f"€ {float(value or 0):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')


def _date(value):
    if not value:
        return '—'
    try:
        return value.strftime('%d-%m-%Y')
    except AttributeError:
        return str(value)


def _draw_brand_header(c, title, order, subtitle=''):
    c.setFillColor(ACCENT)
    c.rect(0, PAGE_H - 34 * mm, PAGE_W, 34 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 17)
    c.drawString(MARGIN, PAGE_H - 16 * mm, _company_name(order)[:55])
    c.setFont('Helvetica', 8.5)
    c.setFillColor(colors.HexColor('#D1D5DB'))
    c.drawString(MARGIN, PAGE_H - 22 * mm, 'Voorraad- en orderbeheer')
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 20)
    c.drawRightString(PAGE_W - MARGIN, PAGE_H - 15 * mm, title)
    if subtitle:
        c.setFont('Helvetica', 8.5)
        c.setFillColor(colors.HexColor('#D1D5DB'))
        c.drawRightString(PAGE_W - MARGIN, PAGE_H - 22 * mm, subtitle)
    return PAGE_H - 45 * mm


def _draw_info_boxes(c, y, order, relation):
    gap = 7 * mm
    box_w = (PAGE_W - 2 * MARGIN - gap) / 2
    box_h = 37 * mm

    def box(x, heading, lines):
        c.setFillColor(colors.white)
        c.setStrokeColor(LINE)
        c.roundRect(x, y - box_h, box_w, box_h, 3 * mm, stroke=1, fill=1)
        c.setFillColor(MID)
        c.setFont('Helvetica-Bold', 8)
        c.drawString(x + 5 * mm, y - 7 * mm, heading.upper())
        cy = y - 13 * mm
        for i, line in enumerate(lines[:5]):
            c.setFillColor(DARK)
            c.setFont('Helvetica-Bold' if i == 0 else 'Helvetica', 9 if i == 0 else 8.3)
            c.drawString(x + 5 * mm, cy, str(line)[:48])
            cy -= 5 * mm

    box(MARGIN, 'Van', _company_lines(order))
    box(MARGIN + box_w + gap, 'Aan', _relation_lines(order, relation))
    return y - box_h - 8 * mm


def _draw_meta(c, y, order, document_number_label='Ordernummer'):
    fields = [
        (document_number_label, order.get('order_number') or '—'),
        ('Referentie', order.get('reference') or '—'),
        ('Datum', _date(order.get('order_date'))),
        ('Status', order.get('status') or '—'),
    ]
    total_w = PAGE_W - 2 * MARGIN
    col = total_w / len(fields)
    c.setStrokeColor(LINE)
    c.line(MARGIN, y, PAGE_W - MARGIN, y)
    for i, (label, value) in enumerate(fields):
        x = MARGIN + i * col
        c.setFillColor(MID)
        c.setFont('Helvetica-Bold', 7.5)
        c.drawString(x, y - 6 * mm, label.upper())
        c.setFillColor(DARK)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(x, y - 12 * mm, str(value)[:24])
    c.setStrokeColor(LINE)
    c.line(MARGIN, y - 17 * mm, PAGE_W - MARGIN, y - 17 * mm)
    return y - 25 * mm


def _table_header(c, y, show_prices):
    c.setFillColor(LIGHT)
    c.roundRect(MARGIN, y - 8 * mm, PAGE_W - 2 * MARGIN, 8 * mm, 2 * mm, stroke=0, fill=1)
    c.setFillColor(DARK)
    c.setFont('Helvetica-Bold', 7.8)
    c.drawString(MARGIN + 4 * mm, y - 5.5 * mm, 'ARTIKEL')
    c.drawString(104 * mm, y - 5.5 * mm, 'SKU')
    c.drawRightString(143 * mm, y - 5.5 * mm, 'AANTAL')
    if show_prices:
        c.drawRightString(168 * mm, y - 5.5 * mm, 'PRIJS')
        c.drawRightString(PAGE_W - MARGIN - 4 * mm, y - 5.5 * mm, 'TOTAAL')
    return y - 13 * mm


def _table(c, y, lines, show_prices=True):
    y = _table_header(c, y, show_prices)
    total = 0
    for idx, line in enumerate(lines):
        if y < 35 * mm:
            c.showPage()
            y = PAGE_H - 24 * mm
            y = _table_header(c, y, show_prices)
        qty = float(line.get('quantity') or 0)
        price = float(line.get('unit_price') or 0)
        amount = qty * price
        total += amount
        if idx % 2:
            c.setFillColor(colors.HexColor('#FAFAFA'))
            c.rect(MARGIN, y - 1.5 * mm, PAGE_W - 2 * MARGIN, 7 * mm, stroke=0, fill=1)
        c.setFillColor(DARK)
        c.setFont('Helvetica', 8.3)
        c.drawString(MARGIN + 4 * mm, y + 1 * mm, str(line.get('item_name') or '')[:47])
        c.setFillColor(MID)
        c.setFont('Helvetica', 7.7)
        c.drawString(104 * mm, y + 1 * mm, str(line.get('sku') or '—')[:17])
        c.setFillColor(DARK)
        c.setFont('Helvetica', 8.3)
        c.drawRightString(143 * mm, y + 1 * mm, f'{qty:g}')
        if show_prices:
            c.drawRightString(168 * mm, y + 1 * mm, _money(price))
            c.setFont('Helvetica-Bold', 8.3)
            c.drawRightString(PAGE_W - MARGIN - 4 * mm, y + 1 * mm, _money(amount))
        y -= 8 * mm
    return y, total


def _footer(c, order, note=''):
    c.setStrokeColor(LINE)
    c.line(MARGIN, 18 * mm, PAGE_W - MARGIN, 18 * mm)
    c.setFillColor(MID)
    c.setFont('Helvetica', 7.2)
    left = []
    if order.get('vat_number'):
        left.append('BTW ' + str(order['vat_number']))
    if order.get('chamber_number'):
        left.append('KvK ' + str(order['chamber_number']))
    if order.get('invoice_email'):
        left.append(str(order['invoice_email']))
    c.drawString(MARGIN, 11 * mm, ' · '.join(left)[:90] if left else _company_name(order))
    c.drawRightString(PAGE_W - MARGIN, 11 * mm, note or 'Gegenereerd door Stockroom')


def _totals_box(c, y, total):
    y = max(y - 3 * mm, 42 * mm)
    box_w = 62 * mm
    x = PAGE_W - MARGIN - box_w
    c.setFillColor(LIGHT)
    c.roundRect(x, y - 17 * mm, box_w, 17 * mm, 3 * mm, stroke=0, fill=1)
    c.setFillColor(MID)
    c.setFont('Helvetica-Bold', 8)
    c.drawString(x + 5 * mm, y - 7 * mm, 'TOTAAL')
    c.setFillColor(DARK)
    c.setFont('Helvetica-Bold', 15)
    c.drawRightString(x + box_w - 5 * mm, y - 8 * mm, _money(total))
    return y - 22 * mm


def invoice_pdf(stockroom_id, order_id):
    order, lines, relation = _load(stockroom_id, order_id)
    if order['order_type'] != 'sales':
        raise ValueError('Facturen zijn alleen beschikbaar voor verkooporders.')
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = _draw_brand_header(c, 'FACTUUR', order, order.get('order_number') or '')
    y = _draw_info_boxes(c, y, order, relation)
    y = _draw_meta(c, y, order, 'Factuurnummer')
    y, total = _table(c, y, lines, True)
    y = _totals_box(c, y, total)
    c.setFillColor(MID)
    c.setFont('Helvetica', 8)
    c.drawString(MARGIN, max(y, 28 * mm), 'Bedankt voor uw bestelling.')
    _footer(c, order, 'Factuur gegenereerd ' + datetime.now().strftime('%d-%m-%Y %H:%M'))
    c.save()
    return buf.getvalue(), f"factuur-{order.get('order_number') or order_id}.pdf"


def packing_slip_pdf(stockroom_id, order_id):
    order, lines, relation = _load(stockroom_id, order_id)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = _draw_brand_header(c, 'PAKBON', order, order.get('order_number') or '')
    y = _draw_info_boxes(c, y, order, relation)
    y = _draw_meta(c, y, order)
    y, _ = _table(c, y, lines, False)
    y = max(y - 5 * mm, 40 * mm)
    c.setFillColor(LIGHT)
    c.roundRect(MARGIN, y - 20 * mm, PAGE_W - 2 * MARGIN, 20 * mm, 3 * mm, stroke=0, fill=1)
    c.setFillColor(DARK)
    c.setFont('Helvetica-Bold', 8)
    c.drawString(MARGIN + 5 * mm, y - 7 * mm, 'MAGAZIJNCONTROLE')
    c.setFont('Helvetica', 8)
    c.drawString(MARGIN + 5 * mm, y - 14 * mm, 'Gecontroleerd door: ____________________    Verpakt door: ____________________')
    _footer(c, order, 'Pakbon')
    c.save()
    return buf.getvalue(), f"pakbon-{order.get('order_number') or order_id}.pdf"


def return_pdf(stockroom_id, order_id):
    order, lines, relation = _load(stockroom_id, order_id)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = _draw_brand_header(c, 'RETOURDOCUMENT', order, order.get('order_number') or '')
    y = _draw_info_boxes(c, y, order, relation)
    y = _draw_meta(c, y, order)
    y, _ = _table(c, y, lines, False)
    y = max(y - 5 * mm, 48 * mm)
    c.setFillColor(LIGHT)
    c.roundRect(MARGIN, y - 28 * mm, PAGE_W - 2 * MARGIN, 28 * mm, 3 * mm, stroke=0, fill=1)
    c.setFillColor(DARK)
    c.setFont('Helvetica-Bold', 8)
    c.drawString(MARGIN + 5 * mm, y - 7 * mm, 'RETOURGEGEVENS')
    c.setFont('Helvetica', 8)
    c.drawString(MARGIN + 5 * mm, y - 14 * mm, 'Reden retour: ______________________________________________________________')
    c.drawString(MARGIN + 5 * mm, y - 21 * mm, 'Verwerkt door: ____________________    Datum: ____________________')
    _footer(c, order, 'Retourdocument')
    c.save()
    return buf.getvalue(), f"retour-{order.get('order_number') or order_id}.pdf"
