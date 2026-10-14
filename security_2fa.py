"""TOTP MFA primitives. Secrets encrypted at rest with AES-256-GCM."""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote

def _key():
    raw = os.environ.get("SECURITY_ENCRYPTION_KEY", "")
    if len(raw) != 64:
        raise ValueError("SECURITY_ENCRYPTION_KEY must be 64 hex characters")
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError("Invalid SECURITY_ENCRYPTION_KEY") from exc
    if len(key) != 32:
        raise ValueError("Invalid encryption key length")
    return key

def encrypt(secret):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return base64.b64encode(nonce + AESGCM(_key()).encrypt(nonce, secret.encode(), b"stockroom-totp-v1")).decode()

def decrypt(payload):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    data = base64.b64decode(payload, validate=True)
    return AESGCM(_key()).decrypt(data[:12], data[12:], b"stockroom-totp-v1").decode()

def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")

def code(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return str((struct.unpack(">I", digest[offset:offset+4])[0] & 0x7fffffff) % 1000000).zfill(6)

def verify(secret, supplied, last_step=-1):
    if not isinstance(supplied, str) or len(supplied) != 6 or not supplied.isascii() or not supplied.isdigit():
        return None
    now = int(time.time()) // 30
    for step in (now-1, now, now+1):
        if step > last_step and hmac.compare_digest(code(secret, step), supplied):
            return step
    return None

def uri(secret, email):
    return "otpauth://totp/Stockroom:" + quote(email, safe="") + "?secret=" + quote(secret) + "&issuer=Stockroom&algorithm=SHA1&digits=6&period=30"
