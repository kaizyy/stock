import json, uuid
import server

PREF_TYPES=('low_stock','unpaid','delivery','order','system')

def initialize_account_tools():
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS notification_preferences(
          user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
          notification_type TEXT NOT NULL,
          in_app BOOLEAN NOT NULL DEFAULT TRUE,
          email BOOLEAN NOT NULL DEFAULT FALSE,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY(user_id,stockroom_id,notification_type))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS notification_email_deliveries(
          user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
          notification_key TEXT NOT NULL,
          notification_type TEXT NOT NULL,
          sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY(user_id,stockroom_id,notification_key))""")
        conn.commit()

def sessions_for(session,current_token_hash=None):
    with server.db() as conn:
        rows=conn.execute("""SELECT token_hash,active_stockroom_id::text,created_at,expires_at
          FROM sessions WHERE user_id=%s AND expires_at>NOW() ORDER BY created_at DESC""",(session['user_id'],)).fetchall()
    out=[]
    for r in rows: out.append({'id':r['token_hash'],'stockroom_id':r['active_stockroom_id'],'created_at':r['created_at'],'expires_at':r['expires_at'],'current':bool(current_token_hash and r['token_hash']==current_token_hash)})
    return out

def revoke_session(session,token_hash,current_hash=None,all_others=False):
    with server.db() as conn:
        if all_others: conn.execute("DELETE FROM sessions WHERE user_id=%s AND token_hash<>%s",(session['user_id'],current_hash or ''))
        else:
            row=conn.execute("SELECT token_hash FROM sessions WHERE token_hash=%s AND user_id=%s",(token_hash,session['user_id'])).fetchone()
            if not row: raise PermissionError('Sessie niet gevonden.')
            conn.execute("DELETE FROM sessions WHERE token_hash=%s",(token_hash,))
        conn.commit()
    return {'updated':True}

def preferences(session):
    with server.db() as conn:
        rows=conn.execute("SELECT notification_type,in_app,email FROM notification_preferences WHERE user_id=%s AND stockroom_id=%s",(session['user_id'],session['stockroom_id'])).fetchall()
    found={r['notification_type']:r for r in rows}
    return [{'type':t,'in_app':bool(found.get(t,{}).get('in_app',True)),'email':bool(found.get(t,{}).get('email',False))} for t in PREF_TYPES]

def save_preferences(session,values):
    with server.db() as conn:
        for t in PREF_TYPES:
            ina=str(values.get(f'{t}_in_app') or '')=='1'; em=str(values.get(f'{t}_email') or '')=='1'
            conn.execute("""INSERT INTO notification_preferences(user_id,stockroom_id,notification_type,in_app,email)
              VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id,stockroom_id,notification_type)
              DO UPDATE SET in_app=EXCLUDED.in_app,email=EXCLUDED.email,updated_at=NOW()""",(session['user_id'],session['stockroom_id'],t,ina,em))
        conn.commit()
    return {'saved':True}

def filter_notifications(session,notes):
    prefs={p['type']:p for p in preferences(session)}
    return [n for n in notes if prefs.get(n.get('type'),{'in_app':True})['in_app']]

def _role_can_import(role,kind):
    if role in ('owner','admin','member'): return True
    if kind=='suppliers' and role=='buyer': return True
    if kind=='customers' and role=='seller': return True
    return False

def preview_import(session,kind,rows):
    if kind not in ('inventory','customers','suppliers'): raise ValueError('Ongeldig importtype.')
    if not _role_can_import(session['role'],kind): raise PermissionError('Geen rechten voor deze import.')
    if not isinstance(rows,list): raise ValueError('Importgegevens zijn ongeldig.')
    out=[]; errors=[]
    for i,r in enumerate(rows[:2000],start=2):
        if not isinstance(r,dict): errors.append(f'Rij {i}: ongeldig'); continue
        clean={str(k).strip().lower():str(v if v is not None else '').strip() for k,v in r.items()}
        if kind=='inventory':
            name=clean.get('name') or clean.get('item') or clean.get('artikel'); sku=clean.get('sku') or clean.get('artikelnummer')
            if not name or not sku: errors.append(f'Rij {i}: naam en SKU zijn verplicht'); continue
            try: stock=float((clean.get('stock') or clean.get('voorraad') or '0').replace(',','.')); buy=float((clean.get('buy') or clean.get('inkoopprijs') or '0').replace(',','.')); sell=float((clean.get('sell') or clean.get('verkoopprijs') or '0').replace(',','.'))
            except ValueError: errors.append(f'Rij {i}: ongeldige numerieke waarde'); continue
            out.append({'name':name[:200],'sku':sku[:100],'stock':stock,'buy':buy,'sell':sell,'barcode':clean.get('barcode','')[:100],'location':clean.get('location') or clean.get('locatie','')})
        else:
            name=clean.get('name') or clean.get('naam') or clean.get('bedrijf')
            if not name: errors.append(f'Rij {i}: naam is verplicht'); continue
            out.append({'name':name[:200],'contact_name':(clean.get('contact_name') or clean.get('contactpersoon') or '')[:200],'email':clean.get('email','')[:320],'phone':(clean.get('phone') or clean.get('telefoon') or '')[:100],'address':(clean.get('address') or clean.get('adres') or '')[:1000],'notes':(clean.get('notes') or clean.get('notities') or '')[:2000]})
    return {'rows':out,'errors':errors[:200],'valid_count':len(out),'error_count':len(errors)}

def apply_import(session,kind,rows):
    preview=preview_import(session,kind,rows)
    if preview['errors']: raise ValueError('Import bevat nog fouten. Los deze eerst op.')
    rows=preview['rows']
    with server.db() as conn:
        if kind=='inventory':
            room=conn.execute('SELECT state FROM stockrooms WHERE id=%s FOR UPDATE',(session['stockroom_id'],)).fetchone(); state=(room or {}).get('state') or {'items':[],'transactions':[]}
            existing={str(i.get('sku','')).lower():i for i in state.get('items',[])}
            for r in rows:
                item=existing.get(r['sku'].lower())
                if item: item.update({'name':r['name'],'stock':r['stock'],'buy':r['buy'],'sell':r['sell'],'barcode':r['barcode'],'location':r['location']})
                else: state.setdefault('items',[]).append({'id':str(uuid.uuid4()),'name':r['name'],'sku':r['sku'],'stock':r['stock'],'buy':r['buy'],'sell':r['sell'],'barcode':r['barcode'],'location':r['location']})
            conn.execute('UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s',(json.dumps(state,ensure_ascii=False),session['stockroom_id']))
        else:
            table='customers' if kind=='customers' else 'suppliers'
            for r in rows:
                conn.execute(f"INSERT INTO {table}(id,stockroom_id,name,contact_name,email,phone,address,notes) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",(str(uuid.uuid4()),session['stockroom_id'],r['name'],r['contact_name'],r['email'],r['phone'],r['address'],r['notes']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'import.applied',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'kind':kind,'count':len(rows)})))
        conn.commit()
    return {'imported':len(rows)}

def deliver_notification_emails(stockroom_id):
    import platform_admin
    notes=platform_admin.stockroom_notifications(stockroom_id)
    active_keys={n.get('key') or platform_admin._notification_key(n) for n in notes}
    with server.db() as conn:
        if active_keys:
            conn.execute("DELETE FROM notification_email_deliveries WHERE stockroom_id=%s AND NOT (notification_key = ANY(%s))",(stockroom_id,list(active_keys)))
        else:
            conn.execute("DELETE FROM notification_email_deliveries WHERE stockroom_id=%s",(stockroom_id,))
        recipients=conn.execute("""SELECT u.id::text user_id,u.email,p.notification_type
          FROM memberships m JOIN users u ON u.id=m.user_id
          JOIN notification_preferences p ON p.user_id=u.id AND p.stockroom_id=m.stockroom_id
          WHERE m.stockroom_id=%s AND p.email=TRUE""",(stockroom_id,)).fetchall()
        sent={(r['user_id'],r['notification_key']) for r in conn.execute("SELECT user_id::text,notification_key FROM notification_email_deliveries WHERE stockroom_id=%s",(stockroom_id,)).fetchall()}
        room=conn.execute("SELECT name FROM stockrooms WHERE id=%s",(stockroom_id,)).fetchone()
        conn.commit()
    by_type={}
    for r in recipients: by_type.setdefault(r['notification_type'],[]).append(r)
    delivered=0
    for n in notes:
        key=n.get('key') or platform_admin._notification_key(n); ntype=n.get('type')
        for r in by_type.get(ntype,[]):
            marker=(r['user_id'],key)
            if marker in sent: continue
            link=(server.APP_BASE_URL or '').rstrip('/') or 'Stockroom'
            subject=f"Stockroom · {n.get('title','Melding')}"
            text=f"{n.get('title','Melding')}\n\n{n.get('detail','')}\n\nStockroom: {(room or {}).get('name','')}\nOpenen: {link}/#notifications"
            try:
                server.send_email(r['email'],subject,text)
                with server.db() as conn:
                    conn.execute("INSERT INTO notification_email_deliveries(user_id,stockroom_id,notification_key,notification_type) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",(r['user_id'],stockroom_id,key,ntype));conn.commit()
                delivered+=1;sent.add(marker)
            except Exception as exc:
                try:
                    platform_admin.record_error('notification_email',type(exc).__name__,stockroom_id,r['user_id'],{'notificationType':ntype,'key':key})
                except Exception: pass
    return {'delivered':delivered}
