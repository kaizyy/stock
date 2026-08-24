import unittest
from datetime import datetime, timezone

import dashboard_runner
import server


def state(stock, minimum):
    return {"items": [{"id": "item-1", "name": "Filter", "stock": stock, "minStock": minimum}], "transactions": []}


class LowStockTests(unittest.TestCase):
    def test_notifies_when_stock_reaches_minimum(self):
        self.assertEqual(
            dashboard_runner.low_stock_transitions(state(6, 5), state(5, 5)),
            [{"id": "item-1", "name": "Filter", "stock": 5, "minimum": 5}],
        )

    def test_does_not_repeat_while_stock_remains_low(self):
        self.assertEqual(dashboard_runner.low_stock_transitions(state(5, 5), state(4, 5)), [])

    def test_setting_minimum_above_current_stock_triggers_notification(self):
        self.assertEqual(len(dashboard_runner.low_stock_transitions(state(4, 0), state(4, 5))), 1)

    def test_json_default_serializes_database_timestamps(self):
        value = datetime(2026, 8, 24, 12, 30, tzinfo=timezone.utc)
        self.assertEqual(server.json_default(value), "2026-08-24T12:30:00+00:00")


if __name__ == "__main__":
    unittest.main()
