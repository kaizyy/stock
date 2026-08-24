from functools import partial
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

import server

ORIGINAL_MEMBERS_PAGE = server.members_page


def migrate_viewer_role():
    with server.db() as conn:
        rows = conn.execute("""
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'memberships'::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%role%'
        """).fetchall()
        for row in rows:
            conn.execute(f'ALTER TABLE memberships DROP CONSTRAINT "{row["conname"]}"')
        conn.execute("ALTER TABLE memberships ADD CONSTRAINT memberships_role_check CHECK (role IN ('owner','admin','member','viewer'))")
        conn.commit()


def account_page(session, error=""):
    owned = []
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
<form method="post" action="/account/delete">
<label>Wachtwoord<input name="password" type="password" autocomplete="current-password" required></label>
<label>Typ VERWIJDEREN ter bevestiging<input name="confirm" required autocomplete="off"></label>
<button type="submit" style="background:#991b1b">Account permanent verwijderen</button></form><a class="cancel" href="/">Annuleren en terug naar dashboard</a></main></body></html>"""


def members_page(session, memberships, members, error="", success=""):
    original = ORIGINAL_MEMBERS_PAGE(session, memberships, members, error, success)
    if session["role"] in ("owner", "admin"):
        marker = '<option value="member">Gebruiker</option>'
        original = original.replace(marker, marker + '<option value="viewer">Viewer (alleen lezen)</option>')
    account_link = '<p style="margin-top:24px"><a href="/account">Mijn account</a></p>'
    return original.replace('</main></body></html>', account_link + '</main></body></html>')


server.members_page = members_page


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
                user = conn.execute("SELECT password_salt,password_hash FROM users WHERE id=%s", (session["user_id"],)).fetchone()
                if not user or not server.verify_password(password, user["password_salt"], user["password_hash"]):
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
            role = form.get("role", ["member"])[0] if form else "member"
            if role != "viewer":
                return super().do_POST()
            email = form.get("email", [""])[0].strip().lower() if form else ""
            with server.db() as conn:
                user = conn.execute("SELECT id FROM users WHERE email=%s", (email,)).fetchone()
                if not user:
                    self.send_html(404, server.members_page(session, self.memberships_for(session["user_id"]), self.members_for(session["stockroom_id"]), error="Geen geregistreerde gebruiker gevonden met dit e-mailadres."))
                    return
                conn.execute("""INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'viewer')
                                ON CONFLICT(user_id,stockroom_id) DO UPDATE SET role='viewer'""",
                             (user["id"], session["stockroom_id"]))
                conn.commit()
            self.send_html(200, server.members_page(session, self.memberships_for(session["user_id"]), self.members_for(session["stockroom_id"]), success="Viewer is gekoppeld aan deze stockroom met alleen-lezen toegang."))
            return
        return super().do_POST()

    def do_PUT(self):
        session = self.require_session(api=True)
        if not session:
            return
        if session["role"] == "viewer":
            self.send_json(403, {"error": "Viewer-toegang is alleen-lezen. Wijzigingen zijn niet toegestaan."})
            return
        return super().do_PUT()


if __name__ == "__main__":
    if not server.DATABASE_URL:
        raise SystemExit("DATABASE_URL is verplicht en moet naar PostgreSQL wijzen.")
    server.initialize_database()
    migrate_viewer_role()
    server.cleanup_expired()
    handler = partial(StockroomHandler, directory=str(server.PUBLIC_DIR))
    httpd = ThreadingHTTPServer((server.HOST, server.PORT), handler)
    print(f"Stockroom draait op poort {server.PORT} met viewer read-only rol en accountverwijdering")
    httpd.serve_forever()
