"""30/60/90-day cash-flow forecasting with explicit scenarios."""
import json
from datetime import date, timedelta

import server
import financial_workflow
import purchase_invoices
import payment_batches


def initialize():
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS cashflow_settings(stockroom_id UUID PRIMARY KEY REFERENCES stockrooms(id) ON DELETE CASCADE,
            current_balance NUMERIC(14,2) NOT NULL DEFAULT 0,balance_date DATE NOT NULL DEFAULT CURRENT_DATE,minimum_buffer NUMERIC(14,2) NOT NULL DEFAULT 0,
            updated_by UUID REFERENCES users(id) ON DELETE SET NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""");conn.commit()


def settings(stockroom_id):
    with server.db() as conn:row=conn.execute("SELECT current_balance::float8,balance_date,minimum_buffer::float8 FROM cashflow_settings WHERE stockroom_id=%s",(stockroom_id,)).fetchone()
    return {'configured':bool(row),**(row or {'current_balance':0.0,'balance_date':date.today(),'minimum_buffer':0.0})}


def save_settings(session, values):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan kasstroominstellingen wijzigen.')
    try:balance=round(float(values.get('current_balance') or 0),2);minimum=max(0,round(float(values.get('minimum_buffer') or 0),2));when=date.fromisoformat(values.get('balance_date') or '')
    except (TypeError,ValueError):raise ValueError('Controleer banksaldo, peildatum en minimumbuffer.')
    if when>date.today():raise ValueError('De peildatum mag niet in de toekomst liggen.')
    with server.db() as conn:
        conn.execute("""INSERT INTO cashflow_settings(stockroom_id,current_balance,balance_date,minimum_buffer,updated_by) VALUES(%s,%s,%s,%s,%s)
            ON CONFLICT(stockroom_id) DO UPDATE SET current_balance=EXCLUDED.current_balance,balance_date=EXCLUDED.balance_date,minimum_buffer=EXCLUDED.minimum_buffer,updated_by=EXCLUDED.updated_by,updated_at=NOW()""",(session['stockroom_id'],balance,when,minimum,session['user_id']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'cashflow.settings_updated',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'balance':balance,'balanceDate':when.isoformat(),'minimumBuffer':minimum})));conn.commit()
    return {'saved':True,**settings(session['stockroom_id'])}


def _clamp_day(value, today):
    if not value:return today+timedelta(days=30)
    if isinstance(value,str):value=date.fromisoformat(value[:10])
    return max(today+timedelta(days=1),value)


def _events(stockroom_id):
    today=date.today();events=[];batches=payment_batches.overview(stockroom_id)['batches'];batched=set()
    for batch in batches:
        if batch['status'] in ('approved','exported'):
            for item in batch['items']:batched.add(item['invoice_id'])
            events.append({'date':_clamp_day(batch['execution_date'],today),'kind':'payment_batch','direction':'out','label':batch['batch_number'],'amount':float(batch['total']),'certainty':1.0})
    for invoice in financial_workflow.list_invoices(stockroom_id):
        if invoice['outstanding']>0:events.append({'date':_clamp_day(invoice.get('due_date'),today),'kind':'sales_invoice','direction':'in','label':invoice['invoice_number'],'amount':float(invoice['outstanding']),'certainty':0.9})
    purchase_rows=purchase_invoices.rows(stockroom_id)
    for invoice in purchase_rows:
        if invoice['id'] not in batched and invoice['status'] in ('approved','partially_disputed') and invoice['outstanding']>0:events.append({'date':_clamp_day(invoice['due_date'],today),'kind':'purchase_invoice','direction':'out','label':invoice['invoice_number'],'amount':float(invoice['outstanding']),'certainty':1.0})
    with server.db() as conn:
        orders=conn.execute("""SELECT o.id::text,COALESCE(o.order_number,o.reference,'Inkooporder') label,COALESCE(o.expected_delivery_date,o.confirmed_delivery_date,o.order_date+14) expected_date,
            COALESCE(SUM((l.quantity-l.supplier_cancelled_quantity)*l.unit_price),0)::float8 total FROM orders o JOIN order_lines l ON l.order_id=o.id
            WHERE o.stockroom_id=%s AND o.order_type='purchase' AND o.status IN ('approved','ordered','partial') AND NOT EXISTS(SELECT 1 FROM purchase_invoices i WHERE i.order_id=o.id AND i.status<>'rejected') GROUP BY o.id ORDER BY expected_date""",(stockroom_id,)).fetchall()
        costs=conn.execute("""SELECT COALESCE(SUM(net_amount),0)::float8 total FROM operating_expenses WHERE stockroom_id=%s AND expense_date>=CURRENT_DATE-90 AND expense_date<CURRENT_DATE""",(stockroom_id,)).fetchone()['total']
        fees_ready=conn.execute("SELECT to_regclass('public.bank_allocations') IS NOT NULL AS ready").fetchone()['ready'];fees=0
        if fees_ready:fees=conn.execute("SELECT COALESCE(SUM(a.amount),0)::float8 total FROM bank_allocations a JOIN bank_transactions t ON t.id=a.transaction_id WHERE t.stockroom_id=%s AND a.target_type='bank_fee' AND t.booking_date>=CURRENT_DATE-90 AND t.booking_date<CURRENT_DATE",(stockroom_id,)).fetchone()['total']
    for order in orders:
        if order['total']>0:events.append({'date':_clamp_day(order['expected_date'],today),'kind':'purchase_order','direction':'out','label':order['label'],'amount':float(order['total']),'certainty':0.9})
    monthly=round((float(costs)+float(fees))/3,2)
    if monthly>0:
        for days in (30,60,90):events.append({'date':today+timedelta(days=days),'kind':'recurring_costs','direction':'out','label':'Geschatte terugkerende bedrijfskosten','amount':monthly,'certainty':0.8})
    return sorted(events,key=lambda x:(x['date'],x['direction']))


def _scenario(opening, events, factor, horizon):
    today=date.today();end=today+timedelta(days=horizon);balance=float(opening);points=[{'date':today,'balance':round(balance,2),'incoming':0,'outgoing':0}];incoming=outgoing=0
    for day_index in range(1,horizon+1):
        day=today+timedelta(days=day_index);day_in=day_out=0
        for event in events:
            if event['date']!=day:continue
            if event['direction']=='in':day_in+=event['amount']*factor*event['certainty']
            else:day_out+=event['amount']
        incoming+=day_in;outgoing+=day_out;balance+=day_in-day_out;points.append({'date':day,'balance':round(balance,2),'incoming':round(day_in,2),'outgoing':round(day_out,2)})
    return {'horizon':horizon,'endingBalance':round(balance,2),'incoming':round(incoming,2),'outgoing':round(outgoing,2),'minimumBalance':round(min(x['balance'] for x in points),2),'points':points}


def forecast(stockroom_id):
    config=settings(stockroom_id);events=_events(stockroom_id);scenarios={}
    for name,factor in (('conservative',0.7),('expected',1.0),('optimistic',1.1)):scenarios[name]={str(days):_scenario(config['current_balance'],events,factor,days) for days in (30,60,90)}
    alerts=[];expected=scenarios['expected']['90'];minimum=float(config['minimum_buffer'])
    first_negative=next((point for point in expected['points'] if point['balance']<0),None);first_buffer=next((point for point in expected['points'] if point['balance']<minimum),None)
    if first_negative:alerts.append({'severity':'danger','message':f"Verwacht negatief saldo vanaf {first_negative['date'].strftime('%d-%m-%Y')}."})
    elif first_buffer:alerts.append({'severity':'warning','message':f"De minimumbuffer wordt naar verwachting onderschreden vanaf {first_buffer['date'].strftime('%d-%m-%Y')}."})
    unconfigured=not config['configured']
    if unconfigured:alerts.append({'severity':'warning','message':'Vul het actuele banksaldo in om de prognose betrouwbaar te maken.'})
    return {'settings':config,'events':events,'scenarios':scenarios,'alerts':alerts,'assumptions':{'conservative':'70% van verwachte ontvangsten; uitgaven volledig','expected':'90% zekerheid per verkoopfactuur; uitgaven volledig','optimistic':'110% ontvangstsnelheid begrensd door factuurbedragen; uitgaven volledig'}}
