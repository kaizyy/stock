"""Local supplier-invoice OCR and conservative field suggestions."""
import base64
import io
import re
from datetime import date, timedelta

import server


def _amount(value):
    value=re.sub(r'[^0-9,.-]','',value or '')
    if ',' in value:value=value.replace('.','').replace(',','.')
    elif value.count('.')>1:value=value.replace('.','')
    try:return round(float(value),2)
    except ValueError:return None


def _iso_date(value):
    value=(value or '').strip()
    for pattern in (r'(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})',r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})'):
        found=re.search(pattern,value)
        if found:
            parts=[int(x) for x in found.groups()]
            try:return date(parts[2],parts[1],parts[0]).isoformat() if len(found.group(1))<4 else date(parts[0],parts[1],parts[2]).isoformat()
            except ValueError:pass
    return ''


def extract_text(data, mime):
    if mime=='application/pdf':
        from pypdf import PdfReader
        reader=PdfReader(io.BytesIO(data));return '\n'.join(page.extract_text() or '' for page in reader.pages[:20])
    if mime in ('image/jpeg','image/png'):
        from PIL import Image
        import pytesseract
        return pytesseract.image_to_string(Image.open(io.BytesIO(data)),lang='nld+eng')
    raise ValueError('Gebruik een PDF, JPG of PNG.')


def _first(text, patterns):
    for pattern in patterns:
        found=re.search(pattern,text,re.I|re.M)
        if found:return found.group(1).strip()
    return ''


def parse_text(text):
    compact=re.sub(r'[ \t]+',' ',text or '')
    number=_first(compact,[r'(?:factuurnummer|invoice\s*(?:no|number|nr)?|factuur\s*nr)\s*[:#]?\s*([A-Z0-9][A-Z0-9./_-]{2,})'])
    invoice_date=_iso_date(_first(compact,[r'(?:factuurdatum|invoice\s*date|datum)\s*:?\s*([^\n]{6,20})']))
    due_date=_iso_date(_first(compact,[r'(?:vervaldatum|vervallen\s*op|due\s*date|payment\s*due)\s*:?\s*([^\n]{6,20})']))
    subtotal=_amount(_first(compact,[r'(?:subtotaal|subtotal)\s*(?:€|EUR)?\s*([0-9.,]+)']))
    vat=_amount(_first(compact,[r'(?:btw|vat)(?:\s*\d+(?:[,.]\d+)?\s*%)?\s*(?:€|EUR)?\s*([0-9.,]+)']))
    shipping=_amount(_first(compact,[r'(?:verzendkosten|shipping|freight)\s*(?:€|EUR)?\s*([0-9.,]+)']))
    total=_amount(_first(compact,[r'(?:^|\s)(?:totaal(?:\s*te\s*betalen)?|amount\s*due|total)\s*(?:€|EUR)?\s*([0-9.,]+)']))
    iban=_first(compact,[r'\b(NL\s?\d{2}(?:\s?[A-Z0-9]){14})\b',r'\b([A-Z]{2}\s?\d{2}(?:\s?[A-Z0-9]){11,30})\b']).replace(' ','').upper()
    reference=_first(compact,[r'(?:betalingskenmerk|payment\s*reference|omschrijving)\s*:?\s*([^\n]{3,70})'])
    order_reference=_first(compact,[r'(?:inkooporder|bestelnummer|purchase\s*order|order\s*(?:no|nr|number))\s*[:#]?\s*([A-Z0-9][A-Z0-9./_-]{2,})'])
    if not due_date and invoice_date:
        due_date=(date.fromisoformat(invoice_date)+timedelta(days=30)).isoformat()
    return {'invoice_number':number,'invoice_date':invoice_date,'due_date':due_date,'subtotal':subtotal,'vat_amount':vat,'shipping_amount':shipping,'total_amount':total,'iban':iban,'payment_reference':reference,'order_reference':order_reference}


def recognize(session, values):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om inkoopfacturen te herkennen.')
    mime=(values.get('document_mime') or '')[:100]
    try:data=base64.b64decode(values.get('document_base64') or '',validate=True)
    except Exception as exc:raise ValueError('Factuurbestand is ongeldig.') from exc
    if not data or len(data)>5*1024*1024:raise ValueError('Gebruik een bestand van maximaal 5 MB.')
    try:text=extract_text(data,mime)
    except Exception as exc:raise ValueError('De factuurtekst kon niet worden gelezen. Controleer het bestand of vul de gegevens handmatig in.') from exc
    parsed=parse_text(text)
    with server.db() as conn:
        suppliers=conn.execute("SELECT id::text,name,email,iban FROM suppliers WHERE stockroom_id=%s",(session['stockroom_id'],)).fetchall()
        orders=conn.execute("SELECT id::text,COALESCE(order_number,reference,'') number,relation_id::text,relation_name FROM orders WHERE stockroom_id=%s AND order_type='purchase' ORDER BY created_at DESC",(session['stockroom_id'],)).fetchall()
    lower=text.lower();ranked=[]
    for supplier in suppliers:
        score=0
        if supplier['iban'] and supplier['iban'].replace(' ','').lower() in re.sub(r'\s+','',lower):score+=100
        if supplier['email'] and supplier['email'].lower() in lower:score+=60
        if supplier['name'] and supplier['name'].lower() in lower:score+=40
        if score:ranked.append((score,supplier))
    supplier=max(ranked,key=lambda item:item[0])[1] if ranked else None
    reference=(parsed['order_reference'] or '').lower();order=None
    if reference:order=next((row for row in orders if row['number'] and (reference in row['number'].lower() or row['number'].lower() in reference)),None)
    if not order and supplier:order=next((row for row in orders if row['relation_id']==supplier['id']),None)
    confidence=sum(bool(parsed[key]) for key in ('invoice_number','invoice_date','due_date','total_amount'))/4
    return {'recognized':True,'fields':parsed,'supplier':supplier,'order':order,'confidence':confidence,'textPreview':re.sub(r'\s+',' ',text)[:500]}
