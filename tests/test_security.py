import unittest
from unittest.mock import patch
from io import BytesIO
from email.message import Message

import server


class SecurityUnitTests(unittest.TestCase):
    def make_form_handler(self, body, content_type):
        handler = object.__new__(server.StockroomHandler)
        headers = Message()
        headers["Content-Length"] = str(len(body))
        headers["Content-Type"] = content_type
        handler.headers = headers
        handler.rfile = BytesIO(body)
        return handler

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

    def test_urlencoded_form_data_remains_supported(self):
        handler = self.make_form_handler(b"item_id=abc&min_stock=4", "application/x-www-form-urlencoded")
        self.assertEqual(handler.form_data(), {"item_id": ["abc"], "min_stock": ["4"]})

    def test_multipart_form_data_used_by_inventory_actions(self):
        boundary = "stockroom-test-boundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"item_id\"\r\n\r\nabc-123\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"category\"\r\n\r\nOnderdelen\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        handler = self.make_form_handler(body, f"multipart/form-data; boundary={boundary}")
        self.assertEqual(handler.form_data(), {"item_id": ["abc-123"], "category": ["Onderdelen"]})


if __name__ == "__main__":
    unittest.main()
