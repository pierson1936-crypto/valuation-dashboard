import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from monitoring.config import MonitorConfig
from monitoring.db import MonitorRepository
from monitoring.explanations import EventExplanationAssistant


class EventExplanationAssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = MonitorConfig(
            db_path=Path(self.temp_dir.name) / "monitor.db",
            event_explanation_daily_call_limit=2,
            event_explanation_daily_token_limit=8000,
        )
        self.repository = MonitorRepository(self.config.db_path)
        self.repository.initialize()
        self.repository.upsert_watch("600519", "贵州茅台")
        self.repository.save_watch_logic(
            "600519",
            "回落到明确价格后复核",
            "原假设被证伪",
            "核实公开信息",
        )
        self.rule_id = self.repository.add_rule(
            "600519", "风险价", "sell", "price", "lte", 1000
        )
        self.rule = self.repository.list_rules()[0]
        self.calls = 0

    def tearDown(self):
        self.temp_dir.cleanup()

    def llm(self, system, user, key):
        self.calls += 1
        return (
            json.dumps(
                {
                    "summary": "风险价条件已触发，需要对照原逻辑复核。",
                    "relation": "neutral",
                    "logic_matches": ["价格条件与已保存逻辑相关"],
                    "invalidation_checks": ["核实原假设是否被证伪"],
                    "review_questions": ["公开信息是否发生变化"],
                    "limitations": ["未提供新的公告或财务数据"],
                },
                ensure_ascii=False,
            ),
            321,
        )

    def add_event(self, when):
        return self.repository.add_event(
            self.rule,
            1000,
            "风险提醒",
            {"name": "贵州茅台", "metrics": {"price": 1000}},
            when,
        )

    def test_same_event_and_logic_uses_cache_without_second_call(self):
        now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
        event_id = self.add_event(now)
        assistant = EventExplanationAssistant(
            self.config, self.repository, self.llm
        )

        first = assistant.explain(event_id, "test-key", now=now)
        second = assistant.explain(
            event_id, "test-key", now=now + timedelta(minutes=1)
        )

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["token_usage"], 321)
        self.assertEqual(second["token_usage"], 0)
        self.assertEqual(self.calls, 1)
        self.assertEqual(
            self.repository.count_rows("event_explanations"), 1
        )
        self.assertEqual(first["daily_usage"]["calls"], 1)
        self.assertEqual(first["daily_usage"]["tokens"], 321)

    def test_model_choice_is_part_of_event_explanation_cache(self):
        now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
        event_id = self.add_event(now)
        assistant = EventExplanationAssistant(
            self.config, self.repository, self.llm
        )

        flash = assistant.explain(
            event_id,
            "test-key",
            now=now,
            deepseek_model="deepseek-v4-flash",
        )
        pro = assistant.explain(
            event_id,
            "test-key",
            now=now,
            deepseek_model="deepseek-v4-pro",
        )

        self.assertFalse(flash["cached"])
        self.assertFalse(pro["cached"])
        self.assertEqual(flash["model"], "deepseek-v4-flash")
        self.assertEqual(pro["model"], "deepseek-v4-pro")
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.repository.count_rows("event_explanations"), 2)

    def test_failed_json_releases_reserved_daily_budget(self):
        now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
        event_id = self.add_event(now)
        assistant = EventExplanationAssistant(
            self.config,
            self.repository,
            lambda system, user, key: "not-json",
        )

        with self.assertRaisesRegex(ValueError, "严格 JSON"):
            assistant.explain(event_id, "test-key", now=now)

        usage = self.repository.get_ai_usage(
            "2026-07-28", "event_explanation"
        )
        self.assertEqual(usage["calls"], 0)
        self.assertEqual(usage["tokens"], 0)


if __name__ == "__main__":
    unittest.main()
