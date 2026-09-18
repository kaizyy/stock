import json
import uuid

import server
import inventory_ledger
import purchase_intelligence
import purchase_approvals

PURCHASE_STATUSES = {"draft", "pending_approval", "approved", "rejected", "ordered", "partial", "received", "cancelled"}
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
                inventory_booked_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS inventory_booked_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS expected_delivery_date DATE")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS advice_details JSONB NOT NULL DEFAULT '{}'::jsonb")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS approval_status TEXT NOT NULL DEFAULT 'not_required'")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS approval_reason TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS approved_by UUID REFERENCES users(id) ON DELETE SET NULL")
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ")
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
        conn.execute("""CREATE TABLE IF NOT EXISTS inventory_reservations(
            order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
            item_id TEXT NOT NULL,quantity NUMERIC(14,3) NOT NULL CHECK(quantity>0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(order_id,item_id))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_reservations_room_item ON inventory_reservations(stockroom_id,item_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS quote_reservations(
            quote_id UUID NOT NULL,stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
            item_id TEXT NOT NULL,quantity NUMERIC(14,3) NOT NULL CHECK(quantity>0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(quote_id,item_id))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_quote_reservations_room_item ON quote_reservations(stockroom_id,item_id)")
        conn.commit()
    purchase_approvals.initialize()


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


def relation_row(stockroom_id, kind, relation_id):
    table = "suppliers" if kind == "supplier" else "customers"
    with server.db() as conn:
        return conn.execute(
            f"SELECT id::text,name,contact_name,email,phone,address,notes,created_at,updated_at FROM {table} WHERE id=%s AND stockroom_id=%s",
            (relation_id, stockroom_id),
        ).fetchone()


def save_relation(session, kind, values):
    table = "suppliers" if kind == "supplier" else "customers"
    name = (values.get("name") or "").strip()
    relation_id = (values.get("id") or "").strip()
    payload = tuple((values.get(k) or "").strip()[:5000] for k in ("contact_name", "email", "phone", "address", "notes"))
    if not name:
        name = next((value for value in payload[:3] if value), "")
    if not name and not any(payload):
        raise ValueError("Vul minimaal één relatiegegeven in.")
    if not name:
        name = "Naamloze leverancier" if kind == "supplier" else "Naamloze klant"
    name = name[:200]
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
    if not relation_row(session["stockroom_id"], kind, relation_id):
        raise RuntimeError("Relatie kon niet in de database worden bevestigd.")
    return relation_id


def delete_relation(session, kind, relation_id):
    table = "suppliers" if kind == "supplier" else "customers"
    relation_id = (relation_id or "").strip()
    if not relation_id:
        raise ValueError("Relatie is ongeldig.")
    with server.db() as conn:
        relation = conn.execute(
            f"SELECT name FROM {table} WHERE id=%s AND stockroom_id=%s FOR UPDATE",
            (relation_id, session["stockroom_id"]),
        ).fetchone()
        if not relation:
            raise PermissionError("Relatie niet gevonden.")
        detached = conn.execute(
            "UPDATE orders SET relation_id=NULL,updated_at=NOW() WHERE relation_id=%s AND stockroom_id=%s RETURNING id",
            (relation_id, session["stockroom_id"]),
        ).fetchall()
        conn.execute(f"DELETE FROM {table} WHERE id=%s AND stockroom_id=%s", (relation_id, session["stockroom_id"]))
        conn.execute(
            "INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",
            (session["stockroom_id"], session["user_id"], f"{kind}.deleted", json.dumps({"id": relation_id, "name": relation["name"], "ordersDetached": len(detached)})),
        )
        conn.commit()
    return {"deleted": True, "id": relation_id, "orders_detached": len(detached)}


def order_rows(stockroom_id, order_type):
    with server.db() as conn:
        rows = conn.execute(
            """SELECT o.id::text,o.order_type,o.relation_id::text,o.relation_name,o.status,o.reference,o.notes,o.order_date,o.expected_delivery_date,o.advice_details,
                      o.approval_status,o.approval_reason,o.approved_at,au.name approved_by_name,o.inventory_booked_at,o.created_at,o.updated_at,
                      COALESCE(s.email,c.email,'') relation_email
               FROM orders o
               LEFT JOIN suppliers s ON o.order_type='purchase' AND s.id=o.relation_id AND s.stockroom_id=o.stockroom_id
               LEFT JOIN customers c ON o.order_type='sales' AND c.id=o.relation_id AND c.stockroom_id=o.stockroom_id
               LEFT JOIN users au ON au.id=o.approved_by
               WHERE o.stockroom_id=%s AND o.order_type=%s ORDER BY o.order_date DESC,o.created_at DESC LIMIT 200""",
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


def open_purchase_quantities(stockroom_id):
    with server.db() as conn:
        rows = conn.execute("""SELECT l.item_id,COALESCE(SUM(l.quantity-l.fulfilled_quantity),0)::float8 quantity
            FROM order_lines l JOIN orders o ON o.id=l.order_id
            WHERE o.stockroom_id=%s AND o.order_type='purchase' AND o.status IN ('draft','ordered','partial')
            GROUP BY l.item_id""", (stockroom_id,)).fetchall()
    return {str(row["item_id"]): float(row["quantity"] or 0) for row in rows}


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


def _sync_sales_reservations(conn, stockroom_id, order_id, lines, active=True):
    conn.execute("DELETE FROM inventory_reservations WHERE order_id=%s AND stockroom_id=%s", (order_id, stockroom_id))
    if not active:return
    room=conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE",(stockroom_id,)).fetchone()
    state=(room or {}).get("state") or {"items":[]}
    for item_id,item_name,sku,qty,price in lines:
        item=_find_state_item(state,item_id);stock=float((item or {}).get("stock") or 0)
        reserved=conn.execute("SELECT COALESCE((SELECT SUM(quantity) FROM inventory_reservations WHERE stockroom_id=%s AND item_id=%s),0)::float8+COALESCE((SELECT SUM(quantity) FROM quote_reservations WHERE stockroom_id=%s AND item_id=%s),0)::float8 quantity",(stockroom_id,item_id,stockroom_id,item_id)).fetchone()["quantity"]
        if not item or stock-float(reserved or 0)<qty:raise ValueError(f"Onvoldoende vrije voorraad voor {item_name}: {max(0,stock-float(reserved or 0)):g} beschikbaar.")
        conn.execute("INSERT INTO inventory_reservations(order_id,stockroom_id,item_id,quantity) VALUES(%s,%s,%s,%s)",(order_id,stockroom_id,item_id,qty))


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
        if order_type=="sales":_sync_sales_reservations(conn,session["stockroom_id"],order_id,lines,status!="cancelled")
        conn.execute(
            "INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",
            (session["stockroom_id"], session["user_id"], f"order.{order_type}.created", json.dumps({"id": order_id, "reference": reference, "relation": relation_name, "lines": len(lines)})),
        )
        conn.commit()
    return order_id


def create_purchase_advice_drafts(session, values):
    if not allowed(session["role"], "write_purchase"):
        raise PermissionError("Geen rechten om inkooporders te maken.")
    try:
        requested = json.loads(values.get("lines_json") or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("Besteladvies is ongeldig.") from exc
    if not isinstance(requested, list) or not requested:
        raise ValueError("Selecteer minimaal één besteladvies.")
    requested_by_id = {}
    for row in requested[:100]:
        try:
            item_id, quantity = str(row.get("item_id") or "").strip(), float(row.get("quantity"))
        except (AttributeError, TypeError, ValueError):
            raise ValueError("Controleer de geselecteerde aantallen.")
        if not item_id or quantity <= 0:
            raise ValueError("Ieder geselecteerd aantal moet groter dan nul zijn.")
        requested_by_id[item_id] = {'quantity':quantity,'supplier_id':str(row.get('supplier_id') or '').strip() or None,'override_reason':str(row.get('override_reason') or '').strip()[:500]}
    intelligence=purchase_intelligence.overview(session['stockroom_id']);recommendations={row['itemId']:row for row in intelligence['recommendations']};supplier_metrics={str(row['id']):row for row in intelligence['suppliers'] if row['id']}
    created = []
    with server.db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("purchase-advice:" + session["stockroom_id"],))
        room = conn.execute("SELECT state FROM stockrooms WHERE id=%s", (session["stockroom_id"],)).fetchone()
        state = (room or {}).get("state") or {"items": []}
        items = {str(item.get("id")): item for item in state.get("items", []) if not item.get("archived")}
        open_rows = conn.execute("""SELECT l.item_id,COALESCE(SUM(l.quantity-l.fulfilled_quantity),0)::float8 quantity
            FROM order_lines l JOIN orders o ON o.id=l.order_id
            WHERE o.stockroom_id=%s AND o.order_type='purchase' AND o.status IN ('draft','ordered','partial')
            GROUP BY l.item_id""", (session["stockroom_id"],)).fetchall()
        open_by_id = {str(row["item_id"]): float(row["quantity"] or 0) for row in open_rows}
        suppliers = conn.execute("SELECT id::text,name FROM suppliers WHERE stockroom_id=%s", (session["stockroom_id"],)).fetchall()
        supplier_by_name = {row["name"].strip().casefold(): row for row in suppliers}
        groups = {}
        skipped = []
        for item_id, selection in requested_by_id.items():
            item = items.get(item_id)
            if not item:
                skipped.append(item_id)
                continue
            quantity = max(0, selection['quantity'] - open_by_id.get(item_id, 0))
            if quantity <= 0:
                skipped.append(item_id)
                continue
            advice=recommendations.get(item_id) or {};recommended=advice.get('recommended') or {};supplier=None
            if selection['supplier_id']:supplier=next((row for row in suppliers if row['id']==selection['supplier_id']),None)
            elif recommended.get('supplierId'):supplier=next((row for row in suppliers if row['id']==str(recommended['supplierId'])),None)
            supplier_name=(supplier or {}).get('name') or recommended.get('supplierName') or str(item.get("supplier") or "").strip()
            if not supplier and supplier_name:supplier=supplier_by_name.get(supplier_name.casefold())
            if selection['supplier_id'] and not supplier:raise PermissionError('Geselecteerde leverancier hoort niet bij deze stockroom.')
            if supplier and recommended.get('supplierId') and supplier['id']!=str(recommended['supplierId']) and not selection['override_reason']:raise ValueError(f"Vul een reden in om voor {item.get('name') or 'dit artikel'} af te wijken van de geadviseerde leverancier.")
            key = supplier["id"] if supplier else "name:" + (supplier_name or "Niet gekoppeld")
            metric=supplier_metrics.get((supplier or {}).get('id'));lead_days=max(1,round(float((metric or {}).get('avgLeadDays') or 14)))
            chosen=next((option for option in advice.get('alternatives',[]) if str(option.get('supplierId') or '')==str((supplier or {}).get('id') or '')),None)
            if chosen is None and (not supplier or str((supplier or {}).get('id') or '')==str(recommended.get('supplierId') or '')):chosen=recommended
            chosen=chosen or {}
            price=float((chosen or {}).get('latestPrice') or item.get('buy') or 0);change=(chosen or {}).get('priceChange');warning=bool(change is not None and change>=10)
            group = groups.setdefault(key, {"relation_id": supplier["id"] if supplier else None,
                "relation_name": supplier["name"] if supplier else supplier_name or "Niet gekoppeld", "lead_days":lead_days,"warnings":[],"overrides":[],"lines": []})
            if warning:group['warnings'].append(f"{item.get('name') or 'Artikel'}: prijs {change:+g}%")
            if selection['override_reason']:group['overrides'].append({'itemId':item_id,'reason':selection['override_reason']})
            group["lines"].append((item_id, item.get("name") or "Artikel", item.get("sku") or "", quantity, price))
        if not groups:
            raise ValueError("Voor deze selectie staat al voldoende open op concept- of inkooporders.")
        reference = "Besteladvies " + server.date.today().isoformat()
        for group in groups.values():
            order_id = str(uuid.uuid4())
            details={'source':'purchase_advice','supplierScore':(supplier_metrics.get(str(group['relation_id'])) or {}).get('score'),'priceWarnings':group['warnings'],'overrides':group['overrides']}
            notes="Automatisch concept vanuit voorraadprognose."+(" Prijscontrole nodig: "+"; ".join(group['warnings']) if group['warnings'] else "")
            conn.execute("""INSERT INTO orders(id,stockroom_id,order_type,relation_id,relation_name,status,reference,notes,order_date,expected_delivery_date,advice_details,created_by)
                VALUES(%s,%s,'purchase',%s,%s,'draft',%s,%s,CURRENT_DATE,CURRENT_DATE+(%s * INTERVAL '1 day'),%s::jsonb,%s)""", (order_id, session["stockroom_id"],
                group["relation_id"], group["relation_name"], reference, notes,group['lead_days'],json.dumps(details), session["user_id"]))
            for item_id, item_name, sku, quantity, price in group["lines"]:
                conn.execute("INSERT INTO order_lines(id,order_id,item_id,item_name,sku,quantity,unit_price) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                             (str(uuid.uuid4()), order_id, item_id, item_name, sku, quantity, price))
            approval=purchase_approvals.evaluate(conn,session['stockroom_id'],order_id)
            if approval['required']:conn.execute("UPDATE orders SET status='pending_approval',approval_status='pending',approval_reason=%s WHERE id=%s",('; '.join(approval['reasons']),order_id))
            created.append({"id": order_id, "supplier": group["relation_name"], "lines": len(group["lines"]),"expectedDeliveryDays":group['lead_days'],"priceWarnings":group['warnings'],"approvalRequired":approval['required'],"approvalReasons":approval['reasons']})
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase_advice.drafts_created',%s::jsonb)",
                     (session["stockroom_id"], session["user_id"], json.dumps({"orders": created, "skippedItems": skipped})))
        conn.commit()
    return {"created": created, "skipped": len(skipped)}


def _validate_invoice_balance(conn, stockroom_id, order_id, lines):
    invoice = conn.execute(
        "SELECT vat_percent::float8,paid_amount::float8 FROM invoice_documents WHERE order_id=%s AND stockroom_id=%s AND deleted_at IS NULL",
        (order_id, stockroom_id),
    ).fetchone()
    if not invoice:
        return
    credited = conn.execute(
        "SELECT COALESCE(SUM(amount),0)::float8 amount FROM credit_notes WHERE order_id=%s AND stockroom_id=%s",
        (order_id, stockroom_id),
    ).fetchone()
    total = round(sum(qty * price for _, _, _, qty, price in lines) * (1 + float(invoice["vat_percent"] or 0) / 100), 2)
    settled = float(invoice["paid_amount"] or 0) + float((credited or {}).get("amount") or 0)
    if total + 0.01 < settled:
        raise ValueError("Het nieuwe factuurbedrag mag niet lager zijn dan het reeds betaalde en gecrediteerde bedrag.")


def _reverse_inventory_booking(conn, session, order, old_lines, state):
    transactions = [t for t in state.get("transactions", []) if str(t.get("orderId")) == str(order["id"])]
    for line in old_lines:
        item = _find_state_item(state, line["item_id"])
        if not item:
            raise ValueError(f"Artikel bestaat niet meer: {line['item_name']}")
        qty = float(line["quantity"])
        if order["order_type"] == "purchase":
            if float(item.get("stock") or 0) < qty:
                raise ValueError(f"De geboekte voorraad van {line['item_name']} is al gebruikt; pas deze order daarom niet aan.")
            item["stock"] = float(item.get("stock") or 0) - qty
        else:
            item["stock"] = float(item.get("stock") or 0) + qty
    state["transactions"] = [t for t in state.get("transactions", []) if str(t.get("orderId")) != str(order["id"])]
    return transactions


def update_order(session, values):
    order_id = (values.get("order_id") or "").strip()
    expected_type = (values.get("order_type") or "").strip()
    if not order_id or expected_type not in ("purchase", "sales"):
        raise ValueError("Order is ongeldig.")
    lines = _parse_lines(values.get("lines_json"))
    relation_id = (values.get("relation_id") or "").strip() or None
    relation_name = (values.get("relation_name") or "").strip()
    reference = (values.get("reference") or "").strip()[:120]
    notes = (values.get("notes") or "").strip()[:5000]
    order_date = (values.get("order_date") or "").strip() or None
    relation_table = "suppliers" if expected_type == "purchase" else "customers"

    with server.db() as conn:
        order = conn.execute(
            """SELECT id,stockroom_id,order_type,status,reference,relation_name,order_date,inventory_booked_at
               FROM orders WHERE id=%s AND stockroom_id=%s FOR UPDATE""",
            (order_id, session["stockroom_id"]),
        ).fetchone()
        if not order or order["order_type"] != expected_type:
            raise PermissionError("Order niet gevonden.")
        if relation_id:
            relation = conn.execute(
                f"SELECT name FROM {relation_table} WHERE id=%s AND stockroom_id=%s",
                (relation_id, session["stockroom_id"]),
            ).fetchone()
            if not relation:
                raise PermissionError("Relatie hoort niet bij deze stockroom.")
            relation_name = relation["name"]

        _validate_invoice_balance(conn, session["stockroom_id"], order_id, lines)
        old_lines = conn.execute(
            """SELECT item_id,item_name,sku,quantity::float8,unit_price::float8
               FROM order_lines WHERE order_id=%s ORDER BY created_at,id""",
            (order_id,),
        ).fetchall()
        if expected_type == "purchase" and conn.execute(
            "SELECT 1 FROM purchase_receipts WHERE order_id=%s LIMIT 1", (order_id,)
        ).fetchone():
            raise ValueError("Deze inkooporder heeft ontvangsthistorie en kan daarom niet meer worden bewerkt.")
        returns_ready = conn.execute("SELECT to_regclass('public.order_returns') IS NOT NULL AS ready").fetchone()["ready"]
        if returns_ready and conn.execute("SELECT 1 FROM order_returns WHERE order_id=%s LIMIT 1", (order_id,)).fetchone():
            raise ValueError("Deze order heeft retourhistorie en kan daarom niet meer worden bewerkt.")
        room = None
        state = None
        if order["inventory_booked_at"] is not None:
            room = conn.execute("SELECT state FROM stockrooms WHERE id=%s FOR UPDATE", (session["stockroom_id"],)).fetchone()
            if not room:
                raise PermissionError("Stockroom niet gevonden.")
            state = room["state"] or {"items": [], "transactions": []}
            _reverse_inventory_booking(conn, session, order, old_lines, state)

        conn.execute(
            """UPDATE orders SET relation_id=%s,relation_name=%s,reference=%s,notes=%s,
                      order_date=COALESCE(%s::date,order_date),updated_at=NOW() WHERE id=%s""",
            (relation_id, relation_name, reference, notes, order_date, order_id),
        )
        conn.execute("DELETE FROM order_lines WHERE order_id=%s", (order_id,))
        for item_id, item_name, sku, qty, price in lines:
            conn.execute(
                "INSERT INTO order_lines(id,order_id,item_id,item_name,sku,quantity,unit_price) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (str(uuid.uuid4()), order_id, item_id, item_name, sku, qty, price),
            )

        if expected_type=="sales" and order["inventory_booked_at"] is None:_sync_sales_reservations(conn,session["stockroom_id"],order_id,lines,order["status"]!="cancelled")

        if room is not None:
            updated_order = dict(order)
            updated_order.update(relation_name=relation_name, order_date=order_date or str(order["order_date"]))
            new_lines = [dict(item_id=i, item_name=n, sku=s, quantity=q, unit_price=p) for i, n, s, q, p in lines]
            _book_inventory(conn, session, updated_order, new_lines, state)
            if expected_type == "sales" and order["status"] == "paid":
                conn.execute(
                    "UPDATE stockrooms SET state=jsonb_set(state,'{transactions}',(SELECT jsonb_agg(CASE WHEN tx->>'orderId'=%s THEN tx||'{\"done\":true}'::jsonb ELSE tx END) FROM jsonb_array_elements(state->'transactions') tx)) WHERE id=%s",
                    (order_id, session["stockroom_id"]),
                )

        conn.execute(
            "INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",
            (session["stockroom_id"], session["user_id"], "order.updated", json.dumps({"id": order_id, "type": expected_type, "lines": len(lines), "invoiceSynced": True})),
        )
        conn.commit()
    return order_id


def _find_state_item(state, item_id):
    return next((item for item in state.get("items", []) if str(item.get("id")) == str(item_id)), None)


def _book_inventory(conn, session, order, lines, state):
    order_type = order["order_type"]
    if order_type == "sales":
        shortages = []
        for line in lines:
            item = _find_state_item(state, line["item_id"])
            available = float((item or {}).get("stock") or 0)
            needed = float(line["quantity"])
            if not item or available < needed:
                shortages.append(f"{line['item_name']} ({available:g} beschikbaar, {needed:g} nodig)")
        if shortages:
            raise ValueError("Onvoldoende voorraad: " + "; ".join(shortages))

    for line in lines:
        item = _find_state_item(state, line["item_id"])
        if not item:
            raise ValueError(f"Artikel bestaat niet meer: {line['item_name']}")
        qty = float(line["quantity"])
        price = float(line["unit_price"])
        if order_type == "purchase":
            item["stock"] = float(item.get("stock") or 0) + qty
            item["buy"] = price
            tx = {
                "id": str(uuid.uuid4()),
                "type": "incoming",
                "itemId": str(line["item_id"]),
                "qty": qty,
                "price": price,
                "salePrice": float(item.get("sell") or 0),
                "party": order["relation_name"],
                "done": True,
                "paid": False,
                "date": f"{order['order_date']}T12:00:00",
                "orderId": str(order["id"]),
            }
        else:
            item["stock"] = float(item.get("stock") or 0) - qty
            tx = {
                "id": str(uuid.uuid4()),
                "type": "outgoing",
                "itemId": str(line["item_id"]),
                "qty": qty,
                "price": price,
                "party": order["relation_name"],
                "done": False,
                "date": f"{order['order_date']}T12:00:00",
                "orderId": str(order["id"]),
            }
        state.setdefault("transactions", []).append(tx)

    inventory_ledger.set_context(conn, f"{order_type}_order_booking", order["reference"] or str(order["id"]))
    conn.execute(
        "UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s",
        (json.dumps(state, ensure_ascii=False), session["stockroom_id"]),
    )
    conn.execute(
        "UPDATE order_lines SET fulfilled_quantity=quantity WHERE order_id=%s",
        (order["id"],),
    )
    conn.execute(
        "UPDATE orders SET inventory_booked_at=NOW() WHERE id=%s AND inventory_booked_at IS NULL",
        (order["id"],),
    )
    conn.execute(
        "INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",
        (
            session["stockroom_id"],
            session["user_id"],
            f"order.{order_type}.inventory_booked",
            json.dumps({"id": str(order["id"]), "reference": order["reference"], "lines": len(lines)}),
        ),
    )


def update_order_status(session, expected_type, values):
    order_id = (values.get("order_id") or "").strip()
    status = (values.get("status") or "").strip()
    if expected_type not in ("purchase", "sales"):
        raise ValueError("Ordertype is ongeldig.")

    with server.db() as conn:
        order = conn.execute(
            """SELECT id,stockroom_id,order_type,status,reference,relation_name,order_date,inventory_booked_at
               FROM orders WHERE id=%s AND stockroom_id=%s FOR UPDATE""",
            (order_id, session["stockroom_id"]),
        ).fetchone()
        if not order:
            raise PermissionError("Order niet gevonden.")
        if order["order_type"] != expected_type:
            raise PermissionError("Geen rechten voor dit ordertype.")

        valid = PURCHASE_STATUSES if expected_type == "purchase" else SALES_STATUSES
        if status not in valid:
            raise ValueError("Orderstatus is ongeldig.")
        if expected_type=='purchase' and status in ('pending_approval','approved','rejected'):
            raise ValueError('Gebruik de goedkeuringsknoppen om deze status te wijzigen.')
        if expected_type=='purchase' and status=='ordered' and order['status'] in ('draft','pending_approval','rejected'):
            approval=purchase_approvals.evaluate(conn,session['stockroom_id'],order_id)
            approval_state=conn.execute("SELECT approval_status FROM orders WHERE id=%s",(order_id,)).fetchone()['approval_status']
            if approval['required'] and approval_state!='approved':
                conn.execute("UPDATE orders SET status='pending_approval',approval_status='pending',approval_reason=%s,updated_at=NOW() WHERE id=%s",('; '.join(approval['reasons']),order_id))
                conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'purchase.approval_requested',%s::jsonb)",(session['stockroom_id'],session['user_id'],json.dumps({'id':order_id,'reasons':approval['reasons']})));conn.commit();return expected_type

        already_booked = order["inventory_booked_at"] is not None
        if already_booked:
            if expected_type == "purchase":
                remaining = conn.execute("SELECT COALESCE(SUM(quantity-fulfilled_quantity),0)::float8 remaining FROM order_lines WHERE order_id=%s", (order_id,)).fetchone()["remaining"]
                if status != order["status"] or (status == "received" and remaining > 0.0005):
                    raise ValueError("De orderstatus wordt automatisch bepaald door de geregistreerde ontvangsten.")
            if expected_type == "sales" and status not in {"completed", "paid"}:
                raise ValueError("Deze verkooporder is al uit voorraad geboekt en kan niet meer naar een eerdere status.")

        should_book = (
            not already_booked
            and ((expected_type == "purchase" and status == "received") or (expected_type == "sales" and status == "completed"))
        )

        if should_book:
            room = conn.execute(
                "SELECT state FROM stockrooms WHERE id=%s FOR UPDATE",
                (session["stockroom_id"],),
            ).fetchone()
            if not room:
                raise PermissionError("Stockroom niet gevonden.")
            lines = conn.execute(
                """SELECT item_id,item_name,sku,quantity::float8,unit_price::float8
                   FROM order_lines WHERE order_id=%s ORDER BY created_at,id""",
                (order_id,),
            ).fetchall()
            if not lines:
                raise ValueError("Order bevat geen orderregels.")
            state = room["state"] or {"items": [], "transactions": []}
            _book_inventory(conn, session, order, lines, state)
            if expected_type=="sales":conn.execute("DELETE FROM inventory_reservations WHERE order_id=%s",(order_id,))

        if expected_type=="sales" and not already_booked and not should_book:
            if status=="cancelled":conn.execute("DELETE FROM inventory_reservations WHERE order_id=%s",(order_id,))
            elif order["status"]=="cancelled":
                lines=conn.execute("SELECT item_id,item_name,sku,quantity::float8,unit_price::float8 FROM order_lines WHERE order_id=%s",(order_id,)).fetchall()
                _sync_sales_reservations(conn,session["stockroom_id"],order_id,[(x['item_id'],x['item_name'],x['sku'],x['quantity'],x['unit_price']) for x in lines],True)

        conn.execute("UPDATE orders SET status=%s,updated_at=NOW() WHERE id=%s", (status, order_id))
        conn.execute(
            "INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",
            (
                session["stockroom_id"],
                session["user_id"],
                "order.status_changed",
                json.dumps({"id": order_id, "from": order["status"], "to": status, "type": expected_type, "inventoryBooked": should_book}),
            ),
        )
        conn.commit()
    return expected_type
