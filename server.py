import hashlib
import hmac
import html
import json
import os
import secrets
import smtplib
import ssl
import time
import uuid
import ipaddress
from email.message import EmailMessage
from functools import partial
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psycopg
from psycopg.rows import dict_row

HOST = "0.0.0.0"
PORT = 8000
PUBLIC_DIR = Path("/app/public")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME or "noreply@localhost")
MAX_BODY_BYTES = 1_000_000
SESSION_TTL_SECONDS = 30 * 60
VERIFY_TTL_SECONDS = 24 * 60 * 60
RESET_TTL_SECONDS = 30 * 60
EMAIL_CHANGE_TTL_SECONDS = 30 * 60
PASSWORD_SCRYPT_N = int(os.environ.get("PASSWORD_SCRYPT_N", str(2**15)))
LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCK_SECONDS = int(os.environ.get("LOGIN_LOCK_SECONDS", "900"))
SESSION_COOKIE = "stockroom_session"
EMPTY_STATE = {"items": [], "transactions": []}


def log_event(message):
    print(message, flush=True)


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def initialize_database():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id UUID PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    password_salt BYTEA NOT NULL,
                    password_hash BYTEA NOT NULL,
                    email_verified_at TIMESTAMPTZ DEFAULT NOW(),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ DEFAULT NOW()")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_version SMALLINT NOT NULL DEFAULT 1")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_attempts INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stockrooms (
                    id UUID PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                    state JSONB NOT NULL DEFAULT '{"items":[],"transactions":[]}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS memberships (
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('owner','admin','member')),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, stockroom_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    active_stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
                    expires_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    purpose TEXT NOT NULL,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    expires_at TIMESTAMPTZ NOT NULL,
                    used_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_memberships_stockroom_id ON memberships(stockroom_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_user_purpose ON auth_tokens(user_id,purpose)")
            cur.execute("ALTER TABLE auth_tokens ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb")
            cur.execute("ALTER TABLE auth_tokens DROP CONSTRAINT IF EXISTS auth_tokens_purpose_check")
            cur.execute("ALTER TABLE auth_tokens ADD CONSTRAINT auth_tokens_purpose_check CHECK (purpose IN ('verify_email','reset_password','change_email'))")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rate_limits (
                    scope TEXT NOT NULL,
                    subject_hash TEXT NOT NULL,
                    window_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    attempts INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(scope,subject_hash)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id BIGSERIAL PRIMARY KEY,
                    stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
                    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                    action TEXT NOT NULL,
                    details JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_stockroom_created ON audit_log(stockroom_id,created_at DESC)")
        conn.commit()


def hash_password(password, salt=None, n=PASSWORD_SCRYPT_N):
    salt = os.urandom(16) if salt is None else salt
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=8, p=1, dklen=32, maxmem=128 * 1024 * 1024)
    return salt, digest


def verify_password(password, salt, expected, version=1):
    n = 2**14 if int(version or 1) == 1 else PASSWORD_SCRYPT_N
    _, digest = hash_password(password, bytes(salt), n=n)
    return hmac.compare_digest(digest, bytes(expected))


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def audit_user_action(conn, user_id, action, details=None):
    conn.execute("""
        INSERT INTO audit_log(stockroom_id,user_id,action,details)
        SELECT stockroom_id,%s,%s,%s::jsonb FROM memberships WHERE user_id=%s
    """, (user_id, action, json.dumps(details or {}), user_id))


def valid_state(value):
    return (
        isinstance(value, dict)
        and isinstance(value.get("items"), list)
        and isinstance(value.get("transactions"), list)
        and all(isinstance(record, dict) for record in value["items"] + value["transactions"])
    )


def cleanup_expired():
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= NOW()")
        conn.execute("DELETE FROM auth_tokens WHERE expires_at <= NOW() OR used_at IS NOT NULL")
        conn.commit()


def create_session(user_id, stockroom_id):
    raw = secrets.token_urlsafe(32)
    expires_at = time.time() + SESSION_TTL_SECONDS
    with db() as conn:
        conn.execute(
            """INSERT INTO sessions(token_hash,user_id,active_stockroom_id,expires_at)
               VALUES(%s,%s,%s,to_timestamp(%s))""",
            (token_digest(raw), user_id, stockroom_id, expires_at),
        )
        conn.commit()
    return raw, expires_at


def create_auth_token(user_id, purpose, ttl_seconds, payload=None):
    raw = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            "DELETE FROM auth_tokens WHERE user_id=%s AND purpose=%s AND used_at IS NULL",
            (user_id, purpose),
        )
        conn.execute(
            """INSERT INTO auth_tokens(token_hash,user_id,purpose,expires_at,payload)
               VALUES(%s,%s,%s,NOW() + (%s * INTERVAL '1 second'),%s::jsonb)""",
            (token_digest(raw), user_id, purpose, ttl_seconds, json.dumps(payload or {})),
        )
        conn.commit()
    return raw


