from functools import partial
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

import server
import runner
import dashboard_runner as dashboard


def security_page(session, message="", error=""):
    feedback = f'<p class="success">{server.html.escape(message)}</p>' if message else (f'<p class="error">{server.html.escape(error)}</p>' if error else "")
    return server.auth_page("Accountbeveiliging", f"Ingelogd als {session['email']}", feedback + """
      <form method="post" action="/account/change-email">
        <label>Nieuw e-mailadres<input name="email" type="email" autocomplete="email" required></label>
        <label>Huidig wachtwoord<input name="password" type="password" autocomplete="current-password" required></label>
        <button type="submit">Verificatie naar nieuw adres sturen</button>
      </form>
      <form method="post" action="/account/logout-all">
        <button type="submit">Uitloggen op alle apparaten</button>
      </form>
      <p class="note"><a href="/">Terug naar dashboard</a></p>
    """)


def fixed_permissions_for(role):
    return {
        "manageMembers": role in ("owner", "admin"),
        "assignAdmin": role == "owner",
        "manageItems": role in ("owner", "admin", "member"),
        "incoming": role in ("owner", "admin", "member", "buyer"),
        "outgoing": role in ("owner", "admin", "member", "seller"),
        "readOnly": role == "viewer",
        "audit": role in ("owner", "admin"),
        "createStockroom": role in ("owner", "admin"),
    }


dashboard.permissions_for = fixed_permissions_for


def self_test_permissions():
    matrix = {r: fixed_permissions_for(r) for r in ("owner", "admin", "member", "buyer", "seller", "viewer")}
    assert all(matrix["owner"][k] for k in ("manageMembers", "assignAdmin", "manageItems", "incoming", "outgoing", "audit", "createStockroom"))
    assert matrix["admin"]["manageMembers"] and not matrix["admin"]["assignAdmin"]
    assert matrix["admin"]["manageItems"] and matrix["admin"]["incoming"] and matrix["admin"]["outgoing"] and matrix["admin"]["audit"] and matrix["admin"]["createStockroom"]
    assert matrix["member"]["manageItems"] and matrix["member"]["incoming"] and matrix["member"]["outgoing"] and not matrix["member"]["manageMembers"] and not matrix["member"]["createStockroom"]
    assert matrix["buyer"]["incoming"] and not matrix["buyer"]["outgoing"] and not matrix["buyer"]["manageItems"] and not matrix["buyer"]["createStockroom"]
    assert matrix["seller"]["outgoing"] and not matrix["seller"]["incoming"] and not matrix["seller"]["manageItems"] and not matrix["seller"]["createStockroom"]
    assert matrix["viewer"]["readOnly"] and not any(matrix["viewer"][k] for k in ("manageMembers", "assignAdmin", "manageItems", "incoming", "outgoing", "audit", "createStockroom"))


