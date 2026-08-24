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
    print("Stockroom draait met gecontroleerde rechtenmatrix en rolbewust dashboard", flush=True)
    httpd.serve_forever()
