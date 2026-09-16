import json
import os
import unittest
import uuid

import business_tools
import billing
import dashboard_runner
import documents_v3
import financial_workflow
import order_management
import platform_admin
import runner
import sales_workflow
import server
import warehouse_ops


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is required")
class ActionCenterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.DATABASE_URL = os.environ["TEST_DATABASE_URL"]
        server.initialize_database(); runner.migrate_roles(); dashboard_runner.initialize_enhancements()
        order_management.initialize_order_management(); business_tools.initialize_business_tools()
        warehouse_ops.initialize_warehouse_ops(); billing.initialize_billing(); documents_v3.initialize(); sales_workflow.initialize()
        financial_workflow.initialize(); platform_admin.initialize_platform_admin()

    def setUp(self):
        self.user_id, self.room_id = uuid.uuid4(), uuid.uuid4()
        salt, digest = server.hash_password("action-center-test")
        state = {"items":[{"id":"a","name":"Kritiek artikel","stock":0,"minStock":3,"buy":2}],"transactions":[]}
        with server.db() as conn:
            conn.execute("INSERT INTO users(id,email,name,password_salt,password_hash,password_version) VALUES(%s,%s,'Tester',%s,%s,2)",
                         (self.user_id,f"actions-{self.user_id}@example.test",salt,digest))
            conn.execute("INSERT INTO stockrooms(id,name,created_by,state) VALUES(%s,'Acties',%s,%s::jsonb)",
                         (self.room_id,self.user_id,json.dumps(state)))
            conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner')",(self.user_id,self.room_id))
            conn.commit()
        self.session={"user_id":str(self.user_id),"stockroom_id":str(self.room_id),"role":"owner"}

    def tearDown(self):
        with server.db() as conn:
            conn.execute("DELETE FROM stockrooms WHERE id=%s",(self.room_id,));conn.execute("DELETE FROM users WHERE id=%s",(self.user_id,));conn.commit()

    def test_actions_follow_live_state_and_role(self):
        count_id=warehouse_ops.start_count(self.session,{"title":"Controle"})["id"]
        warehouse_ops.save_count_line(self.session,{"count_id":count_id,"item_id":"a","counted_stock":"0"})
        warehouse_ops.submit_count(self.session,{"count_id":count_id})
        order_id=order_management.create_order(self.session,{"order_type":"purchase","status":"draft","relation_name":"Leverancier",
            "lines_json":json.dumps([{"item_id":"a","item_name":"Kritiek artikel","quantity":3,"unit_price":2}])})
        business_tools.assign_order_number(order_id,str(self.room_id),"purchase")
        owner_types={row["key"].split(':')[0] for row in platform_admin.action_center(str(self.room_id),"owner")}
        self.assertTrue({"stock","count","purchase-draft"}.issubset(owner_types))
        viewer_types={row["key"].split(':')[0] for row in platform_admin.action_center(str(self.room_id),"viewer")}
        self.assertEqual(viewer_types,{"stock"})
        warehouse_ops.approve_count(self.session,{"count_id":count_id})
        order_management.update_order_status(self.session,"purchase",{"order_id":order_id,"status":"ordered"})
        remaining={row["key"].split(':')[0] for row in platform_admin.action_center(str(self.room_id),"owner")}
        self.assertNotIn("count",remaining);self.assertNotIn("purchase-draft",remaining)


if __name__ == "__main__":
    unittest.main()

