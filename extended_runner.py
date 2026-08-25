from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
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
import platform_admin
import billing


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

    def require_session(self, api=False):
        session = super().require_session(api=api)
        if not session: return None
        allowed, message = platform_admin.enforce_access(session)
        if allowed: return session
        token = self.cookie_token()
        if token:
            with server.db() as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash=%s", (server.token_digest(token),)); conn.commit()
        if api: self.send_json(403, {"error": message})
        else: self.send_html(403, server.result_page("Toegang geblokkeerd", message, "/login", "Naar inloggen"))
        return None

    def require_platform_admin(self):
        session=self.require_session(api=True)
        if not session:return None
        if not platform_admin.is_platform_admin(session): self.send_json(403,{"error":"Alleen platformbeheer heeft toegang tot deze functie."});return None
        return session

    def send_pdf(self,data,filename):
        self.send_response(200);self.send_header("Content-Type","application/pdf");self.send_header("Content-Disposition",f'inline; filename="{filename}"');self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)

    def do_GET(self):
        parsed=urlparse(self.path);path=parsed.path
        if path in ("/", "/index.html"):
            session=self.require_session(api=False)
            if not session:return
            content=(server.PUBLIC_DIR/"index.html").read_text(encoding="utf-8")
            content=content.replace("</body>",'<script src="/settings.js?v=20260825-12"></script><script src="/features.js?v=20260825-12"></script><script src="/features_optional_fix.js?v=20260825-12"></script><script src="/role_dashboard.js?v=20260825-12"></script><script src="/analytics_dashboard.js?v=20260825-12"></script><script src="/inventory_intelligence.js?v=20260825-12"></script><script src="/barcode_scanner_fallback.js?v=20260825-12"></script><script src="/dynamic_navigation.js?v=20260825-12"></script><script src="/crm_orders.js?v=20260825-12"></script><script src="/order_delete_ui.js?v=20260825-12"></script><script src="/warehouse_ops.js?v=20260825-12"></script><script src="/business_tools.js?v=20260825-12"></script><script src="/platform_admin_ui.js?v=20260825-12"></script><script src="/billing_ui.js?v=20260825-12"></script></body>')
            self.send_html(200,content);return
        if path=="/api/billing":
            s=self.require_session(api=True)
            if s:self.send_json(200,billing.account(s['stockroom_id']))
            return
        if path=="/api/platform-admin/status":
            s=self.require_session(api=True)
            if s:self.send_json(200,{"platformAdmin":platform_admin.is_platform_admin(s)})
            return
        if path=="/api/platform-admin":
            if not self.require_platform_admin():return
            data=platform_admin.platform_overview();data['billing']=billing.platform_metrics();self.send_json(200,data);return
        if path=="/api/notifications":
            s=self.require_session(api=True)
            if s:self.send_json(200,{"notifications":platform_admin.stockroom_notifications(s['stockroom_id'])})
            return
        if path in ("/api/suppliers","/api/customers"):
            s=self.require_session(api=True)
            if not s:return
            kind="supplier" if path.endswith("suppliers") else "customer";cap="read_suppliers" if kind=="supplier" else "read_customers"
            if not orders.allowed(s['role'],cap):self.send_json(403,{"error":"Geen rechten."});return
            self.send_json(200,{"items":orders.relation_rows(s['stockroom_id'],kind)});return
        if path=="/api/orders":
            s=self.require_session(api=True)
            if not s:return
            ot=parse_qs(parsed.query).get("type",["purchase"])[0]
            if ot not in ("purchase","sales"):self.send_json(400,{"error":"Ongeldig ordertype."});return
            cap="read_purchase" if ot=="purchase" else "read_sales"
            if not orders.allowed(s['role'],cap):self.send_json(403,{"error":"Geen rechten."});return
            self.send_json(200,{"orders":business_tools.enrich_orders(s['stockroom_id'],orders.order_rows(s['stockroom_id'],ot))});return
        if path=="/api/search":
            s=self.require_session(api=True)
            if s:self.send_json(200,{"results":business_tools.search_all(s['stockroom_id'],parse_qs(parsed.query).get('q',[''])[0])})
            return
        if path=="/api/documents/order.pdf":
            s=self.require_session(api=True)
            if not s:return
            try:data,name=business_tools.order_pdf(s['stockroom_id'],parse_qs(parsed.query).get('id',[''])[0]);self.send_pdf(data,name)
            except PermissionError as e:self.send_json(404,{"error":str(e)})
            except Exception as e:platform_admin.record_error('order_pdf',type(e).__name__,s['stockroom_id'],s['user_id']);self.send_json(500,{"error":"PDF kon niet worden gegenereerd."})
            return
        if path=="/api/documents/inventory.pdf":
            s=self.require_session(api=True)
            if not s:return
            try:data,name=business_tools.inventory_pdf(s['stockroom_id']);self.send_pdf(data,name)
            except Exception as e:platform_admin.record_error('inventory_pdf',type(e).__name__,s['stockroom_id'],s['user_id']);self.send_json(500,{"error":"PDF kon niet worden gegenereerd."})
            return
        if path=="/api/warehouse":
            s=self.require_session(api=True)
            if not s:return
            p=warehouse.permissions(s['role'])
            if not p['read']:self.send_json(403,{"error":"Geen rechten."});return
            self.send_json(200,{"warehousePermissions":p,"targets":warehouse.transfer_targets(s['user_id'],s['stockroom_id']) if p['transfer'] else [],"history":warehouse.history(s['stockroom_id'])});return
        return super().do_GET()

    def do_POST(self):
        path=urlparse(self.path).path
        # Stripe webhook is intentionally outside browser origin/session auth.
        if path=="/api/billing/webhook":
            try:
                length=int(self.headers.get('Content-Length','0'));raw=self.rfile.read(length);event=json.loads(raw or b'{}');billing.apply_webhook(event);self.send_json(200,{"received":True})
            except Exception as e:platform_admin.record_error('stripe_webhook',type(e).__name__);self.send_json(400,{"error":"Webhook ongeldig."})
            return
        handled={"/api/suppliers","/api/customers","/api/orders","/api/orders/status","/api/orders/delete","/api/warehouse/count","/api/warehouse/return","/api/warehouse/transfer","/api/platform-admin/suspension","/api/billing/profile","/api/billing/checkout","/api/billing/portal"}
        if path in handled:
            if not self.enforce_origin():return
            s=self.require_platform_admin() if path.startswith('/api/platform-admin/') else self.require_session(api=True)
            if not s:return
            values=flat_form(self)
            try:
                if path=="/api/billing/profile":self.send_json(200,billing.save_profile(s['stockroom_id'],values));return
                if path=="/api/billing/checkout":
                    base=self.base_url();self.send_json(200,billing.checkout(s['stockroom_id'],values.get('plan',''),base+'/?billing=success',base+'/?billing=cancel'));return
                if path=="/api/billing/portal":self.send_json(200,billing.portal(s['stockroom_id'],self.base_url()+'/'));return
                if path=="/api/platform-admin/suspension":self.send_json(200,platform_admin.set_suspension(s,values.get('target_type') or '',values.get('target_id') or '',str(values.get('suspended') or '0')=='1',values.get('reason') or ''));return
                if path in ("/api/suppliers","/api/customers"):
                    kind="supplier" if path.endswith('suppliers') else 'customer';cap="write_suppliers" if kind=='supplier' else 'write_customers'
                    if not orders.allowed(s['role'],cap):self.send_json(403,{"error":"Geen rechten."});return
                    self.send_json(200,{"saved":True,"id":orders.save_relation(s,kind,values)});return
                if path=="/api/orders":
                    ot=values.get('order_type');cap='write_purchase' if ot=='purchase' else 'write_sales'
                    if ot not in ('purchase','sales') or not orders.allowed(s['role'],cap):self.send_json(403,{"error":"Geen rechten voor dit ordertype."});return
                    oid=orders.create_order(s,values);num=business_tools.assign_order_number(oid,s['stockroom_id'],ot);self.send_json(200,{"created":True,"id":oid,"order_number":num});return
                if path=="/api/orders/status":
                    ot=values.get('order_type');cap='write_purchase' if ot=='purchase' else 'write_sales'
                    if ot not in ('purchase','sales') or not orders.allowed(s['role'],cap):self.send_json(403,{"error":"Geen rechten voor dit ordertype."});return
                    orders.update_order_status(s,ot,values);self.send_json(200,{"updated":True});return
                if path=="/api/orders/delete":
                    ot=values.get('order_type');cap='write_purchase' if ot=='purchase' else 'write_sales'
                    if ot not in ('purchase','sales') or not orders.allowed(s['role'],cap):self.send_json(403,{"error":"Geen rechten om deze order te verwijderen."});return
                    self.send_json(200,order_delete.delete_order(s,ot,values));return
                if path=="/api/warehouse/count":self.send_json(200,{"updated":True,**warehouse.apply_count(s,values)});return
                if path=="/api/warehouse/return":
                    kind=values.get('return_type')
                    if kind not in ('sales','purchase'):raise ValueError('Retourtype is ongeldig.')
                    self.send_json(200,{"updated":True,**warehouse.apply_return(s,values,kind)});return
                if path=="/api/warehouse/transfer":self.send_json(200,{"updated":True,**warehouse.apply_transfer(s,values)});return
            except ValueError as e:self.send_json(400,{"error":str(e)});return
            except PermissionError as e:self.send_json(403,{"error":str(e)});return
            except Exception as e:platform_admin.record_error(path,type(e).__name__,s.get('stockroom_id'),s.get('user_id'),{'path':path});self.send_json(500,{"error":"Onverwachte serverfout. De fout is geregistreerd."});return
        return super().do_POST()


if __name__=="__main__":
    if not server.DATABASE_URL:raise SystemExit("DATABASE_URL is verplicht en moet naar PostgreSQL wijzen.")
    server.initialize_database();runner.migrate_roles();dashboard.initialize_enhancements();orders.initialize_order_management();business_tools.initialize_business_tools();warehouse.initialize_warehouse_ops();platform_admin.initialize_platform_admin();billing.initialize_billing();app_runner.self_test_permissions();server.cleanup_expired()
    handler=partial(ExtendedHandler,directory=str(server.PUBLIC_DIR));httpd=ThreadingHTTPServer((server.HOST,server.PORT),handler);print("Stockroom draait met SaaS-abonnementen, Stripe, platformbeheer en magazijnprocessen",flush=True);httpd.serve_forever()
