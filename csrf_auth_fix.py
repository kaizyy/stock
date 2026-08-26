from urllib.parse import urlparse
import server

_AUTH_POST_PATHS={
    '/login','/register','/forgot-password','/resend-verification','/reset-password'
}
_original=server.StockroomHandler.enforce_origin

def _fixed_enforce_origin(self):
    source=self.headers.get('Origin') or self.headers.get('Referer')
    path=urlparse(self.path).path
    if not source and path in _AUTH_POST_PATHS:
        return True
    return _original(self)

def install():
    server.StockroomHandler.enforce_origin=_fixed_enforce_origin