class AppHandler(dashboard.DashboardHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/account/security":
            session = self.require_session(api=False)
            if session:
                self.send_html(200, security_page(session))
            return
        if path in ("/", "/index.html"):
            session = self.require_session(api=False)
            if not session:
                return
            content = (server.PUBLIC_DIR / "index.html").read_text(encoding="utf-8")
            content = content.replace(
                "</body>",
                '<script src="/settings.js?v=20260825-1"></script><script src="/features.js?v=20260825-1"></script><script src="/features_optional_fix.js?v=20260825-1"></script><script src="/role_dashboard.js?v=20260825-1"></script><script src="/average_sale_price.js?v=20260825-1"></script></body>'
            )
            self.send_html(200, content)
            return
        if path == "/api/stockrooms":
            session = self.require_session(api=True)
            if not session:
                return
            self.send_json(200, {
                "active": session["stockroom_id"],
                "stockrooms": self.memberships_for(session["user_id"]),
                "canCreate": session["role"] in ("owner", "admin"),
            })
            return
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.enforce_origin():
            return

        if path == "/account/logout-all":
            session = self.require_session(api=False)
            if not session:
                return
            with server.db() as conn:
                dashboard.audit(conn, session, "account.sessions_revoked", {"scope": "all"})
                conn.execute("DELETE FROM sessions WHERE user_id=%s", (session["user_id"],))
                conn.commit()
            self.redirect("/login", clear_cookie=True)
            return

        if path == "/account/change-email":
            session = self.require_session(api=False)
            if not session:
                return
            form = self.form_data()
            new_email = form.get("email", [""])[0].strip().lower() if form else ""
            password = form.get("password", [""])[0] if form else ""
            if "@" not in new_email or not self.rate_limit("change_email", new_email, 3, 3600):
                self.send_html(400, security_page(session, error="Ongeldig e-mailadres of te veel pogingen."))
                return
            with server.db() as conn:
                user = conn.execute("SELECT password_salt,password_hash,password_version FROM users WHERE id=%s", (session["user_id"],)).fetchone()
                exists = conn.execute("SELECT 1 FROM users WHERE email=%s AND id<>%s", (new_email, session["user_id"])).fetchone()
            if exists or not user or not server.verify_password(password, user["password_salt"], user["password_hash"], user["password_version"]):
                self.send_html(403, security_page(session, error="Wijziging kon niet worden aangevraagd."))
                return
            raw = server.create_auth_token(session["user_id"], "change_email", server.EMAIL_CHANGE_TTL_SECONDS, {"new_email": new_email})
            try:
                server.send_email(new_email, "Bevestig je nieuwe Stockroom-e-mailadres", f"Bevestig je nieuwe e-mailadres via:\n{self.base_url()}/verify-email?token={raw}\n\nDeze link is 30 minuten geldig en eenmalig bruikbaar.")
            except Exception as exc:
                server.log_event(f"[EMAIL ERROR] E-mailwijziging verzenden mislukt: {type(exc).__name__}")
                self.send_html(503, security_page(session, error="De verificatiemail kon niet worden verzonden. Probeer het later opnieuw."))
                return
            with server.db() as conn:
                dashboard.audit(conn, session, "account.email_change_requested", {"newEmailHash": server.token_digest(new_email)})
                conn.commit()
            self.send_html(200, security_page(session, message="Controleer het nieuwe e-mailadres om de wijziging te bevestigen."))
            return

        if path == "/api/mobile/login":
            form = self.form_data()
            email = form.get("email", [""])[0].strip().lower() if form else ""
            password = form.get("password", [""])[0] if form else ""
            if not email or not password:
                self.send_json(400, {"error": "E-mailadres en wachtwoord zijn verplicht."})
                return
            if not self.rate_limit("mobile_login", email, 10, 900):
                self.send_json(429, {"error": "Te veel inlogpogingen. Probeer het later opnieuw."})
                return
            with server.db() as conn:
                user = conn.execute(
                    "SELECT id::text,email,name,password_salt,password_hash,password_version,email_verified_at,locked_until FROM users WHERE email=%s",
                    (email,),
                ).fetchone()
            locked = user and user["locked_until"] and user["locked_until"].timestamp() > server.time.time()
            if not user or locked or not server.verify_password(password, user["password_salt"], user["password_hash"], user["password_version"]):
                self.send_json(403, {"error": "E-mailadres of wachtwoord is onjuist."})
                return
            if not user["email_verified_at"]:
                self.send_json(403, {"error": "Verifieer eerst je e-mailadres."})
                return
            memberships = self.memberships_for(user["id"])
            if not memberships:
                self.send_json(403, {"error": "Dit account is niet gekoppeld aan een stockroom."})
                return
            room = memberships[0]
            token, expires_at = server.create_session(user["id"], room["stockroom_id"])
            self.send_json(200, {
                "token": token,
                "expiresAt": expires_at,
                "user": {"id": user["id"], "name": user["name"], "email": user["email"]},
                "stockroom": {"id": room["stockroom_id"], "name": room["stockroom_name"], "role": room["role"]},
            })
            return

        if path == "/api/mobile/logout":
            session = self.require_session(api=True)
            if not session:
                return
            token = self.cookie_token()
            if token:
                with server.db() as conn:
                    conn.execute("DELETE FROM sessions WHERE token_hash=%s", (server.token_digest(token),))
                    conn.commit()
            self.send_json(200, {"loggedOut": True})
            return

        if path == "/api/mobile/switch-stockroom":
            session = self.require_session(api=True)
            if not session:
                return
            form = self.form_data()
            stockroom_id = form.get("stockroom_id", [""])[0] if form else ""
            membership = next((m for m in self.memberships_for(session["user_id"]) if m["stockroom_id"] == stockroom_id), None)
            if not membership:
                self.send_json(403, {"error": "Je hebt geen toegang tot deze stockroom."})
                return
            token = self.cookie_token()
            with server.db() as conn:
                conn.execute(
                    "UPDATE sessions SET active_stockroom_id=%s WHERE token_hash=%s",
                    (stockroom_id, server.token_digest(token)),
                )
                conn.commit()
            self.send_json(200, {"switched": True, "stockroom": membership})
            return

        if path == "/api/stockrooms/create":
            session = self.require_session(api=True)
            if not session:
                return
            if session["role"] not in ("owner", "admin"):
                self.send_json(403, {"error": "Alleen Owner of Admin kan een nieuwe stockroom aanmaken."})
                return
        return super().do_POST()


if __name__ == "__main__":
    if not server.DATABASE_URL:
        raise SystemExit("DATABASE_URL is verplicht en moet naar PostgreSQL wijzen.")
    server.initialize_database()
    runner.migrate_roles()
    dashboard.initialize_enhancements()
    self_test_permissions()
    server.cleanup_expired()
    handler = partial(AppHandler, directory=str(server.PUBLIC_DIR))
    httpd = ThreadingHTTPServer((server.HOST, server.PORT), handler)
    print("Stockroom draait met gecontroleerde rechtenmatrix, rolbewust dashboard en mobiele API", flush=True)
    httpd.serve_forever()