def send_email(to_email, subject, text):
    if not SMTP_HOST:
        log_event("[SMTP ERROR] SMTP_HOST ontbreekt")
        raise RuntimeError("SMTP_HOST ontbreekt")

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(text)
    context = ssl.create_default_context()

    recipient_ref = hashlib.sha256(to_email.lower().encode()).hexdigest()[:12]
    log_event(f"[EMAIL] Verzenden recipient={recipient_ref} onderwerp={subject!r}")
    log_event(f"[SMTP] host={SMTP_HOST} port={SMTP_PORT} tls={'implicit' if SMTP_PORT == 465 else 'starttls'}")

    try:
        if SMTP_PORT == 465:
            log_event("[SMTP] Verbinden via implicit TLS (465)")
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15, context=context) as smtp:
                log_event("[SMTP] Verbinding OK")
                if SMTP_USERNAME:
                    log_event("[SMTP] Authenticeren")
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                    log_event("[SMTP] Authenticatie OK")
                smtp.send_message(msg)
                log_event("[SMTP] Bericht verzonden")
        else:
            log_event(f"[SMTP] Verbinden via STARTTLS ({SMTP_PORT})")
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
                code, _ = smtp.ehlo()
                log_event(f"[SMTP] EHLO antwoord={code}")
                smtp.starttls(context=context)
                log_event("[SMTP] STARTTLS OK")
                code, _ = smtp.ehlo()
                log_event(f"[SMTP] EHLO na TLS antwoord={code}")
                if SMTP_USERNAME:
                    log_event("[SMTP] Authenticeren")
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                    log_event("[SMTP] Authenticatie OK")
                smtp.send_message(msg)
                log_event("[SMTP] Bericht verzonden")
    except smtplib.SMTPAuthenticationError as exc:
        log_event(f"[SMTP ERROR] Authenticatie mislukt: code={exc.smtp_code}")
        raise
    except smtplib.SMTPResponseException as exc:
        log_event(f"[SMTP ERROR] Server antwoordde met fout: code={exc.smtp_code}")
        raise
    except (TimeoutError, OSError, ssl.SSLError, smtplib.SMTPException) as exc:
        log_event(f"[SMTP ERROR] {type(exc).__name__}")
        raise


AUTH_CSS = """
:root{color-scheme:light;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f6f8;color:#111827;padding:24px}
.card{width:min(100%,440px);background:#fff;border:1px solid #e5e7eb;border-radius:18px;padding:30px;box-shadow:0 18px 50px rgba(15,23,42,.08)}
h1{margin:0 0 8px;font-size:26px}.sub{margin:0 0 24px;color:#6b7280;font-size:14px;line-height:1.5}
label{display:block;margin:14px 0 6px;font-size:13px;font-weight:700}input,select{width:100%;border:1px solid #d1d5db;border-radius:10px;padding:12px 13px;font:inherit;outline:none}
input:focus,select:focus{border-color:#111827;box-shadow:0 0 0 3px rgba(17,24,39,.08)}
button,.button{display:inline-block;width:100%;margin-top:20px;border:0;border-radius:10px;padding:12px 14px;background:#111827;color:#fff;font:inherit;font-weight:700;cursor:pointer;text-align:center;text-decoration:none}
.error{margin:0 0 14px;padding:10px 12px;border-radius:9px;background:#fef2f2;color:#991b1b;font-size:13px}
.success{margin:0 0 14px;padding:10px 12px;border-radius:9px;background:#ecfdf5;color:#065f46;font-size:13px}
.note{margin:16px 0 0;color:#9ca3af;text-align:center;font-size:12px;line-height:1.6}.note a{color:#374151}
"""


def auth_page(title, subtitle, body_html, error="", success=""):
    feedback = ""
    if error:
        feedback += f'<p class="error">{html.escape(error)}</p>'
    if success:
        feedback += f'<p class="success">{html.escape(success)}</p>'
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · Stockroom</title><style>{AUTH_CSS}</style></head>
<body><main class="card"><h1>{html.escape(title)}</h1><p class="sub">{html.escape(subtitle)}</p>{feedback}{body_html}</main></body></html>"""


def login_page(error="", success=""):
    body = """
<form method="post" action="/login">
<label>E-mailadres<input name="email" type="email" autocomplete="email" required autofocus></label>
<label>Wachtwoord<input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Inloggen</button></form>
<p class="note"><a href="/forgot-password">Wachtwoord vergeten?</a><br>
Nog geen account? <a href="/register">Registreer een eigen stockroom</a><br>
Geen verificatiemail ontvangen? <a href="/resend-verification">Opnieuw sturen</a></p>
"""
    return auth_page("Inloggen", "Open jouw bedrijfsvoorraad.", body, error, success)


def register_page(error=""):
    body = """
