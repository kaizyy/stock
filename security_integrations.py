import base64, hashlib, hmac, html, ipaddress, json, os, secrets, socket, struct, time, urllib.parse, urllib.request, uuid
from datetime import datetime, timezone

import server

_installed=False


def _key_bytes():
    raw=os.environ.get('SECURITY_ENCRYPTION_KEY','')
    return hashlib.sha256(raw.encode()).digest() if raw else None

def _xor(data,key,nonce):
    out=bytearray(); counter=0
    while len(out)<len(data):
        out.extend(hashlib.sha256(key+nonce+counter.to_bytes(4,'big')).digest()); counter+=1
    return bytes(a^b for a,b in zip(data,out))

def encrypt_secret(text):
    key=_key_bytes()
    if not key: raise ValueError('SECURITY_ENCRYPTION_KEY ontbreekt.')
    nonce=os.urandom(16); ct=_xor(text.encode(),key,nonce); tag=hmac.new(key,nonce+ct,hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce+tag+ct).decode()

def decrypt_secret(token):
    key=_key_bytes(); raw=base64.urlsafe_b64decode(token.encode()); nonce,tag,ct=raw[:16],raw[16:48],raw[48:]
    if not key or not hmac.compare_digest(tag,hmac.new(key,nonce+ct,hashlib.sha256).digest()): raise ValueError('2FA-geheim kan niet worden ontsleuteld.')
    return _xor(ct,key,nonce).decode()

