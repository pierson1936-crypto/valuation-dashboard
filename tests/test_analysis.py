import unittest
import json
import math
from unittest.mock import patch

import app

from tests.fixtures import kline_rows, stock_meta, valuation_rows


class DeepSeekModelTests(unittest.TestCase):
    def test_only_supported_deepseek_models_are_accepted(self):
        self.assertEqual(
            app.resolve_deepseek_model("deepseek-v4-flash"),
            "deepseek-v4-flash",
        )
        self.assertEqual(
            app.resolve_deepseek_model("deepseek-v4-pro"),
            "deepseek-v4-pro",
        )
        self.assertEqual(app.resolve_deepseek_model(""), app.AGENT_MODEL)
        with self.assertRaisesRegex(ValueError, "V4 Flash 或 V4 Pro"):
            app.resolve_deepseek_model("deepseek-custom")


class PercentileRankTests(unittest.TestCase):
    def test_midrank_keeps_negative_values_and_ignores_none(self):
        self.assertEqual(app.percentile_rank([None, -1, 0, 1, 1, 2], 1), 60.0)

    def test_empty_sample_or_missing_current_value_returns_none(self):
        self.assertIsNone(app.percentile_rank([], 1))
        self.assertIsNone(app.percentile_rank([1, 2], None))


class KeyLevelAnalysisTests(unittest.TestCase):
    @staticmethod
    def box_chart(trending=False):
        dates = ["2026-05-%02d" % (index + 1) for index in range(60)]
        candles = []
        for index in range(60):
            if trending:
                close = 10.0 + index * 0.12
                opened = close - 0.05
                low, high = opened - 0.08, close + 0.08
            else:
                close = 11.0 + math.sin(index * math.pi / 5) * 0.82
                opened = close - (0.08 if index % 2 else -0.08)
                low = min(opened, close) - 0.16
                high = max(opened, close) + 0.16
            candles.append([opened, close, low, high])
        return {"dates": dates, "candle": candles}

    @staticmethod
    def chip_rows(close_scale=1.0):
        rows = []
        for index in range(120):
            base = (10.0 + math.sin(index * math.pi / 9) * 0.55) * close_scale
            rows.append({
                "date": "2026-08-%02d" % ((index % 28) + 1),
                "open": base - 0.08 * close_scale,
                "close": base,
                "high": base + 0.22 * close_scale,
                "low": base - 0.22 * close_scale,
                "volume": 100000 + index,
                "turnover_pct": 2.0 + index % 4,
            })
        rows[-1]["date"] = "2026-08-12"
        return rows

    @staticmethod
    def confirmed_swing_chart():
        dates = ["2026-06-%02d" % (index + 1) for index in range(60)]
        candles = []
        for index in range(60):
            close = 10.96 + index * 0.002
            opened = close - 0.03
            candles.append([opened, close, close - 0.12, close + 0.12])
        for index in (8, 28, 48):
            candles[index][2] = 10.4
        for index in (16, 36, 54):
            candles[index][3] = 11.8
        candles[-1] = [11.02, 11.08, 10.96, 11.18]
        return {"dates": dates, "candle": candles}

    def test_clear_sideways_range_is_detected(self):
        result = app.detect_consolidation_box(self.box_chart())

        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "inside")
        self.assertGreaterEqual(result["coverage_pct"], 72.0)
        self.assertGreaterEqual(result["lower_touches"], 2)
        self.assertGreaterEqual(result["upper_touches"], 2)

    def test_directional_move_does_not_force_a_box(self):
        self.assertIsNone(app.detect_consolidation_box(self.box_chart(trending=True)))

    def test_confirmed_swing_levels_require_repeated_price_touches(self):
        result = app.detect_confirmed_swing_levels(self.confirmed_swing_chart())

        self.assertAlmostEqual(result["support"]["price"], 10.4, places=2)
        self.assertAlmostEqual(result["pressure"]["price"], 11.8, places=2)
        self.assertGreaterEqual(result["support"]["touches"], 2)
        self.assertGreaterEqual(result["pressure"]["touches"], 2)
        self.assertEqual(result["support"]["source"], "swing")

    def test_directional_move_does_not_force_swing_levels(self):
        result = app.detect_confirmed_swing_levels(self.box_chart(trending=True))

        self.assertIsNone(result["support"])
        self.assertIsNone(result["pressure"])

    def test_price_structure_keeps_box_as_primary_levels(self):
        result = app.detect_price_structure(self.box_chart())

        self.assertIsNotNone(result["box"])
        self.assertEqual(result["support"]["source"], "box")
        self.assertEqual(result["pressure"]["source"], "box")
        self.assertEqual(result["support"]["price"], result["box"]["lower"])
        self.assertEqual(result["pressure"]["price"], result["box"]["upper"])

    def test_chip_kline_fetch_retries_a_transient_disconnect(self):
        payload = {"data": {"klines": []}}
        with patch.object(app, "fetch_json", return_value=payload) as fetch:
            app.fetch_eastmoney_chip_kline("sh", "600000")

        self.assertEqual(fetch.call_args.kwargs["retries"], 2)

    def test_baostock_rows_are_normalized_as_qfq_chip_input(self):
        rows = app._parse_baostock_chip_kline(
            ["date", "open", "high", "low", "close", "volume", "turn"],
            [["2026-08-12", "9.9", "10.2", "9.8", "10.0", "123456", "2.5"]],
        )

        self.assertEqual(rows, [{
            "date": "2026-08-12", "open": 9.9, "close": 10.0,
            "high": 10.2, "low": 9.8, "volume": 123456.0, "turnover_pct": 2.5,
        }])

    def test_chip_estimate_uses_baostock_only_after_eastmoney_fails(self):
        with (
            patch.object(app, "fetch_eastmoney_chip_kline", side_effect=ConnectionError("断开")) as eastmoney,
            patch.object(app, "fetch_baostock_chip_kline", return_value=self.chip_rows()) as baostock,
        ):
            chip = app.estimate_chip_distribution_for_security("sh", "600000")

        self.assertEqual(chip["source"], "baostock")
        self.assertEqual(chip["source_label"], "Baostock")
        eastmoney.assert_called_once_with("sh", "600000")
        baostock.assert_called_once_with("sh", "600000")

    def test_chip_estimate_keeps_eastmoney_when_it_succeeds(self):
        with (
            patch.object(app, "fetch_eastmoney_chip_kline", return_value=self.chip_rows()) as eastmoney,
            patch.object(app, "fetch_baostock_chip_kline") as baostock,
        ):
            chip = app.estimate_chip_distribution_for_security("sh", "600000")

        self.assertEqual(chip["source"], "eastmoney")
        self.assertEqual(chip["source_label"], "东方财富")
        eastmoney.assert_called_once_with("sh", "600000")
        baostock.assert_not_called()

    def test_available_chip_result_keeps_its_raw_source_label(self):
        rows = self.chip_rows()
        latest = rows[-1]
        analyzed = {
            "code": "600000",
            "name": "固定股票",
            "date": latest["date"],
            "is_stock": True,
            "chart": {
                "dates": [latest["date"]],
                "candle": [[latest["open"], latest["close"], latest["low"], latest["high"]]],
            },
        }
        with patch.object(app, "fetch_eastmoney_chip_kline", return_value=rows):
            result = app.build_key_levels(analyzed)

        self.assertEqual(result["chip_status"], "available")
        self.assertEqual(result["chip"]["source_label"], "东方财富")
        self.assertIn("东方财富", result["source_note"])

    def test_chip_estimate_reports_ranges_and_provenance_fields(self):
        result = app.estimate_chip_distribution(self.chip_rows())

        self.assertEqual(result["as_of"], "2026-08-12")
        self.assertEqual(result["sample_count"], 120)
        self.assertEqual(result["adjustment"], "qfq")
        self.assertLess(result["cost_70"]["low"], result["cost_70"]["high"])
        self.assertLessEqual(result["cost_90"]["low"], result["cost_70"]["low"])
        self.assertGreaterEqual(result["profit_ratio_pct"], 0.0)
        self.assertLessEqual(result["profit_ratio_pct"], 100.0)

    def test_chip_peak_relation_uses_price_structure_only_as_confirmation(self):
        chip = {
            "latest_close": 10.5,
            "peak_price": 9.98,
        }
        box = {"lower": 10.0, "upper": 11.0}

        result = app._describe_chip_peak(chip, box)

        self.assertEqual(result["peak_position"], "现价下方")
        self.assertEqual(result["structure_overlap"], "support")
        self.assertIn("筹码重合，参考增强", result["structure_overlap_note"])
        self.assertEqual(result["estimate_label"], "近120日本地模型估算")

    def test_chip_peak_without_price_overlap_stays_cost_density_only(self):
        result = app._describe_chip_peak(
            {"latest_close": 10.0, "peak_price": 12.0},
            {"lower": 9.0, "upper": 11.0},
        )

        self.assertEqual(result["peak_position"], "现价上方")
        self.assertIsNone(result["structure_overlap"])
        self.assertIn("未与已识别", result["structure_overlap_note"])

    def test_etf_uses_box_only_without_fetching_chip_data(self):
        analyzed = {
            "code": "159326",
            "name": "固定 ETF",
            "date": "2026-08-12",
            "is_stock": False,
            "chart": self.box_chart(),
        }
        with patch.object(app, "fetch_eastmoney_chip_kline") as fetch:
            result = app.build_key_levels(analyzed)

        self.assertEqual(result["chip_status"], "not_applicable")
        self.assertIsNone(result["chip"])
        self.assertIsNotNone(result["support"])
        self.assertIsNotNone(result["pressure"])
        fetch.assert_not_called()

    def test_explicit_retry_bypasses_only_an_unavailable_cached_result(self):
        original_cache = dict(app._KEY_LEVEL_CACHE)
        stale = {"code": "600000", "chip_status": "unavailable"}
        fresh = {"code": "600000", "chip_status": "available"}
        try:
            app._KEY_LEVEL_CACHE.clear()
            app._KEY_LEVEL_CACHE["600000"] = (app.time.time(), stale)
            with (
                patch.object(app, "analyze_cached", return_value={"code": "600000"}),
                patch.object(app, "build_key_levels", return_value=fresh) as build,
            ):
                result = app.key_levels_cached("600000", retry_failure=True)
        finally:
            app._KEY_LEVEL_CACHE.clear()
            app._KEY_LEVEL_CACHE.update(original_cache)

        self.assertEqual(result, fresh)
        build.assert_called_once()

    def test_price_mismatch_blocks_chip_overlay(self):
        rows = self.chip_rows(close_scale=1.25)
        analyzed = {
            "code": "600000",
            "name": "固定股票",
            "date": "2026-08-12",
            "is_stock": True,
            "chart": {
                **self.box_chart(),
                "dates": ["2026-08-12"],
                "candle": [[9.9, 10.0, 9.8, 10.1]],
            },
        }
        with patch.object(app, "fetch_eastmoney_chip_kline", return_value=rows):
            result = app.build_key_levels(analyzed)

        self.assertEqual(result["chip_status"], "price_mismatch")
        self.assertIsNone(result["chip"])


