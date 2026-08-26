import json
import os
import uuid

import server


def platform_admin_emails():
    return {e.strip().lower() for e in os.environ.get('PLATFORM_ADMIN_EMAILS', '').split(',') if e.strip()}


def is_platform_admin(session):
    return bool(session and str(session.get('email') or '').lower() in platform_admin_emails())


def initialize_platform_admin():
    with server.db() as conn:
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_reason TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE stockrooms ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE stockrooms ADD COLUMN IF NOT EXISTS suspended_reason TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS platform_audit_log (id BIGSERIAL PRIMARY KEY,actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,action TEXT NOT NULL,target_type TEXT NOT NULL,target_id TEXT NOT NULL,details JSONB NOT NULL DEFAULT '{}'::jsonb,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_platform_audit_created ON platform_audit_log(created_at DESC)")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_error_log (id BIGSERIAL PRIMARY KEY,level TEXT NOT NULL DEFAULT 'error',component TEXT NOT NULL DEFAULT 'app',message TEXT NOT NULL,stockroom_id UUID REFERENCES stockrooms(id) ON DELETE SET NULL,user_id UUID REFERENCES users(id) ON DELETE SET NULL,details JSONB NOT NULL DEFAULT '{}'::jsonb,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_app_error_created ON app_error_log(created_at DESC)")
        conn.execute("""CREATE TABLE IF NOT EXISTS notification_states (
          user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
          notification_key TEXT NOT NULL,
          read_at TIMESTAMPTZ,dismissed_at TIMESTAMPTZ,snoozed_until TIMESTAMPTZ,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY(user_id,stockroom_id,notification_key))""")
        conn.commit()


def account_status(session):
    if not session:return None
    with server.db() as conn:
        return conn.execute("SELECT u.suspended_at user_suspended_at,u.suspended_reason user_suspended_reason,r.suspended_at stockroom_suspended_at,r.suspended_reason stockroom_suspended_reason FROM users u JOIN stockrooms r ON r.id=%s WHERE u.id=%s",(session['stockroom_id'],session['user_id'])).fetchone()


def enforce_access(session):
    status=account_status(session)
    if not status:return True,''
    if status['user_suspended_at']:return False,'Dit account is door de platformbeheerder geblokkeerd.'
    if status['stockroom_suspended_at'] and not is_platform_admin(session):return False,'Deze stockroom is tijdelijk geblokkeerd.'
    return True,''


def _audit(conn,session,action,target_type,target_id,details=None):
    conn.execute("INSERT INTO platform_audit_log(actor_user_id,action,target_type,target_id,details) VALUES(%s,%s,%s,%s,%s::jsonb)",(session['user_id'],action,target_type,str(target_id),json.dumps(details or {},ensure_ascii=False)))


def platform_overview():
    with server.db() as conn:
        stats=conn.execute("SELECT (SELECT COUNT(*) FROM users) users,(SELECT COUNT(*) FROM stockrooms) stockrooms,(SELECT COUNT(*) FROM sessions WHERE expires_at>NOW()) active_sessions,(SELECT COUNT(*) FROM users WHERE suspended_at IS NOT NULL) suspended_users,(SELECT COUNT(*) FROM stockrooms WHERE suspended_at IS NOT NULL) suspended_stockrooms,(SELECT COUNT(*) FROM app_error_log WHERE created_at>NOW()-INTERVAL '24 hours') errors_24h").fetchone()
        rooms=conn.execute("SELECT r.id::text,r.name,r.created_at,r.updated_at,r.suspended_at,r.suspended_reason,u.email owner_email,u.name owner_name,COUNT(DISTINCT m.user_id) member_count,COUNT(DISTINCT o.id) order_count FROM stockrooms r JOIN users u ON u.id=r.created_by LEFT JOIN memberships m ON m.stockroom_id=r.id LEFT JOIN orders o ON o.stockroom_id=r.id GROUP BY r.id,u.email,u.name ORDER BY r.created_at DESC LIMIT 250").fetchall()
        users=conn.execute("SELECT u.id::text,u.name,u.email,u.created_at,u.email_verified_at,u.suspended_at,u.suspended_reason,COUNT(DISTINCT m.stockroom_id) stockroom_count,MAX(s.created_at) last_session_at FROM users u LEFT JOIN memberships m ON m.user_id=u.id LEFT JOIN sessions s ON s.user_id=u.id GROUP BY u.id ORDER BY u.created_at DESC LIMIT 500").fetchall()
        errors=conn.execute("SELECT id,level,component,message,stockroom_id::text,user_id::text,details,created_at FROM app_error_log ORDER BY created_at DESC LIMIT 100").fetchall()
        audit=conn.execute("SELECT p.id,p.action,p.target_type,p.target_id,p.details,p.created_at,u.email actor_email FROM platform_audit_log p LEFT JOIN users u ON u.id=p.actor_user_id ORDER BY p.created_at DESC LIMIT 100").fetchall()
    return {'stats':stats,'stockrooms':rooms,'users':users,'errors':errors,'audit':audit}


