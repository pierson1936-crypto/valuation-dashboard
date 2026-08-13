import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from monitoring.config import MonitorConfig
from monitoring.db import MonitorRepository
from monitoring.portfolio import (
    COMPARISON_MAX_TOKENS,
    COMPARISON_SYSTEM_PROMPT,
    GPT_INDEPENDENT_REPORT_SCHEMA,
    INDEPENDENT_MAX_TOKENS,
    INDEPENDENT_SYSTEM_PROMPT,
    PortfolioReportAssistant,
    _normalize_independent,
    build_confirmed_fact_catalog,
    build_comparison_facts,
    build_portfolio_snapshot,
)


def _analysis(code):
    prices = {"600519": 1500.0, "510300": 4.0}
    return {
        "code": code,
        "name": "贵州茅台" if code == "600519" else "沪深300ETF",
        "type_name": "股票" if code == "600519" else "基金",
        "price": prices[code],
        "chg": 1.5 if code == "600519" else -0.5,
        "date": "2026-08-04",
        "rt_time": "2026-08-04 10:30",
        "price_pct": 72.5,
        "from_hi": -20.0,
        "from_lo": 35.0,
        "pe": 20.0 if code == "600519" else None,
        "pb": 5.0 if code == "600519" else None,
        "pe_pct": 40.0 if code == "600519" else None,
        "pb_pct": 60.0 if code == "600519" else None,
        "tech": {
            "ma20": 1490.0,
            "rsi": 55.0,
            "macd_hist": 0.2,
            "vol_ratio": 1.2,
            "mdd": -30.0,
            "vola": 25.0,
        },
        "moneyflow": {
            "main_today": 0.5,
            "main_sum5": -1.0,
            "streak": 2,
            "streak_dir": -1,
        },
        "fund": [],
    }


def _market():
    return {
        "time": "2026-08-04 10:30",
        "indices": [{"code": "000001", "name": "上证指数", "chg": 0.2}],
        "sectors": [{"code": "516120", "name": "化工", "chg": 1.0}],
    }


def _independent_payload(evidence_ids=None):
    confirmed_fact_ids = list(evidence_ids or ["portfolio.coverage"])
    return {
        "summary": "组合存在集中度、盈亏贡献和数据边界三个主要问题。",
        "issues": [
            {
                "title": f"问题 {index}",
                "why_important": "影响组合判断。",
                "confirmed_fact_ids": confirmed_fact_ids,
                "data_inferences": ["这是基于数据的有限推断。"],
                "missing_information": ["仍缺少验证信息。"],
            }
            for index in range(1, 4)
        ],
        "overall_missing_information": ["不包含新闻和用户买入理由。"],
    }


def _confirmed_fact_catalog():
    return [
        {
            "evidence_id": "portfolio.coverage",
            "fact": "组合数据覆盖2/2只持仓，当前价格覆盖2/2只，完整组合：是。",
        }
    ]


def _comparison_payload():
    return {
        "agreements": ["双方都关注组合波动。"],
        "disagreements": [
            {
                "topic": "集中度",
                "user_view": "用户认为可以接受。",
                "ai_view": "AI认为需要继续观察。",
                "evidence_boundary": "只有当前仓位数据，没有完整资产信息。",
            }
        ],
        "possible_omissions": ["现金仓位未提供。"],
    }


class PortfolioReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = MonitorConfig(
            db_path=Path(self.temp_dir.name) / "monitor.db",
            portfolio_report_daily_call_limit=0,
            portfolio_report_daily_token_limit=0,
        )
        self.repository = MonitorRepository(self.config.db_path)
        self.repository.initialize()
        self.repository.upsert_holdings(
            [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "quantity": 1,
                    "cost_price": 1000,
                },
                {
                    "code": "510300",
                    "name": "沪深300ETF",
                    "quantity": 100,
                    "cost_price": 3,
                },
            ]
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_snapshot_reuses_analyzer_and_calculates_portfolio_fields(self):
        calls = []

        def analyzer(code):
            calls.append(code)
            return _analysis(code)

        snapshot = build_portfolio_snapshot(self.repository, analyzer, _market)

        self.assertEqual(set(calls), {"600519", "510300"})
        self.assertEqual(snapshot["totals"]["total_cost"], 1300.0)
        self.assertEqual(snapshot["totals"]["total_market_value"], 1900.0)
        self.assertEqual(snapshot["totals"]["total_profit_amount"], 600.0)
        self.assertEqual(snapshot["totals"]["total_profit_pct"], 46.15)
        self.assertAlmostEqual(
            sum(item["weight_pct"] for item in snapshot["holdings"]),
            100.0,
            places=1,
        )
        self.assertEqual(snapshot["market"]["time"], "2026-08-04 10:30")
        self.assertTrue(snapshot["coverage"]["complete"])
        day_start_value = 1500 / 1.015 + 400 / 0.995
        portfolio_day_return = (1900 / day_start_value - 1) * 100
        self.assertAlmostEqual(
            sum(item["day_contribution_pct"] for item in snapshot["holdings"]),
            portfolio_day_return,
            delta=0.01,
        )

    def test_failed_holding_does_not_masquerade_as_complete_portfolio(self):
        def analyzer(code):
            if code == "510300":
                return {"error": "暂时不可用"}
            return _analysis(code)

        snapshot = build_portfolio_snapshot(self.repository, analyzer, _market)

        self.assertFalse(snapshot["coverage"]["complete"])
        self.assertEqual(snapshot["coverage"]["analyzed_count"], 1)
        self.assertEqual(snapshot["coverage"]["cost_coverage_pct"], 76.92)
        self.assertIsNone(snapshot["totals"]["total_market_value"])
        self.assertEqual(snapshot["totals"]["analyzed_market_value"], 1500.0)
        self.assertEqual(snapshot["analytics"]["concentration"]["assessment"], "数据不足")
        self.assertEqual(snapshot["analytics"]["portfolio_trend"]["state"], "数据不足")
        self.assertIn("成功子集", snapshot["data_boundaries"][0])

    def test_scan_is_keyless_zero_token_and_does_not_create_report(self):
        assistant = PortfolioReportAssistant(
            self.config,
            self.repository,
            analyzer=_analysis,
            market_provider=_market,
            llm_call=lambda *args: self.fail("扫描不应调用模型"),
        )

        tracked_tables = (
            "portfolio_reports",
            "ai_usage",
            "daily_snapshots",
            "events",
        )
        before = {
            table: self.repository.count_rows(table) for table in tracked_tables
        }
        result = assistant.scan()
        after = {
            table: self.repository.count_rows(table) for table in tracked_tables
        }

        self.assertFalse(result["ai_used"])
        self.assertEqual(result["token_usage"], 0)
        self.assertEqual(after, before)

    def test_incomplete_legacy_holding_is_rejected_before_market_analysis(self):
        self.repository.upsert_watch(
            "600000", "浦发银行", quantity=100, cost_price=None
        )
        assistant = PortfolioReportAssistant(
            self.config,
            self.repository,
            analyzer=lambda code: self.fail("输入不完整时不应请求行情"),
            market_provider=_market,
        )

        with self.assertRaisesRegex(ValueError, "600000"):
            assistant.scan()

        self.assertEqual(self.repository.count_rows("portfolio_reports"), 0)

    def test_first_stage_cannot_see_locked_user_judgment_and_cache_is_reused(self):
        secret_judgment = "我的秘密判断：短期波动让我担忧。"
        calls = []

        def fake_llm(system, user, api_key, max_tokens):
            latest = self.repository.get_latest_portfolio_report()
            calls.append({"system": system, "user": user, "api_key": api_key})
            self.assertEqual(latest["status"], "pending")
            self.assertEqual(latest["user_judgment"], secret_judgment)
            if system == INDEPENDENT_SYSTEM_PROMPT:
                self.assertNotIn(secret_judgment, user)
                self.assertEqual(max_tokens, INDEPENDENT_MAX_TOKENS)
                first_input = json.loads(user)
                catalog = first_input["confirmed_fact_catalog"]
                self.assertTrue(catalog)
                self.assertIn(
                    "portfolio.coverage",
                    {row["evidence_id"] for row in catalog},
                )
                return {"content": json.dumps(_independent_payload(), ensure_ascii=False), "token_usage": 111}
            self.assertEqual(system, COMPARISON_SYSTEM_PROMPT)
            self.assertIn(secret_judgment, user)
            self.assertEqual(max_tokens, COMPARISON_MAX_TOKENS)
            self.assertIn('"comparison_facts"', user)
            self.assertNotIn('"technical"', user)
            self.assertNotIn('"moneyflow"', user)
            return {"content": json.dumps(_comparison_payload(), ensure_ascii=False), "token_usage": 89}

        assistant = PortfolioReportAssistant(
            self.config,
            self.repository,
            analyzer=_analysis,
            market_provider=_market,
            llm_call=fake_llm,
        )
        now = datetime(2026, 8, 4, 3, 0, tzinfo=timezone.utc)
        first = assistant.generate(secret_judgment, "test-key", now=now)
        second = assistant.generate(secret_judgment, "", now=now)

        self.assertEqual(len(calls), 2)
        self.assertEqual(first["token_usage"], 200)
        self.assertFalse(first["cached"])
        self.assertEqual(
            first["independent_analysis"]["issues"][0]["confirmed_fact_ids"],
            ["portfolio.coverage"],
        )
        self.assertIn(
            "2/2只持仓",
            first["independent_analysis"]["issues"][0]["confirmed_facts"][0],
        )
        self.assertTrue(second["cached"])
        self.assertEqual(second["token_usage"], 0)
        self.assertEqual(second["user_judgment"], secret_judgment)
        self.assertEqual(self.repository.count_rows("portfolio_reports"), 2)
        latest = assistant.latest()["report"]
        self.assertEqual(latest["status"], "complete")
        self.assertEqual(latest["provider"], "deepseek")
        self.assertEqual(latest["comparison"]["agreements"], ["双方都关注组合波动。"])
        cleanup = self.repository.cleanup(7, 365, now + timedelta(days=7))
        self.assertEqual(cleanup["portfolio_reports"], 0)
        self.assertEqual(self.repository.count_rows("portfolio_reports"), 2)
        self.assertEqual(len(assistant.history()["reports"]), 2)
        self.assertIsNone(
            self.repository.get_cached_portfolio_report(
                latest["cache_key"], now + timedelta(days=7)
            )
        )

    def test_empty_judgment_is_rejected_before_persistence_or_model_call(self):
        assistant = PortfolioReportAssistant(
            self.config,
            self.repository,
            analyzer=_analysis,
            market_provider=_market,
            llm_call=lambda *args: self.fail("不应调用模型"),
        )

        with self.assertRaisesRegex(ValueError, "我的当前判断"):
            assistant.generate("   ", "test-key")

        self.assertEqual(self.repository.count_rows("portfolio_reports"), 0)

    def test_model_choice_is_part_of_portfolio_report_cache(self):
        calls = []

        def fake_llm(system, user, api_key, max_tokens):
            calls.append(system)
            payload = (
                _independent_payload()
                if system == INDEPENDENT_SYSTEM_PROMPT
                else _comparison_payload()
            )
            return {
                "content": json.dumps(payload, ensure_ascii=False),
                "token_usage": 10,
            }

        assistant = PortfolioReportAssistant(
            self.config,
            self.repository,
            analyzer=_analysis,
            market_provider=_market,
            llm_call=fake_llm,
        )
        now = datetime(2026, 8, 4, 3, 0, tzinfo=timezone.utc)

        flash = assistant.generate(
            "同一判断",
            "test-key",
            now=now,
            deepseek_model="deepseek-v4-flash",
        )
        pro = assistant.generate(
            "同一判断",
            "test-key",
            now=now,
            deepseek_model="deepseek-v4-pro",
        )

        self.assertFalse(flash["cached"])
        self.assertFalse(pro["cached"])
        self.assertEqual(flash["model"], "deepseek-v4-flash")
        self.assertEqual(pro["model"], "deepseek-v4-pro")
        self.assertEqual(len(calls), 4)

    def test_two_stage_budget_is_rejected_before_first_call(self):
        limited_config = MonitorConfig(
            db_path=self.config.db_path,
            portfolio_report_daily_call_limit=1,
            portfolio_report_daily_token_limit=0,
        )
        calls = []
        assistant = PortfolioReportAssistant(
            limited_config,
            self.repository,
            analyzer=_analysis,
            market_provider=_market,
            llm_call=lambda *args: calls.append(args),
        )
        now = datetime(2026, 8, 4, 3, 0, tzinfo=timezone.utc)

        with self.assertRaisesRegex(ValueError, "次数已达上限"):
            assistant.generate("测试判断", "test-key", now=now)

        self.assertEqual(calls, [])
        self.assertEqual(
            self.repository.get_ai_usage("2026-08-04", "portfolio_report")["calls"],
            0,
        )

    def test_independent_validation_rejects_empty_required_content(self):
        cases = []
        empty_summary = _independent_payload()
        empty_summary["summary"] = "  "
        cases.append((empty_summary, "summary 不能为空"))
        empty_title = _independent_payload()
        empty_title["issues"][0]["title"] = ""
        cases.append((empty_title, "标题和重要性不能为空"))
        empty_why = _independent_payload()
        empty_why["issues"][0]["why_important"] = "  "
        cases.append((empty_why, "标题和重要性不能为空"))
        no_facts = _independent_payload()
        no_facts["issues"][0]["confirmed_fact_ids"] = []
        cases.append((no_facts, "必须引用已确认事实"))
        unknown_fact = _independent_payload(["model.invented.fact"])
        cases.append((unknown_fact, "引用未知证据 ID"))

        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    _normalize_independent(payload, _confirmed_fact_catalog())

    def test_confirmed_facts_are_restored_from_deterministic_catalog(self):
        payload = _independent_payload()
        normalized = _normalize_independent(payload, _confirmed_fact_catalog())

        issue = normalized["issues"][0]
        self.assertEqual(issue["confirmed_fact_ids"], ["portfolio.coverage"])
        self.assertEqual(
            issue["confirmed_facts"],
            ["组合数据覆盖2/2只持仓，当前价格覆盖2/2只，完整组合：是。"],
        )
        self.assertNotIn("confirmed_facts", payload["issues"][0])

    def test_snapshot_builds_unique_confirmed_fact_catalog(self):
        snapshot = build_portfolio_snapshot(self.repository, _analysis, _market)

        catalog = build_confirmed_fact_catalog(snapshot)
        evidence_ids = [row["evidence_id"] for row in catalog]
        facts = {row["evidence_id"]: row["fact"] for row in catalog}

        self.assertEqual(len(evidence_ids), len(set(evidence_ids)))
        self.assertIn("portfolio.coverage", facts)
        self.assertIn("holding.600519.overview", facts)
        self.assertIn("2/2只持仓", facts["portfolio.coverage"])

    def test_real_call_contract_requests_deterministic_json(self):
        response = {
            "choices": [
                {
                    "message": {"content": "{}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 17},
        }
        with patch("app.api_post", return_value=response) as api_post:
            result = PortfolioReportAssistant._call_llm(
                "只输出 JSON",
                "输入",
                "test-key",
                300,
                "deepseek-v4-pro",
            )

        body = api_post.call_args.args[2]
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 300)
        self.assertEqual(body["model"], "deepseek-v4-pro")
        self.assertEqual(result["token_usage"], 17)

    def test_gpt_call_uses_responses_api_and_explicit_low_reasoning(self):
        response = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "{}"}],
                }
            ],
            "usage": {"total_tokens": 23},
        }
        with patch("app.api_post", return_value=response) as api_post:
            result = PortfolioReportAssistant._call_gpt(
                "只输出 JSON", "输入", "test-openai-key", 300, GPT_INDEPENDENT_REPORT_SCHEMA
            )

        url, headers, body = api_post.call_args.args[:3]
        self.assertEqual(url, "https://api.openai.com/v1/responses")
        self.assertEqual(headers["Authorization"], "Bearer test-openai-key")
        self.assertEqual(body["model"], "gpt-5.6-sol")
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertEqual(body["text"]["format"]["name"], "portfolio_report")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["text"]["format"]["schema"], GPT_INDEPENDENT_REPORT_SCHEMA)
        self.assertFalse(body["store"])
        self.assertEqual(result["content"], "{}")
        self.assertEqual(result["token_usage"], 23)

    def test_gpt_call_keeps_provider_400_detail_without_leaking_key(self):
        error = HTTPError(
            "https://api.openai.com/v1/responses",
            400,
            "Bad Request",
            None,
            BytesIO(b'{"error":{"message":"Unsupported request field"}}'),
        )
        with patch("app.api_post", side_effect=error):
            with self.assertRaisesRegex(
                ValueError, "GPT 请求被拒（HTTP 400）：Unsupported request field"
            ):
                PortfolioReportAssistant._call_gpt(
                    "只输出 JSON",
                    "输入",
                    "test-openai-key",
                    300,
                    GPT_INDEPENDENT_REPORT_SCHEMA,
                )

    def test_comparison_facts_keep_core_values_without_repeating_detail(self):
        snapshot = build_portfolio_snapshot(self.repository, _analysis, _market)

        facts = build_comparison_facts(snapshot)

        self.assertEqual(len(facts["holdings"]), 2)
        self.assertIn("profit_pct", facts["holdings"][0])
        self.assertIn("valuation", facts["holdings"][0])
        self.assertNotIn("technical", facts["holdings"][0])
        self.assertNotIn("moneyflow", facts["holdings"][0])
        self.assertNotIn("latest_fundamental", facts["holdings"][0])
        self.assertNotIn("holdings", facts["diagnostic_replay"])
        self.assertNotIn("holding_states", facts["diagnostic_replay"])

    def test_comparison_facts_keep_all_indices_and_sectors_compact(self):
        snapshot = build_portfolio_snapshot(self.repository, _analysis, _market)

        def market_row(code, index, ranked=False):
            return {
                "code": code,
                "name": f"样本 {code}",
                "data_end": "2026-08-04",
                "returns": {"5d": index / 10, "20d": index / 5, "60d": 99},
                "relative_market": {"20d": index},
                "drawdown_60d": -10,
                "volatility_20d": 20,
                "trend": "强势" if index < 3 else "弱势",
                "rank_5d": index + 1 if ranked else None,
                "rank_20d": 16 - index if ranked else None,
                "rank_change": 15 - index * 2 if ranked else None,
                "stale": index == 15,
            }

        indices = [market_row(f"I{index:02d}", index) for index in range(6)]
        sectors = [market_row(f"S{index:02d}", index, True) for index in range(16)]
        snapshot["analytics"]["market_context"] = {
            "benchmark": indices[0],
            "indices": indices,
            "sectors": sectors,
        }

        facts = build_comparison_facts(snapshot)

        self.assertEqual(len(facts["market_context"]["indices"]), 6)
        self.assertEqual(len(facts["market_context"]["sectors"]), 16)
        self.assertEqual(facts["market_context"]["sectors"][-1]["code"], "S15")
        compact = facts["market_context"]["sectors"][-1]
        self.assertEqual(compact["return_5d_pct"], 1.5)
        self.assertEqual(compact["return_20d_pct"], 3.0)
        self.assertEqual(compact["rank_5d"], 16)
        self.assertTrue(compact["stale"])
        self.assertNotIn("returns", compact)
        self.assertNotIn("relative_market", compact)
        self.assertNotIn("volatility_20d", compact)

    def test_gpt_provider_is_persisted_for_latest_and_history(self):
        def fake_llm(system, user, api_key, max_tokens):
            payload = (
                _independent_payload()
                if system == INDEPENDENT_SYSTEM_PROMPT
                else _comparison_payload()
            )
            return json.dumps(payload, ensure_ascii=False), 10

        assistant = PortfolioReportAssistant(
            self.config,
            self.repository,
            analyzer=_analysis,
            market_provider=_market,
            llm_call=fake_llm,
        )

        report = assistant.generate("测试判断", "test-key", provider="gpt")
        latest = assistant.latest()["report"]
        history = assistant.history()["reports"]

        self.assertEqual(report["provider"], "gpt")
        self.assertEqual(latest["provider"], "gpt")
        self.assertEqual(history[0]["provider"], "gpt")
        self.assertEqual(latest["model"], "gpt-5.6-sol")

    def test_length_finish_reason_is_counted_and_stops_before_second_stage(self):
        calls = []

        def fake_llm(*args):
            calls.append(args)
            return {
                "content": '{"summary":"未完成',
                "token_usage": 321,
                "finish_reason": "length",
            }

        assistant = PortfolioReportAssistant(
            self.config,
            self.repository,
            analyzer=_analysis,
            market_provider=_market,
            llm_call=fake_llm,
        )
        now = datetime(2026, 8, 4, 3, 0, tzinfo=timezone.utc)

        with self.assertRaisesRegex(ValueError, "输出过长"):
            assistant.generate("测试判断", "test-key", now=now)

        usage = self.repository.get_ai_usage("2026-08-04", "portfolio_report")
        self.assertEqual(len(calls), 1)
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["tokens"], 321)

    def test_single_fenced_json_block_is_accepted(self):
        def fake_llm(system, user, api_key, max_tokens):
            payload = (
                _independent_payload()
                if system == INDEPENDENT_SYSTEM_PROMPT
                else _comparison_payload()
            )
            return {
                "content": "```json\n"
                + json.dumps(payload, ensure_ascii=False)
                + "\n```",
                "token_usage": 20,
                "finish_reason": "stop",
            }

        assistant = PortfolioReportAssistant(
            self.config,
            self.repository,
            analyzer=_analysis,
            market_provider=_market,
            llm_call=fake_llm,
        )

        result = assistant.generate("测试判断", "test-key")

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["token_usage"], 40)

    def test_invalid_json_keeps_actual_failed_call_usage(self):
        assistant = PortfolioReportAssistant(
            self.config,
            self.repository,
            analyzer=_analysis,
            market_provider=_market,
            llm_call=lambda *args: {
                "content": "这不是 JSON",
                "token_usage": 123,
                "finish_reason": "stop",
            },
        )
        now = datetime(2026, 8, 4, 3, 0, tzinfo=timezone.utc)

        with self.assertRaisesRegex(ValueError, "严格 JSON"):
            assistant.generate("测试判断", "test-key", now=now, provider="gpt")

        usage = self.repository.get_ai_usage("2026-08-04", "portfolio_report")
        latest = self.repository.get_latest_portfolio_report(now)
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["tokens"], 123)
        self.assertEqual(latest["status"], "failed")
        self.assertEqual(latest["payload"]["provider"], "gpt")

    def test_second_stage_failure_settles_both_returned_calls(self):
        calls = []

        def fake_llm(system, user, api_key, max_tokens):
            calls.append(system)
            if system == INDEPENDENT_SYSTEM_PROMPT:
                return {
                    "content": json.dumps(_independent_payload(), ensure_ascii=False),
                    "token_usage": 111,
                    "finish_reason": "stop",
                }
            return {
                "content": "不是 JSON",
                "token_usage": 89,
                "finish_reason": "stop",
            }

        assistant = PortfolioReportAssistant(
            self.config,
            self.repository,
            analyzer=_analysis,
            market_provider=_market,
            llm_call=fake_llm,
        )
        now = datetime(2026, 8, 4, 3, 0, tzinfo=timezone.utc)

        with self.assertRaisesRegex(ValueError, "严格 JSON"):
            assistant.generate("测试判断", "test-key", now=now)

        usage = self.repository.get_ai_usage("2026-08-04", "portfolio_report")
        self.assertEqual(len(calls), 2)
        self.assertEqual(usage["calls"], 2)
        self.assertEqual(usage["tokens"], 200)

    def test_history_keeps_only_latest_seven_completed_reports(self):
        now = datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)
        completed_ids = []
        for index in range(8):
            report_id = self.repository.create_portfolio_report(
                f"判断 {index + 1}", "test-model", 30, now
            )
            self.repository.complete_portfolio_report(
                report_id,
                f"cache-{index}",
                f"snapshot-{index}",
                {
                    "independent_analysis": _independent_payload(),
                    "comparison": _comparison_payload(),
                },
                index + 1,
                now,
            )
            completed_ids.append(report_id)
        failed_id = self.repository.create_portfolio_report(
            "失败判断", "test-model", 30, now
        )
        self.repository.fail_portfolio_report(failed_id, "失败", now)
        pending_id = self.repository.create_portfolio_report(
            "待处理判断", "test-model", 30, now
        )

        removed = self.repository.trim_portfolio_reports(7)
        cleanup = self.repository.cleanup(7, 365, now + timedelta(days=31))
        assistant = PortfolioReportAssistant(self.config, self.repository)
        history = assistant.history()

        self.assertEqual(removed, 1)
        self.assertEqual(
            [report["id"] for report in history["reports"]],
            list(reversed(completed_ids[-7:])),
        )
        self.assertEqual(history["limit"], 7)
        self.assertEqual(cleanup["portfolio_reports"], 2)
        self.assertEqual(self.repository.count_rows("portfolio_reports"), 7)
        self.assertIsNone(
            self.repository.get_latest_portfolio_report(now + timedelta(days=31))
        )
        self.assertGreater(pending_id, failed_id)


if __name__ == "__main__":
    unittest.main()
