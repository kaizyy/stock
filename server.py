import hashlib
import hmac
import html
import json
import os
import secrets
import time
import uuid
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
MAX_BODY_BYTES = 1_000_000
SESSION_TTL_SECONDS = 30 * 60
SESSION_COOKIE = "stockroom_session"

EMPTY_STATE = {"items": [], "transactions": []}


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
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
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
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_memberships_stockroom_id ON memberships(stockroom_id)")
        conn.commit()


def hash_password(password, salt=None):
    salt = os.urandom(16) if salt is None else salt
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt, digest


def verify_password(password, salt, expected):
    _, digest = hash_password(password, bytes(salt))
    return hmac.compare_digest(digest, bytes(expected))


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_state(value):
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("items"), list) or not isinstance(value.get("transactions"), list):
        return False
    return all(isinstance(record, dict) for record in value["items"] + value["transactions"])


def cleanup_sessions():
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= NOW()")
        conn.commit()


def create_session(user_id, stockroom_id):
    raw = secrets.token_urlsafe(32)
    expires_at = time.time() + SESSION_TTL_SECONDS
    with db() as conn:
        conn.execute(
            """
            INSERT INTO sessions (token_hash, user_id, active_stockroom_id, expires_at)
            VALUES (%s, %s, %s, to_timestamp(%s))
            """,
            (token_digest(raw), user_id, stockroom_id, expires_at),
        )
        conn.commit()
    return raw, expires_at


AUTH_CSS = """
:root{color-scheme:light;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f6f8;color:#111827;padding:24px}
.card{width:min(100%,430px);background:#fff;border:1px solid #e5e7eb;border-radius:18px;padding:30px;box-shadow:0 18px 50px rgba(15,23,42,.08)}
h1{margin:0 0 8px;font-size:26px}.sub{margin:0 0 24px;color:#6b7280;font-size:14px;line-height:1.5}
label{display:block;margin:14px 0 6px;font-size:13px;font-weight:700}input,select{width:100%;border:1px solid #d1d5db;border-radius:10px;padding:12px 13px;font:inherit;outline:none}
input:focus,select:focus{border-color:#111827;box-shadow:0 0 0 3px rgba(17,24,39,.08)}
button,.button{display:inline-block;width:100%;margin-top:20px;border:0;border-radius:10px;padding:12px 14px;background:#111827;color:#fff;font:inherit;font-weight:700;cursor:pointer;text-align:center;text-decoration:none}
.secondary{background:#fff;color:#111827;border:1px solid #d1d5db}.error{margin:0 0 14px;padding:10px 12px;border-radius:9px;background:#fef2f2;color:#991b1b;font-size:13px}
.success{margin:0 0 14px;padding:10px 12px;border-radius:9px;background:#ecfdf5;color:#065f46;font-size:13px}
.note{margin:16px 0 0;color:#9ca3af;text-align:center;font-size:12px}.note a{color:#374151}
"""


def auth_page(title, subtitle, form_html, error="", success=""):
    feedback = ""
    if error:
        feedback += f'<p class="error">{html.escape(error)}</p>'
    if success:
        feedback += f'<p class="success">{html.escape(success)}</p>'
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · Stockroom</title><style>{AUTH_CSS}</style></head>
<body><main class="card"><h1>{html.escape(title)}</h1><p class="sub">{html.escape(subtitle)}</p>{feedback}{form_html}</main></body></html>"""


def login_page(error=""):
    form = """
<form method="post" action="/login" autocomplete="on">
<label for="email">E-mailadres</label><input id="email" name="email" type="email" autocomplete="email" required autofocus>
<label for="password">Wachtwoord</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Inloggen</button></form>
<p class="note">Nog geen account? <a href="/register">Registreer een eigen stockroom</a><br>Je sessie verloopt automatisch na 30 minuten.</p>
"""
    return auth_page("Inloggen", "Open jouw bedrijfsvoorraad.", form, error=error)


def register_page(error=""):
    form = """
