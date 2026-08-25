import os
import unittest
import uuid

import business_tools
import order_management
import runner
import server

DB_URL=os.environ.get('TEST_DATABASE_URL')

@unittest.skipUnless(DB_URL,'TEST_DATABASE_URL is required')
class BusinessToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.DATABASE_URL=DB_URL
        server.initialize_database(); runner.migrate_roles(); order_management.initialize_order_management(); business_tools.initialize_business_tools()

    def setUp(self):
        self.user=uuid.uuid4(); self.room=uuid.uuid4(); self.other=uuid.uuid4(); self.other_room=uuid.uuid4()
        salt,digest=server.hash_password('business-test-password')
        with server.db() as conn:
            conn.execute("INSERT INTO users(id,email,name,password_salt,password_hash,password_version) VALUES(%s,%s,'A',%s,%s,2),(%s,%s,'B',%s,%s,2)",(self.user,f'a-{uuid.uuid4()}@test.local',salt,digest,self.other,f'b-{uuid.uuid4()}@test.local',salt,digest))
            conn.execute("INSERT INTO stockrooms(id,name,created_by,state) VALUES(%s,'Room A',%s,%s::jsonb),(%s,'Room B',%s,%s::jsonb)",(self.room,self.user,'{"items":[{"id":"item-a","name":"Hammer","sku":"HAM-1","stock":5,"location":"A / 1 / 2"}],"transactions":[]}',self.other_room,self.other,'{"items":[{"id":"item-b","name":"Secret drill","sku":"DRL-X","stock":2}],"transactions":[]}'))
            conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner'),(%s,%s,'owner')",(self.user,self.room,self.other,self.other_room));conn.commit()
        self.session={'stockroom_id':str(self.room),'user_id':str(self.user),'role':'owner'}

    def tearDown(self):
        with server.db() as conn:
            conn.execute('DELETE FROM stockrooms WHERE id IN (%s,%s)',(self.room,self.other_room));conn.execute('DELETE FROM users WHERE id IN (%s,%s)',(self.user,self.other));conn.commit()

    def make_order(self,kind='sales'):
        oid=order_management.create_order(self.session,{'order_type':kind,'relation_name':'Test','status':'draft','lines_json':'[{"item_id":"item-a","item_name":"Hammer","sku":"HAM-1","quantity":1,"unit_price":10}]'})
        return oid,business_tools.assign_order_number(oid,str(self.room),kind)

    def test_order_numbers_are_sequential_and_prefixed(self):
        _,a=self.make_order('sales');_,b=self.make_order('sales')
        self.assertTrue(a.startswith('SO-'));self.assertTrue(b.startswith('SO-'));self.assertNotEqual(a,b);self.assertGreater(int(b.rsplit('-',1)[1]),int(a.rsplit('-',1)[1]))

    def test_search_is_tenant_scoped(self):
        self.assertTrue(any(r['title']=='Hammer' for r in business_tools.search_all(str(self.room),'hammer')))
        self.assertFalse(any('Secret drill' in r['title'] for r in business_tools.search_all(str(self.room),'secret')))

    def test_pdf_generation(self):
        oid,number=self.make_order('purchase')
        data,name=business_tools.order_pdf(str(self.room),oid)
        self.assertTrue(data.startswith(b'%PDF'));self.assertIn(number,name)
        inv,inv_name=business_tools.inventory_pdf(str(self.room))
        self.assertTrue(inv.startswith(b'%PDF'));self.assertEqual(inv_name,'voorraadlijst.pdf')

if __name__=='__main__': unittest.main()
