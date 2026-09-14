import unittest
from decimal import Decimal

from inventory_ledger import reconcile


def event(before, after, source="stock_change"):
    before, after = Decimal(str(before)), Decimal(str(after))
    return {"item_id": "a", "previous_stock": str(before), "new_stock": str(after),
            "delta": str(after - before), "source": source, "created_at": "2026-09-14T10:00:00Z"}


class InventoryReconciliationTests(unittest.TestCase):
    def test_decimal_changes_and_reversal_are_replayed(self):
        history = [event(0, 10, "opening_balance"), event(10, 10.1), event(10.1, 9.7),
                   event(9.7, 10.1, "order_deleted")]
        state = {"items": [{"id": "a", "name": "Artikel", "stock": 10.1}]}
        result = reconcile(state, history)[0]
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["expected"], 10.1)
        state["items"][0]["stock"] = 10
        result = reconcile(state, history)[0]
        self.assertEqual(result["status"], "difference")
        self.assertAlmostEqual(result["difference"], -0.1)

    def test_missing_baseline_and_broken_chain_are_not_guessed(self):
        state = {"items": [{"id": "a", "stock": 10}, {"id": "b", "stock": 5},
                           {"id": "c", "stock": 1, "archived": True}]}
        rows = reconcile(state, [event(0, 10, "opening_balance")])
        self.assertEqual([row["status"] for row in rows], ["ok", "unavailable"])
        self.assertEqual(reconcile(state, [event(0, 10, "opening_balance"), event(9, 10)])[0]["status"], "unavailable")
        self.assertEqual(reconcile(state, [event(0, 10)])[0]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()

