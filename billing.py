import json
import os
import urllib.parse
import urllib.request

import server

PLANS = {
    'free': {'name':'Free','price_cents':0,'users':2,'stockrooms':1,'orders_month':50},
    'pro': {'name':'Pro','price_cents':1900,'users':10,'stockrooms':3,'orders_month':1000},
    'business': {'name':'Business','price_cents':4900,'users':50,'stockrooms':10,'orders_month':10000},
}


def initialize_billing():
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS billing_accounts(
          stockroom_id UUID PRIMARY KEY REFERENCES stockrooms(id) ON DELETE CASCADE,
          plan TEXT NOT NULL DEFAULT 'free', status TEXT NOT NULL DEFAULT 'active',
          company_name TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '', postal_code TEXT NOT NULL DEFAULT '',
          city TEXT NOT NULL DEFAULT '', country TEXT NOT NULL DEFAULT 'NL', vat_number TEXT NOT NULL DEFAULT '',
          chamber_number TEXT NOT NULL DEFAULT '', invoice_email TEXT NOT NULL DEFAULT '',
          stripe_customer_id TEXT NOT NULL DEFAULT '', stripe_subscription_id TEXT NOT NULL DEFAULT '',
          current_period_end TIMESTAMPTZ, cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("INSERT INTO billing_accounts(stockroom_id) SELECT id FROM stockrooms ON CONFLICT(stockroom_id) DO NOTHING")
        conn.commit()


def account(stockroom_id):
    with server.db() as conn:
        row=conn.execute('SELECT * FROM billing_accounts WHERE stockroom_id=%s',(stockroom_id,)).fetchone()
    data=dict(row or {})
    plan=data.get('plan','free')
    data['plan_details']=PLANS.get(plan,PLANS['free'])
    data['plans']=PLANS
    data['stripe_configured']=bool(os.environ.get('STRIPE_SECRET_KEY'))
    return data


def save_profile(stockroom_id, values):
    fields=['company_name','address','postal_code','city','country','vat_number','chamber_number','invoice_email']
    vals=[str(values.get(f) or '').strip()[:500] for f in fields]
    with server.db() as conn:
        conn.execute("""UPDATE billing_accounts SET company_name=%s,address=%s,postal_code=%s,city=%s,country=%s,
          vat_number=%s,chamber_number=%s,invoice_email=%s,updated_at=NOW() WHERE stockroom_id=%s""",(*vals,stockroom_id))
        conn.commit()
    return {'saved':True}


def _stripe(method,path,data=None):
    key=os.environ.get('STRIPE_SECRET_KEY','')
    if not key: raise ValueError('Stripe is nog niet geconfigureerd.')
    encoded=urllib.parse.urlencode(data or {},doseq=True).encode()
    req=urllib.request.Request('https://api.stripe.com/v1'+path,data=encoded if method!='GET' else None,method=method)
    req.add_header('Authorization','Basic '+__import__('base64').b64encode((key+':').encode()).decode())
    req.add_header('Content-Type','application/x-www-form-urlencoded')
    with urllib.request.urlopen(req,timeout=20) as resp: return json.loads(resp.read())


def checkout(stockroom_id, plan, success_url, cancel_url):
    if plan not in ('pro','business'): raise ValueError('Ongeldig abonnement.')
    price=os.environ.get('STRIPE_PRICE_PRO' if plan=='pro' else 'STRIPE_PRICE_BUSINESS','')
    if not price: raise ValueError('Stripe prijs-ID voor dit pakket ontbreekt.')
    acct=account(stockroom_id)
    data={'mode':'subscription','success_url':success_url,'cancel_url':cancel_url,'line_items[0][price]':price,'line_items[0][quantity]':'1','metadata[stockroom_id]':str(stockroom_id),'metadata[plan]':plan,'allow_promotion_codes':'true'}
    if acct.get('stripe_customer_id'): data['customer']=acct['stripe_customer_id']
    elif acct.get('invoice_email'): data['customer_email']=acct['invoice_email']
    session=_stripe('POST','/checkout/sessions',data)
    return {'url':session['url']}


def portal(stockroom_id, return_url):
    acct=account(stockroom_id)
    if not acct.get('stripe_customer_id'): raise ValueError('Nog geen Stripe-klant gekoppeld.')
    session=_stripe('POST','/billing_portal/sessions',{'customer':acct['stripe_customer_id'],'return_url':return_url})
    return {'url':session['url']}


def apply_webhook(event):
    obj=(event.get('data') or {}).get('object') or {}
    etype=event.get('type','')
    stockroom_id=(obj.get('metadata') or {}).get('stockroom_id')
    if etype=='checkout.session.completed' and stockroom_id:
        plan=(obj.get('metadata') or {}).get('plan','pro')
        with server.db() as conn:
            conn.execute("UPDATE billing_accounts SET plan=%s,status='active',stripe_customer_id=%s,stripe_subscription_id=%s,updated_at=NOW() WHERE stockroom_id=%s",(plan,obj.get('customer') or '',obj.get('subscription') or '',stockroom_id));conn.commit()
    elif etype.startswith('customer.subscription.'):
        customer=obj.get('customer')
        if customer:
            status=obj.get('status') or 'active'; cancel=bool(obj.get('cancel_at_period_end'))
            with server.db() as conn:
                conn.execute("UPDATE billing_accounts SET status=%s,cancel_at_period_end=%s,stripe_subscription_id=%s,updated_at=NOW() WHERE stripe_customer_id=%s",(status,cancel,obj.get('id') or '',customer));conn.commit()
    return True


def platform_metrics():
    with server.db() as conn:
        rows=conn.execute("SELECT plan,status,COUNT(*) n FROM billing_accounts GROUP BY plan,status").fetchall()
    counts={};mrr=0
    for r in rows:
        counts[f"{r['plan']}:{r['status']}"]=r['n']
        if r['status'] in ('active','trialing'): mrr += PLANS.get(r['plan'],PLANS['free'])['price_cents']*r['n']
    return {'mrr_cents':mrr,'subscriptions':counts}
