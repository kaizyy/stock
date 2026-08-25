import json
import uuid

import server


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
        "salesReturn": _role_can(role, "sales_return"),
        "purchaseReturn": _role_can(role, "purchase_return"),
        "transfer": _role_can(role, "transfer"),
    }


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
        conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s", (json.dumps(source_state, ensure_ascii=False), source_id))
        conn.execute("UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s", (json.dumps(dest_state, ensure_ascii=False), destination_id))
        transfer_ref = str(uuid.uuid4())
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