def set_suspension(session,target_type,target_id,suspended,reason=''):
    if target_type not in ('user','stockroom'):raise ValueError('Ongeldig doel.')
    table='users' if target_type=='user' else 'stockrooms'
    with server.db() as conn:
        if target_type=='user' and str(target_id)==str(session['user_id']) and suspended:raise ValueError('Je kunt je eigen platformaccount niet blokkeren.')
        row=conn.execute(f"UPDATE {table} SET suspended_at=CASE WHEN %s THEN NOW() ELSE NULL END,suspended_reason=CASE WHEN %s THEN %s ELSE '' END WHERE id=%s RETURNING id::text",(suspended,suspended,reason[:500],target_id)).fetchone()
        if not row:raise ValueError('Doel niet gevonden.')
        if target_type=='user' and suspended:conn.execute('DELETE FROM sessions WHERE user_id=%s',(target_id,))
        if target_type=='stockroom' and suspended:conn.execute('DELETE FROM sessions WHERE active_stockroom_id=%s',(target_id,))
        _audit(conn,session,'suspend' if suspended else 'unsuspend',target_type,target_id,{'reason':reason[:500]});conn.commit()
    return {'updated':True}


def _notification_key(n):
    target=str(n.get('targetId') or '')
    if target:return f"{n.get('type','notice')}:{n.get('targetType','target')}:{target}"
    return f"{n.get('type','notice')}:{n.get('title','')}:{n.get('detail','')}"[:500]


