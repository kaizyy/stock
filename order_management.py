import json
import uuid

import server

PURCHASE_STATUSES = {"draft", "ordered", "partial", "received", "cancelled"}
SALES_STATUSES = {"draft", "processing", "shipped", "completed", "paid", "cancelled"}


def initialize_order_management():
    with server.db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS suppliers (
                id UUID PRIMARY KEY,
                stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                contact_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_suppliers_stockroom_name ON suppliers(stockroom_id,name)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id UUID PRIMARY KEY,
                stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                contact_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_stockroom_name ON customers(stockroom_id,name)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id UUID PRIMARY KEY,
                stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
                order_type TEXT NOT NULL CHECK (order_type IN ('purchase','sales')),
                relation_id UUID,
                relation_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                reference TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                order_date DATE NOT NULL DEFAULT CURRENT_DATE,
                created_by UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_stockroom_type_date ON orders(stockroom_id,order_type,order_date DESC)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS order_lines (
                id UUID PRIMARY KEY,
                order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                item_id TEXT NOT NULL,
                item_name TEXT NOT NULL,
                sku TEXT NOT NULL DEFAULT '',
                quantity NUMERIC(14,3) NOT NULL CHECK (quantity > 0),
                unit_price NUMERIC(14,4) NOT NULL CHECK (unit_price >= 0),
                fulfilled_quantity NUMERIC(14,3) NOT NULL DEFAULT 0 CHECK (fulfilled_quantity >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_lines_order ON order_lines(order_id)")
        conn.commit()


def allowed(role, capability):
    if role in ("owner", "admin", "member"):
        return True
    if role == "viewer":
        return capability.startswith("read_")
    if role == "buyer":
        return capability in {"read_suppliers", "write_suppliers", "read_purchase", "write_purchase"}
    if role == "seller":
        return capability in {"read_customers", "write_customers", "read_sales", "write_sales"}
    return False


def relation_rows(stockroom_id, kind):
    table = "suppliers" if kind == "supplier" else "customers"
    with server.db() as conn:
        return conn.execute(
            f"SELECT id::text,name,contact_name,email,phone,address,notes,created_at,updated_at FROM {table} WHERE stockroom_id=%s ORDER BY lower(name),created_at",
            (stockroom_id,),
        ).fetchall()


def save_relation(session, kind, values):
    table = "suppliers" if kind == "supplier" else "customers"
    name = (values.get("name") or "").strip()
    if not name:
        raise ValueError("Naam is verplicht.")
    relation_id = (values.get("id") or "").strip()
    payload = tuple((values.get(k) or "").strip()[:5000] for k in ("contact_name", "email", "phone", "address", "notes"))
    with server.db() as conn:
        if relation_id:
            row = conn.execute(
                f"UPDATE {table} SET name=%s,contact_name=%s,email=%s,phone=%s,address=%s,notes=%s,updated_at=NOW() WHERE id=%s AND stockroom_id=%s RETURNING id::text",
                (name, *payload, relation_id, session["stockroom_id"]),
            ).fetchone()
            if not row:
                raise PermissionError("Relatie niet gevonden.")
        else:
            relation_id = str(uuid.uuid4())
            conn.execute(
                f"INSERT INTO {table}(id,stockroom_id,name,contact_name,email,phone,address,notes) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (relation_id, session["stockroom_id"], name, *payload),
            )
        conn.execute(
            "INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",
            (session["stockroom_id"], session["user_id"], f"{kind}.saved", json.dumps({"id": relation_id, "name": name})),
        )
        conn.commit()
    return relation_id


def order_rows(stockroom_id, order_type):
    with server.db() as conn:
        rows = conn.execute(
            """SELECT id::text,order_type,relation_id::text,relation_name,status,reference,notes,order_date,created_at,updated_at
               FROM orders WHERE stockroom_id=%s AND order_type=%s ORDER BY order_date DESC,created_at DESC LIMIT 200""",
            (stockroom_id, order_type),
        ).fetchall()
        for order in rows:
            order["lines"] = conn.execute(
                """SELECT id::text,item_id,item_name,sku,quantity::float8,unit_price::float8,fulfilled_quantity::float8
                   FROM order_lines WHERE order_id=%s ORDER BY created_at,id""",
                (order["id"],),
            ).fetchall()
            order["total"] = sum(float(line["quantity"]) * float(line["unit_price"]) for line in order["lines"])
        return rows


def _parse_lines(raw):
    try:
        lines = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("Orderregels zijn ongeldig.") from exc
    if not isinstance(lines, list) or not lines:
        raise ValueError("Voeg minimaal één orderregel toe.")
    normalized = []
    for line in lines[:100]:
        if not isinstance(line, dict):
            raise ValueError("Orderregel is ongeldig.")
        item_id = str(line.get("item_id") or "").strip()
        item_name = str(line.get("item_name") or "").strip()
        sku = str(line.get("sku") or "").strip()
        try:
            qty = float(line.get("quantity"))
            price = float(line.get("unit_price"))
        except (TypeError, ValueError):
            raise ValueError("Aantal of prijs is ongeldig.")
        if not item_id or not item_name or qty <= 0 or price < 0:
            raise ValueError("Controleer de orderregels.")
        normalized.append((item_id, item_name[:200], sku[:100], qty, price))
    return normalized


def create_order(session, values):
    order_type = values.get("order_type")
    if order_type not in ("purchase", "sales"):
        raise ValueError("Ordertype is ongeldig.")
    statuses = PURCHASE_STATUSES if order_type == "purchase" else SALES_STATUSES
    status = values.get("status") or "draft"
    if status not in statuses:
        raise ValueError("Orderstatus is ongeldig.")
    lines = _parse_lines(values.get("lines_json"))
    relation_id = (values.get("relation_id") or "").strip() or None
    relation_name = (values.get("relation_name") or "").strip()
    reference = (values.get("reference") or "").strip()[:120]
    notes = (values.get("notes") or "").strip()[:5000]
    order_date = (values.get("order_date") or "").strip() or None
    order_id = str(uuid.uuid4())
    relation_table = "suppliers" if order_type == "purchase" else "customers"
    with server.db() as conn:
        if relation_id:
            relation = conn.execute(
                f"SELECT name FROM {relation_table} WHERE id=%s AND stockroom_id=%s",
                (relation_id, session["stockroom_id"]),
            ).fetchone()
            if not relation:
                raise PermissionError("Relatie hoort niet bij deze stockroom.")
            relation_name = relation["name"]
        conn.execute(
            """INSERT INTO orders(id,stockroom_id,order_type,relation_id,relation_name,status,reference,notes,order_date,created_by)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s::date,CURRENT_DATE),%s)""",
            (order_id, session["stockroom_id"], order_type, relation_id, relation_name, status, reference, notes, order_date, session["user_id"]),
        )
        for item_id,item_name,sku,qty,price in lines:
            conn.execute(
                "INSERT INTO order_lines(id,order_id,item_id,item_name,sku,quantity,unit_price) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (str(uuid.uuid4()), order_id, item_id, item_name, sku, qty, price),
            )
        conn.execute(
            "INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",
            (session["stockroom_id"], session["user_id"], f"order.{order_type}.created", json.dumps({"id": order_id, "reference": reference, "relation": relation_name, "lines": len(lines)})),
        )
        conn.commit()
    return order_id


def update_order_status(session, expected_type, values):
    order_id = (values.get("order_id") or "").strip()
    status = (values.get("status") or "").strip()
    if expected_type not in ("purchase", "sales"):
        raise ValueError("Ordertype is ongeldig.")
    with server.db() as conn:
        row = conn.execute(
            "SELECT order_type,status FROM orders WHERE id=%s AND stockroom_id=%s FOR UPDATE",
            (order_id, session["stockroom_id"]),
        ).fetchone()
        if not row:
            raise PermissionError("Order niet gevonden.")
        if row["order_type"] != expected_type:
            raise PermissionError("Geen rechten voor dit ordertype.")
        valid = PURCHASE_STATUSES if row["order_type"] == "purchase" else SALES_STATUSES
        if status not in valid:
            raise ValueError("Orderstatus is ongeldig.")
        conn.execute("UPDATE orders SET status=%s,updated_at=NOW() WHERE id=%s", (status, order_id))
        conn.execute(
            "INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",
            (session["stockroom_id"], session["user_id"], "order.status_changed", json.dumps({"id": order_id, "from": row["status"], "to": status, "type": row["order_type"]})),
        )
        conn.commit()
    return row["order_type"]
