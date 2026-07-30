import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from monitoring.config import MonitorConfig
from monitoring.db import MonitorRepository
from monitoring.notifications import NotificationResult
from monitoring.service import MonitorService, is_market_session
from monitoring.trading_calendar import TradingCalendar


class FakeProvider:
    def __init__(self):
        self.analysis_calls = 0

    def get_quotes(self, codes):
        return [
            {
                "code": code,
                "name": "固定样例",
                "price": 90,
                "change_pct": -3,
                "change_amount": -2.8,
            }
            for code in codes
        ]

    def get_analysis(self, code):
        self.analysis_calls += 1
        return {
            "code": code,
            "name": "固定样例",
            "trade_date": "2026-07-28",
            "metrics": {"price": 999, "pe_percentile": 10, "rsi": 25},
            "sample": {"count": 100},
        }


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send(self, title, content):
        self.messages.append((title, content))
        return [NotificationResult("console", True)]

class RetryNotifier:
    def __init__(self):
        self.retry_calls = 0

    def send(self, title, content):
        return [
            NotificationResult("console", True),
            NotificationResult("wecom", False, "TimeoutError"),
        ]

    def send_to_channel(self, channel, title, content):
        self.retry_calls += 1
        return NotificationResult(channel, True)


class MonitorServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = MonitorConfig(
            db_path=Path(self.temp_dir.name) / "monitor.db",
            analysis_refresh_seconds=1800,
        )
        self.repository = MonitorRepository(self.config.db_path)
        self.repository.initialize()
        self.repository.upsert_watch("600000", "固定样例")
        self.provider = FakeProvider()
        self.notifier = FakeNotifier()
        self.service = MonitorService(
            self.config, self.repository, self.provider, self.notifier
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_quote_rule_triggers_once_after_confirmation_without_tokens(self):
        self.repository.add_rule(
            "600000",
            "价格低于观察线",
            "buy",
            "price",
            "lte",
            100,
            confirm_count=2,
            hysteresis=1,
        )
        now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)

        first = self.service.run_once(force=True, now=now)
        second = self.service.run_once(force=True, now=now + timedelta(minutes=1))
        third = self.service.run_once(force=True, now=now + timedelta(minutes=2))

        self.assertEqual(first.triggered_count, 0)
        self.assertEqual(second.triggered_count, 1)
        self.assertEqual(third.triggered_count, 0)
        self.assertEqual(second.token_usage, 0)
        self.assertEqual(self.repository.count_rows("events"), 1)
        self.assertEqual(self.repository.count_rows("minute_quotes"), 3)
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertEqual(self.provider.analysis_calls, 0)

    def test_analysis_metric_is_persisted_and_reused_within_refresh_window(self):
        self.repository.add_rule(
            "600000",
            "估值分位观察",
            "buy",
            "pe_percentile",
            "lte",
            20,
        )
        now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)

        first = self.service.run_once(force=True, now=now)
        second = self.service.run_once(force=True, now=now + timedelta(minutes=5))

        self.assertEqual(first.analysis_refresh_count, 1)
        self.assertEqual(second.analysis_refresh_count, 0)
        self.assertEqual(self.provider.analysis_calls, 1)
        self.assertEqual(self.repository.count_rows("daily_snapshots"), 1)

    def test_analysis_snapshot_does_not_override_live_quote_metric(self):
        self.repository.add_rule(
            "600000", "实时价格观察", "buy", "price", "lte", 100
        )
        self.repository.add_rule(
            "600000", "估值观察", "buy", "pe_percentile", "lte", 20
        )
        now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)

        result = self.service.run_once(force=True, now=now)
        events = self.repository.list_events()
        price_event = next(event for event in events if event["metric"] == "price")

        self.assertEqual(result.triggered_count, 2)
        self.assertEqual(price_event["observed_value"], 90)

    def test_market_session_uses_china_standard_time(self):
        market_open = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
        before_open = datetime(2026, 7, 28, 1, 0, tzinfo=timezone.utc)
        self.assertTrue(is_market_session(market_open))
        self.assertFalse(is_market_session(before_open))

    def test_market_calendar_can_close_weekday_and_open_weekend(self):
        weekday = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
        weekend = datetime(2026, 8, 1, 2, 0, tzinfo=timezone.utc)
        calendar = TradingCalendar(
            closed_dates=frozenset({"2026-07-28"}),
            open_dates=frozenset({"2026-08-01"}),
            source="fixed-test",
        )

        self.assertFalse(is_market_session(weekday, calendar))
        self.assertTrue(is_market_session(weekend, calendar))

    def test_failed_external_notification_is_retried_by_channel(self):
        retry_notifier = RetryNotifier()
        service = MonitorService(
            self.config,
            self.repository,
            self.provider,
            retry_notifier,
        )
        self.repository.add_rule(
            "600000", "风险提醒", "sell", "price", "lte", 100
        )
        now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)

        first = service.run_once(force=True, now=now)
        second = service.run_once(force=True, now=now + timedelta(seconds=61))
        event = self.repository.list_events()[0]
        jobs = self.repository.list_notification_jobs(event["id"])

        self.assertEqual(first.triggered_count, 1)
        self.assertEqual(second.notification_retry_count, 1)
        self.assertEqual(retry_notifier.retry_calls, 1)
        self.assertEqual(jobs[0]["status"], "sent")
        self.assertEqual(event["notification_status"], "sent")


if __name__ == "__main__":
    unittest.main()