<form method="post" action="/register">
<label>Naam<input name="name" autocomplete="name" required autofocus></label>
<label>Bedrijfsnaam / stockroomnaam<input name="company" required></label>
<label>E-mailadres<input name="email" type="email" autocomplete="email" required></label>
<label>Wachtwoord<input name="password" type="password" minlength="8" autocomplete="new-password" required></label>
<button type="submit">Account en stockroom aanmaken</button></form>
<p class="note">Iedere registratie krijgt een eigen, afgescheiden stockroom.<br><a href="/login">Al een account? Inloggen</a></p>
"""
    return auth_page("Registreren", "Maak een eigen stockroom aan.", body, error=error)


def forgot_page(success="", error=""):
    body = """
<form method="post" action="/forgot-password">
<label>E-mailadres<input name="email" type="email" autocomplete="email" required autofocus></label>
<button type="submit">Resetlink aanvragen</button></form>
<p class="note"><a href="/login">Terug naar inloggen</a></p>
"""
    return auth_page("Wachtwoord vergeten", "We sturen een eenmalige resetlink als het account bestaat.", body, error, success)


def resend_page(success="", error=""):
    body = """
<form method="post" action="/resend-verification">
<label>E-mailadres<input name="email" type="email" autocomplete="email" required autofocus></label>
<button type="submit">Verificatiemail opnieuw sturen</button></form>
<p class="note"><a href="/login">Terug naar inloggen</a></p>
"""
    return auth_page("E-mail verifiëren", "Vraag een nieuwe verificatielink aan.", body, error, success)


def reset_page(token, error=""):
    safe = html.escape(token, quote=True)
    body = f"""
<form method="post" action="/reset-password">
<input type="hidden" name="token" value="{safe}">
<label>Nieuw wachtwoord<input name="password" type="password" minlength="8" autocomplete="new-password" required autofocus></label>
<label>Herhaal wachtwoord<input name="password2" type="password" minlength="8" autocomplete="new-password" required></label>
<button type="submit">Wachtwoord wijzigen</button></form>
"""
    return auth_page("Nieuw wachtwoord", "Kies minimaal 8 tekens.", body, error=error)


def result_page(title, message, link="/login", link_text="Naar inloggen"):
    return auth_page(title, message, f'<a class="button" href="{html.escape(link, quote=True)}">{html.escape(link_text)}</a>')


def members_page(session, memberships, members, error="", success=""):
    can_manage = session["role"] in ("owner", "admin")
    rooms = "".join(
        f'<form method="post" action="/switch-stockroom"><input type="hidden" name="stockroom_id" value="{m["stockroom_id"]}">'
        f'<button type="submit">{html.escape(m["stockroom_name"])} ({html.escape(m["role"])})</button></form>'
        for m in memberships
    )
    rows = "".join(
        f"<tr><td>{html.escape(m['name'])}</td><td>{html.escape(m['email'])}</td><td>{html.escape(m['role'])}</td>"
        + (
            f'<td><form method="post" action="/members/remove"><input type="hidden" name="user_id" value="{m["user_id"]}"><button type="submit">Verwijderen</button></form></td>'
            if can_manage and m["role"] != "owner" and m["user_id"] != session["user_id"]
            else "<td></td>"
        )
        + "</tr>"
        for m in members
    )
    role_options = '<option value="member">Gebruiker</option>'
    if session["role"] == "owner":
        role_options += '<option value="admin">Beheerder</option>'
    add = ""
    if can_manage:
        add = f"""<section class="panel"><h2>Gebruiker koppelen</h2>
<form method="post" action="/members/add"><label>E-mailadres<input name="email" type="email" required></label>
<label>Rol<select name="role">{role_options}</select></label><button type="submit">Koppelen</button></form></section>"""
    feedback = f'<p class="error">{html.escape(error)}</p>' if error else (f'<p class="success">{html.escape(success)}</p>' if success else "")
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gebruikers · Stockroom</title><style>{AUTH_CSS}
body{{display:block}}main{{max-width:1000px;margin:auto}}.grid{{display:grid;grid-template-columns:280px 1fr;gap:20px}}.panel{{background:white;padding:22px;border-radius:16px;margin-bottom:20px}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid #eee;text-align:left}}.panel button{{margin-top:8px}}@media(max-width:760px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body><main><p><a href="/">← Dashboard</a> · <a href="/logout">Uitloggen</a></p><h1>Gebruikers</h1>{feedback}
<div class="grid"><aside class="panel"><h2>Mijn stockrooms</h2>{rooms}</aside><div>
<section class="panel"><h2>Toegang tot {html.escape(session["stockroom_name"])}</h2><table><thead><tr><th>Naam</th><th>E-mail</th><th>Rol</th><th></th></tr></thead><tbody>{rows}</tbody></table></section>{add}
</div></div></main></body></html>"""


