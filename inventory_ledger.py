"""Append-only stock balance journal maintained by PostgreSQL triggers."""

from decimal import Decimal, InvalidOperation

def initialize():
    import server
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS inventory_ledger (
            id BIGSERIAL PRIMARY KEY,
            stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
            item_id TEXT NOT NULL,
            item_name TEXT NOT NULL DEFAULT '',
            previous_stock NUMERIC NOT NULL,
            new_stock NUMERIC NOT NULL,
            delta NUMERIC NOT NULL,
            source TEXT NOT NULL,
            reference TEXT NOT NULL DEFAULT '',
            event_key TEXT UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_ledger_room_item ON inventory_ledger(stockroom_id,item_id,id)")
        conn.execute("""CREATE OR REPLACE FUNCTION record_inventory_ledger() RETURNS trigger AS $$
        DECLARE
            before_state JSONB;
            change_row RECORD;
            event_source TEXT;
            event_reference TEXT;
        BEGIN
            before_state := CASE WHEN TG_OP = 'INSERT' THEN '{}'::jsonb ELSE OLD.state END;
            event_source := NULLIF(current_setting('stockroom.ledger_source', true), '');
            event_reference := COALESCE(current_setting('stockroom.ledger_reference', true), '');
            FOR change_row IN
                SELECT COALESCE(before_item.item_id, after_item.item_id) AS item_id,
                       COALESCE(after_item.item_name, before_item.item_name, '') AS item_name,
                       before_item.stock AS old_stock, after_item.stock AS new_stock,
                       before_item.item_id IS NULL AS created,
                       after_item.item_id IS NULL AS removed
                FROM (
                    SELECT value->>'id' AS item_id, value->>'name' AS item_name,
                           COALESCE((value->>'stock')::numeric, 0) AS stock
                    FROM jsonb_array_elements(COALESCE(before_state->'items', '[]'::jsonb))
                ) before_item
                FULL OUTER JOIN (
                    SELECT value->>'id' AS item_id, value->>'name' AS item_name,
                           COALESCE((value->>'stock')::numeric, 0) AS stock
                    FROM jsonb_array_elements(COALESCE(NEW.state->'items', '[]'::jsonb))
                ) after_item USING (item_id)
                WHERE before_item.item_id IS NULL OR after_item.item_id IS NULL
                   OR before_item.stock IS DISTINCT FROM after_item.stock
            LOOP
                INSERT INTO inventory_ledger(stockroom_id,item_id,item_name,previous_stock,new_stock,delta,source,reference)
                VALUES (NEW.id, change_row.item_id, change_row.item_name,
                        COALESCE(change_row.old_stock, 0), COALESCE(change_row.new_stock, 0),
                        COALESCE(change_row.new_stock, 0) - COALESCE(change_row.old_stock, 0),
                        COALESCE(event_source,
                            CASE WHEN TG_OP = 'INSERT' THEN 'opening_balance'
                                 WHEN change_row.created THEN 'item_created'
                                 WHEN change_row.removed THEN 'item_removed'
                                 ELSE 'stock_change' END),
                        event_reference);
            END LOOP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql""")
        # Lock while seeding and installing the trigger: no stock update can fall between them.
        conn.execute("LOCK TABLE stockrooms IN SHARE ROW EXCLUSIVE MODE")
        conn.execute("""INSERT INTO inventory_ledger
            (stockroom_id,item_id,item_name,previous_stock,new_stock,delta,source,event_key)
            SELECT room.id, item.value->>'id', COALESCE(item.value->>'name',''), 0,
                   COALESCE((item.value->>'stock')::numeric,0),
                   COALESCE((item.value->>'stock')::numeric,0), 'opening_balance',
                   'opening:' || room.id::text || ':' || (item.value->>'id')
            FROM stockrooms room
            CROSS JOIN LATERAL jsonb_array_elements(COALESCE(room.state->'items','[]'::jsonb)) item
            WHERE NOT EXISTS (
                SELECT 1 FROM inventory_ledger existing
                WHERE existing.stockroom_id=room.id AND existing.item_id=item.value->>'id'
            )
            ON CONFLICT (event_key) DO NOTHING""")
        conn.execute("DROP TRIGGER IF EXISTS stockroom_inventory_ledger ON stockrooms")
        conn.execute("""CREATE TRIGGER stockroom_inventory_ledger
            AFTER INSERT OR UPDATE OF state ON stockrooms
            FOR EACH ROW EXECUTE FUNCTION record_inventory_ledger()""")
        conn.commit()


def set_context(conn, source, reference=''):
    conn.execute("SELECT set_config('stockroom.ledger_source', %s, true)", (source,))
    conn.execute("SELECT set_config('stockroom.ledger_reference', %s, true)", (str(reference or '')[:500],))


def rows_for_stockroom(stockroom_id):
    import server
    with server.db() as conn:
        return conn.execute("""SELECT id,item_id,item_name,previous_stock::text,new_stock::text,
                   delta::text,source,reference,created_at
            FROM inventory_ledger WHERE stockroom_id=%s ORDER BY id""", (stockroom_id,)).fetchall()


def rows_for_item(stockroom_id, item_id):
    import server
    with server.db() as conn:
        return conn.execute("""SELECT id,item_id,item_name,previous_stock::text,new_stock::text,
                   delta::text,source,reference,created_at
            FROM inventory_ledger WHERE stockroom_id=%s AND item_id=%s ORDER BY id DESC""",
            (stockroom_id, item_id)).fetchall()


def _number(value):
    try:
        number = Decimal(str(value))
    except (TypeError, InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def reconcile(state, ledger_rows):
    grouped = {}
    for entry in ledger_rows:
        grouped.setdefault(str(entry['item_id']), []).append(entry)
    result = []
    for item in state.get('items', []):
        if item.get('archived'):
            continue
        item_id = str(item.get('id'))
        actual = _number(item.get('stock'))
        history = grouped.get(item_id, [])
        expected = None
        valid = bool(history and history[0]['source'] in ('opening_balance', 'item_created'))
        for entry in history:
            before = _number(entry.get('previous_stock'))
            after = _number(entry.get('new_stock'))
            delta = _number(entry.get('delta'))
            if not valid or before is None or after is None or delta is None or after - before != delta:
                valid = False
                break
            if expected is not None and expected != before:
                valid = False
                break
            expected = after
        difference = actual - expected if valid and actual is not None else None
        result.append({'item_id': item_id, 'name': item.get('name') or 'Artikel',
                       'sku': item.get('sku') or '', 'actual': float(actual) if actual is not None else None,
                       'expected': float(expected) if difference is not None else None,
                       'difference': float(difference) if difference is not None else None,
                       'started_at': history[0]['created_at'] if history else None,
                       'status': 'unavailable' if difference is None else 'difference' if difference != 0 else 'ok'})
    return result