class MarketHistoryCacheTests(unittest.TestCase):
    def test_market_history_reuses_fifteen_minute_cache(self):
        original_cache = list(app._MKT_HISTORY)
        calls = []

        def fake_kline(prefix, code, count):
            calls.append((prefix, code, count))
            rows = [
                {
                    "date": f"2026-07-{day:02d}",
                    "open": 100.0,
                    "close": 100.0 + day,
                    "high": 101.0 + day,
                    "low": 99.0,
                    "vol": 1000.0,
                }
                for day in range(1, 21)
            ]
            return rows, "固定名称", None

        try:
            app._MKT_HISTORY[:] = [0.0, None]
            with patch.object(app, "fetch_kline", side_effect=fake_kline):
                first = app.market_history()
                second = app.market_history()
        finally:
            app._MKT_HISTORY[:] = original_cache

        expected = len(app.MARKET_INDICES) + len(app.MARKET_SECTORS)
        self.assertEqual(len(calls), expected)
        self.assertIs(first, second)
        self.assertEqual(len(first["indices"]), len(app.MARKET_INDICES))
        self.assertEqual(len(first["sectors"]), len(app.MARKET_SECTORS))
        self.assertEqual(first["errors"], [])


class IntradayAnalysisTests(unittest.TestCase):
    def tearDown(self):
        app._INTRADAY_CACHE.clear()
        app._INTRADAY_COMPARISON_CACHE.clear()
        app._INDEX_REFERENCE_CACHE.clear()

    def test_tencent_minute_payload_is_normalized_to_previous_close(self):
        quote = [""] * 5
        quote[1], quote[4] = "固定样例", "9.90"
        payload = {
            "data": {
                "sh600000": {
                    "data": {
                        "date": "20260812",
                        "data": [
                            "0930 10.00 100 100000",
                            "0931 10.10 130 130300",
                        ],
                    },
                    "qt": {"sh600000": quote},
                }
            }
        }

        result = app._parse_intraday_payload(payload, "sh600000")

        self.assertEqual(result["name"], "固定样例")
        self.assertEqual(result["previous_close"], 9.9)
        self.assertEqual(result["points"][0]["time"], "09:30")
        self.assertEqual(result["points"][0]["volume"], 100.0)
        self.assertEqual(result["points"][1]["volume"], 30.0)
        self.assertEqual(result["points"][1]["change_pct"], 2.02)
        self.assertAlmostEqual(result["points"][1]["average_price"], 10.023, places=3)

    def test_etf_uses_matched_tracking_index_without_code_special_case(self):
        result = {
            "code": "159326",
            "name": "固定ETF",
            "is_stock": False,
            "classify": "Fund",
            "etf_context": {"tracking_index": "固定主题指数"},
        }
        matched = {"prefix": "sz", "code": "399999", "name": "固定主题指数"}

        with patch.object(app, "resolve_index_reference", return_value=matched):
            benchmark = app._intraday_benchmark(result)

        self.assertEqual(benchmark["code"], "399999")
        self.assertEqual(benchmark["basis"], "ETF 跟踪指数")

    def test_intraday_comparison_calculates_relative_strength(self):
        result = {
            "code": "600000",
            "name": "固定股票",
            "is_stock": True,
            "classify": "AStock",
            "type_name": "股票",
            "chart": {
                "dates": ["2026-08-11"],
                "candle": [[10.0, 10.0, 9.9, 10.1]],
            },
        }
        subject = {
            "name": "固定股票",
            "date": "20260812",
            "as_of": "09:31",
            "points": [
                {"time": "09:30", "price": 10.0, "average_price": 10.0, "volume": 100.0, "change_pct": 0.0},
                {"time": "09:31", "price": 10.2, "average_price": 10.1, "volume": 40.0, "change_pct": 2.0},
            ],
        }
        benchmark = {
            "name": "上证指数",
            "date": "20260812",
            "as_of": "09:31",
            "points": [
                {"time": "09:30", "price": 3000.0, "average_price": None, "volume": 1000.0, "change_pct": 0.0},
                {"time": "09:31", "price": 3015.0, "average_price": None, "volume": 500.0, "change_pct": 0.5},
            ],
        }

        with patch.object(app, "fetch_intraday", side_effect=[subject, benchmark]):
            comparison = app.build_intraday_comparison(result)

        self.assertEqual(comparison["benchmark"]["name"], "上证指数")
        self.assertEqual(comparison["summary"]["relative_latest_pct"], 1.5)
        self.assertEqual(comparison["summary"]["above_benchmark_pct"], 50.0)
        self.assertEqual(comparison["checkpoints"][-1]["relative_pct"], 1.5)

    def test_failed_intraday_comparison_uses_short_cache(self):
        result = {
            "code": "600000",
            "name": "固定股票",
            "is_stock": True,
            "classify": "AStock",
            "chart": {"dates": ["2026-08-11"], "candle": [[10.0, 10.0, 9.9, 10.1]]},
        }
        app._INTRADAY_COMPARISON_CACHE[("600000", "")] = (
            app.time.time() - app.INTRADAY_FAILURE_TTL - 1,
            {"error": "已过期的固定失败"},
        )

        with patch.object(app, "fetch_intraday", return_value=None) as fetch:
            result = app.build_intraday_comparison(result)

        self.assertEqual(result["error"], "今日分时暂不可用，主分析不受影响。")
        self.assertEqual(fetch.call_count, 2)


