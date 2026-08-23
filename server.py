import base64
import binascii
import hmac
import json
import os
import sqlite3
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = "0.0.0.0"
PORT = 8000
PUBLIC_DIR = Path("/app/public")
DB_PATH = Path(os.environ.get("STOCKROOM_DB_PATH", "/data/stockroom.db"))
USERNAME = os.environ.get("STOCKROOM_USERNAME", "")
PASSWORD = os.environ.get("STOCKROOM_PASSWORD", "")
MAX_BODY_BYTES = 1_000_000

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


class StockroomHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        super().end_headers()

    def authenticated(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            supplied_user, supplied_password = decoded.split(":", 1)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return False
        return hmac.compare_digest(supplied_user, USERNAME) and hmac.compare_digest(supplied_password, PASSWORD)

    def require_auth(self):
        if self.authenticated():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Stockroom", charset="UTF-8"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
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
        if not self.require_auth():
            return
        if path == "/api/state":
            self.send_json(200, read_state())
            return
        super().do_GET()

    def do_HEAD(self):
        if urlparse(self.path).path == "/health":
            self.send_response(200)
            self.end_headers()
            return
        if not self.require_auth():
            return
        super().do_HEAD()

    def do_PUT(self):
        if not self.require_auth():
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
    print(f"Stockroom draait op poort {PORT} met database {DB_PATH}")
    server.serve_forever()

