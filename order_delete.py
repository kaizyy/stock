import json

import server


def _find_item(state, item_id):
    return next((item for item in state.get("items", []) if str(item.get("id")) == str(item_id)), None)


def delete_order(session, expected_type, values):
    order_id = (values.get("order_id") or "").strip()
    if expected_type not in ("purchase", "sales") or not order_id:
        raise ValueError("Ongeldige order.")

    with server.db() as conn:
        order = conn.execute(
            """SELECT id,stockroom_id,order_type,reference,inventory_booked_at
               FROM orders WHERE id=%s AND stockroom_id=%s FOR UPDATE""",
            (order_id, session["stockroom_id"]),
        ).fetchone()
        if not order:
            raise PermissionError("Order niet gevonden.")
        if order["order_type"] != expected_type:
            raise PermissionError("Geen rechten voor dit ordertype.")

        reversed_inventory = False
        if order["inventory_booked_at"] is not None:
            room = conn.execute(
                "SELECT state FROM stockrooms WHERE id=%s FOR UPDATE",
                (session["stockroom_id"],),
            ).fetchone()
            if not room:
                raise PermissionError("Stockroom niet gevonden.")
            state = room["state"] or {"items": [], "transactions": []}
            lines = conn.execute(
                "SELECT item_id,item_name,quantity::float8 FROM order_lines WHERE order_id=%s ORDER BY created_at,id",
                (order_id,),
            ).fetchall()

            if expected_type == "purchase":
                shortages = []
                for line in lines:
                    item = _find_item(state, line["item_id"])
                    available = float((item or {}).get("stock") or 0)
                    qty = float(line["quantity"])
                    if not item or available < qty:
                        shortages.append(f"{line['item_name']} ({available:g} beschikbaar, {qty:g} terug te draaien)")
                if shortages:
                    raise ValueError("Order kan niet worden verwijderd omdat de ontvangen voorraad inmiddels niet meer volledig aanwezig is: " + "; ".join(shortages))
                for line in lines:
                    item = _find_item(state, line["item_id"])
                    item["stock"] = float(item.get("stock") or 0) - float(line["quantity"])
            else:
                for line in lines:
                    item = _find_item(state, line["item_id"])
                    if not item:
                        raise ValueError(f"Artikel bestaat niet meer: {line['item_name']}")
                    item["stock"] = float(item.get("stock") or 0) + float(line["quantity"])

            state["transactions"] = [
                tx for tx in state.get("transactions", [])
                if str(tx.get("orderId") or "") != order_id
            ]
            conn.execute(
                "UPDATE stockrooms SET state=%s::jsonb,updated_at=NOW() WHERE id=%s",
                (json.dumps(state, ensure_ascii=False), session["stockroom_id"]),
            )
            reversed_inventory = True

        conn.execute("DELETE FROM orders WHERE id=%s AND stockroom_id=%s", (order_id, session["stockroom_id"]))
        conn.execute(
            "INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,%s,%s::jsonb)",
            (
                session["stockroom_id"],
                session["user_id"],
                f"order.{expected_type}.deleted",
                json.dumps({
                    "id": order_id,
                    "reference": order["reference"],
                    "inventoryReversed": reversed_inventory,
                }),
            ),
        )
        conn.commit()
    return {"deleted": True, "inventoryReversed": reversed_inventory}
