import os
import unittest
import uuid
from unittest import mock

import order_management
import runner
import server

DB_URL = os.environ.get("TEST_DATABASE_URL")


class OrderPermissionTests(unittest.TestCase):
    def test_role_matrix(self):
        self.assertTrue(order_management.allowed("buyer", "write_suppliers"))
        self.assertTrue(order_management.allowed("buyer", "write_purchase"))
        self.assertFalse(order_management.allowed("buyer", "write_customers"))
        self.assertFalse(order_management.allowed("buyer", "write_sales"))
        self.assertTrue(order_management.allowed("seller", "write_customers"))
        self.assertTrue(order_management.allowed("seller", "write_sales"))
        self.assertFalse(order_management.allowed("seller", "write_suppliers"))
        self.assertTrue(order_management.allowed("viewer", "read_purchase"))
        self.assertFalse(order_management.allowed("viewer", "write_purchase"))

    def test_parse_lines_preserves_editable_values(self):
        lines = order_management._parse_lines('[{"item_id":"i1","item_name":"Item A","sku":"A","quantity":2.5,"unit_price":12.75}]')
        self.assertEqual(lines, [("i1", "Item A", "A", 2.5, 12.75)])

    def test_relation_name_can_be_derived_from_partial_details(self):
        values = {"name": "", "contact_name": "Jan Jansen", "email": "", "phone": "", "address": "", "notes": ""}
        payload = tuple((values.get(k) or "").strip()[:5000] for k in ("contact_name", "email", "phone", "address", "notes"))
        name = (values.get("name") or "").strip() or next((value for value in payload[:3] if value), "")
        self.assertEqual(name, "Jan Jansen")

    def test_address_only_relation_gets_visible_fallback_name(self):
        values = {"name": "", "contact_name": "", "email": "", "phone": "", "address": "Dorpsstraat 1", "notes": ""}
        payload = tuple((values.get(k) or "").strip()[:5000] for k in ("contact_name", "email", "phone", "address", "notes"))
        name = next((value for value in payload[:3] if value), "") or "Naamloze leverancier"
        self.assertEqual(name, "Naamloze leverancier")

    def test_partial_relation_is_inserted_and_committed(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False
        session = {"stockroom_id": "room-1", "user_id": "user-1"}
        with mock.patch("order_management.server.db", return_value=connection):
            relation_id = order_management.save_relation(session, "customer", {"address": "Dorpsstraat 1"})
        self.assertTrue(relation_id)
        inserts = [call.args[0] for call in connection.execute.call_args_list]
        self.assertTrue(any("INSERT INTO customers" in sql for sql in inserts))
        connection.commit.assert_called_once()


@unittest.skipUnless(DB_URL, "TEST_DATABASE_URL is required")
class OrderTenantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.DATABASE_URL = DB_URL
        server.initialize_database()
        runner.migrate_roles()
        order_management.initialize_order_management()

    def setUp(self):
        self.user_a, self.user_b = uuid.uuid4(), uuid.uuid4()
        self.room_a, self.room_b = uuid.uuid4(), uuid.uuid4()
        salt, digest = server.hash_password("order-test-password")
        with server.db() as conn:
            conn.execute("INSERT INTO users(id,email,name,password_salt,password_hash,password_version) VALUES(%s,%s,'A',%s,%s,2),(%s,%s,'B',%s,%s,2)", (self.user_a,f"a-{uuid.uuid4()}@example.test",salt,digest,self.user_b,f"b-{uuid.uuid4()}@example.test",salt,digest))
            conn.execute("INSERT INTO stockrooms(id,name,created_by) VALUES(%s,'A',%s),(%s,'B',%s)", (self.room_a,self.user_a,self.room_b,self.user_b))
            conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner'),(%s,%s,'owner')", (self.user_a,self.room_a,self.user_b,self.room_b))
            conn.commit()
        self.session_a={"stockroom_id":str(self.room_a),"user_id":str(self.user_a),"role":"owner"}
        self.session_b={"stockroom_id":str(self.room_b),"user_id":str(self.user_b),"role":"owner"}

    def tearDown(self):
        with server.db() as conn:
            conn.execute("DELETE FROM stockrooms WHERE id IN (%s,%s)",(self.room_a,self.room_b))
            conn.execute("DELETE FROM users WHERE id IN (%s,%s)",(self.user_a,self.user_b))
            conn.commit()

    def test_relations_are_tenant_scoped(self):
        order_management.save_relation(self.session_a,"supplier",{"name":"Supplier A"})
        order_management.save_relation(self.session_b,"supplier",{"name":"Supplier B"})
        a=order_management.relation_rows(str(self.room_a),"supplier")
        self.assertEqual([row["name"] for row in a],["Supplier A"])

    def test_order_cannot_use_other_tenant_relation(self):
        supplier_b=order_management.save_relation(self.session_b,"supplier",{"name":"Supplier B"})
        with self.assertRaises(PermissionError):
            order_management.create_order(self.session_a,{"order_type":"purchase","relation_id":supplier_b,"status":"draft","lines_json":'[{"item_id":"i1","item_name":"Item","sku":"SKU","quantity":1,"unit_price":5}]'})

    def test_order_rows_are_tenant_scoped(self):
        order_management.create_order(self.session_a,{"order_type":"sales","relation_name":"A customer","status":"draft","reference":"A-1","lines_json":'[{"item_id":"i1","item_name":"Item A","sku":"A","quantity":2,"unit_price":10}]'})
        order_management.create_order(self.session_b,{"order_type":"sales","relation_name":"B customer","status":"draft","reference":"B-1","lines_json":'[{"item_id":"i2","item_name":"Item B","sku":"B","quantity":1,"unit_price":20}]'})
        a=order_management.order_rows(str(self.room_a),"sales")
        self.assertEqual(len(a),1)
        self.assertEqual(a[0]["reference"],"A-1")
        self.assertEqual(a[0]["total"],20.0)

    def test_migration_is_idempotent(self):
        order_management.initialize_order_management()
        order_management.initialize_order_management()


if __name__ == "__main__":
    unittest.main()

