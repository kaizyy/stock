import os

import server

_pool=None


def install():
    global _pool
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