def stockroom_notifications(stockroom_id,user_id=None):
    notifications=[]
    with server.db() as conn:
        row=conn.execute('SELECT state FROM stockrooms WHERE id=%s',(stockroom_id,)).fetchone();state=(row or {}).get('state') or {'items':[],'transactions':[]}
        for item in state.get('items',[]):
            if item.get('archived'):continue
            stock=float(item.get('stock') or 0);minimum=float(item.get('minStock') or 0)
            if minimum>0 and stock<=minimum:
                iid=str(item.get('id') or '');notifications.append({'type':'low_stock','severity':'warning','title':f"Lage voorraad: {item.get('name','Artikel')}",'detail':f'{stock:g} op voorraad · minimum {minimum:g}','targetView':'inventory','targetType':'item','targetId':iid,'itemId':iid})
        for tx in state.get('transactions',[]):
            tid=str(tx.get('id') or '')
            if tx.get('type')=='outgoing' and not tx.get('done'):notifications.append({'type':'unpaid','severity':'warning','title':'Openstaande verkoop','detail':f"{tx.get('party') or tx.get('itemName') or 'Verkoop'} · nog niet afgerond",'targetView':'outgoing','targetType':'transaction','targetId':tid,'transactionId':tid})
            elif tx.get('type')=='incoming' and not tx.get('done'):notifications.append({'type':'delivery','severity':'info','title':'Levering nog niet ontvangen','detail':f"{tx.get('party') or tx.get('itemName') or 'Inkoop'}",'targetView':'incoming','targetType':'transaction','targetId':tid,'transactionId':tid})
        pending=conn.execute("SELECT id::text,order_type,status,order_number,reference,relation_name,order_date FROM orders WHERE stockroom_id=%s AND status NOT IN ('received','completed','paid','cancelled') ORDER BY order_date LIMIT 100",(stockroom_id,)).fetchall()
        for order in pending:notifications.append({'type':'order','severity':'info','title':f"Open order {order.get('order_number') or order.get('reference') or ''}",'detail':f"{order['relation_name'] or 'Geen relatie'} · {order['status']}",'targetView':'orders','targetType':'order','targetId':order['id'],'orderId':order['id'],'orderType':order['order_type'],'status':order['status']})
        errors=conn.execute("SELECT component,message,created_at FROM app_error_log WHERE stockroom_id=%s AND created_at>NOW()-INTERVAL '7 days' ORDER BY created_at DESC LIMIT 20",(stockroom_id,)).fetchall()
        for error in errors:notifications.append({'type':'system','severity':'danger','title':f"Systeemmelding: {error['component']}",'detail':error['message'][:250],'createdAt':error['created_at']})
        states={}
        if user_id:
            rows=conn.execute("SELECT notification_key,read_at,dismissed_at,snoozed_until FROM notification_states WHERE user_id=%s AND stockroom_id=%s",(user_id,stockroom_id)).fetchall();states={r['notification_key']:r for r in rows}
        visible=[]
        for n in notifications:
            key=_notification_key(n);n['key']=key;s=states.get(key)
            if s and s['dismissed_at']:continue
            if s and s['snoozed_until'] and s['snoozed_until']>__import__('datetime').datetime.now(__import__('datetime').timezone.utc):continue
            n['read']=bool(s and s['read_at']);visible.append(n)
    return visible[:100]


def update_notification_state(session,key,action):
    key=(key or '')[:500]
    if not key:raise ValueError('Melding ontbreekt.')
    if action not in ('read','unread','dismiss','snooze'):raise ValueError('Ongeldige meldingactie.')
    with server.db() as conn:
        conn.execute("INSERT INTO notification_states(user_id,stockroom_id,notification_key) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",(session['user_id'],session['stockroom_id'],key))
        if action=='read':conn.execute("UPDATE notification_states SET read_at=NOW(),updated_at=NOW() WHERE user_id=%s AND stockroom_id=%s AND notification_key=%s",(session['user_id'],session['stockroom_id'],key))
        elif action=='unread':conn.execute("UPDATE notification_states SET read_at=NULL,updated_at=NOW() WHERE user_id=%s AND stockroom_id=%s AND notification_key=%s",(session['user_id'],session['stockroom_id'],key))
        elif action=='dismiss':conn.execute("UPDATE notification_states SET dismissed_at=NOW(),updated_at=NOW() WHERE user_id=%s AND stockroom_id=%s AND notification_key=%s",(session['user_id'],session['stockroom_id'],key))
        else:conn.execute("UPDATE notification_states SET snoozed_until=NOW()+INTERVAL '1 day',updated_at=NOW() WHERE user_id=%s AND stockroom_id=%s AND notification_key=%s",(session['user_id'],session['stockroom_id'],key))
        conn.commit()
    return {'updated':True}


def record_error(component,message,stockroom_id=None,user_id=None,details=None,level='error'):
    try:
        with server.db() as conn:
            conn.execute("INSERT INTO app_error_log(level,component,message,stockroom_id,user_id,details) VALUES(%s,%s,%s,%s,%s,%s::jsonb)",(level[:20],component[:100],str(message)[:2000],stockroom_id,user_id,json.dumps(details or {},ensure_ascii=False)));conn.commit()
    except Exception:pass
