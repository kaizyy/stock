"""Purchase approval policy and monthly budget controls."""
import json
import smtplib
import ssl
from email.message import EmailMessage

import business_tools
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
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS purchase_sent_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS purchase_sent_to TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS purchase_mail_count INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS supplier_confirmed_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS confirmed_delivery_date DATE")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS confirmation_reference TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS confirmation_note TEXT NOT NULL DEFAULT ''")
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


def send_order(session, values):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om een inkooporder te versturen.')
    order_id=(values.get('order_id') or '').strip()
    with server.db() as conn:
        order=conn.execute("""SELECT o.id::text,o.status,o.approval_status,o.relation_id,o.relation_name,o.order_number,
                   s.email supplier_email,s.name supplier_name,b.company_name
            FROM orders o LEFT JOIN suppliers s ON s.id=o.relation_id AND s.stockroom_id=o.stockroom_id
            LEFT JOIN billing_accounts b ON b.stockroom_id=o.stockroom_id
            WHERE o.id=%s AND o.stockroom_id=%s AND o.order_type='purchase'""",(order_id,session['stockroom_id'])).fetchone()
    if not order:raise PermissionError('Inkooporder niet gevonden.')
    if order['status']!='approved' or order['approval_status']!='approved':raise ValueError('Alleen een goedgekeurde inkooporder kan worden verstuurd.')
    recipient=(values.get('recipient') or order['supplier_email'] or '').strip()
    if '@' not in recipient:raise ValueError('Geen geldig e-mailadres voor deze leverancier ingesteld.')
    if not server.SMTP_HOST:raise ValueError('SMTP is niet geconfigureerd.')
    number=order['order_number'] or business_tools.assign_order_number(order_id,session['stockroom_id'],'purchase')
    data,filename=business_tools.order_pdf(session['stockroom_id'],order_id);company=(order['company_name'] or '').strip() or 'Stockroom'
    default=f"Beste {order['supplier_name'] or order['relation_name'] or 'leverancier'},\n\nIn de bijlage vindt u onze inkooporder {number}. Wilt u de ontvangst en verwachte leverdatum bevestigen?\n\nMet vriendelijke groet,\n{company}"
    message=(values.get('message') or default).strip();mail=EmailMessage();mail['From']=server.SMTP_FROM;mail['To']=recipient;mail['Subject']=f"Inkooporder {number} - {company}";mail.set_content(message);mail.add_attachment(data,maintype='application',subtype='pdf',filename=filename)
    context=ssl.create_default_context()
    if server.SMTP_PORT==465:
        with smtplib.SMTP_SSL(server.SMTP_HOST,server.SMTP_PORT,timeout=20,context=context) as smtp:
            if server.SMTP_USERNAME:smtp.login(server.SMTP_USERNAME,server.SMTP_PASSWORD)
            smtp.send_message(mail)
    else:
        with smtplib.SMTP(server.SMTP_HOST,server.SMTP_PORT,timeout=20) as smtp:
            smtp.ehlo();smtp.starttls(context=context);smtp.ehlo()
            if server.SMTP_USERNAME:smtp.login(server.SMTP_USERNAME,server.SMTP_PASSWORD)
            smtp.send_message(mail)
    with server.db() as conn:
        updated=conn.execute("""UPDATE orders SET status='ordered',purchase_sent_at=NOW(),purchase_sent_to=%s,
            purchase_mail_count=purchase_mail_count+1,updated_at=NOW() WHERE id=%s AND stockroom_id=%s AND status='approved' RETURNING id""",(recipient,order_id,session['stockroom_id'])).fetchone()
        if not updated:raise ValueError('De orderstatus is ondertussen gewijzigd; controleer de orderhistorie.')
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase.order_emailed',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':order_id,'recipient':recipient,'orderNumber':number})));conn.commit()
    return {'sent':True,'recipient':recipient,'status':'ordered','orderNumber':number}


def confirm_delivery(session, values):
    if session.get('role') not in ('owner','admin','member','buyer'):raise PermissionError('Geen rechten om een leveranciersbevestiging vast te leggen.')
    order_id=(values.get('order_id') or '').strip();delivery=(values.get('confirmed_delivery_date') or '').strip();reference=(values.get('confirmation_reference') or '').strip()[:120];note=(values.get('confirmation_note') or '').strip()[:1000]
    if not delivery:raise ValueError('Vul de bevestigde leverdatum in.')
    with server.db() as conn:
        order=conn.execute("SELECT id,status,expected_delivery_date FROM orders WHERE id=%s AND stockroom_id=%s AND order_type='purchase' FOR UPDATE",(order_id,session['stockroom_id'])).fetchone()
        if not order or order['status'] not in ('ordered','partial'):raise ValueError('Alleen een bestelde of deels ontvangen order kan worden bevestigd.')
        try:
            row=conn.execute("SELECT %s::date delivery",(delivery,)).fetchone();confirmed=row['delivery']
        except Exception as exc:raise ValueError('De bevestigde leverdatum is ongeldig.') from exc
        variance=(confirmed-order['expected_delivery_date']).days if order['expected_delivery_date'] else None
        conn.execute("UPDATE orders SET supplier_confirmed_at=NOW(),confirmed_delivery_date=%s,confirmation_reference=%s,confirmation_note=%s,updated_at=NOW() WHERE id=%s",(confirmed,reference,note,order_id))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase.supplier_confirmed',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':order_id,'confirmedDeliveryDate':str(confirmed),'reference':reference,'varianceDays':variance})));conn.commit()
    return {'confirmed':True,'confirmedDeliveryDate':str(confirmed),'varianceDays':variance}
