import html
import json
import secrets
import time
import uuid
from functools import partial
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import server
import runner

ROLE_OPTIONS = [
    {"value": "member", "label": "Gebruiker"},
    {"value": "buyer", "label": "Inkoper — alleen inkomend"},
    {"value": "seller", "label": "Verkoper — alleen uitgaand"},
    {"value": "viewer", "label": "Viewer — alleen lezen"},
]


def initialize_enhancements():
    with server.db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invitations (
                id UUID PRIMARY KEY,
                stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
                email TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin','member','buyer','seller','viewer')),
                token_hash TEXT NOT NULL UNIQUE,
                invited_by UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TIMESTAMPTZ NOT NULL,
                accepted_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invitations_stockroom ON invitations(stockroom_id,created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invitations_email ON invitations(email)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id BIGSERIAL PRIMARY KEY,
                stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
                user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                action TEXT NOT NULL,
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_stockroom_created ON audit_log(stockroom_id,created_at DESC)")
        conn.commit()


def audit(conn, session, action, details=None, stockroom_id=None, user_id=None):
    room_id = stockroom_id or (session and session.get("stockroom_id"))
    actor = user_id or (session and session.get("user_id"))
    if not room_id:
        return
    conn.execute(
        "INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",
        (room_id, actor, action, json.dumps(details or {}, ensure_ascii=False)),
    )


def role_allowed(actor_role, target_role):
    allowed = {"member", "buyer", "seller", "viewer"}
    if actor_role == "owner":
        allowed.add("admin")
    return target_role in allowed


def permissions_for(role):
    return {
        "manageMembers": role in ("owner", "admin"),
        "assignAdmin": role == "owner",
        "manageItems": role in ("owner", "admin", "member"),
        "incoming": role in ("owner", "admin", "member", "buyer"),
        "outgoing": role in ("owner", "admin", "member", "seller"),
        "readOnly": role == "viewer",
        "audit": role in ("owner", "admin"),
        "createStockroom": role != "viewer",
    }


def self_test_permissions():
    matrix = {r: permissions_for(r) for r in ("owner", "admin", "member", "buyer", "seller", "viewer")}
    assert matrix["owner"]["assignAdmin"]
    assert not matrix["admin"]["assignAdmin"]
    assert matrix["buyer"]["incoming"] and not matrix["buyer"]["outgoing"]
    assert matrix["seller"]["outgoing"] and not matrix["seller"]["incoming"]
    assert matrix["viewer"]["readOnly"] and not matrix["viewer"]["manageItems"]
    assert matrix["member"]["incoming"] and matrix["member"]["outgoing"]


def item_map(state):
    return {str(i.get("id")): i for i in state.get("items", [])}


def tx_by_type(state, tx_type):
    return sorted((t for t in state.get("transactions", []) if t.get("type") == tx_type), key=lambda t: str(t.get("id", "")))


def specialized_allowed(role, old_state, new_state):
    old_items, new_items = item_map(old_state), item_map(new_state)
    if set(old_items) != set(new_items):
        return False
    if role == "buyer":
        if tx_by_type(old_state, "outgoing") != tx_by_type(new_state, "outgoing"):
            return False
        frozen = ("id", "name", "sku", "archived", "category", "supplier", "minStock")
        for item_id, old in old_items.items():
            new = new_items[item_id]
            if any(old.get(k) != new.get(k) for k in frozen):
                return False
        return True
    if role == "seller":
        if tx_by_type(old_state, "incoming") != tx_by_type(new_state, "incoming"):
            return False
        frozen = ("id", "name", "sku", "buy", "sell", "archived", "category", "supplier", "minStock")
        for item_id, old in old_items.items():
            new = new_items[item_id]
            if any(old.get(k) != new.get(k) for k in frozen):
                return False
        return True
    return False


def state_change_summary(old_state, new_state):
    old_items, new_items = item_map(old_state), item_map(new_state)
    changed_items = []
    for item_id in set(old_items) | set(new_items):
        old, new = old_items.get(item_id), new_items.get(item_id)
        if old != new:
            changed_items.append((new or old or {}).get("name", item_id))
    old_tx = {str(t.get("id")): t for t in old_state.get("transactions", [])}
    new_tx = {str(t.get("id")): t for t in new_state.get("transactions", [])}
    changed_tx = sum(1 for key in set(old_tx) | set(new_tx) if old_tx.get(key) != new_tx.get(key))
    return {"items": changed_items[:20], "transactionChanges": changed_tx}


def low_stock_transitions(old_state, new_state):
    old_items = item_map(old_state)
    transitions = []
    for item_id, item in item_map(new_state).items():
        minimum = max(0, int(item.get("minStock") or 0))
        stock = int(item.get("stock") or 0)
        if minimum <= 0 or stock > minimum:
            continue
        old = old_items.get(item_id)
        old_minimum = max(0, int((old or {}).get("minStock") or 0))
        old_stock = int((old or {}).get("stock") or 0)
        was_low = old is not None and old_minimum > 0 and old_stock <= old_minimum
        if not was_low:
            transitions.append({"id": item_id, "name": str(item.get("name") or item_id), "stock": stock, "minimum": minimum})
    return transitions


def notify_low_stock(stockroom_id, stockroom_name, items):
    if not items:
        return
    with server.db() as conn:
        recipients = conn.execute("""
            SELECT DISTINCT u.email FROM users u
            JOIN memberships m ON m.user_id=u.id
            WHERE m.stockroom_id=%s AND m.role IN ('owner','admin','buyer')
              AND u.email_verified_at IS NOT NULL
            ORDER BY u.email
        """, (stockroom_id,)).fetchall()
    lines = "\n".join(f"- {item['name']}: voorraad {item['stock']}, minimum {item['minimum']}" for item in items)
    sent, failed = 0, 0
    for recipient in recipients:
        email = recipient["email"].decode() if isinstance(recipient["email"], bytes) else recipient["email"]
        try:
            server.send_email(
                email,
                f"Lage voorraad in {stockroom_name}",
                f"De volgende voorraad heeft het ingestelde minimum bereikt:\n\n{lines}\n\nOpen Stockroom om de voorraad te controleren.",
            )
            sent += 1
        except Exception as exc:
            failed += 1
            server.log_event(f"[EMAIL ERROR] Lage-voorraadmelding mislukt: {type(exc).__name__}")
    with server.db() as conn:
        audit(conn, None, "inventory.low_stock_notified", {"items": items, "sent": sent, "failed": failed}, stockroom_id)
        conn.commit()


def safely_notify_low_stock(stockroom_id, stockroom_name, items):
    try:
        notify_low_stock(stockroom_id, stockroom_name, items)
    except Exception as exc:
        server.log_event(f"[LOW STOCK ERROR] Melding kon niet worden verwerkt: {type(exc).__name__}")


def invite_page(token, invitation, error=""):
    feedback = f'<p class="error">{html.escape(error)}</p>' if error else ""
    email = html.escape(invitation["email"])
    room = html.escape(invitation["stockroom_name"])
    safe_token = html.escape(token, quote=True)
    return server.auth_page(
        "Uitnodiging voor Stockroom",
        f"Je bent uitgenodigd voor {invitation['stockroom_name']} als {runner.ROLE_LABELS.get(invitation['role'], invitation['role'])}.",
        feedback + f"""
        <form method="post" action="/invite/login">
          <input type="hidden" name="token" value="{safe_token}">
          <label>E-mailadres<input name="email" type="email" value="{email}" readonly></label>
          <label>Wachtwoord<input name="password" type="password" autocomplete="current-password" required></label>
          <button type="submit">Inloggen en uitnodiging accepteren</button>
        </form>
        <p class="note">Nog geen account?</p>
        <form method="post" action="/invite/register">
          <input type="hidden" name="token" value="{safe_token}">
          <label>Naam<input name="name" autocomplete="name" required></label>
          <label>E-mailadres<input name="email" type="email" value="{email}" readonly></label>
          <label>Wachtwoord<input name="password" type="password" minlength="8" autocomplete="new-password" required></label>
          <button type="submit">Account maken en uitnodiging accepteren</button>
        </form>
        <p class="note">Deze uitnodiging koppelt je alleen aan <strong>{room}</strong>; er wordt geen aparte stockroom aangemaakt.</p>
        """,
    )


class DashboardHandler(runner.StockroomHandler):
    def invitation_by_token(self, token, lock=False):
        sql = """
            SELECT i.id::text,i.stockroom_id::text,i.email,i.role,i.expires_at,i.accepted_at,r.name stockroom_name
            FROM invitations i JOIN stockrooms r ON r.id=i.stockroom_id
            WHERE i.token_hash=%s AND i.expires_at>NOW() AND i.accepted_at IS NULL
        """ + (" FOR UPDATE" if lock else "")
        with server.db() as conn:
            return conn.execute(sql, (server.token_digest(token),)).fetchone()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            session = self.require_session(api=False)
            if not session:
                return
            content = (server.PUBLIC_DIR / "index.html").read_text(encoding="utf-8")
            content = content.replace("</body>", '<script src="/settings.js"></script><script src="/features.js"></script></body>')
            self.send_html(200, content)
            return

        if path in ("/members", "/account"):
            session = self.require_session(api=False)
            if not session:
                return
            self.redirect("/#settings")
            return

        if path == "/invite":
            token = query.get("token", [""])[0]
            invitation = self.invitation_by_token(token) if token else None
            if not invitation:
                self.send_html(400, server.result_page("Uitnodiging ongeldig", "Deze uitnodiging is verlopen, al gebruikt of ongeldig."))
                return
            self.send_html(200, invite_page(token, invitation))
            return

        if path == "/api/me":
            session = self.require_session(api=True)
            if not session:
                return
            self.send_json(200, {"user": {"id": session["user_id"], "name": session["user_name"], "email": session["email"]}, "stockroom": {"id": session["stockroom_id"], "name": session["stockroom_name"], "role": session["role"]}, "permissions": permissions_for(session["role"])})
            return

        if path == "/api/members":
            session = self.require_session(api=True)
            if not session:
                return
            members = self.members_for(session["stockroom_id"])
            roles = list(ROLE_OPTIONS)
            if session["role"] == "owner":
                roles.append({"value": "admin", "label": "Admin"})
            self.send_json(200, {"stockroom": {"id": session["stockroom_id"], "name": session["stockroom_name"], "role": session["role"]}, "user": {"id": session["user_id"], "name": session["user_name"], "email": session["email"]}, "members": members, "canManage": session["role"] in ("owner", "admin"), "roles": roles})
            return

        if path == "/api/stockrooms":
            session = self.require_session(api=True)
            if not session:
                return
            self.send_json(200, {"active": session["stockroom_id"], "stockrooms": self.memberships_for(session["user_id"]), "canCreate": session["role"] != "viewer"})
            return

        if path == "/api/invitations":
            session = self.require_session(api=True)
            if not session:
                return
            if session["role"] not in ("owner", "admin"):
                self.send_json(403, {"error": "Geen rechten."})
                return
            with server.db() as conn:
                rows = conn.execute("""
                    SELECT id::text,email,role,expires_at,accepted_at,created_at
                    FROM invitations WHERE stockroom_id=%s ORDER BY created_at DESC LIMIT 50
                """, (session["stockroom_id"],)).fetchall()
            self.send_json(200, {"invitations": rows})
            return

        if path == "/api/audit":
            session = self.require_session(api=True)
            if not session:
                return
            if session["role"] not in ("owner", "admin"):
                self.send_json(403, {"error": "Geen rechten."})
                return
            with server.db() as conn:
                rows = conn.execute("""
                    SELECT a.id,a.action,a.details,a.created_at,u.name user_name,u.email
                    FROM audit_log a LEFT JOIN users u ON u.id=a.user_id
                    WHERE a.stockroom_id=%s ORDER BY a.created_at DESC LIMIT 100
                """, (session["stockroom_id"],)).fetchall()
            self.send_json(200, {"entries": rows})
            return

        return super().do_GET()

    def accept_invitation(self, token, user_id, email):
        with server.db() as conn:
            invitation = conn.execute("""
                SELECT id,stockroom_id,email,role FROM invitations
                WHERE token_hash=%s AND expires_at>NOW() AND accepted_at IS NULL FOR UPDATE
            """, (server.token_digest(token),)).fetchone()
            if not invitation or invitation["email"].lower() != email.lower():
                return None
            conn.execute("""INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,%s)
                            ON CONFLICT(user_id,stockroom_id) DO UPDATE SET role=CASE WHEN memberships.role='owner' THEN memberships.role ELSE EXCLUDED.role END""",
                         (user_id, invitation["stockroom_id"], invitation["role"]))
            conn.execute("UPDATE invitations SET accepted_at=NOW() WHERE id=%s", (invitation["id"],))
            audit(conn, None, "invitation.accepted", {"email": email, "role": invitation["role"]}, invitation["stockroom_id"], user_id)
            conn.commit()
            return str(invitation["stockroom_id"])

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.enforce_origin():
            return

        if path == "/invite/login":
            form = self.form_data()
            token = form.get("token", [""])[0] if form else ""
            email = form.get("email", [""])[0].strip().lower() if form else ""
            password = form.get("password", [""])[0] if form else ""
            invitation = self.invitation_by_token(token)
            if not invitation:
                self.send_html(400, server.result_page("Uitnodiging ongeldig", "Deze uitnodiging is verlopen of al gebruikt."))
                return
            with server.db() as conn:
                user = conn.execute("SELECT id,email,password_salt,password_hash,password_version FROM users WHERE email=%s", (email,)).fetchone()
            if not self.rate_limit("invite_login", email, 10, 900) or not user or not server.verify_password(password, user["password_salt"], user["password_hash"], user["password_version"]):
                self.send_html(403, invite_page(token, invitation, "E-mailadres of wachtwoord is onjuist."))
                return
            room_id = self.accept_invitation(token, user["id"], user["email"])
            if not room_id:
                self.send_html(400, server.result_page("Uitnodiging ongeldig", "De uitnodiging kon niet worden geaccepteerd."))
                return
            raw, _ = server.create_session(user["id"], room_id)
            self.redirect("/#settings", token=raw)
            return

        if path == "/invite/register":
            form = self.form_data()
            token = form.get("token", [""])[0] if form else ""
            name = form.get("name", [""])[0].strip() if form else ""
            email = form.get("email", [""])[0].strip().lower() if form else ""
            password = form.get("password", [""])[0] if form else ""
            invitation = self.invitation_by_token(token)
            if not invitation:
                self.send_html(400, server.result_page("Uitnodiging ongeldig", "Deze uitnodiging is verlopen of al gebruikt."))
                return
            if email != invitation["email"].lower() or not name or len(password) < 8:
                self.send_html(400, invite_page(token, invitation, "Controleer naam, e-mailadres en wachtwoord."))
                return
            salt, digest = server.hash_password(password)
            user_id = uuid.uuid4()
            try:
                with server.db() as conn:
                    conn.execute("INSERT INTO users(id,email,name,password_salt,password_hash,password_version,email_verified_at) VALUES(%s,%s,%s,%s,%s,2,NOW())", (user_id, email, name, salt, digest))
                    conn.commit()
            except Exception:
                self.send_html(409, invite_page(token, invitation, "Er bestaat al een account met dit e-mailadres. Gebruik hierboven Inloggen."))
                return
            room_id = self.accept_invitation(token, user_id, email)
            raw, _ = server.create_session(user_id, room_id)
            self.redirect("/#settings", token=raw)
            return

        if path == "/members/role":
            session = self.require_session(api=True)
            if not session:
                return
            if session["role"] not in ("owner", "admin"):
                self.send_json(403, {"error": "Geen rechten om rollen te wijzigen."})
                return
            form = self.form_data()
            target_user = form.get("user_id", [""])[0] if form else ""
            new_role = form.get("role", [""])[0] if form else ""
            if not target_user or target_user == session["user_id"] or not role_allowed(session["role"], new_role):
                self.send_json(403, {"error": "Deze rolwijziging is niet toegestaan."})
                return
            with server.db() as conn:
                target = conn.execute("SELECT role FROM memberships WHERE user_id=%s AND stockroom_id=%s FOR UPDATE", (target_user, session["stockroom_id"])).fetchone()
                if not target or target["role"] == "owner" or (session["role"] == "admin" and target["role"] == "admin"):
                    self.send_json(403, {"error": "Deze gebruiker kan niet door jou worden gewijzigd."})
                    return
                old_role = target["role"]
                conn.execute("UPDATE memberships SET role=%s WHERE user_id=%s AND stockroom_id=%s", (new_role, target_user, session["stockroom_id"]))
                audit(conn, session, "member.role_changed", {"targetUserId": target_user, "from": old_role, "to": new_role})
                conn.commit()
            self.send_json(200, {"saved": True, "role": new_role})
            return

        if path == "/api/invitations":
            session = self.require_session(api=True)
            if not session:
                return
            if session["role"] not in ("owner", "admin"):
                self.send_json(403, {"error": "Geen rechten."})
                return
            form = self.form_data()
            email = form.get("email", [""])[0].strip().lower() if form else ""
            role = form.get("role", ["member"])[0] if form else "member"
            if not email or not role_allowed(session["role"], role):
                self.send_json(400, {"error": "Ongeldige uitnodiging."})
                return
            raw = secrets.token_urlsafe(32)
            invitation_id = uuid.uuid4()
            with server.db() as conn:
                conn.execute("DELETE FROM invitations WHERE stockroom_id=%s AND email=%s AND accepted_at IS NULL", (session["stockroom_id"], email))
                conn.execute("INSERT INTO invitations(id,stockroom_id,email,role,token_hash,invited_by,expires_at) VALUES(%s,%s,%s,%s,%s,%s,NOW()+INTERVAL '7 days')", (invitation_id, session["stockroom_id"], email, role, server.token_digest(raw), session["user_id"]))
                audit(conn, session, "invitation.created", {"email": email, "role": role})
                conn.commit()
            link = f"{self.base_url()}/invite?token={raw}"
            try:
                server.send_email(email, f"Uitnodiging voor {session['stockroom_name']}", f"Je bent uitgenodigd voor Stockroom '{session['stockroom_name']}' als {runner.ROLE_LABELS.get(role, role)}.\n\nAccepteer de uitnodiging via:\n{link}\n\nDeze link is 7 dagen geldig.")
            except Exception as exc:
                self.send_json(502, {"error": f"Uitnodiging opgeslagen, maar e-mail verzenden mislukte: {type(exc).__name__}"})
                return
            self.send_json(200, {"sent": True})
            return

        if path == "/api/stockrooms/create":
            session = self.require_session(api=True)
            if not session:
                return
            if session["role"] == "viewer":
                self.send_json(403, {"error": "Viewer kan geen stockroom aanmaken."})
                return
            form = self.form_data()
            name = form.get("name", [""])[0].strip() if form else ""
            if not name or len(name) > 120:
                self.send_json(400, {"error": "Geef een geldige stockroomnaam op."})
                return
            room_id = uuid.uuid4()
            with server.db() as conn:
                conn.execute("INSERT INTO stockrooms(id,name,created_by,state) VALUES(%s,%s,%s,%s::jsonb)", (room_id, name, session["user_id"], json.dumps(server.EMPTY_STATE)))
                conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner')", (session["user_id"], room_id))
                audit(conn, session, "stockroom.created", {"name": name}, room_id)
                conn.execute("UPDATE sessions SET active_stockroom_id=%s WHERE token_hash=%s", (room_id, server.token_digest(self.cookie_token())))
                conn.commit()
            self.send_json(200, {"created": True, "id": str(room_id)})
            return

        if path == "/api/inventory/meta":
            session = self.require_session(api=True)
            if not session:
                return
            if session["role"] not in ("owner", "admin", "member"):
                self.send_json(403, {"error": "Geen rechten om artikelinstellingen te wijzigen."})
                return
            form = self.form_data()
            item_id = form.get("item_id", [""])[0] if form else ""
            category = form.get("category", [""])[0].strip() if form else ""
            supplier = form.get("supplier", [""])[0].strip() if form else ""
            try:
                min_stock = max(0, int(form.get("min_stock", ["0"])[0]))
            except ValueError:
                self.send_json(400, {"error": "Minimumvoorraad moet een geheel getal zijn."})
                return
            with server.db() as conn:
                row = conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE", (session["stockroom_id"],)).fetchone()
                state = row["state"]
                old_state = json.loads(json.dumps(state))
                target = next((i for i in state.get("items", []) if str(i.get("id")) == item_id), None)
                if not target:
                    self.send_json(404, {"error": "Artikel niet gevonden."})
                    return
                before = {"category": target.get("category", ""), "supplier": target.get("supplier", ""), "minStock": target.get("minStock", 0)}
                target.update({"category": category, "supplier": supplier, "minStock": min_stock})
                conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s", (json.dumps(state, ensure_ascii=False), session["stockroom_id"]))
                audit(conn, session, "item.settings_changed", {"item": target.get("name"), "before": before, "after": {"category": category, "supplier": supplier, "minStock": min_stock}})
                conn.commit()
            safely_notify_low_stock(session["stockroom_id"], session["stockroom_name"], low_stock_transitions(old_state, state))
            self.send_json(200, {"saved": True})
            return

        if path == "/api/inventory/correct":
            session = self.require_session(api=True)
            if not session:
                return
            if session["role"] not in ("owner", "admin", "member"):
                self.send_json(403, {"error": "Geen rechten voor voorraadcorrecties."})
                return
            form = self.form_data()
            item_id = form.get("item_id", [""])[0] if form else ""
            reason = form.get("reason", [""])[0].strip() if form else ""
            try:
                delta = int(form.get("delta", ["0"])[0])
            except ValueError:
                self.send_json(400, {"error": "Correctie moet een geheel getal zijn."})
                return
            if delta == 0 or not reason:
                self.send_json(400, {"error": "Geef een correctie en reden op."})
                return
            with server.db() as conn:
                row = conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE", (session["stockroom_id"],)).fetchone()
                state = row["state"]
                old_state = json.loads(json.dumps(state))
                target = next((i for i in state.get("items", []) if str(i.get("id")) == item_id), None)
                if not target:
                    self.send_json(404, {"error": "Artikel niet gevonden."})
                    return
                old_stock = int(target.get("stock", 0))
                new_stock = old_stock + delta
                if new_stock < 0:
                    self.send_json(400, {"error": "Voorraad kan niet negatief worden."})
                    return
                target["stock"] = new_stock
                state.setdefault("transactions", []).append({"id": str(uuid.uuid4()), "type": "adjustment", "itemId": item_id, "qty": delta, "reason": reason, "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s", (json.dumps(state, ensure_ascii=False), session["stockroom_id"]))
                audit(conn, session, "inventory.corrected", {"item": target.get("name"), "from": old_stock, "to": new_stock, "delta": delta, "reason": reason})
                conn.commit()
            safely_notify_low_stock(session["stockroom_id"], session["stockroom_name"], low_stock_transitions(old_state, state))
            self.send_json(200, {"saved": True, "stock": new_stock})
            return

        return super().do_POST()

    def do_PUT(self):
        if not self.enforce_origin():
            return
        if urlparse(self.path).path != "/api/state":
            return super().do_PUT()
        session = self.require_session(api=True)
        if not session:
            return
        if session["role"] == "viewer":
            self.send_json(403, {"error": "Viewer-toegang is alleen-lezen."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Ongeldige aanvraag."})
            return
        if length < 2 or length > server.MAX_BODY_BYTES:
            self.send_json(413, {"error": "Aanvraag is te groot of leeg."})
            return
        try:
            value = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json(400, {"error": "Ongeldige JSON."})
            return
        if not server.valid_state(value):
            self.send_json(422, {"error": "Ongeldige voorraadgegevens."})
            return
        with server.db() as conn:
            row = conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE", (session["stockroom_id"],)).fetchone()
            old_state = row["state"] if row else server.EMPTY_STATE
            if session["role"] in ("buyer", "seller") and not specialized_allowed(session["role"], old_state, value):
                self.send_json(403, {"error": "Deze wijziging past niet binnen je rol."})
                return
            conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s", (json.dumps(value, ensure_ascii=False), session["stockroom_id"]))
            summary = state_change_summary(old_state, value)
            if summary["items"] or summary["transactionChanges"]:
                audit(conn, session, "state.updated", summary)
            conn.commit()
        safely_notify_low_stock(session["stockroom_id"], session["stockroom_name"], low_stock_transitions(old_state, value))
        self.send_json(200, {"saved": True})


if __name__ == "__main__":
    if not server.DATABASE_URL:
        raise SystemExit("DATABASE_URL is verplicht en moet naar PostgreSQL wijzen.")
    server.initialize_database()
    runner.migrate_roles()
    initialize_enhancements()
    self_test_permissions()
    server.cleanup_expired()
    handler = partial(DashboardHandler, directory=str(server.PUBLIC_DIR))
    httpd = ThreadingHTTPServer((server.HOST, server.PORT), handler)
    print("Stockroom draait op poort 8000 met rollen, uitnodigingen, meerdere stockrooms, voorraadbeheer en auditlog")
    httpd.serve_forever()