<form method="post" action="/register" autocomplete="on">
<label for="name">Naam</label><input id="name" name="name" autocomplete="name" required autofocus>
<label for="company">Bedrijfsnaam / stockroomnaam</label><input id="company" name="company" required>
<label for="email">E-mailadres</label><input id="email" name="email" type="email" autocomplete="email" required>
<label for="password">Wachtwoord</label><input id="password" name="password" type="password" minlength="8" autocomplete="new-password" required>
<button type="submit">Account en stockroom aanmaken</button></form>
<p class="note">Elke nieuwe registratie krijgt een volledig eigen stockroom.<br><a href="/login">Al een account? Inloggen</a></p>
"""
    return auth_page("Registreren", "Maak een eigen, afgescheiden stockroom aan.", form, error=error)


def members_page(session, memberships, members, error="", success=""):
    can_manage = session["role"] in ("owner", "admin")
    stockroom_options = "".join(
        f'<form method="post" action="/switch-stockroom"><input type="hidden" name="stockroom_id" value="{m["stockroom_id"]}">'
        f'<button class="{"active" if m["stockroom_id"] == session["stockroom_id"] else ""}" type="submit">'
        f'{html.escape(m["stockroom_name"])} <small>{html.escape(m["role"])}</small></button></form>'
        for m in memberships
    )
    member_rows = "".join(
        f"<tr><td>{html.escape(m['name'])}</td><td>{html.escape(m['email'])}</td><td>{html.escape(m['role'])}</td>"
        + (f'<td><form method="post" action="/members/remove"><input type="hidden" name="user_id" value="{m["user_id"]}"><button type="submit">Verwijderen</button></form></td>'
           if can_manage and m["role"] != "owner" and m["user_id"] != session["user_id"] else "<td></td>") + "</tr>"
        for m in members
    )
    role_options = '<option value="member">Gebruiker</option>'
    if session["role"] == "owner":
        role_options += '<option value="admin">Beheerder</option>'
    add_form = ""
    if can_manage:
        add_form = f"""
        <section class="panel"><h2>Gebruiker koppelen</h2>
        <p>De gebruiker moet eerst zelf een account hebben geregistreerd. Koppel daarna diens e-mailadres aan deze stockroom.</p>
        <form method="post" action="/members/add">
        <label>E-mailadres<input name="email" type="email" required></label>
        <label>Rol<select name="role">{role_options}</select></label>
        <button type="submit">Koppelen</button></form></section>"""
    feedback = ""
    if error:
        feedback = f'<p class="error">{html.escape(error)}</p>'
    elif success:
        feedback = f'<p class="success">{html.escape(success)}</p>'
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gebruikers · Stockroom</title><style>
{AUTH_CSS}
body{{display:block;padding:28px;background:#f5f6f8}}main{{max-width:980px;margin:0 auto}}.top{{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:24px}}
.top a{{color:#111827;text-decoration:none;font-weight:700}}.grid{{display:grid;grid-template-columns:280px 1fr;gap:20px}}.panel{{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:22px;margin-bottom:20px}}
.panel h2{{margin-top:0}}.rooms form{{margin:0 0 8px}}.rooms button{{margin:0;text-align:left;background:#fff;color:#111827;border:1px solid #e5e7eb}}
.rooms button.active{{background:#111827;color:#fff}}.rooms small{{display:block;opacity:.65;margin-top:2px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:12px 8px;border-bottom:1px solid #eee;text-align:left}}
td form button{{width:auto;margin:0;padding:8px 10px;background:#fff;color:#991b1b;border:1px solid #fecaca}}@media(max-width:760px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body><main><div class="top"><div><h1>Gebruikers</h1><p>{html.escape(session["stockroom_name"])}</p></div><div><a href="/">← Dashboard</a> · <a href="/logout">Uitloggen</a></div></div>
{feedback}<div class="grid"><aside class="panel rooms"><h2>Mijn stockrooms</h2>{stockroom_options}</aside><div>
<section class="panel"><h2>Toegang</h2><table><thead><tr><th>Naam</th><th>E-mail</th><th>Rol</th><th></th></tr></thead><tbody>{member_rows}</tbody></table></section>
{add_form}</div></div></main></body></html>"""


