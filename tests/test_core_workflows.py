import json
import os
import unittest
import uuid

import billing
import dashboard_runner
import documents_v3
import financial_workflow
import order_management
import runner
import server

DB_URL = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(DB_URL, "TEST_DATABASE_URL is required for workflow regression tests")
class CoreWorkflowRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.DATABASE_URL = DB_URL
        server.initialize_database()
        runner.migrate_roles()
        dashboard_runner.initialize_enhancements()
        order_management.initialize_order_management()
        billing.initialize_billing()
        documents_v3.initialize()
        financial_workflow.initialize()

    def setUp(self):
        self.user_id, self.room_id = uuid.uuid4(), uuid.uuid4()
        salt, digest = server.hash_password("correct horse battery staple")
        state = {"items": [{"id": "item-1", "name": "Testartikel", "sku": "T-1", "stock": 20, "buy": 4, "sell": 12}], "transactions": []}
        with server.db() as conn:
            conn.execute("INSERT INTO users(id,email,name,password_salt,password_hash,password_version) VALUES(%s,%s,'Workflow tester',%s,%s,2)", (self.user_id, f"{uuid.uuid4()}@example.test", salt, digest))
            conn.execute("INSERT INTO stockrooms(id,name,created_by,state) VALUES(%s,'Workflow room',%s,%s::jsonb)", (self.room_id, self.user_id, json.dumps(state)))
            conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner')", (self.user_id, self.room_id))
            conn.commit()
        self.session = {"user_id": str(self.user_id), "stockroom_id": str(self.room_id), "role": "owner"}

    def tearDown(self):
        with server.db() as conn:
            conn.execute("DELETE FROM stockrooms WHERE id=%s", (self.room_id,))
            conn.execute("DELETE FROM users WHERE id=%s", (self.user_id,))
            conn.commit()

    def create_sales_order(self, relation_id=None, quantity=2, price=10):
        return order_management.create_order(self.session, {
            "order_type": "sales", "status": "draft", "relation_id": relation_id or "",
            "reference": "REGRESSION", "lines_json": json.dumps([{"item_id": "item-1", "item_name": "Testartikel", "sku": "T-1", "quantity": quantity, "unit_price": price}]),
        })

    def test_relation_with_only_email_is_persisted(self):
        relation_id = order_management.save_relation(self.session, "customer", {"email": "klant@example.test"})
        relation = order_management.relation_row(str(self.room_id), "customer", relation_id)
        self.assertEqual(relation["email"], "klant@example.test")
        self.assertEqual(relation["name"], "klant@example.test")

    def test_order_edit_keeps_invoice_number_and_updates_total(self):
        relation_id = order_management.save_relation(self.session, "customer", {"name": "Klant"})
        order_id = self.create_sales_order(relation_id)
        invoice = documents_v3.ensure_invoice(str(self.room_id), order_id)
        order_management.update_order(self.session, {"order_id": order_id, "order_type": "sales", "relation_id": relation_id, "reference": "GEWIJZIGD", "lines_json": json.dumps([{"item_id": "item-1", "item_name": "Testartikel", "sku": "T-1", "quantity": 3, "unit_price": 15}])})
        current = next(row for row in financial_workflow.list_invoices(str(self.room_id)) if row["order_id"] == order_id)
        self.assertEqual(current["invoice_number"], invoice["invoice_number"])
        self.assertEqual(current["total"], 54.45)

    def test_invoice_trash_and_restore_preserve_financial_history(self):
        order_id = self.create_sales_order()
        documents_v3.ensure_invoice(str(self.room_id), order_id)
        financial_workflow.record_payment(self.session, order_id, 5, "Testbetaling")
        financial_workflow.delete_invoice(self.session, order_id)
        self.assertFalse(any(row["order_id"] == order_id for row in financial_workflow.list_invoices(str(self.room_id))))
        trashed = financial_workflow.list_deleted_invoices(str(self.room_id))
        self.assertEqual(next(row for row in trashed if row["order_id"] == order_id)["payments"], 1)
        financial_workflow.restore_invoice(self.session, order_id)
        restored = next(row for row in financial_workflow.list_invoices(str(self.room_id)) if row["order_id"] == order_id)
        self.assertEqual(restored["paid_amount"], 5)
        with server.db() as conn:
            self.assertIsNotNone(conn.execute("SELECT id FROM orders WHERE id=%s", (order_id,)).fetchone())

    def test_completed_sales_order_books_one_transaction_only(self):
        order_id = self.create_sales_order(quantity=3)
        order_management.update_order_status(self.session, "sales", {"order_id": order_id, "status": "completed"})
        order_management.update_order_status(self.session, "sales", {"order_id": order_id, "status": "paid"})
        with server.db() as conn:
            state = conn.execute("SELECT state FROM stockrooms WHERE id=%s", (self.room_id,)).fetchone()["state"]
        matching = [tx for tx in state["transactions"] if tx.get("orderId") == order_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(state["items"][0]["stock"], 17)


if __name__ == "__main__":
    unittest.main()

