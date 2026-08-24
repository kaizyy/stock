from functools import partial
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

import server
from psycopg import sql

ORIGINAL_MEMBERS_PAGE = server.members_page
ROLE_LABELS = {
    "owner": "Owner",
    "admin": "Admin",
    "member": "Gebruiker",
    "buyer": "Inkoper",
    "seller": "Verkoper",
    "viewer": "Viewer",
}


def migrate_roles():
    with server.db() as conn:
        rows = conn.execute("""
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'memberships'::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%role%'
        """).fetchall()
        for row in rows:
            constraint_name = row["conname"].decode() if isinstance(row["conname"], bytes) else row["conname"]
            conn.execute(sql.SQL("ALTER TABLE memberships DROP CONSTRAINT {}").format(sql.Identifier(constraint_name)))
        conn.execute("ALTER TABLE memberships ADD CONSTRAINT memberships_role_check CHECK (role IN ('owner','admin','member','buyer','seller','viewer'))")
        conn.commit()


def account_page(session, error=""):
    with server.db() as conn:
        owned = conn.execute(
            "SELECT id::text,name FROM stockrooms WHERE created_by=%s ORDER BY name",
            (session["user_id"],),
        ).fetchall()
    owned_text = "".join(f"<li>{server.html.escape(row['name'])}</li>" for row in owned) or "<li>Geen eigen stockrooms.</li>"
    warning = (
        "Je account wordt permanent verwijderd. Alle stockrooms waarvan jij eigenaar bent worden ook permanent verwijderd, inclusief voorraad, transacties en memberships. "
        "Toegang tot stockrooms van andere eigenaren wordt eveneens verwijderd. Dit kan niet ongedaan worden gemaakt."
    )
    feedback = f'<p class="error">{server.html.escape(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Account verwijderen · Stockroom</title><style>{server.AUTH_CSS}
