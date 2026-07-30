import tempfile
import threading
import time
import unittest
from pathlib import Path

from monitoring.config import MonitorConfig
from monitoring.db import MonitorRepository
from monitoring.web import MonitorWebController


class FakeSummary:
    def to_dict(self):
        return {
            "observed_at": "2026-07-28T02:00:00+00:00",
            "triggered_count": 0,
            "token_usage": 0,
        }


class FakeService:
    def __init__(self, called):
        self.called = called

    def run_once(self):
        self.called.set()
        return FakeSummary()

class FakeRuleAssistant:
    def __init__(self):
        self.calls = []

    def suggest(
        self,
        code,
        logic_text,
        force=False,
        api_key="",
        advanced_mode=False,
        confirmation=None,
    ):
        self.calls.append(
            {
                "code": code,
                "logic_text": logic_text,
                "force": force,
                "api_key": api_key,
                "advanced_mode": advanced_mode,
                "confirmation": confirmation,
            }
        )
        return {
            "code": code,
            "summary": "规则草案",
            "assumptions": [],
            "risks": [],
            "needs_confirmation": [],
            "rules": [],
            "cached": True,
            "manual_review_items": [],
            "periodic_review_items": [],
            "auto_rule_count": 0,
            "next_question": None,
        }


class FakeExplanationAssistant:
    def explain(self, event_id, api_key, force=False):
        return {
            "event_id": int(event_id),
            "summary": "事件复核",
            "relation": "neutral",
            "logic_matches": [],
            "invalidation_checks": [],
            "review_questions": [],
            "limitations": [],
            "token_usage": 0,
            "cached": True,
            "daily_usage": {"calls": 1, "tokens": 300},
        }


class MonitorWebControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = MonitorConfig(
            db_path=Path(self.temp_dir.name) / "monitor.db",
            poll_seconds=15,
        )
        self.repository = MonitorRepository(self.config.db_path)
        self.called = threading.Event()
        self.controller = MonitorWebController(
            self.config,
            self.repository,
            lambda: FakeService(self.called),
            FakeExplanationAssistant,
            FakeRuleAssistant,
        )

    def tearDown(self):
        self.controller.stop()
        self.temp_dir.cleanup()

    def test_background_runtime_starts_records_summary_and_stops(self):
        started = self.controller.start()
        self.assertTrue(started["running"])
        self.assertTrue(self.called.wait(timeout=2))

        deadline = time.monotonic() + 2
        runtime = self.controller.runtime_status()
        while runtime["last_summary"] is None and time.monotonic() < deadline:
            time.sleep(0.01)
            runtime = self.controller.runtime_status()
        self.assertEqual(runtime["last_summary"]["token_usage"], 0)

        stopped = self.controller.stop()
        self.assertFalse(stopped["running"])

    def test_overview_does_not_expose_notification_credentials(self):
        overview = self.controller.overview()

        self.assertEqual(overview["settings"]["channels"], ["本地控制台"])
        self.assertNotIn("db_path", overview["settings"])
        self.assertNotIn("wecom_webhook_url", overview["settings"])
        self.assertNotIn("serverchan_sendkey", overview["settings"])

    def test_simulated_risk_preview_is_side_effect_free(self):
        self.controller.save_setup(
            {
                "code": "600519",
                "name": "贵州茅台",
                "watch_price": 1100,
                "risk_price": 1000,
                "target_price": 1400,
            }
        )
        event_count = self.repository.count_rows("events")

        preview = self.controller.simulate_watch("600519")

        self.assertTrue(preview["simulated"])
        self.assertEqual(preview["threshold"], 1000)
        self.assertEqual(preview["observed_value"], 1000)
        self.assertIn("【卖出关注条件】贵州茅台（600519）", preview["message"])
        self.assertFalse(preview["persisted"])
        self.assertFalse(preview["notification_sent"])
        self.assertEqual(preview["token_usage"], 0)
        self.assertEqual(self.repository.count_rows("events"), event_count)
        self.assertIsNone(self.controller._service)
        self.assertFalse(self.called.is_set())

    def test_simulated_risk_preview_requires_watch_and_enabled_rule(self):
        with self.assertRaisesRegex(ValueError, "未找到该盯盘标的"):
            self.controller.simulate_watch("600519")

        self.controller.save_setup(
            {
                "code": "600519",
                "watch_price": 1100,
                "risk_price": 1000,
            }
        )
        risk_rule = next(
            rule
            for rule in self.repository.list_rules(["600519"])
            if rule["name"] == "[三线] 风险价"
        )
        self.repository.set_rule_enabled(risk_rule["id"], False)

        with self.assertRaisesRegex(ValueError, "没有可用的风险价规则"):
            self.controller.simulate_watch("600519")

    def test_logic_card_can_create_watch_and_feed_rule_draft(self):
        saved = self.controller.save_logic(
            {
                "code": "600519",
                "name": "贵州茅台",
                "thesis": "回落到明确价格后复核",
                "invalidation": "原假设失效",
                "review_items": "核实公告",
            }
        )
        draft = self.controller.draft_rules(
            {"code": "600519", "logic_text": saved["thesis"]},
            api_key="browser-only-test-key",
        )
        overview = self.controller.overview()

        self.assertTrue(saved["configured"])
        self.assertTrue(saved["logic_saved"])
        self.assertEqual(saved["auto_rule_count"], 0)
        self.assertEqual(saved["manual_review_items"], ["核实公告"])
        self.assertEqual(draft["summary"], "规则草案")
        self.assertTrue(draft["logic_saved"])
        self.assertEqual(draft["manual_review_items"], ["核实公告"])
        self.assertEqual(overview["counts"]["watches"], 1)
        self.assertTrue(overview["watches"][0]["logic"]["configured"])
        self.assertEqual(self.repository.count_rows("rules"), 0)

    def test_logic_draft_uses_saved_text_and_forwards_staged_options(self):
        saved = self.controller.save_logic(
            {
                "code": "516120",
                "thesis": "化工行业盈利修复",
                "invalidation": "产品价格和行业利润持续恶化",
                "review_items": "财报发布时复核现金流",
            }
        )
        confirmation = {
            "action": "derive_historical_low_risk",
            "approved": True,
        }

        result = self.controller.draft_rules(
            {
                "code": "516120",
                "logic_text": "",
                "advanced_mode": False,
                "confirmation": confirmation,
            },
            api_key="browser-only-test-key",
        )
        call = self.controller._rule_assistant.calls[-1]

        self.assertTrue(saved["logic_saved"])
        self.assertEqual(saved["auto_rule_count"], 0)
        self.assertTrue(saved["periodic_review_items"])
        self.assertIn("化工行业盈利修复", call["logic_text"])
        self.assertFalse(call["advanced_mode"])
        self.assertEqual(call["confirmation"], confirmation)
        self.assertTrue(result["logic_saved"])
        self.assertEqual(result["auto_rule_count"], 0)
        self.assertTrue(result["periodic_review_items"])

    def test_logic_draft_rejects_non_object_confirmation(self):
        self.controller.save_logic(
            {"code": "600519", "thesis": "跌破前低时复核"}
        )

        with self.assertRaisesRegex(ValueError, "confirmation 必须是对象"):
            self.controller.draft_rules(
                {
                    "code": "600519",
                    "logic_text": "跌破前低时复核",
                    "confirmation": "yes",
                }
            )

    def test_manual_event_explanation_uses_assistant_only_on_request(self):
        self.controller.save_setup(
            {
                "code": "600519",
                "watch_price": 1100,
                "risk_price": 1000,
            }
        )
        rule = next(
            item
            for item in self.repository.list_rules(["600519"])
            if item["name"] == "[三线] 风险价"
        )
        event_id = self.repository.add_event(
            rule, 1000, "提醒", {"name": "贵州茅台"}
        )

        result = self.controller.explain_event(
            event_id, "browser-only-test-key"
        )

        self.assertEqual(result["event_id"], event_id)
        self.assertTrue(result["cached"])
        self.assertIsNone(self.controller._service)


if __name__ == "__main__":
    unittest.main()
