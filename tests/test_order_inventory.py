import json
import os
import unittest
import uuid

import server
import runner
import dashboard_runner
import order_management

DB_URL = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(DB_URL, "TEST_DATABASE_URL is required for PostgreSQL order tests")
class OrderInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.DATABASE_URL = DB_URL
        server.initialize_database()
        runner.migrate_roles()
        dashboard_runner.initialize_enhancements()
        order_management.initialize_order_management()

    def setUp(self):
        self.user_id = uuid.uuid4()
        self.room_id = uuid.uuid4()
        self.item_id = "item-1"
        salt, digest = server.hash_password("correct horse battery staple")
        state = {"items": [{"id": self.item_id, "name": "Testitem", "sku": "T-1", "stock": 10, "buy": 4, "sell": 10}], "transactions": []}
        with server.db() as conn:
            conn.execute("INSERT INTO users(id,email,name,password_salt,password_hash,password_version) VALUES(%s,%s,'Tester',%s,%s,2)", (self.user_id, f"{uuid.uuid4()}@example.test", salt, digest))
            conn.execute("INSERT INTO stockrooms(id,name,created_by,state) VALUES(%s,'Testroom',%s,%s::jsonb)", (self.room_id, self.user_id, json.dumps(state)))
            conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner')", (self.user_id, self.room_id))
            conn.commit()
        self.session = {"user_id": str(self.user_id), "stockroom_id": str(self.room_id)}

    def tearDown(self):
        with server.db() as conn:
            conn.execute("DELETE FROM stockrooms WHERE id=%s", (self.room_id,))
            conn.execute("DELETE FROM users WHERE id=%s", (self.user_id,))
            conn.commit()

    def create_order(self, order_type, qty, price=10):
        return order_management.create_order(self.session, {
            "order_type": order_type,
            "status": "draft",
            "reference": f"TEST-{uuid.uuid4()}",
            "relation_name": "Relatie",
            "lines_json": json.dumps([{ "item_id": self.item_id, "item_name": "Testitem", "sku": "T-1", "quantity": qty, "unit_price": price }]),
        })

    def get_state(self):
        with server.db() as conn:
            return conn.execute("SELECT state FROM stockrooms WHERE id=%s", (self.room_id,)).fetchone()["state"]

    def test_sales_completed_books_once(self):
        order_id = self.create_order("sales", 3, 10)
        order_management.update_order_status(self.session, "sales", {"order_id": order_id, "status": "completed"})
        state = self.get_state()
        self.assertEqual(state["items"][0]["stock"], 7)
        self.assertEqual(len([t for t in state["transactions"] if t.get("orderId") == order_id]), 1)
        order_management.update_order_status(self.session, "sales", {"order_id": order_id, "status": "paid"})
        state = self.get_state()
        self.assertEqual(state["items"][0]["stock"], 7)
        self.assertEqual(len([t for t in state["transactions"] if t.get("orderId") == order_id]), 1)

    def test_sales_completed_rejects_insufficient_stock(self):
        order_id = self.create_order("sales", 11, 10)
        with self.assertRaises(ValueError):
            order_management.update_order_status(self.session, "sales", {"order_id": order_id, "status": "completed"})
        state = self.get_state()
        self.assertEqual(state["items"][0]["stock"], 10)
        with server.db() as conn:
            order = conn.execute("SELECT status,inventory_booked_at FROM orders WHERE id=%s", (order_id,)).fetchone()
        self.assertEqual(order["status"], "draft")
        self.assertIsNone(order["inventory_booked_at"])

    def test_purchase_received_books_once_and_cannot_go_back(self):
        order_id = self.create_order("purchase", 5, 3.5)
        order_management.update_order_status(self.session, "purchase", {"order_id": order_id, "status": "received"})
        state = self.get_state()
        self.assertEqual(state["items"][0]["stock"], 15)
        self.assertEqual(state["items"][0]["buy"], 3.5)
        self.assertEqual(len([t for t in state["transactions"] if t.get("orderId") == order_id]), 1)
        with self.assertRaises(ValueError):
            order_management.update_order_status(self.session, "purchase", {"order_id": order_id, "status": "ordered"})
        state = self.get_state()
        self.assertEqual(state["items"][0]["stock"], 15)


if __name__ == "__main__":
    unittest.main()
