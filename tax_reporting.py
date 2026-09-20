"""VAT reporting, corrections and traceable accountant export."""
import csv
import io
import json
import uuid
import zipfile
from datetime import date

import server


def initialize():
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS tax_adjustments(id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
            adjustment_date DATE NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('output','input')),net_amount NUMERIC(14,2) NOT NULL DEFAULT 0,vat_amount NUMERIC(14,2) NOT NULL,
            reason TEXT NOT NULL,created_by UUID REFERENCES users(id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tax_adjustments_room_date ON tax_adjustments(stockroom_id,adjustment_date)");conn.commit()


def _period(year, quarter):
    try:year=int(year);quarter=int(quarter)
    except (TypeError,ValueError):raise ValueError('Kies een geldig jaar en kwartaal.')
    if year<2000 or year>2100 or quarter not in (1,2,3,4):raise ValueError('Kies een geldig jaar en kwartaal.')
    month=(quarter-1)*3+1;start=date(year,month,1);end=date(year+1,1,1) if quarter==4 else date(year,month+3,1)
    return year,quarter,start,end


def report(stockroom_id, year, quarter):
    year,quarter,start,end=_period(year,quarter)
    with server.db() as conn:
        sales=conn.execute("""SELECT i.order_id::text id,i.invoice_number,i.invoice_date,o.relation_name,
            ROUND(COALESCE(SUM(l.quantity*l.unit_price),0),2)::float8 net,ROUND(COALESCE(SUM(l.quantity*l.unit_price),0)*i.vat_percent/100,2)::float8 vat,
            ROUND(COALESCE(SUM(l.quantity*l.unit_price),0)*(1+i.vat_percent/100),2)::float8 total,i.vat_percent::float8 vat_percent,COALESCE((SELECT SUM(c.amount) FROM credit_notes c WHERE c.order_id=i.order_id AND c.stockroom_id=i.stockroom_id),0)::float8 credited
            FROM invoice_documents i JOIN orders o ON o.id=i.order_id LEFT JOIN order_lines l ON l.order_id=i.order_id
            WHERE i.stockroom_id=%s AND i.deleted_at IS NULL AND i.invoice_date>=%s AND i.invoice_date<%s GROUP BY i.order_id,i.invoice_number,i.invoice_date,o.relation_name,i.vat_percent ORDER BY i.invoice_date,i.invoice_number""",(stockroom_id,start,end)).fetchall()
        quote_sales=conn.execute("""SELECT q.id::text id,q.invoice_number,q.invoice_date,q.relation_name,
            ROUND(COALESCE(SUM(l.quantity*l.unit_price),0),2)::float8 net,ROUND(COALESCE(SUM(l.quantity*l.unit_price),0)*q.invoice_vat_percent/100,2)::float8 vat,
            ROUND(COALESCE(SUM(l.quantity*l.unit_price),0)*(1+q.invoice_vat_percent/100),2)::float8 total,q.invoice_vat_percent::float8 vat_percent
            FROM quotes q LEFT JOIN quote_lines l ON l.quote_id=q.id WHERE q.stockroom_id=%s AND q.invoice_number IS NOT NULL AND q.converted_order_id IS NULL AND q.invoice_date>=%s AND q.invoice_date<%s
            GROUP BY q.id,q.invoice_number,q.invoice_date,q.relation_name,q.invoice_vat_percent ORDER BY q.invoice_date,q.invoice_number""",(stockroom_id,start,end)).fetchall()
        purchases=conn.execute("""SELECT i.id::text,i.invoice_number,i.invoice_date,o.relation_name,(i.subtotal+i.shipping_amount)::float8 net,i.vat_amount::float8 vat,i.total_amount::float8 total,i.status,COALESCE((SELECT SUM(c.amount) FROM purchase_invoice_credits c WHERE c.invoice_id=i.id),0)::float8 credited
            FROM purchase_invoices i JOIN orders o ON o.id=i.order_id WHERE i.stockroom_id=%s AND i.status<>'rejected' AND i.invoice_date>=%s AND i.invoice_date<%s ORDER BY i.invoice_date,i.invoice_number""",(stockroom_id,start,end)).fetchall()
        adjustments=conn.execute("SELECT id::text,adjustment_date,kind,net_amount::float8,vat_amount::float8,reason FROM tax_adjustments WHERE stockroom_id=%s AND adjustment_date>=%s AND adjustment_date<%s ORDER BY adjustment_date,created_at",(stockroom_id,start,end)).fetchall()
    sales=list(sales)+list(quote_sales);purchases=list(purchases)
    for row in sales:
        credit=float(row.get('credited') or 0);gross=float(row['total'])
        if credit and gross:factor=max(0,(gross-credit)/gross);row['net']=round(float(row['net'])*factor,2);row['vat']=round(float(row['vat'])*factor,2);row['total']=round(gross-credit,2)
    for row in purchases:
        credit=float(row.get('credited') or 0);gross=float(row['total'])
        if credit and gross:factor=max(0,(gross-credit)/gross);row['net']=round(float(row['net'])*factor,2);row['vat']=round(float(row['vat'])*factor,2);row['total']=round(gross-credit,2)
    output_vat=sum(float(x['vat']) for x in sales)+sum(float(x['vat_amount']) for x in adjustments if x['kind']=='output');input_vat=sum(float(x['vat']) for x in purchases)+sum(float(x['vat_amount']) for x in adjustments if x['kind']=='input')
    exceptions=[]
    for row in sales:
        if float(row['total'])>0 and float(row['vat'])==0:exceptions.append({'type':'sales','id':row['id'],'number':row['invoice_number'],'message':'Verkoopfactuur zonder btw; controleer het toegepaste tarief.'})
    for row in purchases:
        if float(row['total'])>0 and float(row['vat'])==0:exceptions.append({'type':'purchase','id':row['id'],'number':row['invoice_number'],'message':'Inkoopfactuur zonder voorbelasting; controleer de factuur.'})
        if row['status']=='blocked':exceptions.append({'type':'purchase','id':row['id'],'number':row['invoice_number'],'message':'Inkoopfactuur heeft nog een three-way-matchafwijking.'})
    return {'year':year,'quarter':quarter,'start':start,'end':end,'sales':sales,'purchases':purchases,'adjustments':adjustments,'exceptions':exceptions,'summary':{'turnover':round(sum(float(x['net']) for x in sales),2),'purchases':round(sum(float(x['net']) for x in purchases),2),'outputVat':round(output_vat,2),'inputVat':round(input_vat,2),'payable':round(output_vat-input_vat,2)}}


def add_adjustment(session, values):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan btw-correcties boeken.')
    kind=(values.get('kind') or '').strip();reason=(values.get('reason') or '').strip()[:1000]
    if kind not in ('output','input') or not reason:raise ValueError('Kies het correctietype en vul een reden in.')
    try:net=round(float(values.get('net_amount') or 0),2);vat=round(float(values.get('vat_amount') or 0),2);when=date.fromisoformat(values.get('adjustment_date') or '')
    except (TypeError,ValueError):raise ValueError('Controleer datum en bedragen.')
    if vat==0:raise ValueError('Het btw-correctiebedrag mag niet nul zijn.')
    adjustment_id=str(uuid.uuid4())
    with server.db() as conn:
        conn.execute("INSERT INTO tax_adjustments(id,stockroom_id,adjustment_date,kind,net_amount,vat_amount,reason,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",(adjustment_id,session['stockroom_id'],when,kind,net,vat,reason,session['user_id']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'tax.adjustment_created',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':adjustment_id,'kind':kind,'vat':vat,'reason':reason})));conn.commit()
    return {'created':True,'id':adjustment_id}


def delete_adjustment(session, adjustment_id):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan btw-correcties verwijderen.')
    with server.db() as conn:
        row=conn.execute("DELETE FROM tax_adjustments WHERE id=%s AND stockroom_id=%s RETURNING reason,vat_amount::float8 vat",(adjustment_id,session['stockroom_id'])).fetchone()
        if not row:raise ValueError('Btw-correctie niet gevonden.')
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'tax.adjustment_deleted',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':adjustment_id,'vat':row['vat'],'reason':row['reason']})));conn.commit()
    return {'deleted':True}


def _csv(rows, fields):
    stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore',delimiter=';');writer.writeheader();writer.writerows(rows);return ('\ufeff'+stream.getvalue()).encode('utf-8')


def export(session, year, quarter):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan de accountantsexport downloaden.')
    data=report(session['stockroom_id'],year,quarter);buffer=io.BytesIO()
    with server.db() as conn:bank=conn.execute("SELECT booking_date,amount::float8 amount,currency,counterparty_name,counterparty_iban,reference,description,status FROM bank_transactions WHERE stockroom_id=%s AND booking_date>=%s AND booking_date<%s ORDER BY booking_date,created_at",(session['stockroom_id'],data['start'],data['end'])).fetchall()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('btw-samenvatting.csv',_csv([{'jaar':data['year'],'kwartaal':data['quarter'],'omzet_ex_btw':data['summary']['turnover'],'inkopen_ex_btw':data['summary']['purchases'],'verschuldigde_btw':data['summary']['outputVat'],'voorbelasting':data['summary']['inputVat'],'te_betalen':data['summary']['payable']}],['jaar','kwartaal','omzet_ex_btw','inkopen_ex_btw','verschuldigde_btw','voorbelasting','te_betalen']))
        archive.writestr('verkoopfacturen.csv',_csv(data['sales'],['invoice_date','invoice_number','relation_name','net','vat_percent','vat','total']))
        archive.writestr('inkoopfacturen.csv',_csv(data['purchases'],['invoice_date','invoice_number','relation_name','net','vat','total','status']))
        archive.writestr('btw-correcties.csv',_csv(data['adjustments'],['adjustment_date','kind','net_amount','vat_amount','reason']))
        archive.writestr('bankmutaties.csv',_csv(bank,['booking_date','amount','currency','counterparty_name','counterparty_iban','reference','description','status']))
        archive.writestr('controlepunten.csv',_csv(data['exceptions'],['type','number','message']))
    return buffer.getvalue(),f"accountantsexport-{data['year']}-Q{data['quarter']}.zip"
