import os
import re
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest import mock

import app_runner
import security_integrations as sec
import server


def handler(form):
    result = mock.Mock()
    result.form_data.return_value = {key: [value] for key, value in form.items()}
    result.rate_limit.return_value = True
    result.enforce_origin.return_value = True
    return result


class LoginTwofaTests(unittest.TestCase):
    def setUp(self):
        self.user = dict(id='user', password_salt='salt', password_hash='hash',
                         password_version=2, locked_until=None, email_verified_at=True,
                         totp_enabled_at=True, totp_secret_encrypted='encrypted')
        self.membership = dict(stockroom_id='room', role='owner')
        self.conn = mock.MagicMock()
        self.conn.__enter__.return_value = self.conn
        self.conn.execute.return_value.fetchone.side_effect = lambda: (
            self.user if self.conn.execute.call_args.args[0].startswith('SELECT *')
            else self.membership)
        for name, value in [('db', self.conn), ('verify_password', True),
                            ('create_session', ('session-token', 123))]:
            patcher = mock.patch.object(server, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(sec, 'record_login')
        self.record = patcher.start()
        self.addCleanup(patcher.stop)

    def login(self):
        request = handler(dict(email='user@example.test', password='password', totp='123456'))
        sec._custom_login(request)
        return request

    def test_first_page_only_asks_for_credentials(self):
        page = server.login_page()
        self.assertIn('name="email"', page)
        self.assertIn('name="password"', page)
        self.assertNotIn('one-time-code', page)
        self.assertNotIn('name="totp"', page)

    def test_base_handler_cannot_bypass_twofa_without_install_wrapper(self):
        request = handler(dict(email='user@example.test', password='password'))
        request.path = '/login'
        server.StockroomHandler.do_POST(request)
        self.assertIn('action="/2fa"', request.send_html.call_args.args[1])
        self.create_session.assert_not_called()

    def test_direct_second_step_get_requires_new_login(self):
        request = handler({})
        request.path = '/2fa?challenge=untrusted'
        server.StockroomHandler.do_GET(request)
        request.redirect.assert_called_once_with('/login')
        request.send_html.assert_not_called()

    def test_cross_origin_second_step_is_rejected(self):
        request = handler(dict(challenge='challenge', code='123456'))
        request.path = '/2fa'
        request.enforce_origin.return_value = False
        server.StockroomHandler.do_POST(request)
        request.form_data.assert_not_called()
        self.create_session.assert_not_called()

    def test_verified_credentials_render_second_step_without_session_or_url_token(self):
        request = self.login()
        status, page = request.send_html.call_args.args
        self.assertEqual(status, 200)
        self.assertIn('action="/2fa"', page)
        self.assertIn('name="challenge"', page)
        self.assertIn('autocomplete="one-time-code"', page)
        self.assertNotIn('name="password"', page)
        self.create_session.assert_not_called()
        request.redirect.assert_not_called()
        self.record.assert_not_called()
        insert = next(call for call in self.conn.execute.call_args_list
                      if call.args[0].startswith('INSERT INTO twofa_challenges'))
        raw = re.search(r'name="challenge" value="([^"]+)"', page).group(1)
        self.assertEqual(insert.args[1][0], server.token_digest(raw))
        self.assertNotIn(raw, insert.args[1])

    def test_wrong_password_never_discloses_second_step(self):
        self.verify_password.return_value = False
        request = self.login()
        self.assertEqual(request.send_html.call_args.args[0], 401)
        self.assertNotIn('name="challenge"', request.send_html.call_args.args[1])
        self.create_session.assert_not_called()

    def test_locked_unverified_or_missing_membership_never_issues_challenge(self):
        for scenario in ('locked', 'unverified', 'no-membership'):
            with self.subTest(scenario=scenario):
                self.user['locked_until'] = (datetime.now(timezone.utc) + timedelta(minutes=5)
                                             if scenario == 'locked' else None)
                self.user['email_verified_at'] = None if scenario == 'unverified' else True
                self.membership = None if scenario == 'no-membership' else dict(stockroom_id='room')
                request = self.login()
                self.assertNotIn('name="challenge"', request.send_html.call_args.args[1])
        self.create_session.assert_not_called()

    def test_account_without_twofa_logs_in_directly(self):
        self.user['totp_enabled_at'] = None
        request = self.login()
        self.create_session.assert_called_once_with('user', 'room')
        request.redirect.assert_called_once_with('/', token='session-token')

    def test_enabled_twofa_with_missing_secret_fails_closed(self):
        self.user['totp_secret_encrypted'] = None
        self.assertEqual(self.login().send_html.call_args.args[0], 403)
        self.create_session.assert_not_called()

    def complete(self, valid=True, row=True):
        self.conn.execute.return_value.fetchone.side_effect = None
        self.conn.execute.return_value.fetchone.return_value = (
            dict(user_id='user', stockroom_id='room', totp_secret_encrypted='encrypted')
            if row else None)
        request = handler(dict(challenge='challenge', code='123456'))
        with mock.patch.object(sec, 'decrypt_secret', return_value='secret'), \
                mock.patch.object(sec, 'verify_totp', return_value=valid) as verify:
            sec._complete_twofa(request)
            return request, verify

    def test_wrong_totp_keeps_challenge_and_creates_no_session(self):
        request, _ = self.complete(valid=False)
        self.assertEqual(request.send_html.call_args.args[0], 401)
        self.create_session.assert_not_called()
        self.assertFalse(any(call.args[0].startswith('DELETE FROM twofa_challenges')
                             for call in self.conn.execute.call_args_list))
        self.conn.commit.assert_called_once()

    def test_success_consumes_challenge_before_session(self):
        order = mock.Mock()
        order.attach_mock(self.conn, 'connection')
        order.attach_mock(self.create_session, 'session')
        request, _ = self.complete()
        self.create_session.assert_called_once_with('user', 'room')
        request.redirect.assert_called_once_with('/', token='session-token')
        calls = order.mock_calls
        deletion = next(i for i, call in enumerate(calls) if call[0] == 'connection.execute'
                        and call.args[0].startswith('DELETE FROM twofa_challenges'))
        commit = next(i for i, call in enumerate(calls) if call[0] == 'connection.commit')
        session = next(i for i, call in enumerate(calls) if call[0] == 'session')
        self.assertLess(deletion, commit)
        self.assertLess(commit, session)

    def test_missing_expired_exhausted_or_consumed_challenge_creates_no_session(self):
        request, verify = self.complete(row=False)
        self.assertEqual(request.send_html.call_args.args[0], 400)
        verify.assert_not_called()
        self.create_session.assert_not_called()

    def test_mobile_api_rejects_wrong_code_before_session(self):
        self.conn.execute.return_value.fetchone.side_effect = None
        self.conn.execute.return_value.fetchone.return_value = self.user
        request = handler(dict(email='user@example.test', password='password', totp='000000'))
        request.path = '/api/mobile/login'
        with mock.patch.object(sec, 'decrypt_secret', return_value='secret'), \
                mock.patch.object(sec, 'verify_totp', return_value=False):
            app_runner.AppHandler.do_POST(request)
        self.assertEqual(request.send_json.call_args.args[0], 403)
        self.create_session.assert_not_called()


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'PostgreSQL test database required')
class LoginTwofaDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_url = server.DATABASE_URL
        server.DATABASE_URL = os.environ['TEST_DATABASE_URL']
        server.initialize_database()
        sec.initialize_security()

    @classmethod
    def tearDownClass(cls):
        server.DATABASE_URL = cls.original_url

    def setUp(self):
        self.key = mock.patch.dict(os.environ, SECURITY_ENCRYPTION_KEY='ab'*32)
        self.key.start()
        self.addCleanup(self.key.stop)
        self.uid, self.room = uuid.uuid4(), uuid.uuid4()
        self.email = f'{self.uid}@example.test'
        self.secret = 'JBSWY3DPEHPK3PXP'
        salt, digest = server.hash_password('correct password')
        with server.db() as conn:
            conn.execute("""INSERT INTO users(id,email,name,password_salt,password_hash,password_version,
                         email_verified_at,totp_enabled_at,totp_secret_encrypted)
                         VALUES(%s,%s,'Test',%s,%s,2,NOW(),NOW(),%s)""",
                         (self.uid, self.email, salt, digest, sec.encrypt_secret(self.secret)))
            conn.execute("INSERT INTO stockrooms(id,name,created_by) VALUES(%s,'Test',%s)", (self.room, self.uid))
            conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner')", (self.uid, self.room))
            conn.commit()

    def tearDown(self):
        with server.db() as conn:
            conn.execute('DELETE FROM stockrooms WHERE id=%s', (self.room,))
            conn.execute('DELETE FROM users WHERE id=%s', (self.uid,))
            conn.commit()

    def challenge(self):
        request = handler(dict(email=self.email, password='correct password'))
        sec._custom_login(request)
        return re.search(r'name="challenge" value="([^"]+)"', request.send_html.call_args.args[1]).group(1)

    def complete(self, raw, code=None):
        request = handler(dict(challenge=raw, code=code or sec.totp(self.secret)))
        sec._complete_twofa(request)
        return request

    def sessions(self):
        with server.db() as conn:
            return conn.execute('SELECT count(*) AS n FROM sessions WHERE user_id=%s', (self.uid,)).fetchone()['n']

    def test_concurrent_completion_creates_exactly_one_session(self):
        raw = self.challenge()
        self.assertEqual(self.sessions(), 0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.complete(raw), range(2)))
        self.assertEqual(sum(result.redirect.called for result in results), 1)
        self.assertEqual(self.sessions(), 1)
        self.assertEqual(self.complete(raw).send_html.call_args.args[0], 400)

    def test_attempt_limit_and_expiry(self):
        raw = self.challenge()
        for _ in range(5):
            self.assertEqual(self.complete(raw, 'invalid').send_html.call_args.args[0], 401)
        self.assertEqual(self.complete(raw).send_html.call_args.args[0], 400)
        raw = self.challenge()
        with server.db() as conn:
            conn.execute("UPDATE twofa_challenges SET expires_at=NOW()-INTERVAL '1 second' WHERE user_id=%s", (self.uid,))
            conn.commit()
        self.assertEqual(self.complete(raw).send_html.call_args.args[0], 400)
        self.assertEqual(self.sessions(), 0)

    def test_password_reset_disable_or_membership_revocation_invalidates_challenge(self):
        for mutation in ("UPDATE users SET password_hash='changed' WHERE id=%s",
                         "UPDATE users SET totp_enabled_at=NULL WHERE id=%s",
                         'DELETE FROM memberships WHERE user_id=%s'):
            with self.subTest(mutation=mutation):
                raw = self.challenge()
                with server.db() as conn:
                    conn.execute(mutation, (self.uid,))
                    conn.commit()
                self.assertEqual(self.complete(raw).send_html.call_args.args[0], 400)
                self.assertEqual(self.sessions(), 0)
                # Restore the fixture for the next independent revocation case.
                salt, digest = server.hash_password('correct password')
                with server.db() as conn:
                    conn.execute('UPDATE users SET password_salt=%s,password_hash=%s,totp_enabled_at=NOW() WHERE id=%s', (salt, digest, self.uid))
                    conn.execute("INSERT INTO memberships(user_id,stockroom_id,role) VALUES(%s,%s,'owner') ON CONFLICT DO NOTHING", (self.uid, self.room))
                    conn.commit()
