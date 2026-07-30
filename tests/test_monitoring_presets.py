import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from monitoring.cli import main
from monitoring.db import MonitorRepository
from monitoring.presets import (
    MANAGED_RULE_NAMES,
    MOVE_DOWN_RULE_NAME,
    MOVE_UP_RULE_NAME,
    RISK_RULE_NAME,
    TARGET_RULE_NAME,
    WATCH_RULE_NAME,
    build_simple_rules,
    simple_rule_summary,
)


class MonitoringPresetTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "monitor.db"
        self.repository = MonitorRepository(self.db_path)
        self.repository.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_simple_preset_builds_three_lines_and_two_move_alerts(self):
        rules = build_simple_rules(100, 90, 120)
        by_name = {rule["name"]: rule for rule in rules}

        self.assertEqual(set(by_name), MANAGED_RULE_NAMES)
        self.assertEqual(by_name[WATCH_RULE_NAME]["threshold"], 100)
        self.assertEqual(by_name[RISK_RULE_NAME]["threshold"], 90)
        self.assertEqual(by_name[TARGET_RULE_NAME]["threshold"], 120)
        self.assertEqual(by_name[MOVE_UP_RULE_NAME]["threshold"], 3)
        self.assertEqual(by_name[MOVE_DOWN_RULE_NAME]["threshold"], -3)
        self.assertTrue(all(rule["confirm_count"] == 2 for rule in rules))

    def test_target_is_optional_and_price_order_is_validated(self):
        rules = build_simple_rules(100, 90)
        self.assertNotIn(TARGET_RULE_NAME, {rule["name"] for rule in rules})
        with self.assertRaisesRegex(ValueError, "风险价必须低于关注价"):
            build_simple_rules(100, 100)
        with self.assertRaisesRegex(ValueError, "目标价必须高于关注价"):
            build_simple_rules(100, 90, 95)

    def test_replacing_preset_preserves_advanced_rule(self):
        self.repository.upsert_watch(
            "600000", "固定样例", quantity=100, cost_price=80, notes="保留"
        )
        advanced_id = self.repository.add_rule(
            "600000", "高级估值规则", "buy", "pe_percentile", "lte", 20
        )
        self.repository.replace_managed_rules(
            "600000", MANAGED_RULE_NAMES, build_simple_rules(100, 90, 120)
        )
        self.repository.replace_managed_rules(
            "600000", MANAGED_RULE_NAMES, build_simple_rules(98, 88)
        )

        rules = self.repository.list_rules(["600000"])
        by_name = {rule["name"]: rule for rule in rules}
        watch = self.repository.list_watch()[0]
        self.assertEqual(len(rules), 5)
        self.assertEqual(by_name["高级估值规则"]["id"], advanced_id)
        self.assertEqual(by_name[WATCH_RULE_NAME]["threshold"], 98)
        self.assertNotIn(TARGET_RULE_NAME, by_name)
        self.assertEqual(watch["quantity"], 100)
        self.assertEqual(watch["cost_price"], 80)
        self.assertEqual(watch["notes"], "保留")

    def test_quick_setup_and_overview_use_simple_fields(self):
        argv = [
            "--db",
            str(self.db_path),
            "quick-setup",
            "600519",
            "--name",
            "贵州茅台",
            "--watch-price",
            "1100",
            "--risk-price",
            "1000",
            "--target-price",
            "1400",
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(argv), 0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(["--db", str(self.db_path), "overview"]),
                0,
            )

        self.assertIn("关注 1100", output.getvalue())
        self.assertIn("风险 1000", output.getvalue())
        self.assertIn("目标 1400", output.getvalue())
        self.assertIn("异动 ±3%", output.getvalue())
        summary = simple_rule_summary(self.repository.list_rules(["600519"]))
        self.assertTrue(summary["configured"])

    def test_invalid_quick_setup_does_not_create_watch(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(
                [
                    "--db",
                    str(self.db_path),
                    "quick-setup",
                    "600519",
                    "--watch-price",
                    "100",
                    "--risk-price",
                    "110",
                ]
            )

        self.assertEqual(result, 1)
        self.assertEqual(self.repository.list_watch(), [])
        self.assertIn("风险价必须低于关注价", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
