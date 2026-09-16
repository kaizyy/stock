import json
import uuid

import server
import inventory_ledger


WRITE_ROLES = {"owner", "admin", "member"}


def initialize_warehouse_ops():
    with server.db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS warehouse_operations (
                id UUID PRIMARY KEY,
                stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
                operation_type TEXT NOT NULL CHECK (operation_type IN ('count','sales_return','purchase_return','transfer_out','transfer_in')),
                item_id TEXT NOT NULL,
                item_name TEXT NOT NULL,
                quantity NUMERIC(14,3) NOT NULL,
                previous_stock NUMERIC(14,3),
                new_stock NUMERIC(14,3),
                related_stockroom_id UUID REFERENCES stockrooms(id) ON DELETE SET NULL,
                reference TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_warehouse_ops_room_created ON warehouse_operations(stockroom_id,created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_warehouse_ops_room_item_created ON warehouse_operations(stockroom_id,item_id,created_at DESC)")
        conn.execute("""CREATE TABLE IF NOT EXISTS inventory_counts (
            id UUID PRIMARY KEY, stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
            title TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (status IN ('draft','submitted','approved','cancelled')),
            created_by UUID REFERENCES users(id) ON DELETE SET NULL,
            submitted_by UUID REFERENCES users(id) ON DELETE SET NULL,
            approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), submitted_at TIMESTAMPTZ,
            approved_at TIMESTAMPTZ, cancelled_at TIMESTAMPTZ)
        """)
        conn.execute("""CREATE TABLE IF NOT EXISTS inventory_count_lines (
            count_id UUID NOT NULL REFERENCES inventory_counts(id) ON DELETE CASCADE,
            item_id TEXT NOT NULL, item_name TEXT NOT NULL, sku TEXT NOT NULL DEFAULT '',
            barcode TEXT NOT NULL DEFAULT '', expected_stock NUMERIC(14,3) NOT NULL,
            counted_stock NUMERIC(14,3), buy_price NUMERIC(14,2) NOT NULL DEFAULT 0,
            counted_by UUID REFERENCES users(id) ON DELETE SET NULL, counted_at TIMESTAMPTZ,
            PRIMARY KEY(count_id,item_id))
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_counts_room_created ON inventory_counts(stockroom_id,created_at DESC)")
        conn.commit()


def _role_can(role, capability):
    if capability in {"count", "transfer"}:
        return role in WRITE_ROLES
    if capability == "sales_return":
        return role in WRITE_ROLES or role == "seller"
    if capability == "purchase_return":
        return role in WRITE_ROLES or role == "buyer"
    if capability == "read":
        return role in {"owner", "admin", "member", "buyer", "seller", "viewer"}
    return False


def permissions(role):
    return {
        "read": _role_can(role, "read"),
        "count": _role_can(role, "count"),
        "approveCount": role in {"owner", "admin"},
        "salesReturn": _role_can(role, "sales_return"),
        "purchaseReturn": _role_can(role, "purchase_return"),
        "transfer": _role_can(role, "transfer"),
    }


def count_sessions(stockroom_id):
    with server.db() as conn:
        sessions = conn.execute("""SELECT c.id::text,c.title,c.note,c.status,c.created_at,c.submitted_at,c.approved_at,
                   creator.name created_by_name,approver.name approved_by_name,
                   COUNT(l.*)::int line_count,COUNT(l.counted_stock)::int counted_count,
                   COALESCE(SUM(CASE WHEN l.counted_stock IS NOT NULL THEN (l.counted_stock-l.expected_stock)*l.buy_price ELSE 0 END),0)::float8 variance_value
            FROM inventory_counts c
            LEFT JOIN inventory_count_lines l ON l.count_id=c.id
            LEFT JOIN users creator ON creator.id=c.created_by LEFT JOIN users approver ON approver.id=c.approved_by
            WHERE c.stockroom_id=%s GROUP BY c.id,creator.name,approver.name ORDER BY c.created_at DESC LIMIT 25""",
            (stockroom_id,)).fetchall()
        for count in sessions:
            count["lines"] = conn.execute("""SELECT item_id,item_name,sku,barcode,expected_stock::float8,
                       counted_stock::float8,buy_price::float8,counted_at
                FROM inventory_count_lines WHERE count_id=%s ORDER BY lower(item_name),item_id""", (count["id"],)).fetchall()
        return sessions


def start_count(session, values):
    if not _role_can(session["role"], "count"):
        raise PermissionError("Geen rechten om een telling te starten.")
    title = (values.get("title") or "Voorraadtelling").strip()[:160]
    note = (values.get("note") or "").strip()[:1000]
    with server.db() as conn:
        active = conn.execute("SELECT 1 FROM inventory_counts WHERE stockroom_id=%s AND status IN ('draft','submitted')",
                              (session["stockroom_id"],)).fetchone()
        if active:
            raise ValueError("Er staat al een telling open. Rond die eerst af of annuleer deze.")
        room = conn.execute("SELECT state FROM stockrooms WHERE id=%s", (session["stockroom_id"],)).fetchone()
        items = [item for item in (room or {}).get("state", {}).get("items", []) if not item.get("archived")]
        if not items:
            raise ValueError("Er zijn geen actieve artikelen om te tellen.")
        count_id = str(uuid.uuid4())
        conn.execute("INSERT INTO inventory_counts(id,stockroom_id,title,note,status,created_by) VALUES(%s,%s,%s,%s,'draft',%s)",
                     (count_id, session["stockroom_id"], title, note, session["user_id"]))
        for item in items:
            conn.execute("""INSERT INTO inventory_count_lines(count_id,item_id,item_name,sku,barcode,expected_stock,buy_price)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""", (count_id, str(item.get("id")), item.get("name") or "Artikel",
                str(item.get("sku") or ""), str(item.get("barcode") or ""), float(item.get("stock") or 0), float(item.get("buy") or 0)))
        _audit(conn, session, "inventory_count.started", {"countId": count_id, "title": title, "items": len(items)})
        conn.commit()
    return {"id": count_id}


def save_count_line(session, values):
    if not _role_can(session["role"], "count"):
        raise PermissionError("Geen rechten om te tellen.")
    count_id = (values.get("count_id") or "").strip()
    lookup = (values.get("item_id") or values.get("barcode") or "").strip()
    actual = _number(values.get("counted_stock"), "Geteld aantal", allow_zero=True)
    with server.db() as conn:
        count = conn.execute("SELECT status FROM inventory_counts WHERE id=%s AND stockroom_id=%s FOR UPDATE",
                             (count_id, session["stockroom_id"])).fetchone()
        if not count:
            raise ValueError("Telling niet gevonden.")
        if count["status"] != "draft":
            raise ValueError("Deze telling kan niet meer worden gewijzigd.")
        line = conn.execute("""SELECT item_id FROM inventory_count_lines WHERE count_id=%s
            AND (item_id=%s OR NULLIF(barcode,'')=%s)""", (count_id, lookup, lookup)).fetchone()
        if not line:
            raise ValueError("Artikel of barcode staat niet in deze telling.")
        conn.execute("UPDATE inventory_count_lines SET counted_stock=%s,counted_by=%s,counted_at=NOW() WHERE count_id=%s AND item_id=%s",
                     (actual, session["user_id"], count_id, line["item_id"]))
        conn.commit()
    return {"itemId": line["item_id"], "countedStock": actual}


def submit_count(session, values):
    if not _role_can(session["role"], "count"):
        raise PermissionError("Geen rechten om de telling in te dienen.")
    count_id = (values.get("count_id") or "").strip()
    with server.db() as conn:
        count = conn.execute("SELECT status FROM inventory_counts WHERE id=%s AND stockroom_id=%s FOR UPDATE",
                             (count_id, session["stockroom_id"])).fetchone()
        if not count or count["status"] != "draft":
            raise ValueError("Alleen een open telling kan worden ingediend.")
        missing = conn.execute("SELECT COUNT(*) count FROM inventory_count_lines WHERE count_id=%s AND counted_stock IS NULL", (count_id,)).fetchone()["count"]
        if missing:
            raise ValueError(f"Tel eerst alle artikelen; nog {missing} niet geteld.")
        conn.execute("UPDATE inventory_counts SET status='submitted',submitted_by=%s,submitted_at=NOW() WHERE id=%s", (session["user_id"], count_id))
        _audit(conn, session, "inventory_count.submitted", {"countId": count_id})
        conn.commit()
    return {"submitted": True}


def approve_count(session, values):
    if session["role"] not in {"owner", "admin"}:
        raise PermissionError("Alleen Owner of Admin kan een telling goedkeuren.")
    count_id = (values.get("count_id") or "").strip()
    with server.db() as conn:
        count = conn.execute("SELECT * FROM inventory_counts WHERE id=%s AND stockroom_id=%s FOR UPDATE",
                             (count_id, session["stockroom_id"])).fetchone()
        if not count or count["status"] != "submitted":
            raise ValueError("Alleen een ingediende telling kan worden goedgekeurd.")
        room = conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE", (session["stockroom_id"],)).fetchone()
        state = room["state"]
        lines = conn.execute("SELECT * FROM inventory_count_lines WHERE count_id=%s ORDER BY item_id", (count_id,)).fetchall()
        changed = []
        for line in lines:
            item = _find_item(state, line["item_id"])
            current = float(item.get("stock") or 0) if item else None
            if current is None or abs(current - float(line["expected_stock"])) > 0.0005:
                raise ValueError(f"Voorraad van {line['item_name']} is gewijzigd sinds de telling startte. Annuleer en start een nieuwe telling.")
            actual, difference = float(line["counted_stock"]), float(line["counted_stock"] - line["expected_stock"])
            if abs(difference) <= 0.0005:
                continue
            item["stock"] = actual
            op_id = str(uuid.uuid4())
            conn.execute("""INSERT INTO warehouse_operations(id,stockroom_id,operation_type,item_id,item_name,quantity,previous_stock,new_stock,reference,note,created_by)
                VALUES(%s,%s,'count',%s,%s,%s,%s,%s,%s,%s,%s)""", (op_id, session["stockroom_id"], line["item_id"],
                line["item_name"], difference, float(line["expected_stock"]), actual, count_id, count["note"], session["user_id"]))
            changed.append({"itemId": line["item_id"], "difference": difference, "value": difference * float(line["buy_price"])})
        inventory_ledger.set_context(conn, "stock_count", count_id)
        conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s", (json.dumps(state, ensure_ascii=False), session["stockroom_id"]))
        conn.execute("UPDATE inventory_counts SET status='approved',approved_by=%s,approved_at=NOW() WHERE id=%s", (session["user_id"], count_id))
        _audit(conn, session, "inventory_count.approved", {"countId": count_id, "changes": changed, "varianceValue": sum(row["value"] for row in changed)})
        conn.commit()
    return {"approved": True, "changes": len(changed)}


def cancel_count(session, values):
    if not _role_can(session["role"], "count"):
        raise PermissionError("Geen rechten om de telling te annuleren.")
    count_id = (values.get("count_id") or "").strip()
    with server.db() as conn:
        row = conn.execute("""UPDATE inventory_counts SET status='cancelled',cancelled_at=NOW()
            WHERE id=%s AND stockroom_id=%s AND status IN ('draft','submitted') RETURNING id""",
            (count_id, session["stockroom_id"])).fetchone()
        if not row:
            raise ValueError("Deze telling kan niet worden geannuleerd.")
        _audit(conn, session, "inventory_count.cancelled", {"countId": count_id})
        conn.commit()
    return {"cancelled": True}


def _find_item(state, item_id):
    return next((item for item in state.get("items", []) if str(item.get("id")) == str(item_id)), None)


def _number(value, name, allow_zero=False):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} is ongeldig.")
    if result < 0 or (not allow_zero and result <= 0):
        raise ValueError(f"{name} moet groter dan nul zijn." if not allow_zero else f"{name} mag niet negatief zijn.")
    return result


def _audit(conn, session, action, details):
    conn.execute(
        "INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",
        (session["stockroom_id"], session["user_id"], action, json.dumps(details, ensure_ascii=False)),
    )


def history(stockroom_id, limit=100):
    with server.db() as conn:
        return conn.execute(
            """SELECT id::text,operation_type,item_id,item_name,quantity::float8,
                      previous_stock::float8,new_stock::float8,related_stockroom_id::text,
                      reference,note,created_at
               FROM warehouse_operations WHERE stockroom_id=%s
               ORDER BY created_at DESC LIMIT %s""",
            (stockroom_id, max(1, min(int(limit), 250))),
        ).fetchall()


def history_for_item(stockroom_id, item_id):
    with server.db() as conn:
        return conn.execute(
            """SELECT id::text,operation_type,item_id,item_name,quantity::float8,
                      previous_stock::float8,new_stock::float8,related_stockroom_id::text,
                      reference,note,created_at
               FROM warehouse_operations WHERE stockroom_id=%s AND item_id=%s
               ORDER BY created_at DESC,id DESC""",
            (stockroom_id, item_id),
        ).fetchall()


def transfer_targets(user_id, source_stockroom_id):
    with server.db() as conn:
        return conn.execute(
            """SELECT s.id::text id,s.name,m.role
               FROM memberships m JOIN stockrooms s ON s.id=m.stockroom_id
               WHERE m.user_id=%s AND s.id<>%s AND m.role IN ('owner','admin','member')
               ORDER BY lower(s.name)""",
            (user_id, source_stockroom_id),
        ).fetchall()


def apply_count(session, values):
    if not _role_can(session["role"], "count"):
        raise PermissionError("Geen rechten voor voorraadtelling.")
    item_id = (values.get("item_id") or "").strip()
    actual = _number(values.get("actual_quantity"), "Getelde voorraad", allow_zero=True)
    note = (values.get("note") or "").strip()[:1000]
    with server.db() as conn:
        room = conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE", (session["stockroom_id"],)).fetchone()
        if not room:
            raise PermissionError("Stockroom niet gevonden.")
        state = room["state"]
        item = _find_item(state, item_id)
        if not item:
            raise ValueError("Artikel niet gevonden.")
        previous = float(item.get("stock") or 0)
        item["stock"] = actual
        difference = actual - previous
        inventory_ledger.set_context(conn, "stock_count", note)
        conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s", (json.dumps(state, ensure_ascii=False), session["stockroom_id"]))
        op_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO warehouse_operations(id,stockroom_id,operation_type,item_id,item_name,quantity,previous_stock,new_stock,note,created_by)
               VALUES(%s,%s,'count',%s,%s,%s,%s,%s,%s,%s)""",
            (op_id, session["stockroom_id"], item_id, item.get("name") or "Artikel", difference, previous, actual, note, session["user_id"]),
        )
        _audit(conn, session, "warehouse.count", {"operationId": op_id, "itemId": item_id, "previous": previous, "actual": actual, "difference": difference})
        conn.commit()
    return {"difference": difference, "newStock": actual}


def apply_return(session, values, kind):
    capability = "sales_return" if kind == "sales" else "purchase_return"
    if not _role_can(session["role"], capability):
        raise PermissionError("Geen rechten voor dit type retour.")
    item_id = (values.get("item_id") or "").strip()
    qty = _number(values.get("quantity"), "Aantal")
    price = _number(values.get("price"), "Prijs", allow_zero=True)
    party = (values.get("party") or "").strip()[:200]
    reference = (values.get("reference") or "").strip()[:120]
    note = (values.get("note") or "").strip()[:1000]
    with server.db() as conn:
        room = conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE", (session["stockroom_id"],)).fetchone()
        if not room:
            raise PermissionError("Stockroom niet gevonden.")
        state = room["state"]
        item = _find_item(state, item_id)
        if not item:
            raise ValueError("Artikel niet gevonden.")
        previous = float(item.get("stock") or 0)
        if kind == "sales":
            new_stock = previous + qty
            tx = {"id": str(uuid.uuid4()), "type": "outgoing", "itemId": item_id, "qty": -qty, "price": price, "party": party or "Retour", "done": True, "date": server.datetime.now().isoformat(timespec="seconds"), "isReturn": True, "reference": reference}
            op_type = "sales_return"
        else:
            if previous < qty:
                raise ValueError(f"Onvoldoende voorraad voor inkoopretour: {previous:g} beschikbaar, {qty:g} nodig.")
            new_stock = previous - qty
            tx = {"id": str(uuid.uuid4()), "type": "incoming", "itemId": item_id, "qty": -qty, "price": price, "salePrice": float(item.get("sell") or 0), "party": party or "Retour leverancier", "done": True, "paid": True, "date": server.datetime.now().isoformat(timespec="seconds"), "isReturn": True, "reference": reference}
            op_type = "purchase_return"
        item["stock"] = new_stock
        state.setdefault("transactions", []).append(tx)
        inventory_ledger.set_context(conn, "sales_return" if kind == "sales" else "purchase_return", reference or note)
        conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s", (json.dumps(state, ensure_ascii=False), session["stockroom_id"]))
        op_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO warehouse_operations(id,stockroom_id,operation_type,item_id,item_name,quantity,previous_stock,new_stock,reference,note,created_by)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (op_id, session["stockroom_id"], op_type, item_id, item.get("name") or "Artikel", qty, previous, new_stock, reference, note, session["user_id"]),
        )
        _audit(conn, session, f"warehouse.{op_type}", {"operationId": op_id, "itemId": item_id, "quantity": qty, "price": price, "reference": reference})
        conn.commit()
    return {"newStock": new_stock}


def _destination_item(dest_state, source_item):
    sku = str(source_item.get("sku") or "").strip().lower()
    barcode = str(source_item.get("barcode") or "").strip()
    found = next((i for i in dest_state.get("items", []) if sku and str(i.get("sku") or "").strip().lower() == sku), None)
    if not found and barcode:
        found = next((i for i in dest_state.get("items", []) if str(i.get("barcode") or "").strip() == barcode), None)
    if found:
        return found
    clone = dict(source_item)
    clone["id"] = str(uuid.uuid4())
    clone["stock"] = 0
    clone["archived"] = False
    dest_state.setdefault("items", []).append(clone)
    return clone


def apply_transfer(session, values):
    if not _role_can(session["role"], "transfer"):
        raise PermissionError("Geen rechten voor voorraadtransfers.")
    source_id = session["stockroom_id"]
    destination_id = (values.get("destination_stockroom_id") or "").strip()
    item_id = (values.get("item_id") or "").strip()
    qty = _number(values.get("quantity"), "Aantal")
    note = (values.get("note") or "").strip()[:1000]
    if not destination_id or destination_id == source_id:
        raise ValueError("Kies een andere doel-stockroom.")
    with server.db() as conn:
        membership = conn.execute(
            "SELECT role FROM memberships WHERE user_id=%s AND stockroom_id=%s AND role IN ('owner','admin','member')",
            (session["user_id"], destination_id),
        ).fetchone()
        if not membership:
            raise PermissionError("Je hebt geen schrijfrechten in de doel-stockroom.")
        ids = sorted([source_id, destination_id])
        locked = conn.execute("SELECT id::text,state,name FROM stockrooms WHERE id=ANY(%s::uuid[]) ORDER BY id FOR UPDATE", (ids,)).fetchall()
        if len(locked) != 2:
            raise ValueError("Bron- of doel-stockroom bestaat niet.")
        rooms = {row["id"]: row for row in locked}
        source = rooms[source_id]
        destination = rooms[destination_id]
        source_state = source["state"]
        dest_state = destination["state"]
        source_item = _find_item(source_state, item_id)
        if not source_item:
            raise ValueError("Artikel niet gevonden in de bron-stockroom.")
        previous_source = float(source_item.get("stock") or 0)
        if previous_source < qty:
            raise ValueError(f"Onvoldoende voorraad: {previous_source:g} beschikbaar, {qty:g} nodig.")
        dest_item = _destination_item(dest_state, source_item)
        previous_dest = float(dest_item.get("stock") or 0)
        source_item["stock"] = previous_source - qty
        dest_item["stock"] = previous_dest + qty
        transfer_ref = str(uuid.uuid4())
        inventory_ledger.set_context(conn, "stock_transfer", transfer_ref)
        conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s", (json.dumps(source_state, ensure_ascii=False), source_id))
        conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s", (json.dumps(dest_state, ensure_ascii=False), destination_id))
        out_id, in_id = str(uuid.uuid4()), str(uuid.uuid4())
        conn.execute(
            """INSERT INTO warehouse_operations(id,stockroom_id,operation_type,item_id,item_name,quantity,previous_stock,new_stock,related_stockroom_id,reference,note,created_by)
               VALUES(%s,%s,'transfer_out',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (out_id, source_id, item_id, source_item.get("name") or "Artikel", qty, previous_source, previous_source - qty, destination_id, transfer_ref, note, session["user_id"]),
        )
        conn.execute(
            """INSERT INTO warehouse_operations(id,stockroom_id,operation_type,item_id,item_name,quantity,previous_stock,new_stock,related_stockroom_id,reference,note,created_by)
               VALUES(%s,%s,'transfer_in',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (in_id, destination_id, str(dest_item["id"]), dest_item.get("name") or "Artikel", qty, previous_dest, previous_dest + qty, source_id, transfer_ref, note, session["user_id"]),
        )
        _audit(conn, session, "warehouse.transfer", {"transferRef": transfer_ref, "itemId": item_id, "quantity": qty, "destinationStockroomId": destination_id})
        conn.execute(
            "INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'warehouse.transfer_received',%s::jsonb)",
            (destination_id, session["user_id"], json.dumps({"transferRef": transfer_ref, "sourceStockroomId": source_id, "quantity": qty, "itemId": str(dest_item["id"])})),
        )
        conn.commit()
    return {"transferRef": transfer_ref, "sourceStock": previous_source - qty, "destinationStock": previous_dest + qty}

