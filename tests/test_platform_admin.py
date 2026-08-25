import os
import unittest
import uuid

import platform_admin
import order_management
import runner
import server

DB_URL = os.environ.get('TEST_DATABASE_URL')


@unittest.skipUnless(DB_URL, 'TEST_DATABASE_URL is required')
class PlatformAdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.DATABASE_URL = DB_URL
        server.initialize_database()
        runner.migrate_roles()
        order_management.initialize_order_management()
        platform_admin.initialize_platform_admin()

    def setUp(self):
        self.admin = uuid.uuid4()
        self.owner = uuid.uuid4()
        self.room = uuid.uuid4()
        salt, digest = server.hash_password('platform-test-password')
        self.admin_email = f'platform-{uuid.uuid4()}@test.local'
        self.owner_email = f'owner-{uuid.uuid4()}@test.local'
        with server.db() as conn:
            conn.execute(
                "INSERT INTO users(id,email,name,password_salt,password_hash,password_version) VALUES(%s,%s,'Platform',%s,%s,2),(%s,%s,'Owner',%s,%s,2)",
                (self.admin, self.admin_email, salt, digest, self.owner, self.owner_email, salt, digest),
            )
            conn.execute(
                "INSERT INTO stockrooms(id,name,created_by,state) VALUES(%s,'Tenant',%s,%s::jsonb)",
                (self.room, self.owner, '{"items":[{"id":"low","name":"Low item","stock":1,"minStock":5}],"transactions":[]}'),
            )
            conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner'),(%s,%s,'admin')", (self.owner, self.room, self.admin, self.room))
            conn.commit()
        self.admin_session = {'user_id': str(self.admin), 'stockroom_id': str(self.room), 'email': self.admin_email, 'role': 'admin'}
        self.owner_session = {'user_id': str(self.owner), 'stockroom_id': str(self.room), 'email': self.owner_email, 'role': 'owner'}
        self.old_env = os.environ.get('PLATFORM_ADMIN_EMAILS')
        os.environ['PLATFORM_ADMIN_EMAILS'] = self.admin_email

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop('PLATFORM_ADMIN_EMAILS', None)
        else:
            os.environ['PLATFORM_ADMIN_EMAILS'] = self.old_env
        with server.db() as conn:
            conn.execute('DELETE FROM stockrooms WHERE id=%s', (self.room,))
            conn.execute('DELETE FROM users WHERE id IN (%s,%s)', (self.admin, self.owner))
            conn.commit()

    def test_platform_admin_is_separate_from_stockroom_owner(self):
        self.assertTrue(platform_admin.is_platform_admin(self.admin_session))
        self.assertFalse(platform_admin.is_platform_admin(self.owner_session))

    def test_stockroom_suspension_blocks_normal_owner_but_not_platform_admin(self):
        platform_admin.set_suspension(self.admin_session, 'stockroom', str(self.room), True, 'test')
        allowed, _ = platform_admin.enforce_access(self.owner_session)
        admin_allowed, _ = platform_admin.enforce_access(self.admin_session)
        self.assertFalse(allowed)
        self.assertTrue(admin_allowed)

    def test_notifications_are_generated_for_active_stockroom(self):
        notes = platform_admin.stockroom_notifications(str(self.room))
        self.assertTrue(any(n['type'] == 'low_stock' and 'Low item' in n['title'] for n in notes))

    def test_monitoring_error_is_recorded(self):
        platform_admin.record_error('unit-test', 'synthetic failure', str(self.room), str(self.owner), {'safe': True})
        data = platform_admin.platform_overview()
        self.assertTrue(any(e['component'] == 'unit-test' and e['message'] == 'synthetic failure' for e in data['errors']))


if __name__ == '__main__':
    unittest.main()
