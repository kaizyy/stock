"""Purchase approval policy and monthly budget controls."""
import json

import server


def initialize():
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS purchase_policies(
            stockroom_id UUID PRIMARY KEY REFERENCES stockrooms(id) ON DELETE CASCADE,
            approval_threshold NUMERIC(14,2) NOT NULL DEFAULT 500,monthly_budget NUMERIC(14,2) NOT NULL DEFAULT 0,
            price_warning_requires_approval BOOLEAN NOT NULL DEFAULT TRUE,updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS approval_status TEXT NOT NULL DEFAULT 'not_required'")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS approval_reason TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS approved_by UUID REFERENCES users(id) ON DELETE SET NULL")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ")
        conn.commit()


def policy(stockroom_id):
    with server.db() as conn:
        row=conn.execute("SELECT approval_threshold::float8,monthly_budget::float8,price_warning_requires_approval FROM purchase_policies WHERE stockroom_id=%s",(stockroom_id,)).fetchone()
    return row or {'approval_threshold':500.0,'monthly_budget':0.0,'price_warning_requires_approval':True}


def save_policy(session, values):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan het inkoopbeleid wijzigen.')
    try:threshold=round(float(values.get('approval_threshold') or 0),2);budget=round(float(values.get('monthly_budget') or 0),2)
    except (TypeError,ValueError):raise ValueError('Controleer de bedragen van het inkoopbeleid.')
    if threshold<0 or budget<0:raise ValueError('Budgetbedragen kunnen niet negatief zijn.')
    warning=str(values.get('price_warning_requires_approval') or '') in ('1','true','on')
    with server.db() as conn:
        conn.execute("""INSERT INTO purchase_policies(stockroom_id,approval_threshold,monthly_budget,price_warning_requires_approval,updated_by)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(stockroom_id) DO UPDATE SET approval_threshold=EXCLUDED.approval_threshold,
            monthly_budget=EXCLUDED.monthly_budget,price_warning_requires_approval=EXCLUDED.price_warning_requires_approval,updated_by=EXCLUDED.updated_by,updated_at=NOW()""",(session['stockroom_id'],threshold,budget,warning,session['user_id']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase.policy_updated',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'approvalThreshold':threshold,'monthlyBudget':budget,'priceWarningApproval':warning})));conn.commit()
    return {'saved':True,**policy(session['stockroom_id'])}


def evaluate(conn, stockroom_id, order_id):
    setting=conn.execute("SELECT approval_threshold::float8,monthly_budget::float8,price_warning_requires_approval FROM purchase_policies WHERE stockroom_id=%s",(stockroom_id,)).fetchone() or {'approval_threshold':500.0,'monthly_budget':0.0,'price_warning_requires_approval':True}
    order=conn.execute("""SELECT COALESCE(SUM(l.quantity*l.unit_price),0)::float8 total,o.advice_details,o.order_date
        FROM orders o JOIN order_lines l ON l.order_id=o.id WHERE o.id=%s AND o.stockroom_id=%s GROUP BY o.id""",(order_id,stockroom_id)).fetchone()
    if not order:return {'required':False,'reasons':[],'total':0,'monthSpend':0}
    month_spend=conn.execute("""SELECT COALESCE(SUM(l.quantity*l.unit_price),0)::float8 total FROM orders o JOIN order_lines l ON l.order_id=o.id
        WHERE o.stockroom_id=%s AND o.order_type='purchase' AND o.id<>%s AND o.status NOT IN ('cancelled','rejected')
        AND date_trunc('month',o.order_date)=date_trunc('month',%s::date)""",(stockroom_id,order_id,order['order_date'])).fetchone()['total']
    reasons=[];total=float(order['total']);threshold=float(setting['approval_threshold']);budget=float(setting['monthly_budget'])
    if threshold>0 and total>=threshold:reasons.append(f'Orderbedrag € {total:.2f} is boven de goedkeuringsgrens € {threshold:.2f}.')
    if setting['price_warning_requires_approval'] and (order['advice_details'] or {}).get('priceWarnings'):reasons.append('De order bevat een prijswaarschuwing.')
    if budget>0 and float(month_spend)+total>budget:reasons.append(f'Maandbudget € {budget:.2f} wordt overschreden.')
    return {'required':bool(reasons),'reasons':reasons,'total':total,'monthSpend':float(month_spend),'projectedMonthSpend':float(month_spend)+total,'monthlyBudget':budget}


def decide(session, values, decision):
    if session.get('role') not in ('owner','admin'):raise PermissionError('Alleen eigenaar of beheerder kan inkooporders beoordelen.')
    order_id=(values.get('order_id') or '').strip();reason=(values.get('reason') or '').strip()[:1000]
    if decision=='reject' and not reason:raise ValueError('Vul een reden voor afwijzing in.')
    with server.db() as conn:
        order=conn.execute("SELECT id,status,approval_status FROM orders WHERE id=%s AND stockroom_id=%s AND order_type='purchase' FOR UPDATE",(order_id,session['stockroom_id'])).fetchone()
        if not order or order['status']!='pending_approval':raise ValueError('Deze inkooporder wacht niet op goedkeuring.')
        status='approved' if decision=='approve' else 'rejected';approval=status
        conn.execute("UPDATE orders SET status=%s,approval_status=%s,approval_reason=%s,approved_by=%s,approved_at=NOW(),updated_at=NOW() WHERE id=%s",(status,approval,reason,session['user_id'],order_id))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",(session['stockroom_id'],session['user_id'],f'purchase.{approval}',json.dumps({'id':order_id,'reason':reason})));conn.commit()
    return {'updated':True,'status':status}
