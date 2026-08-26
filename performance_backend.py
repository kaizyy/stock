import os
import urllib.parse
from http.server import SimpleHTTPRequestHandler

import server

_pool=None
_headers_installed=False


def _install_static_cache_headers():
    global _headers_installed
    if _headers_installed:return
    _headers_installed=True
    original=SimpleHTTPRequestHandler.end_headers
    def optimized_end_headers(self):
        try:
            path=urllib.parse.urlparse(getattr(self,'path','')).path.lower()
            if path.endswith(('.js','.css','.png','.jpg','.jpeg','.svg','.webp','.ico')):
                self.send_header('Cache-Control','public, max-age=300, stale-while-revalidate=60')
            elif path.startswith('/api/'):
                self.send_header('Cache-Control','no-store')
        except Exception:
            pass
        return original(self)
    SimpleHTTPRequestHandler.end_headers=optimized_end_headers


def install():
    global _pool
    _install_static_cache_headers()
    if _pool is not None or not server.DATABASE_URL:
        return
    try:
        from psycopg_pool import ConnectionPool
        from psycopg.rows import dict_row
        min_size=max(1,int(os.environ.get('DB_POOL_MIN','1')))
        max_size=max(min_size,int(os.environ.get('DB_POOL_MAX','10')))
        timeout=float(os.environ.get('DB_POOL_TIMEOUT','5'))
        _pool=ConnectionPool(
            conninfo=server.DATABASE_URL,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            kwargs={'row_factory':dict_row},
            open=True,
            name='stockroom',
        )
        server.db=lambda:_pool.connection()
        server.log_event(f'[PERF] PostgreSQL pool actief min={min_size} max={max_size}')
    except Exception as exc:
        server.log_event(f'[PERF] PostgreSQL pool niet actief: {type(exc).__name__}: {exc}')
