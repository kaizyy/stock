"""Indicative stock reconciliation from the latest physical count per item."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def _number(value):
    try:
        result = Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _date(value):
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _transaction_delta(transaction):
    quantity = _number(transaction.get("qty"))
    if quantity is None:
        return None
    kind = transaction.get("type")
    if kind == "adjustment":
        return quantity
    if kind == "incoming":
        return quantity if transaction.get("done") else Decimal(0)
    if kind == "outgoing":
        return -quantity
    return Decimal(0)


def reconcile(state, operations):
    """Return one read-only row per active item; no count means no estimated balance."""
    counts = {}
    transfers = {}
    for operation in operations:
        item_id = str(operation.get("item_id"))
        kind = operation.get("operation_type")
        moment = _date(operation.get("created_at"))
        if not moment:
            continue
        if kind == "count" and _number(operation.get("new_stock")) is not None:
            if item_id not in counts or moment > counts[item_id][0]:
                counts[item_id] = (moment, _number(operation["new_stock"]))
        elif kind in ("transfer_in", "transfer_out"):
            transfers.setdefault(item_id, []).append((moment, operation))

    transactions = {}
    for transaction in state.get("transactions", []):
        transactions.setdefault(str(transaction.get("itemId")), []).append(transaction)

    rows = []
    for item in state.get("items", []):
        if item.get("archived"):
            continue
        item_id = str(item.get("id"))
        actual = _number(item.get("stock"))
        anchor = counts.get(item_id)
        row = {"item_id": item_id, "name": item.get("name") or "Artikel", "sku": item.get("sku") or "",
               "actual": float(actual) if actual is not None else None,
               "expected": None, "difference": None, "counted_at": anchor[0].isoformat() if anchor else None,
               "status": "unavailable"}
        if not anchor or actual is None:
            rows.append(row)
            continue
        counted_at, expected = anchor
        unreliable = False
        for moment, operation in transfers.get(item_id, []):
            if moment <= counted_at:
                continue
            before = _number(operation.get("previous_stock"))
            after = _number(operation.get("new_stock"))
            if before is None or after is None:
                unreliable = True
                break
            expected += after - before
        for transaction in transactions.get(item_id, []):
            moment = _date(transaction.get("date"))
            delta = _transaction_delta(transaction)
            if moment is None or delta is None:
                unreliable = True
                break
            if moment > counted_at:
                expected += delta
        if not unreliable:
            difference = actual - expected
            row.update(expected=float(expected), difference=float(difference),
                       status="difference" if abs(difference) >= Decimal("0.0005") else "ok")
        rows.append(row)
    return rows

