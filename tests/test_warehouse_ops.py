import json
import os
import unittest
import uuid

import runner
import server
import warehouse_ops
import inventory_ledger

DB_URL = os.environ.get("TEST_DATABASE_URL")


class WarehousePermissionTests(unittest.TestCase):
    def test_role_matrix(self):
        self.assertTrue(warehouse_ops.permissions("owner")["count"])
        self.assertTrue(warehouse_ops.permissions("member")["transfer"])
        self.assertTrue(warehouse_ops.permissions("seller")["salesReturn"])
        self.assertFalse(warehouse_ops.permissions("seller")["purchaseReturn"])
        self.assertTrue(warehouse_ops.permissions("buyer")["purchaseReturn"])
        self.assertFalse(warehouse_ops.permissions("buyer")["transfer"])
        self.assertTrue(warehouse_ops.permissions("viewer")["read"])
        self.assertFalse(warehouse_ops.permissions("viewer")["count"])


@unittest.skipUnless(DB_URL, "TEST_DATABASE_URL is required")
class WarehouseOperationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.DATABASE_URL = DB_URL
        server.initialize_database()
        runner.migrate_roles()
        warehouse_ops.initialize_warehouse_ops()
        inventory_ledger.initialize()

    def setUp(self):
        self.user = uuid.uuid4()
        self.other_user = uuid.uuid4()
        self.room_a = uuid.uuid4()
        self.room_b = uuid.uuid4()
        self.room_c = uuid.uuid4()
        salt, digest = server.hash_password("warehouse-test-password")
        state_a = {"items":[{"id":"item-a","name":"Widget","sku":"W-1","barcode":"871000000001","stock":10,"buy":4,"sell":10}],"transactions":[]}
        state_b = {"items":[],"transactions":[]}
        state_c = {"items":[],"transactions":[]}
        with server.db() as conn:
            conn.execute("INSERT INTO users(id,email,name,password_salt,password_hash,password_version) VALUES(%s,%s,'User',%s,%s,2),(%s,%s,'Other',%s,%s,2)", (self.user,f"wh-{uuid.uuid4()}@example.test",salt,digest,self.other_user,f"other-{uuid.uuid4()}@example.test",salt,digest))
            conn.execute("INSERT INTO stockrooms(id,name,created_by,state) VALUES(%s,'A',%s,%s::jsonb),(%s,'B',%s,%s::jsonb),(%s,'C',%s,%s::jsonb)", (self.room_a,self.user,json.dumps(state_a),self.room_b,self.user,json.dumps(state_b),self.room_c,self.other_user,json.dumps(state_c)))
            conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner'),(%s,%s,'owner'),(%s,%s,'owner')", (self.user,self.room_a,self.user,self.room_b,self.other_user,self.room_c))
            conn.commit()
        self.session={"stockroom_id":str(self.room_a),"user_id":str(self.user),"role":"owner"}

    def tearDown(self):
        with server.db() as conn:
            conn.execute("DELETE FROM stockrooms WHERE id IN (%s,%s,%s)",(self.room_a,self.room_b,self.room_c))
            conn.execute("DELETE FROM users WHERE id IN (%s,%s)",(self.user,self.other_user))
            conn.commit()

    def state(self, room):
        with server.db() as conn:
            return conn.execute("SELECT state FROM stockrooms WHERE id=%s",(room,)).fetchone()["state"]

    def test_count_sets_exact_stock_and_logs_difference(self):
        result=warehouse_ops.apply_count(self.session,{"item_id":"item-a","actual_quantity":"7","note":"test"})
        self.assertEqual(result["difference"],-3)
        self.assertEqual(self.state(self.room_a)["items"][0]["stock"],7)
        self.assertEqual(warehouse_ops.history(str(self.room_a))[0]["operation_type"],"count")

    def test_sales_return_adds_stock_and_negative_sale_transaction(self):
        warehouse_ops.apply_return(self.session,{"item_id":"item-a","quantity":"2","price":"10","reference":"R1"},"sales")
        state=self.state(self.room_a)
        self.assertEqual(state["items"][0]["stock"],12)
        self.assertEqual(state["transactions"][-1]["type"],"outgoing")
        self.assertEqual(state["transactions"][-1]["qty"],-2)
        self.assertTrue(state["transactions"][-1]["isReturn"])

    def test_purchase_return_blocks_insufficient_stock(self):
        with self.assertRaises(ValueError):
            warehouse_ops.apply_return(self.session,{"item_id":"item-a","quantity":"11","price":"4"},"purchase")
        self.assertEqual(self.state(self.room_a)["items"][0]["stock"],10)

    def test_transfer_moves_stock_and_creates_destination_item(self):
        result=warehouse_ops.apply_transfer(self.session,{"item_id":"item-a","quantity":"3","destination_stockroom_id":str(self.room_b)})
        self.assertEqual(result["sourceStock"],7)
        self.assertEqual(result["destinationStock"],3)
        self.assertEqual(self.state(self.room_a)["items"][0]["stock"],7)
        dest=self.state(self.room_b)["items"][0]
        self.assertEqual(dest["sku"],"W-1")
        self.assertEqual(dest["stock"],3)

    def test_transfer_cannot_cross_to_unrelated_tenant(self):
        with self.assertRaises(PermissionError):
            warehouse_ops.apply_transfer(self.session,{"item_id":"item-a","quantity":"1","destination_stockroom_id":str(self.room_c)})
        self.assertEqual(self.state(self.room_a)["items"][0]["stock"],10)

    def test_initialization_is_idempotent(self):
        warehouse_ops.initialize_warehouse_ops()

    def test_count_workflow_requires_review_before_stock_changes(self):
        count_id = warehouse_ops.start_count(self.session, {"title":"Maandtelling", "note":"controle"})["id"]
        self.assertEqual(self.state(self.room_a)["items"][0]["stock"], 10)
        warehouse_ops.save_count_line(self.session, {"count_id":count_id, "barcode":"871000000001", "counted_stock":"7.5"})
        warehouse_ops.submit_count(self.session, {"count_id":count_id})
        self.assertEqual(self.state(self.room_a)["items"][0]["stock"], 10)
        result = warehouse_ops.approve_count(self.session, {"count_id":count_id})
        self.assertEqual(result["changes"], 1)
        self.assertEqual(self.state(self.room_a)["items"][0]["stock"], 7.5)
        count = warehouse_ops.count_sessions(str(self.room_a))[0]
        self.assertEqual(count["status"], "approved")
        self.assertEqual(count["variance_value"], -10)

    def test_count_approval_blocks_intervening_stock_change(self):
        count_id = warehouse_ops.start_count(self.session, {"title":"Veilige telling"})["id"]
        warehouse_ops.save_count_line(self.session, {"count_id":count_id, "item_id":"item-a", "counted_stock":"9"})
        warehouse_ops.submit_count(self.session, {"count_id":count_id})
        with server.db() as conn:
            state = conn.execute("SELECT state FROM stockrooms WHERE id=%s", (self.room_a,)).fetchone()["state"]
            state["items"][0]["stock"] = 11
            conn.execute("UPDATE stockrooms SET state=%s::jsonb WHERE id=%s", (json.dumps(state), self.room_a))
            conn.commit()
        with self.assertRaisesRegex(ValueError, "gewijzigd sinds"):
            warehouse_ops.approve_count(self.session, {"count_id":count_id})
        warehouse_ops.cancel_count(self.session, {"count_id":count_id})
        self.assertEqual(self.state(self.room_a)["items"][0]["stock"], 11)
        warehouse_ops.initialize_warehouse_ops()

    def test_item_history_is_complete_and_tenant_scoped(self):
        with server.db() as conn:
            for index in range(110):
                conn.execute("""INSERT INTO warehouse_operations
                    (id,stockroom_id,operation_type,item_id,item_name,quantity,previous_stock,new_stock)
                    VALUES(%s,%s,'count','item-a','Widget',1,9,10)""", (uuid.uuid4(), self.room_a))
            conn.execute("""INSERT INTO warehouse_operations
                (id,stockroom_id,operation_type,item_id,item_name,quantity,previous_stock,new_stock)
                VALUES(%s,%s,'count','item-a','Widget',1,9,10),
                      (%s,%s,'count','item-b','Ander',1,9,10)""",
                (uuid.uuid4(), self.room_b, uuid.uuid4(), self.room_a))
            conn.commit()
        self.assertEqual(len(warehouse_ops.history_for_item(str(self.room_a), 'item-a')), 110)
        self.assertEqual(len(warehouse_ops.history_for_item(str(self.room_b), 'item-a')), 1)
        self.assertEqual(len(warehouse_ops.history_for_item(str(self.room_a), 'item-b')), 1)


if __name__ == "__main__":
    unittest.main()