class StockroomHandler(SimpleHTTPRequestHandler):
    session = None

    def log_message(self, fmt, *args):
        log_event(f"{self.address_string()} - {fmt % args}")

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        if self.command == "GET" and urlparse(self.path).path in ("/", "/index.html") and self.session:
            remaining = max(1, int(self.session["expires_at"] - time.time()))
            self.send_header("Refresh", f"{remaining}; url=/logout")
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def base_url(self):
        if APP_BASE_URL:
            return APP_BASE_URL
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",", 1)[0].strip()
        host = self.headers.get("Host", "localhost")
        return f"{proto}://{host}"

    def form_data(self, max_bytes=16_384):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 1 or length > max_bytes:
            return None
        try:
            return parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            return None

    def cookie_token(self):
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def current_session(self):
        raw = self.cookie_token()
        if not raw:
            return None
        with db() as conn:
            row = conn.execute("""
                SELECT s.user_id::text user_id,s.active_stockroom_id::text stockroom_id,
                       EXTRACT(EPOCH FROM s.expires_at) expires_at,u.email,u.name user_name,
                       r.name stockroom_name,m.role
                FROM sessions s
                JOIN users u ON u.id=s.user_id
                JOIN stockrooms r ON r.id=s.active_stockroom_id
                JOIN memberships m ON m.user_id=s.user_id AND m.stockroom_id=s.active_stockroom_id
                WHERE s.token_hash=%s AND s.expires_at>NOW()
            """, (token_digest(raw),)).fetchone()
            if not row:
                conn.execute("DELETE FROM sessions WHERE token_hash=%s", (token_digest(raw),))
                conn.commit()
                return None
        row["expires_at"] = float(row["expires_at"])
        self.session = row
        return row

    def secure_request(self):
        return APP_BASE_URL.startswith("https://") or self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower() == "https"

    def client_subject(self, value=""):
        forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        address = forwarded or self.client_address[0]
        try:
            address = str(ipaddress.ip_address(address))
        except ValueError:
            address = "invalid"
        return token_digest(f"{address}|{value.lower()}")

    def enforce_origin(self):
        """Reject cross-site state changes. SameSite cookies are defense-in-depth, not the CSRF control."""
        source = self.headers.get("Origin") or self.headers.get("Referer")
        if not source:
            # Cookie-authenticated requests must prove they came from our origin.
            if self.cookie_token():
                self.send_json(403, {"error": "Aanvraag geblokkeerd door CSRF-beveiliging."})
                return False
            return True
        expected = urlparse(self.base_url())
        actual = urlparse(source)
        if (actual.scheme, actual.netloc.lower()) == (expected.scheme, expected.netloc.lower()):
            return True
        self.send_json(403, {"error": "Aanvraag geblokkeerd door CSRF-beveiliging."})
        return False

    def rate_limit(self, scope, value, maximum, window_seconds):
        subject = self.client_subject(value)
        with db() as conn:
            row = conn.execute("""
                INSERT INTO rate_limits(scope,subject_hash,window_started_at,attempts)
                VALUES(%s,%s,NOW(),1)
                ON CONFLICT(scope,subject_hash) DO UPDATE SET
                  attempts=CASE WHEN rate_limits.window_started_at <= NOW()-(%s*INTERVAL '1 second') THEN 1 ELSE rate_limits.attempts+1 END,
                  window_started_at=CASE WHEN rate_limits.window_started_at <= NOW()-(%s*INTERVAL '1 second') THEN NOW() ELSE rate_limits.window_started_at END
                RETURNING attempts
            """, (scope, subject, window_seconds, window_seconds)).fetchone()
            conn.commit()
        return row["attempts"] <= maximum

    def session_cookie_header(self, token, max_age=SESSION_TTL_SECONDS):
        parts = [f"{SESSION_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
        if self.secure_request():
            parts.append("Secure")
        return "; ".join(parts)

    def clear_cookie_header(self):
        parts = [f"{SESSION_COOKIE}=", "Path=/", "HttpOnly", "SameSite=Strict", "Max-Age=0"]
        if self.secure_request():
            parts.append("Secure")
        return "; ".join(parts)

    def send_html(self, status, content):
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location, clear_cookie=False, token=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if clear_cookie:
            self.send_header("Set-Cookie", self.clear_cookie_header())
        elif token:
            self.send_header("Set-Cookie", self.session_cookie_header(token))
        self.end_headers()

    def require_session(self, api=False):
        session = self.current_session()
        if session:
            return session
        if api:
            self.send_json(401, {"error": "Sessie verlopen. Log opnieuw in."})
        else:
            self.redirect("/login", clear_cookie=True)
        return None

    def memberships_for(self, user_id):
        with db() as conn:
            return conn.execute("""
                SELECT m.stockroom_id::text,r.name stockroom_name,m.role
                FROM memberships m JOIN stockrooms r ON r.id=m.stockroom_id
                WHERE m.user_id=%s
                ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,r.name
            """, (user_id,)).fetchall()

    def members_for(self, stockroom_id):
        with db() as conn:
            return conn.execute("""
                SELECT u.id::text user_id,u.name,u.email,m.role
                FROM memberships m JOIN users u ON u.id=m.user_id
                WHERE m.stockroom_id=%s
                ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,u.name
            """, (stockroom_id,)).fetchall()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            try:
                with db() as conn:
                    conn.execute("SELECT 1")
                body, status = b"healthy\n", 200
            except Exception:
                body, status = b"unhealthy\n", 503
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/login":
            self.redirect("/") if self.current_session() else self.send_html(200, login_page())
            return
        if path == "/register":
            self.redirect("/") if self.current_session() else self.send_html(200, register_page())
            return
        if path == "/forgot-password":
            self.send_html(200, forgot_page())
            return
        if path == "/resend-verification":
            self.send_html(200, resend_page())
            return
        if path == "/reset-password":
            token = query.get("token", [""])[0]
            self.send_html(200, reset_page(token) if token else result_page("Ongeldige link", "De resetlink ontbreekt of is ongeldig."))
            return
        if path == "/verify-email":
            raw = query.get("token", [""])[0]
            if not raw:
                self.send_html(400, result_page("Ongeldige link", "De verificatielink ontbreekt."))
                return
            with db() as conn:
                row = conn.execute("""
                    SELECT user_id,purpose,payload FROM auth_tokens
                    WHERE token_hash=%s AND purpose IN ('verify_email','change_email') AND used_at IS NULL AND expires_at>NOW()
                    FOR UPDATE
                """, (token_digest(raw),)).fetchone()
                if not row:
                    self.send_html(400, result_page("Link verlopen", "Deze verificatielink is ongeldig of verlopen.", "/resend-verification", "Nieuwe link aanvragen"))
                    return
                if row["purpose"] == "change_email":
                    new_email = row["payload"].get("new_email", "").strip().lower()
                    if not new_email:
                        self.send_html(400, result_page("Ongeldige link", "Deze e-mailwijziging is ongeldig."))
                        return
                    try:
                        conn.execute("UPDATE users SET email=%s,email_verified_at=NOW() WHERE id=%s", (new_email, row["user_id"]))
                    except psycopg.errors.UniqueViolation:
                        self.send_html(409, result_page("E-mailadres bezet", "Dit e-mailadres hoort inmiddels bij een ander account."))
                        return
                    conn.execute("DELETE FROM sessions WHERE user_id=%s", (row["user_id"],))
                    audit_user_action(conn, row["user_id"], "account.email_changed")
                else:
                    conn.execute("UPDATE users SET email_verified_at=NOW() WHERE id=%s", (row["user_id"],))
                    audit_user_action(conn, row["user_id"], "account.email_verified")
                conn.execute("UPDATE auth_tokens SET used_at=NOW() WHERE token_hash=%s", (token_digest(raw),))
                conn.commit()
            message = "Je nieuwe e-mailadres is bevestigd. Alle apparaten zijn uitgelogd." if row["purpose"] == "change_email" else "Je e-mailadres is bevestigd. Je kunt nu inloggen."
            self.send_html(200, result_page("E-mail geverifieerd", message))
            return
        if path == "/logout":
            if not self.current_session():
                self.redirect("/login", clear_cookie=True)
            else:
                self.send_html(200, auth_page("Uitloggen", "Bevestig dat je deze sessie wilt beëindigen.", '<form method="post" action="/logout"><button type="submit">Uitloggen</button></form>'))
            return

        is_api = path.startswith("/api/")
        session = self.require_session(api=is_api)
        if not session:
            return
        if path == "/api/state":
            with db() as conn:
                row = conn.execute("SELECT state FROM stockrooms WHERE id=%s", (session["stockroom_id"],)).fetchone()
            self.send_json(200, row["state"] if row else EMPTY_STATE)
            return
        if path == "/api/session":
            self.send_json(200, {"expiresAt": session["expires_at"]})
            return
        if path == "/api/me":
            self.send_json(200, {
                "user": {"id": session["user_id"], "name": session["user_name"], "email": session["email"]},
                "stockroom": {"id": session["stockroom_id"], "name": session["stockroom_name"], "role": session["role"]},
                "stockrooms": self.memberships_for(session["user_id"]),
            })
            return
        if path == "/members":
            self.send_html(200, members_page(session, self.memberships_for(session["user_id"]), self.members_for(session["stockroom_id"])))
            return
        super().do_GET()

    def do_HEAD(self):
        if urlparse(self.path).path == "/health":
            self.send_response(200)
            self.end_headers()
            return
        if not self.require_session(api=urlparse(self.path).path.startswith("/api/")):
            return
        super().do_HEAD()

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.enforce_origin():
            return

        if path == "/login":
            form = self.form_data()
            if not form:
                self.send_html(400, login_page("Ongeldige aanvraag."))
                return
            email = form.get("email", [""])[0].strip().lower()
            password = form.get("password", [""])[0]
            if not self.rate_limit("login", email, 10, 900):
                self.send_html(429, login_page("Te veel inlogpogingen. Probeer het later opnieuw."))
                return
            with db() as conn:
                user = conn.execute("SELECT * FROM users WHERE email=%s", (email,)).fetchone()
                locked = user and user["locked_until"] and user["locked_until"].timestamp() > time.time()
                valid = user and not locked and verify_password(password, user["password_salt"], user["password_hash"], user.get("password_version", 1))
                if not valid:
                    if user and not locked:
                        conn.execute("""UPDATE users SET failed_login_attempts=failed_login_attempts+1,
                          locked_until=CASE WHEN failed_login_attempts+1 >= %s THEN NOW()+(%s*INTERVAL '1 second') ELSE locked_until END
                          WHERE id=%s""", (LOGIN_MAX_ATTEMPTS, LOGIN_LOCK_SECONDS, user["id"]))
                        conn.commit()
                    self.send_html(401, login_page("E-mailadres of wachtwoord is onjuist."))
                    return
                conn.execute("UPDATE users SET failed_login_attempts=0,locked_until=NULL WHERE id=%s", (user["id"],))
                if int(user.get("password_version", 1)) < 2:
                    salt, digest = hash_password(password)
                    conn.execute("UPDATE users SET password_salt=%s,password_hash=%s,password_version=2 WHERE id=%s", (salt, digest, user["id"]))
                conn.commit()
                if user["email_verified_at"] is None:
                    self.send_html(403, login_page("Verifieer eerst je e-mailadres. Je kunt een nieuwe verificatielink aanvragen."))
                    return
                membership = conn.execute("""
                    SELECT stockroom_id::text,role FROM memberships WHERE user_id=%s
                    ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,created_at LIMIT 1
                """, (user["id"],)).fetchone()
                if membership:
                    audit_user_action(conn, user["id"], "account.login")
                    conn.commit()
            if not membership:
                self.send_html(403, login_page("Dit account heeft geen stockroomtoegang."))
                return
            token, _ = create_session(str(user["id"]), membership["stockroom_id"])
            self.redirect("/", token=token)
            return

        if path == "/register":
            form = self.form_data()
            if not form:
                self.send_html(400, register_page("Ongeldige aanvraag."))
                return
            name = form.get("name", [""])[0].strip()
            company = form.get("company", [""])[0].strip()
            email = form.get("email", [""])[0].strip().lower()
            password = form.get("password", [""])[0]
            if not self.rate_limit("register", email, 5, 3600):
                self.send_html(429, register_page("Te veel registratiepogingen. Probeer het later opnieuw."))
                return
            if not name or not company or "@" not in email or len(password) < 8:
                self.send_html(400, register_page("Vul alle velden correct in. Gebruik minimaal 8 tekens voor het wachtwoord."))
                return
            user_id, stockroom_id = str(uuid.uuid4()), str(uuid.uuid4())
            salt, digest = hash_password(password)
            try:
                with db() as conn:
                    conn.execute("""INSERT INTO users(id,email,name,password_salt,password_hash,password_version,email_verified_at)
                                    VALUES(%s,%s,%s,%s,%s,2,NULL)""", (user_id, email, name, salt, digest))
                    conn.execute("INSERT INTO stockrooms(id,name,created_by,state) VALUES(%s,%s,%s,%s::jsonb)",
                                 (stockroom_id, company, user_id, json.dumps(EMPTY_STATE)))
                    conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner')",
                                 (user_id, stockroom_id))
                    conn.commit()
            except psycopg.errors.UniqueViolation:
                self.send_html(409, register_page("Dit e-mailadres is al geregistreerd."))
                return
            raw = create_auth_token(user_id, "verify_email", VERIFY_TTL_SECONDS)
            link = f"{self.base_url()}/verify-email?token={raw}"
            try:
                send_email(email, "Bevestig je Stockroom-account",
                           f"Hallo {name},\n\nBevestig je e-mailadres via deze link:\n{link}\n\nDe link is 24 uur geldig.")
            except Exception as exc:
                log_event(f"[EMAIL ERROR] Verificatiemail verzenden mislukt voor {email}: {type(exc).__name__}: {exc}")
                self.send_html(503, result_page("Account aangemaakt", "Je account is aangemaakt, maar de verificatiemail kon niet worden verzonden. Controleer de SMTP-instellingen en vraag daarna een nieuwe verificatielink aan.", "/resend-verification", "Nieuwe verificatielink"))
                return
            self.send_html(200, result_page("Controleer je e-mail", "Je account en eigen stockroom zijn aangemaakt. Bevestig eerst je e-mailadres voordat je inlogt."))
            return

        if path == "/resend-verification":
            form = self.form_data()
            email = form.get("email", [""])[0].strip().lower() if form else ""
            if not self.rate_limit("resend", email, 3, 3600):
                self.send_html(200, resend_page(success="Als dit account verificatie nodig heeft, is er een nieuwe link verstuurd."))
                return
            with db() as conn:
                user = conn.execute("SELECT id,name,email,email_verified_at FROM users WHERE email=%s", (email,)).fetchone()
            if user and user["email_verified_at"] is None:
                raw = create_auth_token(str(user["id"]), "verify_email", VERIFY_TTL_SECONDS)
                link = f"{self.base_url()}/verify-email?token={raw}"
                try:
                    send_email(email, "Nieuwe Stockroom-verificatielink",
                               f"Hallo {user['name']},\n\nBevestig je e-mailadres via:\n{link}\n\nDe link is 24 uur geldig.")
                except Exception as exc:
                    log_event(f"[EMAIL ERROR] Verificatiemail opnieuw verzenden mislukt voor {email}: {type(exc).__name__}: {exc}")
            self.send_html(200, resend_page(success="Als dit account verificatie nodig heeft, is er een nieuwe link verstuurd."))
            return

        if path == "/forgot-password":
            form = self.form_data()
            email = form.get("email", [""])[0].strip().lower() if form else ""
            if not self.rate_limit("forgot", email, 5, 3600):
                self.send_html(200, forgot_page(success="Als dit e-mailadres geregistreerd is, is er een resetlink verstuurd."))
                return
            with db() as conn:
                user = conn.execute("SELECT id,name,email FROM users WHERE email=%s", (email,)).fetchone()
            if user:
                raw = create_auth_token(str(user["id"]), "reset_password", RESET_TTL_SECONDS)
                link = f"{self.base_url()}/reset-password?token={raw}"
                try:
                    send_email(email, "Stockroom wachtwoord opnieuw instellen",
                               f"Hallo {user['name']},\n\nStel je wachtwoord opnieuw in via:\n{link}\n\nDe link is 30 minuten geldig.\nHeb je dit niet aangevraagd, negeer deze e-mail.")
                except Exception as exc:
                    log_event(f"[EMAIL ERROR] Resetmail verzenden mislukt voor {email}: {type(exc).__name__}: {exc}")
            self.send_html(200, forgot_page(success="Als dit e-mailadres geregistreerd is, is er een resetlink verstuurd."))
            return

        if path == "/reset-password":
            form = self.form_data()
            raw = form.get("token", [""])[0] if form else ""
            password = form.get("password", [""])[0] if form else ""
            password2 = form.get("password2", [""])[0] if form else ""
            if not self.rate_limit("reset", raw, 5, 900):
                self.send_html(429, result_page("Te veel pogingen", "Vraag een nieuwe resetlink aan."))
                return
            if len(password) < 8 or password != password2:
                self.send_html(400, reset_page(raw, "Wachtwoorden moeten gelijk zijn en minimaal 8 tekens bevatten."))
                return
            salt, digest = hash_password(password)
            with db() as conn:
                row = conn.execute("""
                    SELECT user_id FROM auth_tokens
                    WHERE token_hash=%s AND purpose='reset_password' AND used_at IS NULL AND expires_at>NOW()
                    FOR UPDATE
                """, (token_digest(raw),)).fetchone()
                if not row:
                    self.send_html(400, result_page("Link verlopen", "Deze resetlink is ongeldig of verlopen.", "/forgot-password", "Nieuwe resetlink aanvragen"))
                    return
                conn.execute("UPDATE users SET password_salt=%s,password_hash=%s,password_version=2,failed_login_attempts=0,locked_until=NULL WHERE id=%s", (salt, digest, row["user_id"]))
                conn.execute("UPDATE auth_tokens SET used_at=NOW() WHERE token_hash=%s", (token_digest(raw),))
                conn.execute("DELETE FROM sessions WHERE user_id=%s", (row["user_id"],))
                audit_user_action(conn, row["user_id"], "account.password_reset")
                conn.commit()
            self.send_html(200, result_page("Wachtwoord gewijzigd", "Je wachtwoord is aangepast. Alle bestaande sessies zijn uitgelogd."))
            return

        session = self.require_session(api=False)
        if not session:
            return

        if path == "/switch-stockroom":
            form = self.form_data()
            stockroom_id = form.get("stockroom_id", [""])[0] if form else ""
            with db() as conn:
                member = conn.execute("SELECT 1 FROM memberships WHERE user_id=%s AND stockroom_id=%s",
                                      (session["user_id"], stockroom_id)).fetchone()
                if not member:
                    self.send_error(403)
                    return
                conn.execute("UPDATE sessions SET active_stockroom_id=%s WHERE token_hash=%s",
                             (stockroom_id, token_digest(self.cookie_token())))
                conn.commit()
            self.redirect("/")
            return

        if path == "/members/add":
            if session["role"] not in ("owner", "admin"):
                self.send_error(403)
                return
            form = self.form_data()
            email = form.get("email", [""])[0].strip().lower() if form else ""
            role = form.get("role", ["member"])[0] if form else "member"
            if role not in ("member", "admin") or (role == "admin" and session["role"] != "owner"):
                self.send_error(403)
                return
            with db() as conn:
                user = conn.execute("SELECT id FROM users WHERE email=%s", (email,)).fetchone()
                if not user:
                    self.send_html(404, members_page(session, self.memberships_for(session["user_id"]), self.members_for(session["stockroom_id"]), error="Geen geregistreerde gebruiker gevonden met dit e-mailadres."))
                    return
                conn.execute("""INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,%s)
                                ON CONFLICT(user_id,stockroom_id) DO UPDATE SET role=EXCLUDED.role""",
                             (user["id"], session["stockroom_id"], role))
                conn.commit()
            self.send_html(200, members_page(session, self.memberships_for(session["user_id"]), self.members_for(session["stockroom_id"]), success="Gebruiker is gekoppeld aan deze stockroom."))
            return

        if path == "/members/remove":
            if session["role"] not in ("owner", "admin"):
                self.send_error(403)
                return
            form = self.form_data()
            target_user = form.get("user_id", [""])[0] if form else ""
            with db() as conn:
                target = conn.execute("SELECT role FROM memberships WHERE user_id=%s AND stockroom_id=%s",
                                      (target_user, session["stockroom_id"])).fetchone()
                if not target or target["role"] == "owner" or target_user == session["user_id"] or (session["role"] == "admin" and target["role"] == "admin"):
                    self.send_error(403)
                    return
                conn.execute("DELETE FROM memberships WHERE user_id=%s AND stockroom_id=%s",
                             (target_user, session["stockroom_id"]))
                conn.execute("DELETE FROM sessions WHERE user_id=%s AND active_stockroom_id=%s",
                             (target_user, session["stockroom_id"]))
                conn.commit()
            self.redirect("/members")
            return

        if path == "/logout":
            raw = self.cookie_token()
            if raw:
                with db() as conn:
                    audit_user_action(conn, session["user_id"], "account.logout")
                    conn.execute("DELETE FROM sessions WHERE token_hash=%s", (token_digest(raw),))
                    conn.commit()
            self.redirect("/login", clear_cookie=True)
            return

        self.send_error(404)

    def do_PUT(self):
        if not self.enforce_origin():
            return
        session = self.require_session(api=True)
        if not session:
            return
        if urlparse(self.path).path != "/api/state":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Ongeldige aanvraag."})
            return
        if length < 2 or length > MAX_BODY_BYTES:
            self.send_json(413, {"error": "Aanvraag is te groot of leeg."})
            return
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Ongeldige JSON."})
            return
        if not valid_state(value):
            self.send_json(422, {"error": "Ongeldige voorraadgegevens."})
            return
        with db() as conn:
            conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s",
                         (json.dumps(value, ensure_ascii=False), session["stockroom_id"]))
            conn.commit()
        self.send_json(200, {"saved": True})


if __name__ == "__main__":
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL is verplicht en moet naar PostgreSQL wijzen.")
    initialize_database()
    cleanup_expired()
    handler = partial(StockroomHandler, directory=str(PUBLIC_DIR))
    server = ThreadingHTTPServer((HOST, PORT), handler)
    log_event(f"Stockroom draait op poort {PORT} met PostgreSQL, e-mailverificatie en {SESSION_TTL_SECONDS // 60} minuten sessies")
    server.serve_forever()
