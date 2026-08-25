from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import time
from urllib.parse import parse_qs, urlparse

import server
import runner
import dashboard_runner as dashboard
import app_runner
import order_management as orders
import order_delete
import warehouse_ops as warehouse
import business_tools


def flat_form(handler):
    form = handler.form_data() or {}
    return {key: (value[0] if isinstance(value, list) and value else value) for key, value in form.items()}


class ExtendedHandler(app_runner.AppHandler):
    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        if self.command == "GET" and urlparse(self.path).path in ("/", "/index.html") and self.session:
            remaining = max(1, int(self.session["expires_at"] - time.time()))
            self.send_header("Refresh", f"{remaining}; url=/logout")
            self.send_header("Cache-Control", "no-store")
        SimpleHTTPRequestHandler.end_headers(self)

    def send_pdf(self, data, filename):
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            session = self.require_session(api=False)
            if not session:
                return
            content = (server.PUBLIC_DIR / "index.html").read_text(encoding="utf-8")
            content = content.replace(
                "</body>",
                '<script src="/settings.js?v=20260825-10"></script><script src="/features.js?v=20260825-10"></script><script src="/features_optional_fix.js?v=20260825-10"></script><script src="/role_dashboard.js?v=20260825-10"></script><script src="/analytics_dashboard.js?v=20260825-10"></script><script src="/inventory_intelligence.js?v=20260825-10"></script><script src="/barcode_scanner_fallback.js?v=20260825-10"></script><script src="/dynamic_navigation.js?v=20260825-10"></script><script src="/crm_orders.js?v=20260825-10"></script><script src="/order_delete_ui.js?v=20260825-10"></script><script src="/warehouse_ops.js?v=20260825-10"></script><script src="/business_tools.js?v=20260825-10"></script></body>'
            )
            self.send_html(200, content)
            return

        if path in ("/api/suppliers", "/api/customers"):
            session = self.require_session(api=True)
            if not session:
                return
            kind = "supplier" if path.endswith("suppliers") else "customer"
            capability = "read_suppliers" if kind == "supplier" else "read_customers"
            if not orders.allowed(session["role"], capability):
                self.send_json(403, {"error": "Geen rechten."})
                return
            self.send_json(200, {"items": orders.relation_rows(session["stockroom_id"], kind)})
            return

        if path == "/api/orders":
            session = self.require_session(api=True)
            if not session:
                return
            query = parse_qs(parsed.query)
            order_type = query.get("type", ["purchase"])[0]
            if order_type not in ("purchase", "sales"):
                self.send_json(400, {"error": "Ongeldig ordertype."})
                return
            capability = "read_purchase" if order_type == "purchase" else "read_sales"
            if not orders.allowed(session["role"], capability):
                self.send_json(403, {"error": "Geen rechten."})
                return
            rows = business_tools.enrich_orders(session["stockroom_id"], orders.order_rows(session["stockroom_id"], order_type))
            self.send_json(200, {"orders": rows})
            return

        if path == "/api/search":
            session = self.require_session(api=True)
            if not session:
                return
            query = parse_qs(parsed.query).get("q", [""])[0]
            self.send_json(200, {"results": business_tools.search_all(session["stockroom_id"], query)})
            return

        if path == "/api/documents/order.pdf":
            session = self.require_session(api=True)
            if not session:
                return
            order_id = parse_qs(parsed.query).get("id", [""])[0]
            try:
                data, filename = business_tools.order_pdf(session["stockroom_id"], order_id)
                self.send_pdf(data, filename)
            except PermissionError as exc:
                self.send_json(404, {"error": str(exc)})
            return

        if path == "/api/documents/inventory.pdf":
            session = self.require_session(api=True)
            if not session:
                return
            data, filename = business_tools.inventory_pdf(session["stockroom_id"])
            self.send_pdf(data, filename)
            return

        if path == "/api/warehouse":
            session = self.require_session(api=True)
            if not session:
                return
            perms = warehouse.permissions(session["role"])
            if not perms["read"]:
                self.send_json(403, {"error": "Geen rechten."})
                return
            self.send_json(200, {
                "warehousePermissions": perms,
                "targets": warehouse.transfer_targets(session["user_id"], session["stockroom_id"]) if perms["transfer"] else [],
                "history": warehouse.history(session["stockroom_id"]),
            })
            return

        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        handled = {
            "/api/suppliers", "/api/customers", "/api/orders", "/api/orders/status", "/api/orders/delete",
            "/api/warehouse/count", "/api/warehouse/return", "/api/warehouse/transfer",
        }
        if path in handled:
            if not self.enforce_origin():
                return
            session = self.require_session(api=True)
            if not session:
                return
            values = flat_form(self)
            try:
                if path in ("/api/suppliers", "/api/customers"):
                    kind = "supplier" if path.endswith("suppliers") else "customer"
                    capability = "write_suppliers" if kind == "supplier" else "write_customers"
                    if not orders.allowed(session["role"], capability):
                        self.send_json(403, {"error": "Geen rechten."})
                        return
                    relation_id = orders.save_relation(session, kind, values)
                    self.send_json(200, {"saved": True, "id": relation_id})
                    return

                if path == "/api/orders":
                    order_type = values.get("order_type")
                    capability = "write_purchase" if order_type == "purchase" else "write_sales"
                    if order_type not in ("purchase", "sales") or not orders.allowed(session["role"], capability):
                        self.send_json(403, {"error": "Geen rechten voor dit ordertype."})
                        return
                    order_id = orders.create_order(session, values)
                    order_number = business_tools.assign_order_number(order_id, session["stockroom_id"], order_type)
                    self.send_json(200, {"created": True, "id": order_id, "order_number": order_number})
                    return

                if path == "/api/orders/status":
                    order_type = values.get("order_type")
                    capability = "write_purchase" if order_type == "purchase" else "write_sales"
                    if order_type not in ("purchase", "sales") or not orders.allowed(session["role"], capability):
                        self.send_json(403, {"error": "Geen rechten voor dit ordertype."})
                        return
                    orders.update_order_status(session, order_type, values)
                    self.send_json(200, {"updated": True})
                    return

                if path == "/api/orders/delete":
                    order_type = values.get("order_type")
                    capability = "write_purchase" if order_type == "purchase" else "write_sales"
                    if order_type not in ("purchase", "sales") or not orders.allowed(session["role"], capability):
                        self.send_json(403, {"error": "Geen rechten om deze order te verwijderen."})
                        return
                    self.send_json(200, order_delete.delete_order(session, order_type, values))
                    return

                if path == "/api/warehouse/count":
                    self.send_json(200, {"updated": True, **warehouse.apply_count(session, values)})
                    return
                if path == "/api/warehouse/return":
                    kind = values.get("return_type")
                    if kind not in ("sales", "purchase"):
                        raise ValueError("Retourtype is ongeldig.")
                    self.send_json(200, {"updated": True, **warehouse.apply_return(session, values, kind)})
                    return
                if path == "/api/warehouse/transfer":
                    self.send_json(200, {"updated": True, **warehouse.apply_transfer(session, values)})
                    return
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            except PermissionError as exc:
                self.send_json(403, {"error": str(exc)})
                return
        return super().do_POST()


if __name__ == "__main__":
    if not server.DATABASE_URL:
        raise SystemExit("DATABASE_URL is verplicht en moet naar PostgreSQL wijzen.")
    server.initialize_database()
    runner.migrate_roles()
    dashboard.initialize_enhancements()
    orders.initialize_order_management()
    business_tools.initialize_business_tools()
    warehouse.initialize_warehouse_ops()
    app_runner.self_test_permissions()
    server.cleanup_expired()
    handler = partial(ExtendedHandler, directory=str(server.PUBLIC_DIR))
    httpd = ThreadingHTTPServer((server.HOST, server.PORT), handler)
    print("Stockroom draait met orderbeheer, magazijnprocessen, zoekfunctie en PDF-documenten", flush=True)
    httpd.serve_forever()