.danger{{border:1px solid #fecaca;background:#fff7f7;border-radius:14px;padding:18px;margin:18px 0}}.danger h2{{color:#991b1b;margin-top:0}}ul{{padding-left:20px;color:#4b5563}}.cancel{{display:block;text-align:center;margin-top:14px;color:#374151}}
</style></head><body><main class="card"><h1>Account verwijderen</h1><p class="sub">{server.html.escape(session['email'])}</p>{feedback}<div class="danger"><h2>Permanent verwijderen</h2><p>{server.html.escape(warning)}</p><p><strong>Eigen stockrooms die worden verwijderd:</strong></p><ul>{owned_text}</ul></div>
<form method="post" action="/account/delete"><label>Wachtwoord<input name="password" type="password" autocomplete="current-password" required></label><label>Typ VERWIJDEREN ter bevestiging<input name="confirm" required autocomplete="off"></label><button type="submit" style="background:#991b1b">Account permanent verwijderen</button></form><a class="cancel" href="/">Annuleren en terug naar dashboard</a></main></body></html>"""


def members_page(session, memberships, members, error="", success=""):
    original = ORIGINAL_MEMBERS_PAGE(session, memberships, members, error, success)
    if session["role"] in ("owner", "admin"):
        marker = '<option value="member">Gebruiker</option>'
        extra = marker + '<option value="buyer">Inkoper — alleen inkomend</option><option value="seller">Verkoper — alleen uitgaand</option><option value="viewer">Viewer — alleen lezen</option>'
        original = original.replace(marker, extra)
    original = original.replace("(owner)", "(Owner)").replace("(admin)", "(Admin)").replace("(member)", "(Gebruiker)").replace("(buyer)", "(Inkoper)").replace("(seller)", "(Verkoper)").replace("(viewer)", "(Viewer)")
    account_link = '<p style="margin-top:24px"><a href="/account">Mijn account</a></p>'
    return original.replace('</main></body></html>', account_link + '</main></body></html>')


server.members_page = members_page


def tx_by_type(state, tx_type):
    return sorted((t for t in state.get("transactions", []) if t.get("type") == tx_type), key=lambda t: str(t.get("id", "")))


def item_map(state):
    return {str(i.get("id")): i for i in state.get("items", [])}


def specialized_change_allowed(role, old_state, new_state):
    old_items, new_items = item_map(old_state), item_map(new_state)
    if set(old_items) != set(new_items):
        return False

    if role == "buyer":
        if tx_by_type(old_state, "outgoing") != tx_by_type(new_state, "outgoing"):
            return False
        for item_id, old in old_items.items():
            new = new_items[item_id]
            for key in ("id", "name", "sku", "archived"):
                if old.get(key) != new.get(key):
                    return False
        return True

    if role == "seller":
        if tx_by_type(old_state, "incoming") != tx_by_type(new_state, "incoming"):
            return False
        for item_id, old in old_items.items():
            new = new_items[item_id]
            for key in ("id", "name", "sku", "buy", "sell", "archived"):
                if old.get(key) != new.get(key):
                    return False
        return True

    return False


class StockroomHandler(server.StockroomHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/account":
            session = self.require_session(api=False)
            if not session:
                return
            self.send_html(200, account_page(session))
            return
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.enforce_origin():
            return

        if path == "/account/delete":
            session = self.require_session(api=False)
            if not session:
                return
            form = self.form_data()
            password = form.get("password", [""])[0] if form else ""
            confirm = form.get("confirm", [""])[0].strip() if form else ""
            if confirm != "VERWIJDEREN":
                self.send_html(400, account_page(session, "Typ exact VERWIJDEREN om de verwijdering te bevestigen."))
                return
            with server.db() as conn:
                user = conn.execute("SELECT password_salt,password_hash,password_version FROM users WHERE id=%s", (session["user_id"],)).fetchone()
                if not user or not server.verify_password(password, user["password_salt"], user["password_hash"], user["password_version"]):
                    self.send_html(403, account_page(session, "Het opgegeven wachtwoord is onjuist."))
                    return
                owned = conn.execute("SELECT id FROM stockrooms WHERE created_by=%s FOR UPDATE", (session["user_id"],)).fetchall()
                for row in owned:
                    conn.execute("DELETE FROM stockrooms WHERE id=%s", (row["id"],))
                conn.execute("DELETE FROM users WHERE id=%s", (session["user_id"],))
                conn.commit()
            self.redirect("/login", clear_cookie=True)
            return

        if path == "/members/add":
            session = self.require_session(api=False)
            if not session:
                return
            if session["role"] not in ("owner", "admin"):
                self.send_error(403)
                return
            form = self.form_data()
            email = form.get("email", [""])[0].strip().lower() if form else ""
            role = form.get("role", ["member"])[0] if form else "member"
            allowed = {"member", "buyer", "seller", "viewer"}
            if session["role"] == "owner":
                allowed.add("admin")
            if role not in allowed:
                self.send_error(403)
                return
            with server.db() as conn:
                user = conn.execute("SELECT id FROM users WHERE email=%s", (email,)).fetchone()
                if not user:
                    self.send_html(404, server.members_page(session, self.memberships_for(session["user_id"]), self.members_for(session["stockroom_id"]), error="Geen geregistreerde gebruiker gevonden met dit e-mailadres."))
                    return
                existing = conn.execute("SELECT role FROM memberships WHERE user_id=%s AND stockroom_id=%s", (user["id"], session["stockroom_id"])).fetchone()
                if existing and existing["role"] == "owner":
                    self.send_error(403)
                    return
                if session["role"] == "admin" and existing and existing["role"] == "admin":
                    self.send_error(403)
                    return
                conn.execute("""INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,%s)
                                ON CONFLICT(user_id,stockroom_id) DO UPDATE SET role=EXCLUDED.role""",
                             (user["id"], session["stockroom_id"], role))
                conn.commit()
            self.send_html(200, server.members_page(session, self.memberships_for(session["user_id"]), self.members_for(session["stockroom_id"]), success=f"Gebruiker is gekoppeld als {ROLE_LABELS[role]}."))
            return

        return super().do_POST()

    def do_PUT(self):
        if not self.enforce_origin():
            return
        session = self.require_session(api=True)
        if not session:
            return
        if urlparse(self.path).path != "/api/state":
            self.send_error(404)
            return
        if session["role"] == "viewer":
            self.send_json(403, {"error": "Viewer-toegang is alleen-lezen. Wijzigingen zijn niet toegestaan."})
            return
        if session["role"] not in ("buyer", "seller"):
            return super().do_PUT()

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Ongeldige aanvraag."})
            return
        if length < 2 or length > server.MAX_BODY_BYTES:
            self.send_json(413, {"error": "Aanvraag is te groot of leeg."})
            return
        try:
            value = server.json.loads(self.rfile.read(length))
        except (server.json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Ongeldige JSON."})
            return
        if not server.valid_state(value):
            self.send_json(422, {"error": "Ongeldige voorraadgegevens."})
            return
        with server.db() as conn:
            row = conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE", (session["stockroom_id"],)).fetchone()
            old_state = row["state"] if row else server.EMPTY_STATE
            if not specialized_change_allowed(session["role"], old_state, value):
                message = "Een inkoper mag alleen inkomende transacties wijzigen." if session["role"] == "buyer" else "Een verkoper mag alleen uitgaande transacties wijzigen."
                self.send_json(403, {"error": message})
                return
            conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s", (server.json.dumps(value, ensure_ascii=False), session["stockroom_id"]))
            conn.commit()
        self.send_json(200, {"saved": True})


if __name__ == "__main__":
    if not server.DATABASE_URL:
        raise SystemExit("DATABASE_URL is verplicht en moet naar PostgreSQL wijzen.")
    server.initialize_database()
    migrate_roles()
    server.cleanup_expired()
    handler = partial(StockroomHandler, directory=str(server.PUBLIC_DIR))
    httpd = ThreadingHTTPServer((server.HOST, server.PORT), handler)
    print(f"Stockroom draait op poort {server.PORT} met rollen owner/admin/gebruiker/inkoper/verkoper/viewer")
    httpd.serve_forever()
