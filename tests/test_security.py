import unittest
from unittest.mock import patch

import server


class SecurityUnitTests(unittest.TestCase):
    def test_password_hash_roundtrip_and_wrong_password(self):
        salt, digest = server.hash_password("a sufficiently long password")
        self.assertTrue(server.verify_password("a sufficiently long password", salt, digest, 2))
        self.assertFalse(server.verify_password("wrong password", salt, digest, 2))

    def test_legacy_password_hash_remains_compatible(self):
        salt, digest = server.hash_password("legacy password", n=2**14)
        self.assertTrue(server.verify_password("legacy password", salt, digest, 1))

    def test_smtp_error_log_never_contains_credentials_or_recipient(self):
        messages = []
        with patch.object(server, "SMTP_HOST", "smtp.example.test"), patch.object(server, "SMTP_USERNAME", "smtp-user"), patch.object(server, "SMTP_PASSWORD", "super-secret"), patch.object(server, "log_event", messages.append), patch("smtplib.SMTP", side_effect=OSError("super-secret recipient@example.test")):
            with self.assertRaises(OSError):
                server.send_email("recipient@example.test", "Subject", "Body")
        output = "\n".join(messages)
        self.assertNotIn("super-secret", output)
        self.assertNotIn("recipient@example.test", output)
        self.assertNotIn("smtp-user", output)

    def test_cookie_authenticated_request_without_origin_is_rejected(self):
        handler = object.__new__(server.StockroomHandler)
        handler.headers = {}
        handler.cookie_token = lambda: "ambient-cookie"
        responses = []
        handler.send_json = lambda status, value: responses.append((status, value))
        self.assertFalse(handler.enforce_origin())
        self.assertEqual(responses[0][0], 403)


if __name__ == "__main__":
    unittest.main()
