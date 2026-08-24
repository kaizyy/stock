from functools import partial
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

import server
import runner


class DashboardHandler(runner.StockroomHandler):
    def do_GET(self):
        path = urlparse(self.path).path

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
            roles = [
                {"value": "member", "label": "Gebruiker"},
                {"value": "buyer", "label": "Inkoper — alleen inkomend"},
                {"value": "seller", "label": "Verkoper — alleen uitgaand"},
                {"value": "viewer", "label": "Viewer — alleen lezen"},
            ]
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


if __name__ == "__main__":
    if not server.DATABASE_URL:
        raise SystemExit("DATABASE_URL is verplicht en moet naar PostgreSQL wijzen.")
    server.initialize_database()
    runner.migrate_roles()
    server.cleanup_expired()
    handler = partial(DashboardHandler, directory=str(server.PUBLIC_DIR))
    httpd = ThreadingHTTPServer((server.HOST, server.PORT), handler)
    print(f"Stockroom draait op poort {server.PORT} met geïntegreerde instellingen")
    httpd.serve_forever()
