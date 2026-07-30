import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from monitoring.db import MonitorRepository


class MonitorRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = MonitorRepository(Path(self.temp_dir.name) / "monitor.db")
        self.repository.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cleanup_prunes_short_lived_data_but_keeps_daily_snapshots(self):
        now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
        old = now - timedelta(days=10)
        recent = now - timedelta(days=1)
        self.repository.upsert_watch("600000", "固定样例")
        rule_id = self.repository.add_rule(
            "600000",
            "价格提醒",
            "alert",
            "price",
            "lte",
            10,
            now=old,
        )
        rule = self.repository.list_rules()[0]
        quote = {
            "code": "600000",
            "name": "固定样例",
            "price": 9.9,
            "change_pct": -1,
            "change_amount": -0.1,
        }
        self.repository.add_minute_quote(quote, old)
        self.repository.add_minute_quote(quote, recent)
        self.repository.upsert_daily_snapshot(
            "600000",
            "2026-07-18",
            {"metrics": {"price": 10}},
            old,
        )
        self.repository.add_event(rule, 9.9, "old", {}, old)
        self.repository.add_event(rule, 9.9, "recent", {}, recent)
        self.repository.save_draft(
            "expired",
            "600000",
            "test",
            {"rules": []},
            retention_days=1,
            now=old,
        )

        result = self.repository.cleanup(7, 7, now)

        self.assertEqual(result["minute_quotes"], 1)
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["ai_rule_drafts"], 1)
        self.assertEqual(self.repository.count_rows("minute_quotes"), 1)
        self.assertEqual(self.repository.count_rows("events"), 1)
        self.assertEqual(self.repository.count_rows("daily_snapshots"), 1)
        self.assertEqual(self.repository.count_rows("rules"), 1)
        self.assertEqual(rule_id, rule["id"])

    def test_watch_and_rule_survive_reopening_database(self):
        self.repository.upsert_watch(
            "600519", "贵州茅台", quantity=100, cost_price=1200
        )
        self.repository.add_rule(
            "600519", "价格下沿", "buy", "price", "lte", 1100
        )

        reopened = MonitorRepository(self.repository.db_path)
        reopened.initialize()

        self.assertEqual(reopened.list_watch()[0]["quantity"], 100)
        self.assertEqual(reopened.list_rules()[0]["threshold"], 1100)

    def test_logic_usage_and_notification_queue_are_persistent(self):
        now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
        self.repository.upsert_watch("600519", "贵州茅台")
        self.repository.save_watch_logic(
            "600519",
            "回落到明确价格后复核",
            "基本假设失效",
            "核实公告与财务数据",
            now,
        )
        rule_id = self.repository.add_rule(
            "600519", "风险价", "sell", "price", "lte", 1000, now=now
        )
        rule = next(
            item for item in self.repository.list_rules() if item["id"] == rule_id
        )
        event_id = self.repository.add_event(
            rule, 1000, "提醒", {"name": "贵州茅台"}, now
        )
        self.repository.enqueue_notification_job(
            event_id,
            "wecom",
            "提醒",
            "内容",
            now + timedelta(minutes=1),
            "TimeoutError",
            now,
        )
        usage = self.repository.reserve_ai_usage(
            "2026-07-28", "event_explanation", 500, 5, 8000, now
        )

        reopened = MonitorRepository(self.repository.db_path)
        reopened.initialize()

        self.assertEqual(reopened.get_watch_logic("600519")["thesis"], "回落到明确价格后复核")
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(
            reopened.get_ai_usage("2026-07-28", "event_explanation")["tokens"],
            500,
        )
        self.assertEqual(
            reopened.list_due_notification_jobs(
                now + timedelta(minutes=1), 3
            )[0]["channel"],
            "wecom",
        )

    def test_ai_usage_limits_are_enforced_before_call(self):
        now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
        self.repository.reserve_ai_usage(
            "2026-07-28", "event_explanation", 700, 1, 1000, now
        )

        with self.assertRaisesRegex(ValueError, "次数已达上限"):
            self.repository.reserve_ai_usage(
                "2026-07-28", "event_explanation", 100, 1, 1000, now
            )


if __name__ == "__main__":
    unittest.main()
