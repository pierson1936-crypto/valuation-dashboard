import json
import tempfile
import unittest
from pathlib import Path

from monitoring.assistant import (
    BASIC_SYSTEM_PROMPT,
    HISTORICAL_LOW_ACTION,
    HISTORICAL_LOW_QUESTION,
    SYSTEM_PROMPT,
    RuleDraftAssistant,
)
from monitoring.config import MonitorConfig
from monitoring.db import MonitorRepository


class RuleDraftAssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = MonitorConfig(
            db_path=Path(self.temp_dir.name) / "monitor.db",
            deepseek_api_key="".join(("test", "-only")),
        )
        self.repository = MonitorRepository(self.config.db_path)
        self.repository.initialize()
        self.calls = 0
        self.analyzer_calls = 0
        self.last_system = ""
        self.last_user = ""

    def tearDown(self):
        self.temp_dir.cleanup()

    def analyzer(self, code):
        self.analyzer_calls += 1
        return {
            "code": code,
            "price": 100,
            "historical_low": {
                "price": 88,
                "date": "2026-06-18",
                "method": "固定样例",
            },
            "valuation": {"pe_percentile_5y": 20},
            "technical": {"rsi14": 35},
        }

    def llm(self, system, user):
        self.calls += 1
        self.last_system = system
        self.last_user = user
        return json.dumps(
            {
                "summary": "把用户逻辑整理成两项确认条件。",
                "assumptions": ["阈值需要用户确认"],
                "risks": ["历史分位不代表未来收益"],
                "needs_confirmation": [],
                "rules": [
                    {
                        "name": "价格观察线",
                        "direction": "buy",
                        "metric": "price",
                        "operator": "lte",
                        "threshold": 95,
                        "confirm_count": 2,
                        "cooldown_seconds": 3600,
                        "hysteresis": 1,
                        "reason": "避免单次瞬时波动",
                    }
                ],
            },
            ensure_ascii=False,
        )

    @staticmethod
    def valid_rule(**overrides):
        rule = {
            "name": "价格观察线",
            "direction": "buy",
            "metric": "price",
            "operator": "lte",
            "threshold": 95,
            "confirm_count": 2,
            "cooldown_seconds": 3600,
            "hysteresis": 0,
            "reason": "对应用户明确提供的关注价",
        }
        rule.update(overrides)
        return rule

    @staticmethod
    def valid_payload(rules=None, **overrides):
        payload = {
            "summary": "规则草案",
            "assumptions": [],
            "risks": [],
            "needs_confirmation": [],
            "rules": rules if rules is not None else [],
        }
        payload.update(overrides)
        return payload

    def test_same_logic_uses_cached_draft_without_second_model_call(self):
        assistant = RuleDraftAssistant(
            self.config, self.repository, self.analyzer, self.llm
        )

        first = assistant.suggest("600000", "价格回落且估值较低时提醒")
        second = assistant.suggest("600000", "价格回落且估值较低时提醒")

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.repository.count_rows("ai_rule_drafts"), 1)
        self.assertEqual(self.repository.count_rows("rules"), 0)
        self.assertEqual(first["needs_confirmation"], [])
        self.assertEqual(self.analyzer_calls, 0)

    def test_system_prompt_injects_allowlists_and_defaults(self):
        self.assertNotIn("{{ALLOWED_METRICS}}", SYSTEM_PROMPT)
        self.assertNotIn("{{DEFAULT_CONFIRM_COUNT}}", SYSTEM_PROMPT)
        self.assertIn("price", SYSTEM_PROMPT)
        self.assertIn("crosses_below", SYSTEM_PROMPT)
        self.assertIn("confirm_count = 2", SYSTEM_PROMPT)
        self.assertIn("cooldown_seconds = 3600", SYSTEM_PROMPT)
        self.assertIn("hysteresis = 0", SYSTEM_PROMPT)
        self.assertIn("needs_confirmation", SYSTEM_PROMPT)
        self.assertNotIn("RSI", BASIC_SYSTEM_PROMPT)
        self.assertNotIn("MACD", BASIC_SYSTEM_PROMPT)
        self.assertNotIn("估值", BASIC_SYSTEM_PROMPT)

    def test_invalid_model_metric_is_rejected(self):
        def invalid_llm(system, user):
            return json.dumps(
                self.valid_payload(
                    [self.valid_rule(metric="future_price")]
                )
            )

        assistant = RuleDraftAssistant(
            self.config, self.repository, self.analyzer, invalid_llm
        )
        with self.assertRaisesRegex(ValueError, "不支持的指标"):
            assistant.suggest("600000", "预测未来价格")

    def test_more_than_three_draft_rules_is_rejected(self):
        def verbose_llm(system, user):
            return json.dumps(
                self.valid_payload(
                    [
                        self.valid_rule(
                            name="规则 %d" % index,
                            direction="alert",
                            operator="gte",
                            threshold=index,
                        )
                        for index in range(4)
                    ]
                )
            )

        assistant = RuleDraftAssistant(
            self.config, self.repository, self.analyzer, verbose_llm
        )
        with self.assertRaisesRegex(ValueError, "0 到 3"):
            assistant.suggest("600000", "给我很多规则")

    def test_markdown_wrapped_json_is_rejected(self):
        def markdown_llm(system, user):
            return "```json\n%s\n```" % json.dumps(self.valid_payload())

        assistant = RuleDraftAssistant(
            self.config, self.repository, self.analyzer, markdown_llm
        )
        with self.assertRaisesRegex(ValueError, "严格 JSON"):
            assistant.suggest("600000", "没有明确价格")

    def test_missing_rule_field_is_rejected(self):
        def incomplete_llm(system, user):
            rule = self.valid_rule()
            rule.pop("reason")
            return json.dumps(self.valid_payload([rule]))

        assistant = RuleDraftAssistant(
            self.config, self.repository, self.analyzer, incomplete_llm
        )
        with self.assertRaisesRegex(ValueError, "缺少字段：reason"):
            assistant.suggest("600000", "95 元提醒")

    def test_empty_rules_preserve_confirmation_questions(self):
        def no_rule_llm(system, user):
            return json.dumps(
                self.valid_payload(
                    needs_confirmation=["缺少可验证的价格或指标阈值"]
                ),
                ensure_ascii=False,
            )

        assistant = RuleDraftAssistant(
            self.config, self.repository, self.analyzer, no_rule_llm
        )
        result = assistant.suggest("600000", "价格合适时提醒")

        self.assertEqual(result["rules"], [])
        self.assertEqual(
            result["needs_confirmation"], [HISTORICAL_LOW_QUESTION]
        )
        self.assertEqual(result["next_question"]["confirmation"]["action"],
                         HISTORICAL_LOW_ACTION)

    def test_basic_mode_skips_analyzer_and_advanced_context(self):
        def no_rule_llm(system, user):
            self.last_system = system
            self.last_user = user
            return json.dumps(
                self.valid_payload(
                    needs_confirmation=[
                        "请提供行业利润和产品价格的具体阈值",
                        "请提供前低价格",
                    ]
                ),
                ensure_ascii=False,
            )

        assistant = RuleDraftAssistant(
            self.config, self.repository, self.analyzer, no_rule_llm
        )
        result = assistant.suggest(
            "516120",
            "行业利润和产品价格恶化时复核；放量跌破前低时关注",
            advanced_mode=False,
        )

        self.assertEqual(self.analyzer_calls, 0)
        self.assertNotIn("当前压缩数据", self.last_user)
        self.assertNotIn("RSI", self.last_system)
        self.assertNotIn("MACD", self.last_system)
        self.assertNotIn("估值", self.last_system)
        self.assertEqual(len(result["needs_confirmation"]), 1)
        self.assertEqual(result["needs_confirmation"], [HISTORICAL_LOW_QUESTION])
        self.assertTrue(result["periodic_review_items"])
        self.assertTrue(
            any(
                "行业利润" in item
                for item in result["periodic_review_items"]
            )
        )
        self.assertTrue(
            any("放量" in item for item in result["manual_review_items"])
        )
        self.assertEqual(result["auto_rule_count"], 0)

    def test_advanced_mode_can_use_injected_analysis_context(self):
        assistant = RuleDraftAssistant(
            self.config, self.repository, self.analyzer, self.llm
        )

        assistant.suggest(
            "600000",
            "高级模式下结合已提供指标整理 95 元关注价",
            advanced_mode=True,
        )

        self.assertEqual(self.analyzer_calls, 1)
        self.assertIn("当前压缩数据", self.last_user)
        self.assertIn("rsi14", self.last_user)
        self.assertIn("RSI", self.last_system)

    def test_confirmed_historical_low_creates_pending_draft_without_llm_or_rule(self):
        def forbidden_llm(system, user):
            raise AssertionError("确认前低路径不应调用模型")

        assistant = RuleDraftAssistant(
            self.config, self.repository, self.analyzer, forbidden_llm
        )
        result = assistant.suggest(
            "600000",
            "跌破前低且长期无法收回时视为持有逻辑失效",
            confirmation={
                "action": HISTORICAL_LOW_ACTION,
                "approved": True,
            },
        )

        self.assertEqual(self.analyzer_calls, 1)
        self.assertEqual(result["auto_rule_count"], 1)
        self.assertEqual(result["rules"][0]["metric"], "price")
        self.assertEqual(result["rules"][0]["threshold"], 88)
        self.assertEqual(
            result["rules"][0]["draft_status"], "pending_confirmation"
        )
        self.assertEqual(result["token_usage"], 0)
        self.assertIsNone(result["next_question"])
        self.assertEqual(self.repository.count_rows("rules"), 0)

    def test_confirmed_historical_low_handles_missing_candidate(self):
        def no_history(code):
            self.analyzer_calls += 1
            return {"code": code, "price": 100}

        assistant = RuleDraftAssistant(
            self.config, self.repository, no_history, self.llm
        )
        result = assistant.suggest(
            "600000",
            "跌破前低时复核",
            confirmation={
                "action": HISTORICAL_LOW_ACTION,
                "approved": True,
            },
        )

        self.assertEqual(result["rules"], [])
        self.assertEqual(result["auto_rule_count"], 0)
        self.assertEqual(result["token_usage"], 0)
        self.assertIsNone(result["next_question"])


if __name__ == "__main__":
    unittest.main()
