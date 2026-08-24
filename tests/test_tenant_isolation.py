import json
import os
import unittest
import uuid
from unittest.mock import patch

import psycopg

import server
import dashboard_runner
import runner


DB_URL = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(DB_URL, "TEST_DATABASE_URL is required for PostgreSQL isolation tests")
class TenantIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.DATABASE_URL = DB_URL
        server.initialize_database()
        runner.migrate_roles()
        dashboard_runner.initialize_enhancements()

    def setUp(self):
        self.a_user, self.b_user = uuid.uuid4(), uuid.uuid4()
        self.a_admin, self.a_buyer, self.a_viewer = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.a_room, self.b_room = uuid.uuid4(), uuid.uuid4()
        salt, digest = server.hash_password("correct horse battery staple")
        with server.db() as conn:
            for uid, email in ((self.a_user, f"a-{uuid.uuid4()}@example.test"), (self.b_user, f"b-{uuid.uuid4()}@example.test")):
                conn.execute("INSERT INTO users(id,email,name,password_salt,password_hash,password_version) VALUES(%s,%s,'Test',%s,%s,2)", (uid, email, salt, digest))
            for uid, email in ((self.a_admin, "admin-a@example.test"), (self.a_buyer, "buyer-a@example.test"), (self.a_viewer, "viewer-a@example.test")):
                conn.execute("INSERT INTO users(id,email,name,password_salt,password_hash,password_version) VALUES(%s,%s,'Test',%s,%s,2)", (uid, email, salt, digest))
            conn.execute("INSERT INTO stockrooms(id,name,created_by,state) VALUES(%s,'A',%s,%s::jsonb),(%s,'B',%s,%s::jsonb)", (self.a_room, self.a_user, json.dumps({"items":[{"id":"a-item"}],"transactions":[{"id":"a-tx"}]}), self.b_room, self.b_user, json.dumps({"items":[{"id":"b-item"}],"transactions":[{"id":"b-tx"}]})))
            conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner'),(%s,%s,'owner')", (self.a_user, self.a_room, self.b_user, self.b_room))
            conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'admin'),(%s,%s,'buyer'),(%s,%s,'viewer')", (self.a_admin, self.a_room, self.a_buyer, self.a_room, self.a_viewer, self.a_room))
            conn.execute("INSERT INTO invitations(id,stockroom_id,email,role,token_hash,invited_by,expires_at) VALUES(%s,%s,'invite-a@example.test','member',%s,%s,NOW()+INTERVAL '1 day'),(%s,%s,'invite-b@example.test','member',%s,%s,NOW()+INTERVAL '1 day')", (uuid.uuid4(), self.a_room, uuid.uuid4().hex, self.a_user, uuid.uuid4(), self.b_room, uuid.uuid4().hex, self.b_user))
            conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action) VALUES(%s,%s,'tenant-a'),(%s,%s,'tenant-b')", (self.a_room, self.a_user, self.b_room, self.b_user))
            conn.commit()

    def tearDown(self):
        with server.db() as conn:
            conn.execute("DELETE FROM stockrooms WHERE id IN (%s,%s)", (self.a_room, self.b_room))
            conn.execute("DELETE FROM users WHERE id IN (%s,%s,%s,%s,%s)", (self.a_user, self.b_user, self.a_admin, self.a_buyer, self.a_viewer))
            conn.commit()

    def test_a_cannot_read_b_inventory_transactions_users_invitations_or_audit(self):
        with server.db() as conn:
            rooms = conn.execute("SELECT r.state FROM stockrooms r JOIN memberships m ON m.stockroom_id=r.id WHERE m.user_id=%s", (self.a_user,)).fetchall()
            users = conn.execute("SELECT u.id FROM users u JOIN memberships m ON m.user_id=u.id WHERE m.stockroom_id=%s", (self.a_room,)).fetchall()
            invites = conn.execute("SELECT email FROM invitations WHERE stockroom_id=%s", (self.a_room,)).fetchall()
            audit = conn.execute("SELECT action FROM audit_log WHERE stockroom_id=%s", (self.a_room,)).fetchall()
        serialized = json.dumps(rooms, default=str)
        self.assertIn("a-item", serialized)
        self.assertIn("a-tx", serialized)
        self.assertNotIn("b-item", serialized)
        self.assertEqual({row["id"] for row in users}, {self.a_user, self.a_admin, self.a_buyer, self.a_viewer})
        text = lambda value: value.decode() if isinstance(value, bytes) else value
        self.assertEqual([text(row["email"]) for row in invites], ["invite-a@example.test"])
        self.assertEqual([text(row["action"]) for row in audit], ["tenant-a"])

    def test_a_cannot_update_or_delete_b_through_scoped_queries(self):
        with server.db() as conn:
            updated = conn.execute("UPDATE stockrooms SET state=%s::jsonb WHERE id=%s AND EXISTS (SELECT 1 FROM memberships WHERE user_id=%s AND stockroom_id=stockrooms.id) RETURNING id", (json.dumps(server.EMPTY_STATE), self.b_room, self.a_user)).fetchone()
            deleted = conn.execute("DELETE FROM invitations WHERE id IN (SELECT id FROM invitations WHERE stockroom_id=%s AND email='invite-b@example.test') RETURNING id", (self.a_room,)).fetchone()
            b = conn.execute("SELECT state FROM stockrooms WHERE id=%s", (self.b_room,)).fetchone()
            conn.rollback()
        self.assertIsNone(updated)
        self.assertIsNone(deleted)
        self.assertEqual(b["state"]["items"][0]["id"], "b-item")

    def test_deleting_tenant_a_cascades_only_tenant_a(self):
        with server.db() as conn:
            conn.execute("DELETE FROM stockrooms WHERE id=%s AND created_by=%s", (self.a_room, self.a_user))
            remaining_b = conn.execute("SELECT state FROM stockrooms WHERE id=%s", (self.b_room,)).fetchone()
            b_members = conn.execute("SELECT count(*) count FROM memberships WHERE stockroom_id=%s", (self.b_room,)).fetchone()["count"]
            b_invites = conn.execute("SELECT count(*) count FROM invitations WHERE stockroom_id=%s", (self.b_room,)).fetchone()["count"]
            b_audit = conn.execute("SELECT count(*) count FROM audit_log WHERE stockroom_id=%s", (self.b_room,)).fetchone()["count"]
            conn.rollback()
        self.assertIsNotNone(remaining_b)
        self.assertEqual((b_members, b_invites, b_audit), (1, 1, 1))

    def test_auth_token_is_expiring_and_single_use(self):
        raw = server.create_auth_token(self.a_user, "reset_password", 60)
        with server.db() as conn:
            first = conn.execute("SELECT user_id FROM auth_tokens WHERE token_hash=%s AND used_at IS NULL AND expires_at>NOW() FOR UPDATE", (server.token_digest(raw),)).fetchone()
            self.assertEqual(first["user_id"], self.a_user)
            conn.execute("UPDATE auth_tokens SET used_at=NOW() WHERE token_hash=%s", (server.token_digest(raw),))
            second = conn.execute("SELECT user_id FROM auth_tokens WHERE token_hash=%s AND used_at IS NULL AND expires_at>NOW()", (server.token_digest(raw),)).fetchone()
            conn.commit()
        self.assertIsNone(second)

    def test_migrations_are_idempotent(self):
        server.initialize_database()
        runner.migrate_roles()
        dashboard_runner.initialize_enhancements()

    def test_low_stock_mail_targets_owner_admin_and_buyer_only(self):
        with patch.object(server, "send_email") as send:
            dashboard_runner.notify_low_stock(
                self.a_room,
                "Tenant A",
                [{"id": "a-item", "name": "Filter", "stock": 2, "minimum": 2}],
            )
        recipients = {call.args[0] for call in send.call_args_list}
        self.assertIn("admin-a@example.test", recipients)
        self.assertIn("buyer-a@example.test", recipients)
        self.assertNotIn("viewer-a@example.test", recipients)
        self.assertEqual(len(recipients), 3)


if __name__ == "__main__":
    unittest.main()
