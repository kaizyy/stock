"""Accrual profit-and-loss and margin analysis."""
import csv
import io
import json
import uuid
import zipfile
from calendar import monthrange
from datetime import date

import server


def initialize():
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS operating_expenses(id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
            expense_date DATE NOT NULL,category TEXT NOT NULL,supplier_name TEXT NOT NULL DEFAULT '',description TEXT NOT NULL,net_amount NUMERIC(14,2) NOT NULL CHECK(net_amount>=0),vat_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
            created_by UUID REFERENCES users(id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_operating_expenses_room_date ON operating_expenses(stockroom_id,expense_date)");conn.commit()


def _period(year, mode, value):
    try:year=int(year);value=int(value)
    except (TypeError,ValueError):raise ValueError('Kies een geldige rapportageperiode.')
    if mode=='month' and 1<=value<=12:start=date(year,value,1);end=date(year+1,1,1) if value==12 else date(year,value+1,1)
    elif mode=='quarter' and 1<=value<=4:month=(value-1)*3+1;start=date(year,month,1);end=date(year+1,1,1) if value==4 else date(year,month+3,1)
    elif mode=='year':start=date(year,1,1);end=date(year+1,1,1);value=year
    else:raise ValueError('Kies maand, kwartaal of jaar.')
    return start,end


def _previous(start, end):
    days=(end-start).days;return start.fromordinal(start.toordinal()-days),start


def _data(stockroom_id, start, end):
    with server.db() as conn:
        room=conn.execute("SELECT state FROM stockrooms WHERE id=%s",(stockroom_id,)).fetchone();state=(room or {}).get('state') or {'items':[]};items={str(x.get('id')):x for x in state.get('items',[])}
        lines=conn.execute("""SELECT i.invoice_number,i.invoice_date,o.id::text order_id,o.relation_name,l.item_id,l.item_name,l.quantity::float8,l.unit_price::float8,l.cost_price::float8,
            COALESCE((SELECT SUM(c.amount) FROM credit_notes c WHERE c.order_id=o.id AND c.stockroom_id=o.stockroom_id),0)::float8 credited,
            ROUND(COALESCE(SUM(l.quantity*l.unit_price) OVER(PARTITION BY o.id),0),2)::float8 invoice_net,ROUND(COALESCE(SUM(l.quantity*l.unit_price) OVER(PARTITION BY o.id),0)*(1+i.vat_percent/100),2)::float8 invoice_gross
            FROM invoice_documents i JOIN orders o ON o.id=i.order_id JOIN order_lines l ON l.order_id=o.id WHERE i.stockroom_id=%s AND i.deleted_at IS NULL AND i.invoice_date>=%s AND i.invoice_date<%s ORDER BY i.invoice_date,i.invoice_number""",(stockroom_id,start,end)).fetchall()
        quote_rows=conn.execute("""SELECT q.invoice_number,q.invoice_date,q.relation_name,l.item_id,l.item_name,l.quantity::float8,l.unit_price::float8,NULL::float8 cost_price,0::float8 credited,
            ROUND(COALESCE(SUM(l.quantity*l.unit_price) OVER(PARTITION BY q.id),0),2)::float8 invoice_net,NULL::float8 invoice_gross FROM quotes q JOIN quote_lines l ON l.quote_id=q.id
            WHERE q.stockroom_id=%s AND q.invoice_number IS NOT NULL AND q.converted_order_id IS NULL AND q.invoice_date>=%s AND q.invoice_date<%s""",(stockroom_id,start,end)).fetchall()
        expenses=conn.execute("SELECT id::text,expense_date,category,supplier_name,description,net_amount::float8,vat_amount::float8 FROM operating_expenses WHERE stockroom_id=%s AND expense_date>=%s AND expense_date<%s ORDER BY expense_date",(stockroom_id,start,end)).fetchall()
        fees=conn.execute("""SELECT a.id::text,t.booking_date expense_date,'Bankkosten' category,t.counterparty_name supplier_name,COALESCE(t.description,t.reference,'Bankkosten') description,a.amount::float8 net_amount,0::float8 vat_amount
            FROM bank_allocations a JOIN bank_transactions t ON t.id=a.transaction_id WHERE t.stockroom_id=%s AND a.target_type='bank_fee' AND t.booking_date>=%s AND t.booking_date<%s""",(stockroom_id,start,end)).fetchall()
    rows=[]
    for row in list(lines)+list(quote_rows):
        item=items.get(str(row['item_id'])) or {};revenue=float(row['quantity'])*float(row['unit_price']);credit_ratio=min(1,float(row['credited'] or 0)/float(row.get('invoice_gross') or row['invoice_net'] or 1));revenue*=1-credit_ratio;estimated=row['cost_price'] is None;unit_cost=float(row['cost_price']) if row['cost_price'] is not None else float(item.get('buy') or 0);cost=float(row['quantity'])*unit_cost*(1-credit_ratio)
        rows.append({'invoice_number':row['invoice_number'],'invoice_date':row['invoice_date'],'customer':row['relation_name'] or 'Geen klant','item_id':str(row['item_id']),'item':row['item_name'],'supplier':item.get('supplier') or 'Niet gekoppeld','quantity':row['quantity'],'revenue':round(revenue,2),'cost':round(cost,2),'margin':round(revenue-cost,2),'estimatedCost':estimated})
    expenses=list(expenses)+list(fees);revenue=round(sum(x['revenue'] for x in rows),2);cogs=round(sum(x['cost'] for x in rows),2);expense_total=round(sum(float(x['net_amount']) for x in expenses),2)
    return {'lines':rows,'expenses':expenses,'summary':{'revenue':revenue,'costOfGoods':cogs,'grossProfit':round(revenue-cogs,2),'operatingExpenses':expense_total,'netProfit':round(revenue-cogs-expense_total,2),'grossMarginPercent':round((revenue-cogs)/revenue*100,1) if revenue else 0},'estimatedCount':sum(1 for x in rows if x['estimatedCost'])}


def _group(rows, key):
    grouped={}
    for row in rows:
        target=grouped.setdefault(row[key],{'name':row[key],'revenue':0,'cost':0,'margin':0});target['revenue']+=row['revenue'];target['cost']+=row['cost'];target['margin']+=row['margin']
    for value in grouped.values():value.update(revenue=round(value['revenue'],2),cost=round(value['cost'],2),margin=round(value['margin'],2),marginPercent=round(value['margin']/value['revenue']*100,1) if value['revenue'] else 0)
    return sorted(grouped.values(),key=lambda x:x['margin'],reverse=True)


def report(stockroom_id, year, mode, value):
    year=int(year);value=int(value);start,end=_period(year,mode,value);current=_data(stockroom_id,start,end)
    if mode=='month':previous_start,previous_end=_period(year-1 if value==1 else year,'month',12 if value==1 else value-1)
    elif mode=='quarter':previous_start,previous_end=_period(year-1 if value==1 else year,'quarter',4 if value==1 else value-1)
    else:previous_start,previous_end=_period(year-1,'year',year-1)
    previous=_data(stockroom_id,previous_start,previous_end);current['period']={'start':start,'end':end,'year':year,'mode':mode,'value':value};current['previous']=previous['summary'];current['change']={key:round(current['summary'][key]-previous['summary'][key],2) for key in ('revenue','grossProfit','netProfit')};current['byItem']=_group(current['lines'],'item');current['byCustomer']=_group(current['lines'],'customer');current['bySupplier']=_group(current['lines'],'supplier');current['alerts']=[]
    if current['summary']['netProfit']<0:current['alerts'].append('Het nettoresultaat is negatief.')
    if current['change']['grossProfit']<0 and previous['summary']['grossProfit']>0:current['alerts'].append('De brutowinst is lager dan in de vorige vergelijkbare periode.')
    low=[x['name'] for x in current['byItem'] if x['revenue']>0 and x['marginPercent']<10]
    if low:current['alerts'].append(f"Lage marge onder 10%: {', '.join(low[:5])}.")
    if current['estimatedCount']:current['alerts'].append(f"Voor {current['estimatedCount']} regel(s) is de huidige artikelkostprijs gebruikt; historische kostprijs ontbrak.")
    return current


def save_expense(session, values):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan bedrijfskosten boeken.')
    category=(values.get('category') or '').strip()[:100];description=(values.get('description') or '').strip()[:500];supplier=(values.get('supplier_name') or '').strip()[:200]
    try:when=date.fromisoformat(values.get('expense_date') or '');net=round(float(values.get('net_amount') or 0),2);vat=round(float(values.get('vat_amount') or 0),2)
    except (TypeError,ValueError):raise ValueError('Controleer datum en bedragen.')
    if not category or not description or net<0:raise ValueError('Vul categorie, omschrijving en een geldig bedrag in.')
    expense_id=str(uuid.uuid4())
    with server.db() as conn:conn.execute("INSERT INTO operating_expenses(id,stockroom_id,expense_date,category,supplier_name,description,net_amount,vat_amount,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",(expense_id,session['stockroom_id'],when,category,supplier,description,net,vat,session['user_id']));conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'expense.created',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':expense_id,'category':category,'net':net})));conn.commit()
    return {'created':True,'id':expense_id}


def delete_expense(session, expense_id):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan bedrijfskosten verwijderen.')
    with server.db() as conn:
        row=conn.execute("DELETE FROM operating_expenses WHERE id=%s AND stockroom_id=%s RETURNING category,net_amount::float8 net",(expense_id,session['stockroom_id'])).fetchone()
        if not row:raise ValueError('Bedrijfskostenpost niet gevonden.')
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'expense.deleted',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':expense_id,'category':row['category'],'net':row['net']})));conn.commit()
    return {'deleted':True}


def _csv(rows, fields):
    stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore',delimiter=';');writer.writeheader();writer.writerows(rows);return ('\ufeff'+stream.getvalue()).encode()


def export(session, year, mode, value):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan het managementrapport exporteren.')
    data=report(session['stockroom_id'],year,mode,value);buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('winst-en-verlies.csv',_csv([data['summary']],['revenue','costOfGoods','grossProfit','operatingExpenses','netProfit','grossMarginPercent']))
        archive.writestr('marges-per-regel.csv',_csv(data['lines'],['invoice_date','invoice_number','customer','item','supplier','quantity','revenue','cost','margin','estimatedCost']))
        archive.writestr('bedrijfskosten.csv',_csv(data['expenses'],['expense_date','category','supplier_name','description','net_amount','vat_amount']))
        archive.writestr('marges-per-artikel.csv',_csv(data['byItem'],['name','revenue','cost','margin','marginPercent']))
        archive.writestr('marges-per-klant.csv',_csv(data['byCustomer'],['name','revenue','cost','margin','marginPercent']))
        archive.writestr('marges-per-leverancier.csv',_csv(data['bySupplier'],['name','revenue','cost','margin','marginPercent']))
    return buffer.getvalue(),f"resultaatrapport-{data['period']['start']}-{data['period']['end']}.zip"
