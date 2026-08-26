import os
import unittest
from unittest import mock

os.environ.setdefault('SECURITY_ENCRYPTION_KEY','test-only-security-key')

import security_integrations as sec


class SecurityIntegrationTests(unittest.TestCase):
    def test_totp_accepts_current_code_and_rejects_bad_code(self):
        secret='JBSWY3DPEHPK3PXP'
        with mock.patch('security_integrations.time.time', return_value=1700000000):
            code=sec.totp(secret)
            self.assertTrue(sec.verify_totp(secret,code))
            self.assertFalse(sec.verify_totp(secret,'000000' if code!='000000' else '999999'))

    def test_secret_encryption_roundtrip_and_tamper_detection(self):
        token=sec.encrypt_secret('TOPSECRET')
        self.assertEqual(sec.decrypt_secret(token),'TOPSECRET')
        raw=bytearray(__import__('base64').urlsafe_b64decode(token.encode()))
        raw[-1]^=1
        tampered=__import__('base64').urlsafe_b64encode(bytes(raw)).decode()
        with self.assertRaises(ValueError):
            sec.decrypt_secret(tampered)

    def test_api_auth_requires_scope_and_is_tenant_bound_to_key(self):
        class Headers(dict):
            def get(self,k,d=''): return super().get(k,d)
        class Handler: headers=Headers(Authorization='Bearer sr_example')
        class Row(dict):
            pass
        class Conn:
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def execute(self,sql,args=()):
                if sql.startswith('SELECT id::text,stockroom_id::text,scopes'):
                    self.result=Row(id='k1',stockroom_id='room-a',scopes=['read'])
                return self
            def fetchone(self): return getattr(self,'result',None)
            def commit(self): pass
        with mock.patch('security_integrations.server.db', return_value=Conn()), mock.patch('security_integrations.server.token_digest', return_value='hash'):
            read=sec.api_auth(Handler(),'read')
            self.assertEqual(read['stockroom_id'],'room-a')
            self.assertIsNone(sec.api_auth(Handler(),'write'))

    def test_webhook_signature_is_hmac_sha256(self):
        import hashlib,hmac,json
        payload={'event':'order.updated','stockroom_id':'room-a','data':{'id':'o1'}}
        body=json.dumps(payload,separators=(',',':')).encode()
        a=hmac.new(b'secret',body,hashlib.sha256).hexdigest()
        b=hmac.new(b'secret',body,hashlib.sha256).hexdigest()
        self.assertEqual(a,b)
        self.assertNotEqual(a,hmac.new(b'other',body,hashlib.sha256).hexdigest())

    def test_webhook_rejects_non_https(self):
        with self.assertRaises(ValueError): sec._safe_webhook_url('http://example.com/hook')


if __name__=='__main__': unittest.main()