class IndependentSecurityReportTests(unittest.TestCase):
    @staticmethod
    def fixed_result():
        closes = [10 + index * 0.1 for index in range(81)]
        return {
            "code": "600000",
            "name": "固定样例",
            "type_name": "股票",
            "is_stock": True,
            "price": 18.0,
            "chg": 1.2,
            "date": "2026-01-01",
            "start": "2025-01-01",
            "count": 81,
            "price_pct": 80.0,
            "price_lo": 10.0,
            "price_hi": 18.0,
            "from_hi": 0.0,
            "from_lo": 80.0,
            "pe": 20.0,
            "pe_pct": 75.0,
            "pb": 2.0,
            "pb_pct": 70.0,
            "chart": {"candle": [[value, value, value, value] for value in closes]},
            "tech": {
                "ma5": 17.8,
                "ma20": 17.1,
                "ma60": 15.0,
                "rsi": 62.0,
                "macd_dif": 0.2,
                "macd_dea": 0.1,
                "macd_hist": 0.1,
                "vol_ratio": 1.2,
                "vola": 25.0,
                "mdd": 12.0,
            },
            "company_context": {
                "industry_path": ["固定一级", "固定二级"],
                "business_title": "固定主营",
                "concepts": ["固定概念"],
            },
            "risk": {"score": 99, "reasons": ["预写风险结论"]},
            "report": {"technical": ["预写技术结论"]},
            "alerts": [{"t": "预写提醒结论"}],
        }

    @staticmethod
    def fixed_market():
        return {
            "time": "2026-01-01 10:30",
            "indices": [
                {"code": "000001", "name": "上证指数", "chg": 0.5},
                {"code": "399006", "name": "创业板指", "chg": -0.3},
            ],
            "sectors": [
                {"code": "512480", "name": "半导体", "chg": 2.0},
                {"code": "512800", "name": "银行", "chg": -1.0},
            ],
        }

    def test_evidence_catalog_uses_raw_facts_without_rule_conclusions(self):
        evidence = app.build_security_ai_evidence(
            self.fixed_result(), self.fixed_market()
        )

        self.assertEqual(
            [item["id"] for item in evidence],
            ["E%02d" % index for index in range(1, len(evidence) + 1)],
        )
        serialized = json.dumps(evidence, ensure_ascii=False)
        self.assertNotIn("预写风险结论", serialized)
        self.assertNotIn("预写技术结论", serialized)
        self.assertNotIn("预写提醒结论", serialized)
        self.assertIn("多周期涨跌", serialized)
        self.assertIn("大盘涨跌快照", serialized)
        self.assertIn("完整分时走势", serialized)

    def test_security_evidence_keeps_industry_flow_semantics(self):
        market = self.fixed_market()
        market["stale"] = True
        market["flow_complete"] = True
        market["sectors"] = [{
            "code": "BK0436", "name": "计算机", "chg": 2.5, "main_net": 8.9,
        }]

        evidence = app.build_security_ai_evidence(self.fixed_result(), market)
        flow = next(item for item in evidence if item["topic"] == "行业板块资金流快照")

        self.assertEqual(flow["data"]["items"][0]["main_net_inflow_yi"], 8.9)
        self.assertTrue(flow["data"]["stale"])

    def test_security_evidence_labels_ths_net_without_calling_it_main_net(self):
        market = self.fixed_market()
        market["flow_complete"] = True
        market["flow_source"] = "ths"
        market["sectors"] = [{
            "code": "THS:半导体", "name": "半导体", "chg": 3.0,
            "flow_net": 8.9, "main_net": None,
        }]

        evidence = app.build_security_ai_evidence(self.fixed_result(), market)
        flow = next(item for item in evidence if item["topic"] == "行业板块资金流快照")

        self.assertEqual(flow["data"]["items"][0]["net_amount_yi"], 8.9)
        self.assertNotIn("main_net_inflow_yi", flow["data"]["items"][0])
        self.assertIn("同花顺", flow["data"]["meaning"])

    def test_incomplete_industry_snapshot_is_not_fund_flow_evidence(self):
        market = self.fixed_market()
        market["source"] = "industry_flow"
        market["flow_complete"] = False
        market["sectors"] = [{
            "code": "BK0436", "name": "计算机", "chg": 2.5, "main_net": 8.9,
        }]

        evidence = app.build_security_ai_evidence(self.fixed_result(), market)
        serialized = json.dumps(evidence, ensure_ascii=False)

        self.assertNotIn("行业板块资金流快照", [item["topic"] for item in evidence])
        self.assertIn("行业板块涨跌快照", [item["topic"] for item in evidence])
        self.assertNotIn("main_net_inflow_yi", serialized)

    def test_chip_evidence_only_contains_allowed_estimate_fields(self):
        key_levels = {
            "chip_status": "available",
            "chip": {
                "peak_price": 17.2,
                "peak_position": "现价下方",
                "structure_overlap": "support",
                "structure_overlap_note": "筹码峰与 K 线可能支撑重合，筹码重合，参考增强。",
                "as_of": "2026-01-01",
                "sample_count": 120,
                "estimate_label": "近120日本地模型估算",
                "cost_70": {"low": 16.0, "high": 17.5},
                "profit_ratio_pct": 78.0,
            },
        }

        evidence = app.build_security_ai_evidence(
            self.fixed_result(), self.fixed_market(), key_level_data=key_levels
        )
        item = next(row for row in evidence if row["topic"] == "估算成本密集区")

        self.assertEqual(item["data"]["peak_price"], 17.2)
        self.assertEqual(item["data"]["relative_to_current_price"], "现价下方")
        self.assertEqual(item["data"]["structure_overlap"], "support")
        self.assertEqual(item["data"]["sample_days"], 120)
        self.assertNotIn("cost_70", item["data"])
        self.assertNotIn("profit_ratio_pct", item["data"])
        self.assertIn("不代表真实账户持仓", item["data"]["boundary"])

    def test_unavailable_chip_data_is_omitted_from_ai_evidence(self):
        evidence = app.build_security_ai_evidence(
            self.fixed_result(),
            self.fixed_market(),
            key_level_data={"chip_status": "price_mismatch", "chip": None},
        )

        self.assertNotIn("估算成本密集区", [item["topic"] for item in evidence])

    def test_model_selects_evidence_and_prompt_does_not_prescribe_sections(self):
        response = {
            "choices": [{
                "message": {
                    "content": "整体判断由数据关系决定。[[E02]]\n后续观察条件另行说明。[[E05]]"
                }
            }]
        }
        with (
            patch.object(app, "analyze_cached", return_value=self.fixed_result()),
            patch.object(app, "market_overview", return_value=self.fixed_market()),
            patch.object(app, "build_intraday_comparison", return_value={"error": "固定样例无分时"}),
            patch.object(app, "key_levels_cached", return_value={"chip_status": "unavailable"}),
            patch.object(app, "api_post", return_value=response) as api_post,
        ):
            result = app.generate_security_ai_report(
                "600000", "test-key", "deepseek-v4-flash"
            )

        request_body = api_post.call_args.args[2]
        prompt = request_body["messages"][1]["content"]
        self.assertIn("重点选择、顺序和表达由你自行决定", prompt)
        self.assertNotIn("一、今日盘面", prompt)
        self.assertNotIn("预写风险结论", prompt)
        self.assertEqual([item["id"] for item in result["evidence"]], ["E02", "E05"])

    def test_generated_report_prompt_uses_only_allowed_chip_evidence(self):
        response = {
            "choices": [{"message": {"content": "本地估算仅供观察。[[E06]][[E07]]"}}]
        }
        key_levels = {
            "chip_status": "available",
            "chip": {
                "peak_price": 17.2,
                "peak_position": "现价下方",
                "structure_overlap": "support",
                "structure_overlap_note": "筹码峰与 K 线可能支撑重合，筹码重合，参考增强。",
                "as_of": "2026-01-01",
                "sample_count": 120,
                "estimate_label": "近120日本地模型估算",
                "cost_70": {"low": 16.0, "high": 17.5},
                "profit_ratio_pct": 78.0,
            },
        }
        with (
            patch.object(app, "analyze_cached", return_value=self.fixed_result()),
            patch.object(app, "market_overview", return_value=self.fixed_market()),
            patch.object(app, "build_intraday_comparison", return_value={"error": "固定样例无分时"}),
            patch.object(app, "key_levels_cached", return_value=key_levels) as load_levels,
            patch.object(app, "api_post", return_value=response) as api_post,
        ):
            app.generate_security_ai_report(
                "600000", "test-key", "deepseek-v4-flash"
            )

        prompt = api_post.call_args.args[2]["messages"][1]["content"]
        load_levels.assert_called_once_with("600000")
        self.assertIn('"topic": "估算成本密集区"', prompt)
        self.assertIn("近120日本地模型估算", prompt)
        self.assertNotIn("profit_ratio_pct", prompt)
        self.assertNotIn("cost_70", prompt)
        self.assertIn("不得据此推断持有人必然买卖", prompt)

    def test_unknown_evidence_reference_is_rejected(self):
        response = {
            "choices": [{
                "message": {"content": "现有事实支持有限。[[E01]][[E99]]"}
            }]
        }
        with (
            patch.object(app, "analyze_cached", return_value=self.fixed_result()),
            patch.object(app, "market_overview", return_value=self.fixed_market()),
            patch.object(app, "build_intraday_comparison", return_value={"error": "固定样例无分时"}),
            patch.object(app, "key_levels_cached", return_value={"chip_status": "unavailable"}),
            patch.object(app, "api_post", return_value=response),
        ):
            with self.assertRaisesRegex(ValueError, "不存在的事实编号"):
                app.generate_security_ai_report(
                    "600000", "test-key", "deepseek-v4-flash"
                )

    def test_market_report_is_open_form_but_evidence_bound(self):
        response = {
            "choices": [{
                "message": {"content": "市场分化是当前主线。[[E01]][[E02]]"}
            }]
        }
        with patch.object(app, "api_post", return_value=response) as api_post:
            result = app.generate_market_ai_report(
                "test-key", self.fixed_market(), "deepseek-v4-flash"
            )

        prompt = api_post.call_args.args[2]["messages"][1]["content"]
        self.assertIn("组织顺序和小标题由你决定", prompt)
        self.assertNotIn("一、今日盘面", prompt)
        self.assertEqual([item["id"] for item in result["evidence"]], ["E01", "E02"])


