import json
import os
import unittest
import uuid

import inventory_ledger
import server


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is required")
class InventoryLedgerDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.DATABASE_URL = os.environ["TEST_DATABASE_URL"]
        server.initialize_database()
        inventory_ledger.initialize()

    def setUp(self):
        self.user_id = uuid.uuid4()
        self.room_id = uuid.uuid4()
        salt, digest = server.hash_password("ledger-test-password")
        state = {"items": [{"id": "a", "name": "Artikel", "stock": 10}], "transactions": []}
        with server.db() as conn:
            conn.execute("""INSERT INTO users(id,email,name,password_salt,password_hash,password_version)
                VALUES (%s,%s,'Ledger test',%s,%s,2)""",
                (self.user_id, f"ledger-{self.user_id}@example.test", salt, digest))
            conn.execute("INSERT INTO stockrooms(id,name,created_by,state) VALUES (%s,'Ledger',%s,%s::jsonb)",
                         (self.room_id, self.user_id, json.dumps(state)))
            conn.commit()

    def tearDown(self):
        with server.db() as conn:
            conn.execute("DELETE FROM stockrooms WHERE id=%s", (self.room_id,))
            conn.execute("DELETE FROM users WHERE id=%s", (self.user_id,))
            conn.commit()

    def test_start_change_and_reversal_are_individual_events(self):
        with server.db() as conn:
            row = conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE", (self.room_id,)).fetchone()
            state = row["state"]
            state["items"][0]["stock"] = 10.1
            inventory_ledger.set_context(conn, "manual_correction", "test")
            conn.execute("UPDATE stockrooms SET state=%s::jsonb WHERE id=%s", (json.dumps(state), self.room_id))
            state["items"][0]["stock"] = 10
            inventory_ledger.set_context(conn, "order_deleted", "reverse")
            conn.execute("UPDATE stockrooms SET state=%s::jsonb WHERE id=%s", (json.dumps(state), self.room_id))
            conn.commit()
        history = inventory_ledger.rows_for_item(self.room_id, "a")
        self.assertEqual(len(history), 3)
        self.assertEqual([row["source"] for row in reversed(history)],
                         ["opening_balance", "manual_correction", "order_deleted"])
        self.assertEqual([row["delta"] for row in reversed(history)], ["10", "0.1", "-0.1"])
        self.assertEqual(inventory_ledger.reconcile(state, list(reversed(history)))[0]["status"], "ok")
        inventory_ledger.initialize()
        self.assertEqual(len(inventory_ledger.rows_for_item(self.room_id, "a")), 3)


if __name__ == "__main__":
    unittest.main()

