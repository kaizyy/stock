import hmac
import html
import json
import os
import secrets
import sqlite3
import threading
import time
from functools import partial
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = "0.0.0.0"
PORT = 8000
PUBLIC_DIR = Path("/app/public")
DB_PATH = Path(os.environ.get("STOCKROOM_DB_PATH", "/data/stockroom.db"))
USERNAME = os.environ.get("STOCKROOM_USERNAME", "")
PASSWORD = os.environ.get("STOCKROOM_PASSWORD", "")
MAX_BODY_BYTES = 1_000_000
SESSION_TTL_SECONDS = 30 * 60
SESSION_COOKIE = "stockroom_session"

# Sessies staan bewust alleen in het geheugen. Een herstart/deploy logt iedereen uit.
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()

INITIAL_STATE = {
    "items": [
        {"id": "i1", "name": "Canvas draagtas", "sku": "CT-001", "stock": 24, "buy": 8.5, "sell": 19.95},
        {"id": "i2", "name": "Keramische mok", "sku": "KM-012", "stock": 8, "buy": 6.25, "sell": 14.5},
        {"id": "i3", "name": "Notitieboek A5", "sku": "NB-205", "stock": 16, "buy": 4.8, "sell": 12.95},
        {"id": "i4", "name": "Metalen drinkfles", "sku": "DF-110", "stock": 5, "buy": 11.4, "sell": 27.5},
    ],
    "transactions": [],
}


def connect():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def initialize_database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS application_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO application_state (id, payload) VALUES (1, ?)",
            (json.dumps(INITIAL_STATE, separators=(",", ":")),),
        )
        connection.execute("PRAGMA optimize")


def read_state():
    with connect() as connection:
        row = connection.execute(
            "SELECT payload FROM application_state WHERE id = 1"
        ).fetchone()
    return json.loads(row[0])


def write_state(state):
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    with connect() as connection:
        connection.execute(
            "UPDATE application_state SET payload = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
            (payload,),
        )


def valid_state(value):
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("items"), list) or not isinstance(value.get("transactions"), list):
        return False
    return all(isinstance(record, dict) for record in value["items"] + value["transactions"])


def cleanup_sessions(now=None):
    now = time.time() if now is None else now
    with SESSIONS_LOCK:
        expired = [token for token, expires_at in SESSIONS.items() if expires_at <= now]
        for token in expired:
            SESSIONS.pop(token, None)


def create_session():
    now = time.time()
    cleanup_sessions(now)
    token = secrets.token_urlsafe(32)
    expires_at = now + SESSION_TTL_SECONDS
    with SESSIONS_LOCK:
        SESSIONS[token] = expires_at
    return token, expires_at


def destroy_session(token):
    if not token:
        return
    with SESSIONS_LOCK:
        SESSIONS.pop(token, None)


LOGIN_PAGE = """<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inloggen · Stockroom</title>
<style>
:root{color-scheme:light;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f6f8;color:#111827;padding:24px}
.login{width:min(100%,390px);background:#fff;border:1px solid #e5e7eb;border-radius:18px;padding:30px;box-shadow:0 18px 50px rgba(15,23,42,.08)}
h1{margin:0 0 8px;font-size:26px}.sub{margin:0 0 24px;color:#6b7280;font-size:14px;line-height:1.5}
label{display:block;margin:14px 0 6px;font-size:13px;font-weight:700}input{width:100%;border:1px solid #d1d5db;border-radius:10px;padding:12px 13px;font:inherit;outline:none}input:focus{border-color:#111827;box-shadow:0 0 0 3px rgba(17,24,39,.08)}
button{width:100%;margin-top:20px;border:0;border-radius:10px;padding:12px 14px;background:#111827;color:#fff;font:inherit;font-weight:700;cursor:pointer}.error{margin:0 0 14px;padding:10px 12px;border-radius:9px;background:#fef2f2;color:#991b1b;font-size:13px}.note{margin:16px 0 0;color:#9ca3af;text-align:center;font-size:12px}
</style>
</head>
<body><main class="login"><h1>Stockroom</h1><p class="sub">Log in om het dashboard te openen.</p>{error}<form method="post" action="/login" autocomplete="on"><label for="username">Gebruikersnaam</label><input id="username" name="username" type="text" autocomplete="username" required autofocus><label for="password">Wachtwoord</label><input id="password" name="password" type="password" autocomplete="current-password" required><button type="submit">Inloggen</button></form><p class="note">Je sessie verloopt automatisch na 30 minuten.</p></main></body></html>"""


class StockroomHandler(SimpleHTTPRequestHandler):
    session_expires_at = None

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        path = urlparse(self.path).path
        if self.command == "GET" and path in ("/", "/index.html") and self.session_expires_at:
            remaining = max(1, int(self.session_expires_at - time.time()))
            self.send_header("Refresh", f"{remaining}; url=/logout")
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

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
        now = time.time()
        with SESSIONS_LOCK:
            expires_at = SESSIONS.get(token)
            if expires_at is None:
                return None
            if expires_at <= now:
                SESSIONS.pop(token, None)
                return None
        self.session_expires_at = expires_at
        return token

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

    def send_login_page(self, error="", status=200):
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        body = LOGIN_PAGE.replace("{error}", error_html).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location, clear_cookie=False):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if clear_cookie:
            self.send_header("Set-Cookie", self.clear_cookie_header())
        self.end_headers()

    def require_session(self, api=False):
        if self.current_session():
            return True
        if api:
            self.send_json(401, {"error": "Sessie verlopen. Log opnieuw in."})
        else:
            self.redirect("/login", clear_cookie=True)
        return False

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            body = b"healthy\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/login":
            if self.current_session():
                self.redirect("/")
            else:
                self.send_login_page()
            return
        if path == "/logout":
            destroy_session(self.cookie_token())
            self.redirect("/login", clear_cookie=True)
            return

        is_api = path.startswith("/api/")
        if not self.require_session(api=is_api):
            return

        if path == "/api/state":
            self.send_json(200, read_state())
            return
        if path == "/api/session":
            self.send_json(200, {"expiresAt": self.session_expires_at})
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
        if path == "/logout":
            destroy_session(self.cookie_token())
            self.redirect("/login", clear_cookie=True)
            return
        if path != "/login":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_login_page("Ongeldige aanvraag.", 400)
            return
        if length < 1 or length > 16_384:
            self.send_login_page("Ongeldige aanvraag.", 400)
            return
        try:
            form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            self.send_login_page("Ongeldige aanvraag.", 400)
            return
        supplied_user = form.get("username", [""])[0]
        supplied_password = form.get("password", [""])[0]
        valid = hmac.compare_digest(supplied_user, USERNAME) and hmac.compare_digest(supplied_password, PASSWORD)
        if not valid:
            self.send_login_page("Gebruikersnaam of wachtwoord is onjuist.", 401)
            return
        token, expires_at = create_session()
        self.session_expires_at = expires_at
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", self.session_cookie_header(token))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_PUT(self):
        if not self.require_session(api=True):
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
        write_state(value)
        self.send_json(200, {"saved": True})


if __name__ == "__main__":
    if not USERNAME or not PASSWORD:
        raise SystemExit("STOCKROOM_USERNAME en STOCKROOM_PASSWORD zijn verplicht.")
    initialize_database()
    handler = partial(StockroomHandler, directory=str(PUBLIC_DIR))
    server = ThreadingHTTPServer((HOST, PORT), handler)
    print(f"Stockroom draait op poort {PORT} met database {DB_PATH}; sessies verlopen na {SESSION_TTL_SECONDS} seconden")
    server.serve_forever()
