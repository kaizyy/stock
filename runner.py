import json
from functools import partial
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

import server


def migrate_viewer_role():
    with server.db() as conn:
        row = conn.execute("""
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'memberships'::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%role%'
        """).fetchone()
        if row:
            conn.execute(f'ALTER TABLE memberships DROP CONSTRAINT "{row["conname"]}"')
        conn.execute("ALTER TABLE memberships ADD CONSTRAINT memberships_role_check CHECK (role IN ('owner','admin','member','viewer'))")
        conn.commit()


def members_page(session, memberships, members, error="", success=""):
    original = server.members_page(session, memberships, members, error, success)
    if session["role"] not in ("owner", "admin"):
        return original
    marker = '<option value="member">Gebruiker</option>'
    return original.replace(marker, marker + '<option value="viewer">Viewer (alleen lezen)</option>')


server.members_page = members_page


class StockroomHandler(server.StockroomHandler):
    def do_POST(self):
        path = urlparse(self.path).path
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
    print(f"Stockroom draait op poort {server.PORT} met viewer read-only rol")
    httpd.serve_forever()