class StockroomHandler(SimpleHTTPRequestHandler):
    session = None

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

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
        token = self.cookie_token()
        if not token:
            return None
        digest = token_digest(token)
        with db() as conn:
            row = conn.execute(
                """
                SELECT
                    s.user_id::text AS user_id,
                    s.active_stockroom_id::text AS stockroom_id,
                    EXTRACT(EPOCH FROM s.expires_at) AS expires_at,
                    u.email,
                    u.name AS user_name,
                    r.name AS stockroom_name,
                    m.role
                FROM sessions s
                JOIN users u ON u.id=s.user_id
                JOIN stockrooms r ON r.id=s.active_stockroom_id
                JOIN memberships m ON m.user_id=s.user_id AND m.stockroom_id=s.active_stockroom_id
                WHERE s.token_hash=%s AND s.expires_at > NOW()
                """,
                (digest,),
            ).fetchone()
            if not row:
                conn.execute("DELETE FROM sessions WHERE token_hash=%s", (digest,))
                conn.commit()
                return None
        row["expires_at"] = float(row["expires_at"])
        self.session = row
        return row

    def secure_request(self):
        forwarded = self.headers.get("X-Forwarded-Proto", "")
        return forwarded.split(",", 1)[0].strip().lower() == "https"

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
            return conn.execute(
                """
                SELECT m.stockroom_id::text, r.name AS stockroom_name, m.role
                FROM memberships m JOIN stockrooms r ON r.id=m.stockroom_id
                WHERE m.user_id=%s
                ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, r.name
                """,
                (user_id,),
            ).fetchall()

    def members_for(self, stockroom_id):
        with db() as conn:
            return conn.execute(
                """
                SELECT u.id::text AS user_id, u.name, u.email, m.role
                FROM memberships m JOIN users u ON u.id=m.user_id
                WHERE m.stockroom_id=%s
                ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, u.name
                """,
                (stockroom_id,),
            ).fetchall()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            try:
                with db() as conn:
                    conn.execute("SELECT 1")
                body = b"healthy\n"
                self.send_response(200)
            except Exception:
                body = b"unhealthy\n"
                self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/login":
            if self.current_session():
                self.redirect("/")
            else:
                self.send_html(200, login_page())
            return

        if path == "/register":
            if self.current_session():
                self.redirect("/")
            else:
                self.send_html(200, register_page())
            return

        if path == "/logout":
            token = self.cookie_token()
            if token:
                with db() as conn:
                    conn.execute("DELETE FROM sessions WHERE token_hash=%s", (token_digest(token),))
                    conn.commit()
            self.redirect("/login", clear_cookie=True)
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
            self.send_html(200, members_page(
                session,
                self.memberships_for(session["user_id"]),
                self.members_for(session["stockroom_id"]),
            ))
            return

        super().do_GET()

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path == "/health":
            self.send_response(200)
            self.end_headers()
            return
        if not self.require_session(api=path.startswith("/api/")):
            return
        super().do_HEAD()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/login":
            form = self.form_data()
            if form is None:
                self.send_html(400, login_page("Ongeldige aanvraag."))
                return
            email = form.get("email", [""])[0].strip().lower()
            password = form.get("password", [""])[0]
            with db() as conn:
                user = conn.execute("SELECT * FROM users WHERE email=%s", (email,)).fetchone()
                if not user or not verify_password(password, user["password_salt"], user["password_hash"]):
                    self.send_html(401, login_page("E-mailadres of wachtwoord is onjuist."))
                    return
                membership = conn.execute(
                    """
                    SELECT stockroom_id::text, role FROM memberships WHERE user_id=%s
                    ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, created_at
                    LIMIT 1
                    """,
                    (user["id"],),
                ).fetchone()
            if not membership:
                self.send_html(403, login_page("Dit account heeft geen stockroomtoegang."))
                return
            token, _ = create_session(str(user["id"]), membership["stockroom_id"])
            self.redirect("/", token=token)
            return

        if path == "/register":
            form = self.form_data()
            if form is None:
                self.send_html(400, register_page("Ongeldige aanvraag."))
                return
            name = form.get("name", [""])[0].strip()
            company = form.get("company", [""])[0].strip()
            email = form.get("email", [""])[0].strip().lower()
            password = form.get("password", [""])[0]
            if not name or not company or "@" not in email or len(password) < 8:
                self.send_html(400, register_page("Vul alle velden correct in. Gebruik minimaal 8 tekens voor het wachtwoord."))
                return
            user_id = str(uuid.uuid4())
            stockroom_id = str(uuid.uuid4())
            salt, digest = hash_password(password)
            try:
                with db() as conn:
                    conn.execute(
                        "INSERT INTO users(id,email,name,password_salt,password_hash) VALUES(%s,%s,%s,%s,%s)",
                        (user_id, email, name, salt, digest),
                    )
                    conn.execute(
                        "INSERT INTO stockrooms(id,name,created_by,state) VALUES(%s,%s,%s,%s::jsonb)",
                        (stockroom_id, company, user_id, json.dumps(EMPTY_STATE)),
                    )
                    conn.execute(
                        "INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner')",
                        (user_id, stockroom_id),
                    )
                    conn.commit()
            except psycopg.errors.UniqueViolation:
                self.send_html(409, register_page("Dit e-mailadres is al geregistreerd."))
                return
            token, _ = create_session(user_id, stockroom_id)
            self.redirect("/", token=token)
            return

        session = self.require_session(api=False)
        if not session:
            return

        if path == "/switch-stockroom":
            form = self.form_data()
            stockroom_id = form.get("stockroom_id", [""])[0] if form else ""
            with db() as conn:
                member = conn.execute(
                    "SELECT 1 FROM memberships WHERE user_id=%s AND stockroom_id=%s",
                    (session["user_id"], stockroom_id),
                ).fetchone()
                if not member:
                    self.send_error(403)
                    return
                token = self.cookie_token()
                conn.execute(
                    "UPDATE sessions SET active_stockroom_id=%s WHERE token_hash=%s",
                    (stockroom_id, token_digest(token)),
                )
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
                conn.execute(
                    """
                    INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,%s)
                    ON CONFLICT (user_id,stockroom_id) DO UPDATE SET role=EXCLUDED.role
                    """,
                    (user["id"], session["stockroom_id"], role),
                )
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
                target = conn.execute(
                    "SELECT role FROM memberships WHERE user_id=%s AND stockroom_id=%s",
                    (target_user, session["stockroom_id"]),
                ).fetchone()
                if not target or target["role"] == "owner" or target_user == session["user_id"]:
                    self.send_error(403)
                    return
                if session["role"] == "admin" and target["role"] == "admin":
                    self.send_error(403)
                    return
                conn.execute(
                    "DELETE FROM memberships WHERE user_id=%s AND stockroom_id=%s",
                    (target_user, session["stockroom_id"]),
                )
                conn.execute(
                    "DELETE FROM sessions WHERE user_id=%s AND active_stockroom_id=%s",
                    (target_user, session["stockroom_id"]),
                )
                conn.commit()
            self.redirect("/members")
            return

        if path == "/logout":
            token = self.cookie_token()
            if token:
                with db() as conn:
                    conn.execute("DELETE FROM sessions WHERE token_hash=%s", (token_digest(token),))
                    conn.commit()
            self.redirect("/login", clear_cookie=True)
            return

        self.send_error(404)

    def do_PUT(self):
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
            conn.execute(
                "UPDATE stockrooms SET state=%s::jsonb, updated_at=NOW() WHERE id=%s",
                (json.dumps(value, ensure_ascii=False), session["stockroom_id"]),
            )
            conn.commit()
        self.send_json(200, {"saved": True})


if __name__ == "__main__":
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL is verplicht en moet naar PostgreSQL wijzen.")
    initialize_database()
    cleanup_sessions()
    handler = partial(StockroomHandler, directory=str(PUBLIC_DIR))
    server = ThreadingHTTPServer((HOST, PORT), handler)
    print(f"Stockroom draait op poort {PORT} met PostgreSQL en {SESSION_TTL_SECONDS // 60} minuten sessies")
    server.serve_forever()
