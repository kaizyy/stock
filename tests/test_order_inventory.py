import json
import os
import unittest
import uuid

import server
import runner
import dashboard_runner
import order_management
import business_tools
import billing
import documents_v3
import financial_workflow
import sales_workflow

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
        business_tools.initialize_business_tools()
        billing.initialize_billing()
        documents_v3.initialize()
        financial_workflow.initialize()
        sales_workflow.initialize()

    def setUp(self):
        self.user_id = uuid.uuid4()
        self.room_id = uuid.uuid4()
        self.item_id = "item-1"
        salt, digest = server.hash_password("correct horse battery staple")
        state = {"items": [{"id": self.item_id, "name": "Testitem", "sku": "T-1", "stock": 10, "buy": 4, "sell": 10}], "transactions": []}
        with server.db() as conn:
            conn.execute("INSERT INTO users(id,email,name,password_salt,password_hash,password_version) VALUES(%s,%s,'Tester',%s,%s,2)", (self.user_id, f"{uuid.uuid4()}@example.test", salt, digest))
            conn.execute("INSERT INTO stockrooms(id,name,created_by,state) VALUES(%s,'Testroom',%s,%s::jsonb)", (self.room_id, self.user_id, json.dumps(state)))
            conn.execute("INSERT INTO billing_accounts(stockroom_id) VALUES(%s)", (self.room_id,))
            conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner')", (self.user_id, self.room_id))
            conn.commit()
        self.session = {"user_id": str(self.user_id), "stockroom_id": str(self.room_id), "role": "owner"}

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
        with self.assertRaises(ValueError):
            self.create_order("sales", 11, 10)
        state = self.get_state()
        self.assertEqual(state["items"][0]["stock"], 10)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) count FROM orders WHERE stockroom_id=%s",(self.room_id,)).fetchone()["count"],0)

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

    def test_purchase_advice_groups_supplier_and_prevents_duplicate_draft(self):
        supplier_id = order_management.save_relation(self.session, "supplier", {"name":"Supply BV", "email":"inkoop@example.test"})
        with server.db() as conn:
            state = conn.execute("SELECT state FROM stockrooms WHERE id=%s", (self.room_id,)).fetchone()["state"]
            state["items"][0]["supplier"] = "Supply BV"
            conn.execute("UPDATE stockrooms SET state=%s::jsonb WHERE id=%s", (json.dumps(state), self.room_id))
            conn.commit()
        result = order_management.create_purchase_advice_drafts(self.session, {"lines_json":json.dumps([
            {"item_id":self.item_id, "quantity":8}
        ])})
        self.assertEqual(len(result["created"]), 1)
        order = order_management.order_rows(str(self.room_id), "purchase")[0]
        self.assertEqual(order["relation_id"], supplier_id)
        self.assertEqual(order["status"], "draft")
        self.assertEqual(order["lines"][0]["quantity"], 8)
        self.assertEqual(order_management.open_purchase_quantities(str(self.room_id))[self.item_id], 8)
        with self.assertRaisesRegex(ValueError, "voldoende open"):
            order_management.create_purchase_advice_drafts(self.session, {"lines_json":json.dumps([
                {"item_id":self.item_id, "quantity":8}
            ])})

    def test_decimal_purchase_and_sale_preserve_stock_and_reservations(self):
        purchase_id = self.create_order("purchase", 0.1)
        order_management.update_order_status(self.session, "purchase", {"order_id": purchase_id, "status": "received"})
        self.assertAlmostEqual(self.get_state()["items"][0]["stock"], 10.1)

        sales_id = self.create_order("sales", 0.2)
        reservation = financial_workflow.reservation_overview(str(self.room_id))[0]
        self.assertAlmostEqual(reservation["reserved"], 0.2)
        self.assertAlmostEqual(reservation["available"], 9.9)
        order_management.update_order_status(self.session, "sales", {"order_id": sales_id, "status": "completed"})
        state = self.get_state()
        self.assertAlmostEqual(state["items"][0]["stock"], 9.9)
        self.assertAlmostEqual(next(t for t in state["transactions"] if t.get("orderId") == sales_id)["qty"], 0.2)
        self.assertAlmostEqual(financial_workflow.reservation_overview(str(self.room_id))[0]["available"], 9.9)

    def test_decimal_quote_invoice_payment_creates_sales_order_without_double_booking(self):
        quote = sales_workflow.create(self.session, {
            "relation_name": "Relatie",
            "lines_json": json.dumps([{"item_id": self.item_id, "item_name": "Testitem", "sku": "T-1", "quantity": 0.1, "unit_price": 10}]),
        })
        converted = sales_workflow.convert(self.session, quote["id"])
        self.assertTrue(converted["invoiced"])
        reservation = financial_workflow.reservation_overview(str(self.room_id))[0]
        self.assertAlmostEqual(reservation["reserved"], 0.1)
        self.assertAlmostEqual(reservation["available"], 9.9)

        payment = sales_workflow.pay_quote_invoice(self.session, quote["id"], 1.21)
        self.assertTrue(payment["converted"])
        order_id = payment["order_id"]
        reservation = financial_workflow.reservation_overview(str(self.room_id))[0]
        self.assertAlmostEqual(reservation["reserved"], 0.1)
        with server.db() as conn:
            line = conn.execute("SELECT quantity::float8 quantity FROM order_lines WHERE order_id=%s", (order_id,)).fetchone()
            invoice = conn.execute("SELECT paid_amount::float8 paid_amount FROM invoice_documents WHERE order_id=%s", (order_id,)).fetchone()
        self.assertAlmostEqual(line["quantity"], 0.1)
        self.assertAlmostEqual(invoice["paid_amount"], 1.21)
        order_management.update_order_status(self.session, "sales", {"order_id": order_id, "status": "completed"})
        self.assertAlmostEqual(self.get_state()["items"][0]["stock"], 9.9)
        self.assertAlmostEqual(financial_workflow.reservation_overview(str(self.room_id))[0]["reserved"], 0)


if __name__ == "__main__":
    unittest.main()

