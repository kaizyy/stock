from functools import partial
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

import server
import runner
import dashboard_runner as dashboard


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
        if path in ("/", "/index.html"):
            session = self.require_session(api=False)
            if not session:
                return
            content = (server.PUBLIC_DIR / "index.html").read_text(encoding="utf-8")
            content = content.replace(
                "</body>",
                '<script src="/settings.js?v=20260824-4"></script><script src="/features.js?v=20260824-4"></script><script src="/features_optional_fix.js?v=20260824-4"></script><script src="/role_dashboard.js?v=20260824-4"></script></body>'
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

        if path == "/api/mobile/login":
            form = self.form_data()
            email = form.get("email", [""])[0].strip().lower() if form else ""
            password = form.get("password", [""])[0] if form else ""
            if not email or not password:
                self.send_json(400, {"error": "E-mailadres en wachtwoord zijn verplicht."})
                return
            with server.db() as conn:
                user = conn.execute(
                    "SELECT id::text,email,name,password_salt,password_hash,email_verified_at FROM users WHERE email=%s",
                    (email,),
                ).fetchone()
            if not user or not server.verify_password(password, user["password_salt"], user["password_hash"]):
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
