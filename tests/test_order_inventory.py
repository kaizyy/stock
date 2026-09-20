import json
import base64
import io
import zipfile
import os
import unittest
import uuid
from datetime import date, timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import server
import runner
import dashboard_runner
import order_management
import purchase_receipts
import purchase_invoices
import payment_batches
import invoice_recognition
import bank_reconciliation
import tax_reporting
import profit_reporting
import cashflow_forecast
import budget_planning
import order_returns
import purchase_intelligence
import purchase_approvals
import supplier_portal
import purchase_alternatives
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
        purchase_alternatives.initialize()
        purchase_receipts.initialize()
        purchase_invoices.initialize()
        payment_batches.initialize()
        bank_reconciliation.initialize()
        tax_reporting.initialize()
        profit_reporting.initialize()
        cashflow_forecast.initialize()
        budget_planning.initialize()
        order_returns.initialize()
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
        supplier_id = order_management.save_relation(self.session, "supplier", {"name":"Supply BV", "email":"inkoop@example.test","minimum_order_amount":"100","free_shipping_threshold":"50","ordering_weekdays":str(date.today().isoweekday()),"lead_time_days":"5"})
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
        self.assertIsNotNone(order["expected_delivery_date"])
        self.assertEqual(str(order["expected_delivery_date"]),(date.today()+timedelta(days=5)).isoformat())
        self.assertEqual(order["advice_details"]["planning"]["minimumOrderAmount"],100.0)
        self.assertEqual(len(order["advice_details"]["planning"]["warnings"]),2)
        self.assertEqual(order["advice_details"]["source"],"purchase_advice")
        self.assertEqual(order_management.open_purchase_quantities(str(self.room_id))[self.item_id], 8)
        with self.assertRaisesRegex(ValueError, "voldoende open"):
            order_management.create_purchase_advice_drafts(self.session, {"lines_json":json.dumps([
                {"item_id":self.item_id, "quantity":8}
            ])})

    def test_purchase_advice_requires_reason_for_supplier_override(self):
        first=order_management.save_relation(self.session,"supplier",{"name":"Beste leverancier"})
        second=order_management.save_relation(self.session,"supplier",{"name":"Alternatief"})
        historical=order_management.create_order(self.session,{"order_type":"purchase","relation_id":first,"relation_name":"Beste leverancier","lines_json":json.dumps([{"item_id":self.item_id,"item_name":"Testitem","sku":"T-1","quantity":1,"unit_price":3}])})
        order_management.update_order_status(self.session,"purchase",{"order_id":historical,"status":"ordered"})
        line_id=order_management.order_rows(str(self.room_id),"purchase")[0]["lines"][0]["id"]
        purchase_receipts.receive(self.session,{"order_id":historical,"lines_json":json.dumps([{"line_id":line_id,"quantity":1}])})
        with self.assertRaisesRegex(ValueError,"reden"):
            order_management.create_purchase_advice_drafts(self.session,{"lines_json":json.dumps([{"item_id":self.item_id,"quantity":2,"supplier_id":second}])})
        result=order_management.create_purchase_advice_drafts(self.session,{"lines_json":json.dumps([{"item_id":self.item_id,"quantity":2,"supplier_id":second,"override_reason":"Contractuele afspraak"}])})
        self.assertEqual(result["created"][0]["supplier"],"Alternatief")

    def test_supplier_shortage_creates_alternative_without_double_ordering(self):
        first=order_management.save_relation(self.session,"supplier",{"name":"Leverancier A"})
        second=order_management.save_relation(self.session,"supplier",{"name":"Leverancier B","lead_time_days":"4"})
        historical=order_management.create_order(self.session,{"order_type":"purchase","relation_id":second,"lines_json":json.dumps([{"item_id":self.item_id,"item_name":"Testitem","sku":"T-1","quantity":1,"unit_price":3.75}] )})
        order_management.update_order_status(self.session,"purchase",{"order_id":historical,"status":"ordered"})
        history_line=order_management.order_rows(str(self.room_id),"purchase")[0]["lines"][0]
        purchase_receipts.receive(self.session,{"order_id":historical,"lines_json":json.dumps([{"line_id":history_line["id"],"quantity":1}])})
        original=order_management.create_order(self.session,{"order_type":"purchase","relation_id":first,"lines_json":json.dumps([{"item_id":self.item_id,"item_name":"Testitem","sku":"T-1","quantity":5,"unit_price":4}])})
        order_management.update_order_status(self.session,"purchase",{"order_id":original,"status":"ordered"})
        original_order=next(row for row in order_management.order_rows(str(self.room_id),"purchase") if row["id"]==original);line=original_order["lines"][0]
        link=supplier_portal.issue(self.session,original,"https://stock.example.test");token=parse_qs(urlparse(link["url"]).query)["token"][0]
        supplier_portal.submit(token,{"confirmed_delivery_date":(date.today()+timedelta(days=12)).isoformat(),f"availability_{line['id']}":"partial",f"quantity_{line['id']}":"2"})
        options=purchase_alternatives.overview(self.session,original);self.assertEqual(options["shortages"][0]["shortage"],3);self.assertEqual(options["shortages"][0]["alternatives"][0]["supplierId"],second)
        result=purchase_alternatives.create(self.session,{"line_id":line["id"],"supplier_id":second});self.assertEqual(result["quantity"],3)
        refreshed=next(row for row in order_management.order_rows(str(self.room_id),"purchase") if row["id"]==original);self.assertEqual(refreshed["lines"][0]["supplier_cancelled_quantity"],3)
        supplement=next(row for row in order_management.order_rows(str(self.room_id),"purchase") if row["id"]==result["orderId"]);self.assertEqual(supplement["lines"][0]["quantity"],3);self.assertEqual(supplement["lines"][0]["source_order_line_id"],line["id"])
        self.assertEqual(order_management.open_purchase_quantities(str(self.room_id))[self.item_id],5)

    def test_purchase_budget_requires_and_records_approval(self):
        purchase_approvals.save_policy(self.session,{"approval_threshold":"0","monthly_budget":"5","price_warning_requires_approval":"1","auto_followup_enabled":"1","confirmation_reminder_days":"1","delay_reminder_days":"2"})
        result=order_management.create_purchase_advice_drafts(self.session,{"lines_json":json.dumps([{"item_id":self.item_id,"quantity":2}])})
        self.assertTrue(result["created"][0]["approvalRequired"])
        order=order_management.order_rows(str(self.room_id),"purchase")[0]
        self.assertEqual(order["status"],"pending_approval");self.assertIn("Maandbudget",order["approval_reason"])
        purchase_approvals.decide(self.session,{"order_id":order["id"],"reason":"Budget gecontroleerd"},"approve")
        smtp=MagicMock();smtp.__enter__.return_value=smtp
        with patch.object(server,"SMTP_HOST","smtp.example.test"),patch.object(server,"SMTP_PORT",587),patch.object(purchase_approvals.smtplib,"SMTP",return_value=smtp):
            sent=purchase_approvals.send_order(self.session,{"order_id":order["id"],"recipient":"supplier@example.test"})
        self.assertTrue(sent["sent"]);smtp.send_message.assert_called_once()
        with server.db() as conn:
            conn.execute("UPDATE orders SET purchase_sent_at=NOW()-INTERVAL '2 days' WHERE id=%s",(order["id"],));conn.commit()
        reminder_smtp=MagicMock();reminder_smtp.__enter__.return_value=reminder_smtp
        with patch.object(server,"SMTP_HOST","smtp.example.test"),patch.object(server,"SMTP_PORT",587),patch.object(purchase_approvals.smtplib,"SMTP",return_value=reminder_smtp):
            followup=purchase_approvals.run_due_followups(self.session)
        self.assertEqual(len(followup["sent"]),1);reminder_smtp.send_message.assert_called_once()
        self.assertEqual(purchase_approvals.followup_overview(str(self.room_id))["summary"]["awaiting_confirmation"],1)
        delivery=(date.today()+timedelta(days=10)).isoformat()
        token=parse_qs(urlparse(sent["portalUrl"]).query)["token"][0]
        portal=supplier_portal.lookup(token);self.assertEqual(portal["order"]["id"],order["id"])
        portal_line=portal["lines"][0]
        supplier_portal.submit(token,{"confirmed_delivery_date":delivery,"confirmation_reference":"PORTAL-42",f"availability_{portal_line['id']}":"partial",f"quantity_{portal_line['id']}":"1",f"note_{portal_line['id']}":"Rest volgt"})
        self.assertEqual(supplier_portal.lookup(token)["lines"][0]["availability"],"partial")
        confirmation=purchase_approvals.confirm_delivery(self.session,{"order_id":order["id"],"confirmed_delivery_date":delivery,"confirmation_reference":"BEV-42"})
        self.assertTrue(confirmation["confirmed"])
        approved=order_management.order_rows(str(self.room_id),"purchase")[0]
        self.assertEqual(approved["status"],"ordered");self.assertEqual(approved["approval_status"],"approved");self.assertEqual(approved["purchase_sent_to"],"supplier@example.test");self.assertEqual(str(approved["confirmed_delivery_date"]),delivery);self.assertEqual(approved["lines"][0]["supplier_availability"],"partial")
        supplier_portal.revoke(self.session,order["id"]);self.assertIsNone(supplier_portal.lookup(token))

    def test_partial_purchase_receipts_update_stock_status_and_can_reverse(self):
        order_id = self.create_order("purchase", 5, 3.5)
        order_management.update_order_status(self.session, "purchase", {"order_id":order_id, "status":"ordered"})
        line_id = order_management.order_rows(str(self.room_id), "purchase")[0]["lines"][0]["id"]
        first = purchase_receipts.receive(self.session, {"order_id":order_id,"reference":"PB-1","document_name":"pakbon.pdf","document_mime":"application/pdf","document_base64":"aGVsbG8=",
            "lines_json":json.dumps([{"line_id":line_id,"quantity":2,"damaged_quantity":0.5,"note":"Doos beschadigd"}]),"unexpected_items_json":json.dumps([{"barcode":"WRONG-1","quantity":1}])})
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["discrepancies"],2)
        self.assertEqual(self.get_state()["items"][0]["stock"], 11.5)
        receipt_rows=purchase_receipts.rows(str(self.room_id),order_id);self.assertEqual(receipt_rows[0]["lines"][0]["damaged_quantity"],0.5);self.assertTrue(receipt_rows[0]["has_document"])
        self.assertEqual(purchase_receipts.attachment(str(self.room_id),first["receiptId"])[0],b"hello")
        self.assertTrue(purchase_receipts.discrepancy_pdf(str(self.room_id),first["receiptId"])[0].startswith(b"%PDF"))
        action=purchase_receipts.create_discrepancy_action(self.session,{"receipt_id":first["receiptId"],"action_type":"claim","note":"Graag credit"});self.assertTrue(action["created"])
        second = purchase_receipts.receive(self.session, {"order_id":order_id,"reference":"PB-2",
            "lines_json":json.dumps([{"line_id":line_id,"quantity":3}])})
        self.assertEqual(second["status"], "received")
        self.assertEqual(self.get_state()["items"][0]["stock"], 14.5)
        self.assertEqual(len(purchase_receipts.rows(str(self.room_id),order_id)),2)
        reversed_result=purchase_receipts.reverse(self.session,{"receipt_id":second["receiptId"]})
        self.assertEqual(reversed_result["status"],"partial")
        self.assertEqual(self.get_state()["items"][0]["stock"],11.5)
        purchase_receipts.reverse(self.session,{"receipt_id":first["receiptId"]})
        self.assertEqual(self.get_state()["items"][0]["stock"],10)
        self.assertEqual(order_management.order_rows(str(self.room_id),"purchase")[0]["status"],"ordered")

    def test_receipt_reversal_blocks_when_received_stock_was_used(self):
        order_id=self.create_order("purchase",2,3.5);order_management.update_order_status(self.session,"purchase",{"order_id":order_id,"status":"ordered"})
        line_id=order_management.order_rows(str(self.room_id),"purchase")[0]["lines"][0]["id"]
        receipt=purchase_receipts.receive(self.session,{"order_id":order_id,"lines_json":json.dumps([{"line_id":line_id,"quantity":2}])})
        with server.db() as conn:
            state=conn.execute("SELECT state FROM stockrooms WHERE id=%s",(self.room_id,)).fetchone()["state"];state["items"][0]["stock"]=1
            conn.execute("UPDATE stockrooms SET state=%s::jsonb WHERE id=%s",(json.dumps(state),self.room_id));conn.commit()
        with self.assertRaisesRegex(ValueError,"onvoldoende voorraad"):
            purchase_receipts.reverse(self.session,{"receipt_id":receipt["receiptId"]})

    def test_sales_return_is_bounded_processed_and_reversible(self):
        order_id=self.create_order("sales",3,10)
        order_management.update_order_status(self.session,"sales",{"order_id":order_id,"status":"completed"})
        line_id=order_management.order_rows(str(self.room_id),"sales")[0]["lines"][0]["id"]
        result=order_returns.create(self.session,{"order_id":order_id,"reason_code":"defective","reason":"Klantretour","lines_json":json.dumps([{"line_id":line_id,"quantity":2}])})
        self.assertRegex(result["rmaNumber"],r"^RMA-\d{4}-\d{6}$")
        label,filename=order_returns.label_pdf(self.session,result["id"])
        self.assertTrue(label.startswith(b"%PDF"));self.assertTrue(filename.startswith("RMA-"))
        self.assertEqual(self.get_state()["items"][0]["stock"],7)
        order_returns.process(self.session,{"return_id":result["id"]})
        self.assertEqual(self.get_state()["items"][0]["stock"],9)
        report=order_returns.analytics(str(self.room_id))
        self.assertEqual(report["summary"]["return_count"],1);self.assertEqual(report["reasons"][0]["label"],"defective")
        self.assertEqual(report["items"][0]["quantity"],2)
        with self.assertRaisesRegex(ValueError,"hoger dan geleverd"):
            order_returns.create(self.session,{"order_id":order_id,"lines_json":json.dumps([{"line_id":line_id,"quantity":2}])})
        order_returns.change(self.session,{"return_id":result["id"]},"reverse")
        self.assertEqual(self.get_state()["items"][0]["stock"],7)
        order_returns.change(self.session,{"return_id":result["id"]},"cancel")

    def test_purchase_return_uses_only_received_quantity(self):
        order_id=self.create_order("purchase",5,3.5)
        order_management.update_order_status(self.session,"purchase",{"order_id":order_id,"status":"ordered"})
        line_id=order_management.order_rows(str(self.room_id),"purchase")[0]["lines"][0]["id"]
        purchase_receipts.receive(self.session,{"order_id":order_id,"lines_json":json.dumps([{"line_id":line_id,"quantity":3}])})
        result=order_returns.create(self.session,{"order_id":order_id,"lines_json":json.dumps([{"line_id":line_id,"quantity":2}])})
        order_returns.process(self.session,{"return_id":result["id"]})
        self.assertEqual(self.get_state()["items"][0]["stock"],11)
        details=order_returns.overview(str(self.room_id),order_id)["returns"][0]
        self.assertEqual(details["claim_status"],"open");self.assertEqual(details["expected_refund"],7)
        order_returns.update_claim(self.session,{"return_id":result["id"],"claim_reference":"CLAIM-42","expected_refund":"7.00"})
        partial=order_returns.record_refund(self.session,{"return_id":result["id"],"amount":"2.00","note":"deelbetaling"})
        self.assertEqual(partial["claimStatus"],"partial")
        settled=order_returns.record_refund(self.session,{"return_id":result["id"],"amount":"5.00","note":"slotbetaling"})
        self.assertEqual(settled["claimStatus"],"settled")
        intelligence=purchase_intelligence.overview(str(self.room_id))
        self.assertEqual(intelligence["suppliers"][0]["name"],"Relatie")
        self.assertAlmostEqual(intelligence["suppliers"][0]["returnRate"],66.7,places=1)
        self.assertEqual(intelligence["recommendations"][0]["recommended"]["latestPrice"],3.5)
        with self.assertRaisesRegex(ValueError,"hoger dan geleverd"):
            order_returns.create(self.session,{"order_id":order_id,"lines_json":json.dumps([{"line_id":line_id,"quantity":2}])})
        with self.assertRaisesRegex(ValueError,"terugbetaling"):
            order_returns.change(self.session,{"return_id":result["id"]},"reverse")
        self.assertEqual(self.get_state()["items"][0]["stock"],11)

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

    def test_purchase_invoice_three_way_match_blocks_and_prevents_duplicates(self):
        supplier_id = order_management.save_relation(self.session, "supplier", {"name": "Factuurleverancier"})
        order_id = order_management.create_order(self.session, {
            "order_type": "purchase", "relation_id": supplier_id,
            "lines_json": json.dumps([{"item_id": self.item_id, "item_name": "Testitem", "sku": "T-1", "quantity": 5, "unit_price": 4}]),
        })
        order_management.update_order_status(self.session, "purchase", {"order_id": order_id, "status": "ordered"})
        line = next(row for row in order_management.order_rows(str(self.room_id), "purchase") if row["id"] == order_id)["lines"][0]
        purchase_receipts.receive(self.session, {"order_id": order_id, "lines_json": json.dumps([{"line_id": line["id"], "quantity": 4}])})
        values = {"order_id": order_id, "invoice_number": "SUP-2026-1", "invoice_date": date.today().isoformat(), "due_date": (date.today()+timedelta(days=30)).isoformat(), "lines_json": json.dumps([{"order_line_id": line["id"], "quantity": 5, "unit_price": 4}])}
        result = purchase_invoices.create(self.session, values)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(issue["type"] == "quantity" for issue in result["discrepancies"]))
        with self.assertRaisesRegex(ValueError, "bestaat al"):
            purchase_invoices.create(self.session, values)
        with self.assertRaisesRegex(ValueError, "geblokkeerd"):
            purchase_invoices.payment(self.session, {"invoice_id": result["id"], "amount": 20})
        purchase_invoices.decide(self.session, {"invoice_id": result["id"], "dispute_amount": 4}, "dispute")
        purchase_invoices.credit(self.session, {"invoice_id": result["id"], "credit_number": "CR-1", "amount": 4})
        purchase_invoices.payment(self.session, {"invoice_id": result["id"], "amount": 12})
        invoice = purchase_invoices.rows(str(self.room_id))[0]
        self.assertEqual(invoice["status"], "paid")
        self.assertEqual(invoice["outstanding"], 0)

    def test_payment_batch_exports_sepa_and_books_once(self):
        supplier_id = order_management.save_relation(self.session, "supplier", {"name": "SEPA leverancier", "iban": "NL91ABNA0417164300", "bic": "ABNANL2A"})
        order_id = order_management.create_order(self.session, {"order_type": "purchase", "relation_id": supplier_id, "lines_json": json.dumps([{"item_id": self.item_id, "item_name": "Testitem", "sku": "T-1", "quantity": 2, "unit_price": 4}])})
        order_management.update_order_status(self.session, "purchase", {"order_id": order_id, "status": "ordered"})
        line = next(row for row in order_management.order_rows(str(self.room_id), "purchase") if row["id"] == order_id)["lines"][0]
        purchase_receipts.receive(self.session, {"order_id": order_id, "lines_json": json.dumps([{"line_id": line["id"], "quantity": 2}])})
        invoice = purchase_invoices.create(self.session, {"order_id": order_id, "invoice_number": "SEPA-1", "invoice_date": date.today().isoformat(), "due_date": date.today().isoformat(), "lines_json": json.dumps([{"order_line_id": line["id"], "quantity": 2, "unit_price": 4}])})
        purchase_invoices.decide(self.session, {"invoice_id": invoice["id"]}, "approve")
        payment_batches.save_settings(self.session, {"account_name": "Test BV", "iban": "NL91ABNA0417164300", "bic": "ABNANL2A"})
        batch = payment_batches.create(self.session, {"invoice_ids": json.dumps([invoice["id"]]), "execution_date": date.today().isoformat()})
        self.assertEqual(payment_batches.candidates(str(self.room_id)), [])
        payment_batches.change(self.session, {"batch_id": batch["id"]}, "approve")
        xml, filename = payment_batches.sepa(self.session, batch["id"])
        self.assertIn(b"CstmrCdtTrfInitn", xml);self.assertTrue(filename.endswith(".xml"))
        result = payment_batches.process(self.session, {"batch_id": batch["id"]})
        self.assertEqual(result["count"], 1)
        self.assertEqual(purchase_invoices.rows(str(self.room_id))[0]["status"], "paid")
        with self.assertRaisesRegex(ValueError, "Exporteer"):
            payment_batches.process(self.session, {"batch_id": batch["id"]})

    def test_invoice_recognition_suggests_supplier_order_and_fields(self):
        supplier_id = order_management.save_relation(self.session, "supplier", {"name": "Herken Leverancier", "email": "factuur@herken.test", "iban": "NL91ABNA0417164300"})
        order_id = order_management.create_order(self.session, {"order_type": "purchase", "relation_id": supplier_id, "reference": "PO-7788", "lines_json": json.dumps([{"item_id": self.item_id, "item_name": "Testitem", "sku": "T-1", "quantity": 1, "unit_price": 100}])})
        text = "Herken Leverancier factuur@herken.test Factuurnummer: INV-900 Factuurdatum: 20-09-2026 Vervaldatum: 20-10-2026 Inkooporder: PO-7788 Subtotaal 100,00 BTW 21,00 Totaal 121,00 IBAN NL91 ABNA 0417 1643 00 Betalingskenmerk: INV-900"
        with patch.object(invoice_recognition, "extract_text", return_value=text):
            result = invoice_recognition.recognize(self.session, {"document_mime": "application/pdf", "document_base64": base64.b64encode(b"pdf").decode()})
        self.assertEqual(result["supplier"]["id"], supplier_id)
        self.assertEqual(result["order"]["id"], order_id)
        self.assertEqual(result["fields"]["invoice_number"], "INV-900")
        self.assertEqual(result["fields"]["total_amount"], 121.0)

    def test_mt940_import_matches_sales_invoice_once(self):
        order_id = self.create_order("sales", 1, 100)
        order_management.update_order_status(self.session, "sales", {"order_id": order_id, "status": "completed"})
        invoice = documents_v3.ensure_invoice(str(self.room_id), order_id)
        statement = f":20:START\n:25:NL91ABNA0417164300\n:61:260920C121,00NTRF\n:86:FACTUUR {invoice['invoice_number']}\n:62F:C260920EUR121,00\n".encode()
        values = {"filename": "statement.mt940", "file_base64": base64.b64encode(statement).decode()}
        result = bank_reconciliation.import_file(self.session, values)
        self.assertEqual(result["imported"], 1);self.assertEqual(result["automaticallyMatched"], 1)
        paid = next(row for row in financial_workflow.list_invoices(str(self.room_id)) if row["order_id"] == order_id)
        self.assertEqual(paid["status"], "paid")
        with self.assertRaisesRegex(ValueError, "al geïmporteerd"):
            bank_reconciliation.import_file(self.session, values)

    def test_camt_parser_reads_credit_and_debit(self):
        xml = b'''<Document><BkToCstmrStmt><Stmt><Ntry><Amt Ccy="EUR">10.50</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-09-20</Dt></BookgDt><AcctSvcrRef>A1</AcctSvcrRef><NtryDtls><TxDtls><Refs><EndToEndId>INV-1</EndToEndId></Refs><RmtInf><Ustrd>Factuur INV-1</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry><Ntry><Amt Ccy="EUR">2.25</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2026-09-20</Dt></BookgDt><AcctSvcrRef>A2</AcctSvcrRef></Ntry></Stmt></BkToCstmrStmt></Document>'''
        rows = bank_reconciliation.parse_camt(xml)
        self.assertEqual(rows[0]["amount"], 10.5);self.assertEqual(rows[1]["amount"], -2.25)

    def test_tax_report_balances_vat_and_exports_traceable_files(self):
        sales_id = self.create_order("sales", 1, 100)
        order_management.update_order_status(self.session, "sales", {"order_id": sales_id, "status": "completed"})
        documents_v3.ensure_invoice(str(self.room_id), sales_id)
        supplier_id = order_management.save_relation(self.session, "supplier", {"name": "Btw leverancier"})
        purchase_id = order_management.create_order(self.session, {"order_type": "purchase", "relation_id": supplier_id, "lines_json": json.dumps([{"item_id": self.item_id, "item_name": "Testitem", "sku": "T-1", "quantity": 1, "unit_price": 100}])})
        order_management.update_order_status(self.session, "purchase", {"order_id": purchase_id, "status": "ordered"})
        line = next(row for row in order_management.order_rows(str(self.room_id), "purchase") if row["id"] == purchase_id)["lines"][0]
        purchase_receipts.receive(self.session, {"order_id": purchase_id, "lines_json": json.dumps([{"line_id": line["id"], "quantity": 1}])})
        purchase_invoices.create(self.session, {"order_id": purchase_id, "invoice_number": "VAT-1", "invoice_date": date.today().isoformat(), "due_date": date.today().isoformat(), "vat_amount": "21", "lines_json": json.dumps([{"order_line_id": line["id"], "quantity": 1, "unit_price": 100}])})
        quarter = (date.today().month-1)//3+1
        report = tax_reporting.report(str(self.room_id), date.today().year, quarter)
        self.assertEqual(report["summary"]["outputVat"], 21.0);self.assertEqual(report["summary"]["inputVat"], 21.0);self.assertEqual(report["summary"]["payable"], 0.0)
        tax_reporting.add_adjustment(self.session, {"adjustment_date": date.today().isoformat(), "kind": "output", "net_amount": "0", "vat_amount": "1.50", "reason": "Afrondingscorrectie"})
        self.assertEqual(tax_reporting.report(str(self.room_id), date.today().year, quarter)["summary"]["payable"], 1.5)
        archive, name = tax_reporting.export(self.session, date.today().year, quarter)
        self.assertTrue(name.endswith('.zip'))
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:self.assertEqual(set(zipped.namelist()), {'btw-samenvatting.csv','verkoopfacturen.csv','inkoopfacturen.csv','btw-correcties.csv','bankmutaties.csv','controlepunten.csv'})

    def test_profit_report_uses_historical_costs_expenses_and_comparison(self):
        sales_id = self.create_order("sales", 1, 100)
        order_management.update_order_status(self.session, "sales", {"order_id": sales_id, "status": "completed"})
        documents_v3.ensure_invoice(str(self.room_id), sales_id)
        with server.db() as conn:
            cost = conn.execute("SELECT cost_price::float8 cost FROM order_lines WHERE order_id=%s", (sales_id,)).fetchone()["cost"]
        self.assertEqual(cost, 4.0)
        profit_reporting.save_expense(self.session, {"expense_date": date.today().isoformat(), "category": "Software", "supplier_name": "SaaS", "description": "Abonnement", "net_amount": "10", "vat_amount": "2.10"})
        result = profit_reporting.report(str(self.room_id), date.today().year, "month", date.today().month)
        self.assertEqual(result["summary"]["revenue"], 100.0);self.assertEqual(result["summary"]["costOfGoods"], 4.0);self.assertEqual(result["summary"]["grossProfit"], 96.0);self.assertEqual(result["summary"]["netProfit"], 86.0)
        self.assertEqual(result["byItem"][0]["marginPercent"], 96.0);self.assertEqual(result["estimatedCount"], 0)
        archive, name = profit_reporting.export(self.session, date.today().year, "month", date.today().month)
        self.assertTrue(name.endswith('.zip'))
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:self.assertIn('marges-per-artikel.csv',zipped.namelist())

    def test_cashflow_forecast_uses_open_invoices_and_scenarios(self):
        sales_id = self.create_order("sales", 1, 100)
        order_management.update_order_status(self.session, "sales", {"order_id": sales_id, "status": "completed"})
        documents_v3.ensure_invoice(str(self.room_id), sales_id)
        cashflow_forecast.save_settings(self.session, {"current_balance": "1000", "balance_date": date.today().isoformat(), "minimum_buffer": "950"})
        result = cashflow_forecast.forecast(str(self.room_id))
        self.assertTrue(result["settings"]["configured"])
        self.assertTrue(any(event["kind"] == "sales_invoice" for event in result["events"]))
        self.assertAlmostEqual(result["scenarios"]["expected"]["90"]["endingBalance"], 1121.0)
        self.assertLess(result["scenarios"]["conservative"]["90"]["endingBalance"], result["scenarios"]["optimistic"]["90"]["endingBalance"])

    def test_cashflow_forecast_includes_only_unpaid_standalone_transactions(self):
        state = self.get_state()
        tomorrow = (date.today() + timedelta(days=1)).isoformat() + "T12:00:00"
        state["transactions"] = [
            {"id":"manual-sale","type":"outgoing","itemId":self.item_id,"qty":2,"price":10,"party":"Losse klant","done":False,"date":tomorrow},
            {"id":"manual-buy","type":"incoming","itemId":self.item_id,"qty":3,"price":4,"party":"Losse leverancier","done":False,"paid":False,"date":tomorrow},
            {"id":"paid-sale","type":"outgoing","itemId":self.item_id,"qty":99,"price":10,"done":True,"date":tomorrow},
            {"id":"linked-sale","type":"outgoing","itemId":self.item_id,"qty":99,"price":10,"done":False,"date":tomorrow,"orderId":str(uuid.uuid4())},
        ]
        with server.db() as conn:
            conn.execute("UPDATE stockrooms SET state=%s::jsonb WHERE id=%s", (json.dumps(state), self.room_id));conn.commit()
        cashflow_forecast.save_settings(self.session, {"current_balance":"1000","balance_date":date.today().isoformat(),"minimum_buffer":"0"})
        result = cashflow_forecast.forecast(str(self.room_id))
        manual = [event for event in result["events"] if event["kind"].startswith("manual_")]
        self.assertEqual({event["kind"] for event in manual}, {"manual_sale", "manual_purchase"})
        self.assertEqual(sum(event["amount"] for event in manual if event["direction"]=="in"), 20.0)
        self.assertEqual(sum(event["amount"] for event in manual if event["direction"]=="out"), 12.0)
        self.assertAlmostEqual(result["scenarios"]["expected"]["30"]["endingBalance"], 1008.0)

    def test_monthly_budget_compares_actuals_and_forecasts(self):
        sales_id = self.create_order("sales", 1, 100)
        order_management.update_order_status(self.session, "sales", {"order_id": sales_id, "status": "completed"})
        documents_v3.ensure_invoice(str(self.room_id), sales_id)
        profit_reporting.save_expense(self.session, {"expense_date": date.today().isoformat(), "category": "Software", "description": "Abonnement", "net_amount": "10", "vat_amount": "2.10"})
        budget_planning.save(self.session, {"year": date.today().year, "month": date.today().month, "revenue_target": "200", "gross_profit_target": "150", "expense_limit": "20"})
        result = budget_planning.overview(str(self.room_id), date.today().year, date.today().month)
        self.assertEqual(result["metrics"]["revenue"]["actual"], 100.0)
        self.assertEqual(result["metrics"]["grossProfit"]["actual"], 96.0)
        self.assertEqual(result["metrics"]["operatingExpenses"]["actual"], 10.0)
        self.assertEqual(result["budget"]["revenue"], 200.0)
        self.assertIn("forecastVariance", result["metrics"]["revenue"])

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
