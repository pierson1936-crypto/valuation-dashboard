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
        deepseek_model="",
    ):
        self.calls.append(
            {
                "code": code,
                "logic_text": logic_text,
                "force": force,
                "api_key": api_key,
                "advanced_mode": advanced_mode,
                "confirmation": confirmation,
                "deepseek_model": deepseek_model,
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
    def explain(self, event_id, api_key, force=False, deepseek_model=""):
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
            "model": deepseek_model or "deepseek-v4-flash",
        }


class FakeHoldingOCRAssistant:
    def __init__(self):
        self.calls = []

    def recognize(self, image_data_url, api_key, base_url):
        self.calls.append(
            {
                "image_data_url": image_data_url,
                "api_key": api_key,
                "base_url": base_url,
            }
        )
        return {
            "holdings": [],
            "needs_review": ["测试结果"],
            "cached": False,
            "model": "qwen3.7-flash",
            "token_usage": 12,
        }


class FakePortfolioReportAssistant:
    def __init__(self):
        self.calls = []

    def latest(self):
        return {"report": None}

    def history(self):
        return {"reports": [{"id": 7, "status": "complete"}], "limit": 7}

    def scan(self):
        return {"ai_used": False, "token_usage": 0, "analytics": {}}

    def generate(
        self,
        user_judgment,
        api_key,
        force=False,
        provider="deepseek",
        deepseek_model="",
    ):
        self.calls.append(
            {
                "user_judgment": user_judgment,
                "api_key": api_key,
                "force": force,
                "provider": provider,
                "deepseek_model": deepseek_model,
            }
        )
        return {
            "report_id": 1,
            "user_judgment": user_judgment,
            "status": "complete",
            "cached": False,
            "token_usage": 200,
            "independent_analysis": {"summary": "独立判断", "issues": []},
            "comparison": {
                "agreements": [],
                "disagreements": [],
                "possible_omissions": [],
            },
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
            FakeHoldingOCRAssistant,
            FakePortfolioReportAssistant,
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

    def test_holding_screenshot_controller_forwards_ephemeral_credentials(self):
        result = self.controller.recognize_holding_screenshot(
            {
                "image_data_url": "data:image/png;base64,test-only",
                "base_url": "https://workspace.aliyuncs.com/compatible-mode/v1",
            },
            "request-header-key",
        )
        call = self.controller._holding_ocr_assistant.calls[0]

        self.assertEqual(result["needs_review"], ["测试结果"])
        self.assertEqual(call["api_key"], "request-header-key")
        self.assertEqual(
            call["base_url"],
            "https://workspace.aliyuncs.com/compatible-mode/v1",
        )

    def test_portfolio_report_controller_forwards_locked_judgment_and_key(self):
        result = self.controller.generate_portfolio_report(
            {
                "user_judgment": "我担心组合波动",
                "force": True,
                "deepseek_model": "deepseek-v4-pro",
            },
            "request-key",
        )
        call = self.controller._portfolio_report_assistant.calls[0]

        self.assertEqual(result["user_judgment"], "我担心组合波动")
        self.assertEqual(call["api_key"], "request-key")
        self.assertTrue(call["force"])
        self.assertEqual(call["provider"], "deepseek")
        self.assertEqual(call["deepseek_model"], "deepseek-v4-pro")
        self.controller.generate_portfolio_report(
            {"user_judgment": "改用 GPT", "provider": "gpt"},
            "openai-request-key",
        )
        gpt_call = self.controller._portfolio_report_assistant.calls[1]
        self.assertEqual(gpt_call["api_key"], "openai-request-key")
        self.assertEqual(gpt_call["provider"], "gpt")
        self.assertEqual(
            self.controller.portfolio_scan(),
            {"ai_used": False, "token_usage": 0, "analytics": {}},
        )
        self.assertEqual(self.controller.latest_portfolio_report(), {"report": None})
        self.assertEqual(
            self.controller.portfolio_report_history(),
            {"reports": [{"id": 7, "status": "complete"}], "limit": 7},
        )

    def test_holdings_batch_upserts_without_touching_existing_state(self):
        self.repository.upsert_watch(
            "600519",
            "旧名称",
            quantity=100,
            cost_price=1200,
            notes="保留备注",
            enabled=False,
        )
        self.repository.upsert_watch(
            "000001", "平安银行", quantity=200, cost_price=10
        )
        self.repository.upsert_watch("600000", "普通自选")
        rule_id = self.repository.add_rule(
            "600519", "保留规则", "alert", "price", ">=", 1500
        )

        result = self.controller.upsert_holdings(
            {
                "holdings": [
                    {
                        "code": "600519",
                        "name": "贵州茅台",
                        "quantity": "120",
                        "cost_price": "1188.5",
                    },
                    {
                        "code": "300750",
                        "name": "宁德时代",
                        "quantity": 0,
                        "cost_price": 180,
                    },
                ]
            }
        )

        self.assertEqual(result["errors"], [])
        self.assertEqual([item["code"] for item in result["saved"]], ["600519", "300750"])
        watches = {item["code"]: item for item in self.repository.list_watch()}
        self.assertEqual(watches["600519"]["name"], "贵州茅台")
        self.assertEqual(watches["600519"]["quantity"], 120)
        self.assertEqual(watches["600519"]["cost_price"], 1188.5)
        self.assertEqual(watches["600519"]["notes"], "保留备注")
        self.assertEqual(watches["600519"]["enabled"], 0)
        self.assertEqual(watches["000001"]["quantity"], 200)
        self.assertEqual(watches["600000"]["quantity"], None)
        self.assertEqual(self.repository.list_rules()[0]["id"], rule_id)
        self.assertEqual(self.repository.list_rules()[0]["threshold"], 1500)

        listed = self.controller.list_holdings()["holdings"]
        self.assertEqual(
            [item["code"] for item in listed],
            ["000001", "300750", "600519"],
        )
        self.assertNotIn("notes", listed[0])
        self.assertFalse(next(item for item in listed if item["code"] == "600519")["enabled"])

    def test_invalid_holdings_batch_is_not_partially_written(self):
        self.repository.upsert_watch(
            "600519", "贵州茅台", quantity=100, cost_price=1200
        )

        result = self.controller.upsert_holdings(
            {
                "holdings": [
                    {
                        "code": "300750",
                        "name": "宁德时代",
                        "quantity": 100,
                        "cost_price": 180,
                    },
                    {
                        "code": "600519",
                        "name": "贵州茅台",
                        "quantity": -1,
                        "cost_price": float("nan"),
                    },
                ]
            }
        )

        self.assertEqual(result["saved"], [])
        self.assertEqual(
            {error["field"] for error in result["errors"]},
            {"quantity", "cost_price"},
        )
        watches = {item["code"]: item for item in self.repository.list_watch()}
        self.assertNotIn("300750", watches)
        self.assertEqual(watches["600519"]["quantity"], 100)
        self.assertEqual(watches["600519"]["cost_price"], 1200)

    def test_holdings_batch_rejects_duplicate_codes_and_invalid_shape(self):
        duplicate = self.controller.upsert_holdings(
            {
                "holdings": [
                    {"code": "600519", "name": "A", "quantity": 1, "cost_price": 1},
                    {"code": "600519", "name": "B", "quantity": 2, "cost_price": 2},
                ]
            }
        )
        invalid_shape = self.controller.upsert_holdings({"holdings": {}})

        self.assertEqual(duplicate["saved"], [])
        self.assertEqual(duplicate["errors"][0]["field"], "code")
        self.assertEqual(self.repository.list_watch(), [])
        self.assertEqual(invalid_shape["saved"], [])
        self.assertEqual(invalid_shape["errors"][0]["field"], "holdings")

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
                "deepseek_model": "deepseek-v4-pro",
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
        self.assertEqual(call["deepseek_model"], "deepseek-v4-pro")
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
            event_id,
            "browser-only-test-key",
            deepseek_model="deepseek-v4-pro",
        )

        self.assertEqual(result["event_id"], event_id)
        self.assertEqual(result["model"], "deepseek-v4-pro")
        self.assertTrue(result["cached"])
        self.assertIsNone(self.controller._service)


if __name__ == "__main__":
    unittest.main()
