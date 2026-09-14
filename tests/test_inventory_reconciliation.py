import unittest

from inventory_reconciliation import reconcile


class InventoryReconciliationTests(unittest.TestCase):
    def test_replays_decimal_mutations_after_latest_count(self):
        state = {"items": [{"id": "a", "name": "Artikel", "stock": 9.7}], "transactions": [
            {"itemId": "a", "type": "incoming", "qty": 0.1, "done": True, "date": "2026-09-14T10:00:00Z"},
            {"itemId": "a", "type": "incoming", "qty": 3, "done": False, "date": "2026-09-14T10:00:00Z"},
            {"itemId": "a", "type": "outgoing", "qty": 0.2, "date": "2026-09-14T11:00:00Z"},
            {"itemId": "a", "type": "adjustment", "qty": 0.1, "date": "2026-09-14T12:00:00Z"},
            {"itemId": "a", "type": "incoming", "qty": 7, "done": True, "date": "2026-09-12T10:00:00Z"},
        ]}
        operations = [
            {"item_id": "a", "operation_type": "count", "new_stock": 8, "created_at": "2026-09-11T10:00:00Z"},
            {"item_id": "a", "operation_type": "count", "new_stock": 10, "created_at": "2026-09-13T10:00:00Z"},
            {"item_id": "a", "operation_type": "transfer_out", "previous_stock": 10, "new_stock": 9.7, "created_at": "2026-09-14T13:00:00Z"},
        ]
        result = reconcile(state, operations)[0]
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["expected"], 9.7)
        self.assertAlmostEqual(result["difference"], 0)
        state["items"][0]["stock"] = 9.6
        difference = reconcile(state, operations)[0]
        self.assertEqual(difference["status"], "difference")
        self.assertAlmostEqual(difference["difference"], -0.1)

    def test_balanced_and_unavailable_items(self):
        state = {"items": [
            {"id": "a", "name": "Geteld", "stock": 10.1},
            {"id": "b", "name": "Niet geteld", "stock": 5},
            {"id": "c", "name": "Archief", "stock": 1, "archived": True},
        ], "transactions": [{"itemId": "a", "type": "incoming", "qty": 0.1, "done": True, "date": "2026-09-14T10:00:00Z"}]}
        operations = [{"item_id": "a", "operation_type": "count", "new_stock": 10,
                       "created_at": "2026-09-13T10:00:00Z"}]
        rows = reconcile(state, operations)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "ok")
        self.assertAlmostEqual(rows[0]["difference"], 0)
        self.assertEqual(rows[1]["status"], "unavailable")

    def test_invalid_history_does_not_raise_false_alarm(self):
        state = {"items": [{"id": "a", "stock": 9}], "transactions": [
            {"itemId": "a", "type": "outgoing", "qty": 1, "date": "onbekend"},
        ]}
        operations = [{"item_id": "a", "operation_type": "count", "new_stock": 10,
                       "created_at": "2026-09-13T10:00:00Z"}]
        self.assertEqual(reconcile(state, operations)[0]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()

