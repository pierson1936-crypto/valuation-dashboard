import unittest
from unittest.mock import patch

import app

from tests.fixtures import kline_rows, stock_meta, valuation_rows


class PercentileRankTests(unittest.TestCase):
    def test_midrank_keeps_negative_values_and_ignores_none(self):
        self.assertEqual(app.percentile_rank([None, -1, 0, 1, 1, 2], 1), 60.0)

    def test_empty_sample_or_missing_current_value_returns_none(self):
        self.assertIsNone(app.percentile_rank([], 1))
        self.assertIsNone(app.percentile_rank([1, 2], None))


class AnalyzeContractTests(unittest.TestCase):
    def test_fixed_stock_sample_keeps_core_output_contract(self):
        rows = kline_rows()
        with (
            patch.object(app, "resolve", return_value=stock_meta()),
            patch.object(app, "fetch_kline", return_value=(rows, "固定样例", None)),
            patch.object(app, "fetch_moneyflow", return_value=[]),
            patch.object(app, "fetch_valuation", return_value=valuation_rows()),
            patch.object(app, "fetch_fundamentals", return_value=[]),
        ):
            result = app.analyze("600000")

        required = {
            "code",
            "name",
            "date",
            "count",
            "price",
            "price_pct",
            "pe",
            "pb",
            "pe_pct",
            "pb_pct",
            "tech",
            "chart",
            "report",
            "risk",
            "alerts",
        }
        self.assertTrue(required.issubset(result))
        self.assertEqual(result["code"], "600000")
        self.assertEqual(result["count"], 80)
        self.assertEqual(result["price"], 17.9)
        self.assertEqual(result["price_pct"], 99.4)
        self.assertEqual(result["pe"], 20.0)
        self.assertEqual(result["pb"], 2.0)
        self.assertEqual(result["pe_pct"], 75.0)
        self.assertEqual(result["pb_pct"], 75.0)
        self.assertEqual(len(result["chart"]["dates"]), 80)

    def test_missing_kline_returns_error_instead_of_crashing(self):
        with (
            patch.object(app, "resolve", return_value=stock_meta()),
            patch.object(app, "fetch_kline", return_value=([], "", None)),
            patch.object(app, "fetch_moneyflow", return_value=[]),
            patch.object(app, "fetch_valuation", return_value=[]),
            patch.object(app, "fetch_fundamentals", return_value=[]),
        ):
            result = app.analyze("600000")

        self.assertIn("error", result)
        self.assertIn("未取到", result["error"])


if __name__ == "__main__":
    unittest.main()
