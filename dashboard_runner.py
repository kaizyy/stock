from functools import partial
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

import server
import runner


ROLE_OPTIONS = [
    {"value": "member", "label": "Gebruiker"},
    {"value": "buyer", "label": "Inkoper — alleen inkomend"},
    {"value": "seller", "label": "Verkoper — alleen uitgaand"},
    {"value": "viewer", "label": "Viewer — alleen lezen"},
]


class DashboardHandler(runner.StockroomHandler):
    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            session = self.require_session(api=False)
            if not session:
                return
            content = (server.PUBLIC_DIR / "index.html").read_text(encoding="utf-8")
            content = content.replace("</body>", '<script src="/settings.js"></script></body>')
            self.send_html(200, content)
            return

        if path in ("/members", "/account"):
            session = self.require_session(api=False)
            if not session:
                return
            self.redirect("/#settings")
            return

        if path == "/api/members":
            session = self.require_session(api=True)
            if not session:
                return
            members = self.members_for(session["stockroom_id"])
            can_manage = session["role"] in ("owner", "admin")
            roles = list(ROLE_OPTIONS)
            if session["role"] == "owner":
                roles.append({"value": "admin", "label": "Admin"})
            self.send_json(200, {
                "stockroom": {"id": session["stockroom_id"], "name": session["stockroom_name"], "role": session["role"]},
                "user": {"id": session["user_id"], "name": session["user_name"], "email": session["email"]},
                "members": members,
                "canManage": can_manage,
                "roles": roles,
            })
            return

        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/members/role":
            return super().do_POST()

        session = self.require_session(api=True)
        if not session:
            return
        if session["role"] not in ("owner", "admin"):
            self.send_json(403, {"error": "Geen rechten om rollen te wijzigen."})
            return

        form = self.form_data()
        target_user = form.get("user_id", [""])[0] if form else ""
        new_role = form.get("role", [""])[0] if form else ""
        allowed = {"member", "buyer", "seller", "viewer"}
        if session["role"] == "owner":
            allowed.add("admin")
        if not target_user or new_role not in allowed or target_user == session["user_id"]:
            self.send_json(403, {"error": "Deze rolwijziging is niet toegestaan."})
            return

        with server.db() as conn:
            target = conn.execute(
                "SELECT role FROM memberships WHERE user_id=%s AND stockroom_id=%s FOR UPDATE",
                (target_user, session["stockroom_id"]),
            ).fetchone()
            if not target or target["role"] == "owner":
                self.send_json(403, {"error": "De owner-rol kan hier niet worden gewijzigd."})
                return
            if session["role"] == "admin" and target["role"] == "admin":
                self.send_json(403, {"error": "Een admin kan geen andere admin wijzigen."})
                return
            conn.execute(
                "UPDATE memberships SET role=%s WHERE user_id=%s AND stockroom_id=%s",
                (new_role, target_user, session["stockroom_id"]),
            )
            conn.commit()

        self.send_json(200, {"saved": True, "role": new_role})


if __name__ == "__main__":
    if not server.DATABASE_URL:
        raise SystemExit("DATABASE_URL is verplicht en moet naar PostgreSQL wijzen.")
    server.initialize_database()
    runner.migrate_roles()
    server.cleanup_expired()
    handler = partial(DashboardHandler, directory=str(server.PUBLIC_DIR))
    httpd = ThreadingHTTPServer((server.HOST, server.PORT), handler)
    print(f"Stockroom draait op poort {server.PORT} met rolbewuste UI en geïntegreerde instellingen")
    httpd.serve_forever()