def totp(secret,at=None):
    at=int(at or time.time()); key=base64.b32decode(secret+'='*((8-len(secret)%8)%8)); msg=struct.pack('>Q',at//30); digest=hmac.new(key,msg,hashlib.sha1).digest(); o=digest[-1]&15; code=(struct.unpack('>I',digest[o:o+4])[0]&0x7fffffff)%1000000; return f'{code:06d}'

def verify_totp(secret,code):
    code=str(code or '').strip().replace(' ','')
    return len(code)==6 and code.isdigit() and any(hmac.compare_digest(totp(secret,time.time()+d),code) for d in (-30,0,30))

def initialize_security():
    with server.db() as conn:
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret_encrypted TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_pending_encrypted TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled_at TIMESTAMPTZ")
        conn.execute("""CREATE TABLE IF NOT EXISTS login_history(id BIGSERIAL PRIMARY KEY,user_id UUID REFERENCES users(id) ON DELETE CASCADE,success BOOLEAN NOT NULL,ip_address TEXT NOT NULL DEFAULT '',user_agent TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS twofa_challenges(token_hash TEXT PRIMARY KEY,user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,expires_at TIMESTAMPTZ NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS api_keys(id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,created_by UUID REFERENCES users(id) ON DELETE SET NULL,name TEXT NOT NULL,key_hash TEXT NOT NULL UNIQUE,key_prefix TEXT NOT NULL,scopes TEXT[] NOT NULL DEFAULT ARRAY['read']::text[],last_used_at TIMESTAMPTZ,revoked_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS webhooks(id UUID PRIMARY KEY,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,created_by UUID REFERENCES users(id) ON DELETE SET NULL,name TEXT NOT NULL,url TEXT NOT NULL,event_types TEXT[] NOT NULL DEFAULT ARRAY['state.changed']::text[],secret TEXT NOT NULL,enabled BOOLEAN NOT NULL DEFAULT TRUE,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS webhook_deliveries(id BIGSERIAL PRIMARY KEY,webhook_id UUID REFERENCES webhooks(id) ON DELETE CASCADE,event_type TEXT NOT NULL,status_code INTEGER,error TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.commit()

def _client(handler):
    ip=(handler.headers.get('X-Forwarded-For','').split(',',1)[0].strip() or handler.client_address[0])[:80]
    ua=(handler.headers.get('User-Agent','') or '')[:500]
    return ip,ua

def record_login(user_id,success,handler):
    try:
        ip,ua=_client(handler)
        with server.db() as conn: conn.execute("INSERT INTO login_history(user_id,success,ip_address,user_agent) VALUES(%s,%s,%s,%s)",(user_id,success,ip,ua));conn.commit()
    except Exception: pass

def login_history(session):
    with server.db() as conn:return conn.execute("SELECT success,ip_address,user_agent,created_at FROM login_history WHERE user_id=%s ORDER BY created_at DESC LIMIT 50",(session['user_id'],)).fetchall()

def overview(session):
    with server.db() as conn:
        u=conn.execute("SELECT totp_enabled_at,totp_pending_encrypted FROM users WHERE id=%s",(session['user_id'],)).fetchone()
        keys=conn.execute("SELECT id::text,name,key_prefix,scopes,last_used_at,revoked_at,created_at FROM api_keys WHERE stockroom_id=%s ORDER BY created_at DESC",(session['stockroom_id'],)).fetchall()
        hooks=conn.execute("SELECT id::text,name,url,event_types,enabled,created_at FROM webhooks WHERE stockroom_id=%s ORDER BY created_at DESC",(session['stockroom_id'],)).fetchall()
    return {'twofa_enabled':bool(u and u['totp_enabled_at']),'twofa_pending':bool(u and u['totp_pending_encrypted']),'login_history':login_history(session),'api_keys':keys,'webhooks':hooks,'encryption_configured':bool(_key_bytes())}

def start_totp(session):
    if not _key_bytes():raise ValueError('Stel eerst SECURITY_ENCRYPTION_KEY in Coolify in.')
    secret=base64.b32encode(os.urandom(20)).decode().rstrip('=')
    enc=encrypt_secret(secret)
    with server.db() as conn: conn.execute("UPDATE users SET totp_pending_encrypted=%s WHERE id=%s",(enc,session['user_id']));conn.commit()
    label=urllib.parse.quote(f"Stockroom:{session['email']}")
    issuer=urllib.parse.quote('Stockroom')
    return {'secret':secret,'otpauth_uri':f'otpauth://totp/{label}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30'}

def confirm_totp(session,code):
    with server.db() as conn:
        row=conn.execute("SELECT totp_pending_encrypted FROM users WHERE id=%s FOR UPDATE",(session['user_id'],)).fetchone()
        if not row or not row['totp_pending_encrypted']:raise ValueError('Start eerst de 2FA-configuratie.')
        secret=decrypt_secret(row['totp_pending_encrypted'])
        if not verify_totp(secret,code):raise ValueError('De verificatiecode is ongeldig.')
        conn.execute("UPDATE users SET totp_secret_encrypted=totp_pending_encrypted,totp_pending_encrypted=NULL,totp_enabled_at=NOW() WHERE id=%s",(session['user_id'],));conn.commit()
    return {'enabled':True}

def disable_totp(session,code):
    with server.db() as conn:
        row=conn.execute("SELECT totp_secret_encrypted FROM users WHERE id=%s FOR UPDATE",(session['user_id'],)).fetchone()
        if not row or not row['totp_secret_encrypted']:raise ValueError('2FA is niet ingeschakeld.')
        if not verify_totp(decrypt_secret(row['totp_secret_encrypted']),code):raise ValueError('De verificatiecode is ongeldig.')
        conn.execute("UPDATE users SET totp_secret_encrypted=NULL,totp_pending_encrypted=NULL,totp_enabled_at=NULL WHERE id=%s",(session['user_id'],));conn.execute("DELETE FROM sessions WHERE user_id=%s AND token_hash<>%s",(session['user_id'],server.token_digest(session.get('_raw_token',''))));conn.commit()
    return {'enabled':False}

def create_api_key(session,name,scopes):
    if session['role'] not in ('owner','admin'):raise PermissionError('Alleen Owner of Admin kan API-keys beheren.')
    allowed={'read','write'}; scopes=[s for s in scopes if s in allowed] or ['read']; raw='sr_'+secrets.token_urlsafe(32); h=server.token_digest(raw); kid=str(uuid.uuid4())
    with server.db() as conn:conn.execute("INSERT INTO api_keys(id,stockroom_id,created_by,name,key_hash,key_prefix,scopes) VALUES(%s,%s,%s,%s,%s,%s,%s)",(kid,session['stockroom_id'],session['user_id'],(name or 'API key')[:120],h,raw[:12],scopes));conn.commit()
    return {'id':kid,'key':raw,'prefix':raw[:12],'scopes':scopes}

def revoke_api_key(session,key_id):
    if session['role'] not in ('owner','admin'):raise PermissionError('Geen rechten.')
    with server.db() as conn:conn.execute("UPDATE api_keys SET revoked_at=NOW() WHERE id=%s AND stockroom_id=%s",(key_id,session['stockroom_id']));conn.commit()
    return {'revoked':True}

def _safe_webhook_url(url):
    p=urllib.parse.urlparse(url)
    if p.scheme!='https' or not p.hostname:raise ValueError('Webhook-URL moet een geldige HTTPS-URL zijn.')
    try:
        for info in socket.getaddrinfo(p.hostname,p.port or 443,type=socket.SOCK_STREAM):
            ip=ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:raise ValueError('Webhook-URL mag niet naar een privé- of intern netwerk wijzen.')
    except socket.gaierror:raise ValueError('Webhook-hostnaam kan niet worden gevonden.')
    return url[:1000]

def create_webhook(session,name,url,events):
    if session['role'] not in ('owner','admin'):raise PermissionError('Alleen Owner of Admin kan webhooks beheren.')
    allowed={'state.changed','order.created','order.updated'}; events=[e for e in events if e in allowed] or ['state.changed']; wid=str(uuid.uuid4()); secret=secrets.token_urlsafe(32)
    with server.db() as conn:conn.execute("INSERT INTO webhooks(id,stockroom_id,created_by,name,url,event_types,secret) VALUES(%s,%s,%s,%s,%s,%s,%s)",(wid,session['stockroom_id'],session['user_id'],(name or 'Webhook')[:120],_safe_webhook_url(url),events,secret));conn.commit()
    return {'id':wid,'secret':secret,'events':events}

def delete_webhook(session,wid):
    if session['role'] not in ('owner','admin'):raise PermissionError('Geen rechten.')
    with server.db() as conn:conn.execute("DELETE FROM webhooks WHERE id=%s AND stockroom_id=%s",(wid,session['stockroom_id']));conn.commit()
    return {'deleted':True}

def dispatch_event(stockroom_id,event_type,payload):
    with server.db() as conn: hooks=conn.execute("SELECT id::text,url,secret FROM webhooks WHERE stockroom_id=%s AND enabled=TRUE AND %s=ANY(event_types)",(stockroom_id,event_type)).fetchall()
    body=json.dumps({'event':event_type,'stockroom_id':str(stockroom_id),'created_at':datetime.now(timezone.utc).isoformat(),'data':payload},ensure_ascii=False,separators=(',',':')).encode()
    for hook in hooks:
        status=None;err=''
        try:
            sig=hmac.new(hook['secret'].encode(),body,hashlib.sha256).hexdigest();req=urllib.request.Request(hook['url'],data=body,method='POST',headers={'Content-Type':'application/json','User-Agent':'Stockroom-Webhooks/1.0','X-Stockroom-Signature':'sha256='+sig});
            with urllib.request.urlopen(req,timeout=5) as resp:status=resp.status
        except Exception as exc:err=type(exc).__name__[:200]
        with server.db() as conn:conn.execute("INSERT INTO webhook_deliveries(webhook_id,event_type,status_code,error) VALUES(%s,%s,%s,%s)",(hook['id'],event_type,status,err));conn.commit()

def api_auth(handler,scope='read'):
    auth=handler.headers.get('Authorization','')
    if not auth.startswith('Bearer sr_'):return None
    raw=auth[7:].strip();h=server.token_digest(raw)
    with server.db() as conn:
        row=conn.execute("SELECT id::text,stockroom_id::text,scopes FROM api_keys WHERE key_hash=%s AND revoked_at IS NULL",(h,)).fetchone()
        if row:conn.execute("UPDATE api_keys SET last_used_at=NOW() WHERE id=%s",(row['id'],));conn.commit()
    return row if row and scope in row['scopes'] else None

def twofa_page(challenge,error=''):
    safe=html.escape(challenge,quote=True); feedback=f'<p class="error">{html.escape(error)}</p>' if error else ''
    return server.auth_page('Tweestapsverificatie','Voer de 6-cijferige code uit je authenticator-app in.',feedback+f'<form method="post" action="/2fa"><input type="hidden" name="challenge" value="{safe}"><label>Verificatiecode<input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required autofocus></label><button type="submit">Verifiëren</button></form>')

def _custom_login(handler):
    form=handler.form_data()
    if not form:handler.send_html(400,server.login_page('Ongeldige aanvraag.'));return
    email=form.get('email',[''])[0].strip().lower();password=form.get('password',[''])[0]
    if not handler.rate_limit('login',email,10,900):handler.send_html(429,server.login_page('Te veel inlogpogingen. Probeer het later opnieuw.'));return
    with server.db() as conn:
        user=conn.execute('SELECT * FROM users WHERE email=%s',(email,)).fetchone();locked=user and user['locked_until'] and user['locked_until'].timestamp()>time.time();valid=user and not locked and server.verify_password(password,user['password_salt'],user['password_hash'],user.get('password_version',1))
        if not valid:
            if user and not locked:conn.execute("UPDATE users SET failed_login_attempts=failed_login_attempts+1,locked_until=CASE WHEN failed_login_attempts+1 >= %s THEN NOW()+(%s*INTERVAL '1 second') ELSE locked_until END WHERE id=%s",(server.LOGIN_MAX_ATTEMPTS,server.LOGIN_LOCK_SECONDS,user['id']));conn.commit()
            if user:record_login(user['id'],False,handler)
            handler.send_html(401,server.login_page('E-mailadres of wachtwoord is onjuist.'));return
        conn.execute('UPDATE users SET failed_login_attempts=0,locked_until=NULL WHERE id=%s',(user['id'],));conn.commit()
        if user['email_verified_at'] is None:handler.send_html(403,server.login_page('Verifieer eerst je e-mailadres.'));return
        membership=conn.execute("SELECT stockroom_id::text,role FROM memberships WHERE user_id=%s ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,created_at LIMIT 1",(user['id'],)).fetchone()
    if not membership:handler.send_html(403,server.login_page('Dit account heeft geen stockroomtoegang.'));return
    if user.get('totp_enabled_at') and user.get('totp_secret_encrypted'):
        raw=secrets.token_urlsafe(32)
        with server.db() as conn:conn.execute("DELETE FROM twofa_challenges WHERE user_id=%s",(user['id'],));conn.execute("INSERT INTO twofa_challenges(token_hash,user_id,stockroom_id,expires_at) VALUES(%s,%s,%s,NOW()+INTERVAL '5 minutes')",(server.token_digest(raw),user['id'],membership['stockroom_id']));conn.commit()
        handler.redirect('/2fa?challenge='+urllib.parse.quote(raw));return
    record_login(user['id'],True,handler);token,_=server.create_session(str(user['id']),membership['stockroom_id']);handler.redirect('/',token=token)

def install():
    global _installed
    if _installed:return
    _installed=True;initialize_security()
    old_get=server.StockroomHandler.do_GET;old_post=server.StockroomHandler.do_POST
    def do_GET(self):
        path=urllib.parse.urlparse(self.path).path;q=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path=='/2fa':self.send_html(200,twofa_page(q.get('challenge',[''])[0]));return
        if path=='/api/security/overview':
            s=self.require_session(api=True)
            if s:self.send_json(200,overview(s))
            return
        if path=='/api/v1/state':
            key=api_auth(self,'read')
            if not key:self.send_json(401,{'error':'Ongeldige API-key.'});return
            with server.db() as conn:r=conn.execute('SELECT state FROM stockrooms WHERE id=%s',(key['stockroom_id'],)).fetchone()
            self.send_json(200,r['state'] if r else {'items':[],'transactions':[]});return
        return old_get(self)
    def do_POST(self):
        path=urllib.parse.urlparse(self.path).path
        if path=='/login':
            if not self.enforce_origin():return
            return _custom_login(self)
        if path=='/2fa':
            if not self.enforce_origin():return
            f=self.form_data() or {};raw=f.get('challenge',[''])[0];code=f.get('code',[''])[0]
            with server.db() as conn:
                row=conn.execute("SELECT c.user_id,c.stockroom_id,u.totp_secret_encrypted FROM twofa_challenges c JOIN users u ON u.id=c.user_id WHERE c.token_hash=%s AND c.expires_at>NOW() AND c.attempts<5 FOR UPDATE",(server.token_digest(raw),)).fetchone()
                if not row:self.send_html(400,twofa_page(raw,'Deze verificatiepoging is verlopen.'));return
                conn.execute('UPDATE twofa_challenges SET attempts=attempts+1 WHERE token_hash=%s',(server.token_digest(raw),));conn.commit()
            if not verify_totp(decrypt_secret(row['totp_secret_encrypted']),code):self.send_html(401,twofa_page(raw,'De verificatiecode is ongeldig.'));return
            with server.db() as conn:conn.execute('DELETE FROM twofa_challenges WHERE token_hash=%s',(server.token_digest(raw),));conn.commit()
            record_login(row['user_id'],True,self);token,_=server.create_session(str(row['user_id']),str(row['stockroom_id']));self.redirect('/',token=token);return
        secpaths={'/api/security/totp/start','/api/security/totp/confirm','/api/security/totp/disable','/api/security/api-keys/create','/api/security/api-keys/revoke','/api/security/webhooks/create','/api/security/webhooks/delete'}
        if path in secpaths:
            if not self.enforce_origin():return
            s=self.require_session(api=True)
            if not s:return
            f=self.form_data() or {};v=lambda k:(f.get(k,[''])[0] if isinstance(f.get(k),list) else f.get(k,''))
            try:
                if path.endswith('/totp/start'):res=start_totp(s)
                elif path.endswith('/totp/confirm'):res=confirm_totp(s,v('code'))
                elif path.endswith('/totp/disable'):res=disable_totp(s,v('code'))
                elif path.endswith('/api-keys/create'):res=create_api_key(s,v('name'),[x for x in v('scopes').split(',') if x])
                elif path.endswith('/api-keys/revoke'):res=revoke_api_key(s,v('id'))
                elif path.endswith('/webhooks/create'):res=create_webhook(s,v('name'),v('url'),[x for x in v('events').split(',') if x])
                else:res=delete_webhook(s,v('id'))
                self.send_json(200,res)
            except PermissionError as e:self.send_json(403,{'error':str(e)})
            except ValueError as e:self.send_json(400,{'error':str(e)})
            return
        return old_post(self)
    server.StockroomHandler.do_GET=do_GET;server.StockroomHandler.do_POST=do_POST
