import unittest

from dashboard_runner import decimal_json_number, parse_stock_delta


class InventoryCorrectionTests(unittest.TestCase):
    def test_accepts_tenths_up_and_down(self):
        self.assertEqual(str(parse_stock_delta("0.1")), "0.1")
        self.assertEqual(str(parse_stock_delta("-0.1")), "-0.1")

    def test_accepts_dutch_decimal_comma(self):
        self.assertEqual(str(parse_stock_delta("1,2")), "1.2")

    def test_rejects_non_tenth_and_zero(self):
        for value in ("0", "0.05", "tekst"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_stock_delta(value)

    def test_serializes_whole_numbers_without_decimal_suffix(self):
        self.assertEqual(decimal_json_number(parse_stock_delta("2.0")), 2)
        self.assertEqual(decimal_json_number(parse_stock_delta("0.1")), 0.1)


if __name__ == "__main__":
    unittest.main()