class AnalyzeContractTests(unittest.TestCase):
    def test_fixed_stock_sample_keeps_core_output_contract(self):
        rows = kline_rows()
        company_context = {
            "industry": "固定三级行业",
            "industry_path": ["固定一级行业", "固定二级行业", "固定三级行业"],
            "concepts": ["固定概念"],
            "business_title": "固定主营",
            "business_summary": "固定主营摘要",
            "source_note": "固定来源说明",
        }
        with (
            patch.object(app, "resolve", return_value=stock_meta()),
            patch.object(app, "fetch_kline", return_value=(rows, "固定样例", None)),
            patch.object(app, "fetch_moneyflow", return_value=[]),
            patch.object(app, "fetch_valuation", return_value=valuation_rows()),
            patch.object(app, "fetch_fundamentals", return_value=[]),
            patch.object(app, "fetch_company_context", return_value=company_context),
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
            "company_context",
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
        self.assertEqual(result["company_context"], company_context)
        self.assertEqual(len(result["chart"]["dates"]), 80)

    def test_missing_kline_returns_error_instead_of_crashing(self):
        with (
            patch.object(app, "resolve", return_value=stock_meta()),
            patch.object(app, "fetch_kline", return_value=([], "", None)),
            patch.object(app, "fetch_moneyflow", return_value=[]),
            patch.object(app, "fetch_valuation", return_value=[]),
            patch.object(app, "fetch_fundamentals", return_value=[]),
            patch.object(app, "fetch_company_context", return_value=None),
        ):
            result = app.analyze("600000")

        self.assertIn("error", result)
        self.assertIn("未取到", result["error"])

    def test_tencent_quote_exposes_intraday_summary_without_orderbook(self):
        quote = [""] * 52
        quote[1], quote[3], quote[5] = "固定样例", "25.65", "24.96"
        quote[9], quote[10] = "25.65", "48"
        quote[19], quote[20] = "25.66", "85"
        quote[30], quote[32] = "20260804115627", "3.80"
        quote[33], quote[34] = "25.67", "24.85"
        quote[38], quote[43], quote[49], quote[51] = "2.03", "3.32", "1.01", "25.38"

        result = app._parse_qt(quote)

        self.assertEqual(result["price"], 25.65)
        self.assertEqual(result["chg"], 3.8)
        self.assertEqual(
            result["market_snapshot"],
            {
                "open": 24.96,
                "high": 25.67,
                "low": 24.85,
                "avg_price": 25.38,
                "volume_ratio": 1.01,
                "turnover_pct": 2.03,
                "amplitude_pct": 3.32,
            },
        )

    def test_company_context_separates_industry_from_concepts(self):
        payload = {
            "ssbk": [
                {"BOARD_NAME": "电子", "BOARD_RANK": 1},
                {"BOARD_NAME": "光学光电子", "BOARD_RANK": 2},
                {"BOARD_NAME": "光学元件", "BOARD_RANK": 3},
                {"BOARD_NAME": "浙江板块", "BOARD_RANK": 4, "IS_PRECISE": "0"},
                {"BOARD_NAME": "消费电子概念", "BOARD_RANK": 5, "IS_PRECISE": "1"},
                {"BOARD_NAME": "AI眼镜", "BOARD_RANK": 6, "IS_PRECISE": "1"},
                {"BOARD_NAME": "人形机器人", "BOARD_RANK": 7, "IS_PRECISE": "1"},
                {"BOARD_NAME": "激光雷达", "BOARD_RANK": 8, "IS_PRECISE": "1"},
                {"BOARD_NAME": "第五概念", "BOARD_RANK": 9, "IS_PRECISE": "1"},
            ],
            "hxtc": [{
                "KEY_CLASSIF": "主营业务",
                "KEYWORD": "光学解决方案平台",
                "MAINPOINT_CONTENT": "核心产品应用于消费电子、车载光学和 AR/VR 眼镜。",
            }],
        }
        with patch.object(app, "fetch_json", return_value=payload):
            result = app.fetch_company_context("002273.SZ")

        self.assertEqual(result["industry_path"], ["电子", "光学光电子", "光学元件"])
        self.assertEqual(result["industry"], "光学元件")
        self.assertEqual(
            result["concepts"],
            ["消费电子概念", "AI眼镜", "人形机器人", "激光雷达"],
        )
        self.assertNotIn("浙江板块", result["concepts"])
        self.assertIn("不等于主营", result["source_note"])


if __name__ == "__main__":
    unittest.main()
