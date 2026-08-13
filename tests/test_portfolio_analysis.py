import unittest
from datetime import date, timedelta

from monitoring.portfolio_analysis import (
    _replay_observations,
    _static_portfolio_history,
    build_portfolio_analytics,
    series_metrics,
)


def _dates(count=81):
    start = date(2026, 1, 1)
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


def _history(values, dates=None):
    trading_dates = dates or _dates(len(values))
    return list(zip(trading_dates, [float(value) for value in values]))


def _analysis(
    code,
    name,
    values,
    *,
    industry="制造业",
    industry_path=None,
    concepts=(),
    dates=None,
):
    trading_dates = dates or _dates(len(values))
    path = list(industry_path) if industry_path is not None else ([industry] if industry else [])
    return {
        "code": code,
        "name": name,
        "type_name": "股票",
        "company_context": {
            "industry": industry,
            "industry_path": path,
            "concepts": list(concepts),
        },
        "chart": {
            "dates": trading_dates,
            "candle": [
                [float(value), float(value), float(value), float(value)]
                for value in values
            ],
        },
    }


def _item(
    code,
    name,
    quantity,
    weight_pct,
    market_value,
    profit_amount=0.0,
):
    return {
        "code": code,
        "name": name,
        "quantity": float(quantity),
        "weight_pct": float(weight_pct),
        "market_value": float(market_value),
        "cost_value": float(market_value) - float(profit_amount),
        "profit_amount": float(profit_amount),
        "profit_pct": 0.0,
    }


def _market_history(dates, benchmark_values, sectors=None):
    return {
        "indices": [
            {
                "code": "000300",
                "name": "沪深300",
                "dates": dates,
                "closes": [float(value) for value in benchmark_values],
            }
        ],
        "sectors": list(sectors or []),
    }


def _concentration_case(weights, industries, themes, *, complete=True):
    dates = _dates(90)
    benchmark_values = [100 + 0.1 * index for index in range(len(dates))]
    items = []
    analyses = {}
    for index, weight in enumerate(weights):
        code = f"600{index:03d}"
        values = [80 + row * 0.1 + index for row in range(len(dates))]
        items.append(_item(code, f"样例{index}", 1, weight, values[-1]))
        analyses[code] = _analysis(
            code,
            f"样例{index}",
            values,
            industry=industries[index],
            concepts=themes[index],
            dates=dates,
        )
    result = build_portfolio_analytics(
        items,
        analyses,
        _market_history(dates, benchmark_values),
        {
            "complete": complete,
            "holding_count": len(items) if complete else len(items) + 1,
            "analyzed_count": len(items),
        },
        total_cost=sum(item["cost_value"] for item in items),
    )
    return result["concentration"]


class PortfolioAnalysisTests(unittest.TestCase):
    def test_static_portfolio_curve_and_period_contributions_reconcile(self):
        dates = _dates()
        first_values = [100 + index for index in range(len(dates))]
        second_values = [200 + 2 * index for index in range(len(dates))]
        benchmark_values = [100 + 0.25 * index for index in range(len(dates))]
        items = [
            _item("600001", "样例甲", 2, 50, 360, 60),
            _item("600002", "样例乙", 1, 50, 360, -40),
        ]
        analyses = {
            "600001": _analysis("600001", "样例甲", first_values, dates=dates),
            "600002": _analysis("600002", "样例乙", second_values, dates=dates),
        }
        histories = {
            "600001": _history(first_values, dates),
            "600002": _history(second_values, dates),
        }
        benchmark = _history(benchmark_values, dates)

        curve, coverage = _static_portfolio_history(items, histories, benchmark)

        self.assertEqual(coverage, 100.0)
        self.assertEqual(len(curve), len(dates))
        self.assertEqual(curve[0], (dates[0], 400.0))
        self.assertEqual(curve[-1], (dates[-1], 720.0))

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, benchmark_values),
            {"complete": True},
            total_cost=700.0,
        )

        trend = result["portfolio_trend"]
        self.assertEqual(trend["mode"], "按当前持仓数量静态回看")
        self.assertEqual(trend["sample_count"], len(dates))
        self.assertTrue(trend["coverage_complete"])
        for window in (5, 10, 20, 60):
            key = f"{window}d"
            contribution_sum = sum(
                item["period_contribution_pct"][key] for item in items
            )
            self.assertAlmostEqual(
                contribution_sum,
                trend["returns"][key],
                delta=0.02,
                msg=f"{key} 分项贡献应与静态组合收益一致",
            )

    def test_series_metrics_classifies_four_states_and_insufficient_data(self):
        dates = _dates(80)
        flat_benchmark = _history([100.0] * len(dates), dates)
        cases = {
            "强势": (
                [100 + index for index in range(len(dates))],
                [100 + 0.1 * index for index in range(len(dates))],
            ),
            "震荡": ([100.0] * len(dates), [100.0] * len(dates)),
            "转弱": (
                [100 + index for index in range(70)]
                + [168 - 2 * index for index in range(10)],
                [100.0] * len(dates),
            ),
            "弱势": (
                [300 - index for index in range(len(dates))],
                [300 - 0.1 * index for index in range(len(dates))],
            ),
        }

        for expected, (values, benchmark_values) in cases.items():
            with self.subTest(expected=expected):
                metrics = series_metrics(
                    _history(values, dates),
                    _history(benchmark_values, dates),
                )
                self.assertEqual(metrics["state"], expected)

        short_dates = dates[:59]
        insufficient = series_metrics(
            _history([100 + index for index in range(59)], short_dates),
            flat_benchmark[:59],
        )
        self.assertEqual(insufficient["state"], "数据不足")
        self.assertIn("少于60个共同交易日", insufficient["state_reason"])

    def test_relative_sector_requires_a_fresh_complete_proxy_window(self):
        dates = _dates(80)
        stock = _history([100 + index for index in range(80)], dates)
        benchmark = _history([100 + index * 0.2 for index in range(80)], dates)
        sector = _history([100 + index * 0.5 for index in range(80)], dates)

        fresh = series_metrics(stock, benchmark, sector)
        one_day_lag = series_metrics(stock, benchmark, sector[:-1])
        stale = series_metrics(stock, benchmark, sector[:-5])

        self.assertIsNotNone(fresh["relative_sector"]["20d"])
        self.assertIsNotNone(one_day_lag["relative_sector"]["20d"])
        self.assertEqual(
            one_day_lag["sector_data_quality"]["stale_trading_days"], 1
        )
        self.assertIsNone(stale["relative_sector"]["20d"])
        self.assertEqual(stale["sector_data_quality"]["stale_trading_days"], 5)
        self.assertFalse(
            stale["sector_data_quality"]["windows"]["20d"]["usable"]
        )

    def test_correlation_skips_pairs_without_twenty_return_samples(self):
        dates = _dates(20)
        benchmark_values = [100 + index for index in range(len(dates))]
        first_values = [20 + index for index in range(len(dates))]
        second_values = [50 + 2 * index for index in range(len(dates))]
        items = [
            _item("600001", "样例甲", 1, 50, first_values[-1]),
            _item("600002", "样例乙", 1, 50, second_values[-1]),
        ]
        analyses = {
            "600001": _analysis("600001", "样例甲", first_values, dates=dates),
            "600002": _analysis("600002", "样例乙", second_values, dates=dates),
        }

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, benchmark_values),
            {"complete": True},
            total_cost=100.0,
        )

        correlation = result["correlation"]
        self.assertIsNone(correlation["average_correlation"])
        self.assertEqual(correlation["highest_pairs"], [])
        self.assertEqual(correlation["high_correlation_pair_count"], 0)
        self.assertEqual(correlation["risk_level"], "数据不足")
        self.assertIn("少于20个样本不判断", correlation["sample_window"])

    def test_theme_exposure_keeps_overlapping_labels(self):
        dates = _dates()
        benchmark_values = [100 + 0.2 * index for index in range(len(dates))]
        first_values = [50 + 0.3 * index for index in range(len(dates))]
        second_values = [80 + 0.4 * index for index in range(len(dates))]
        items = [
            _item("600001", "样例甲", 1, 60, first_values[-1]),
            _item("600002", "样例乙", 1, 40, second_values[-1]),
        ]
        analyses = {
            "600001": _analysis(
                "600001",
                "样例甲",
                first_values,
                concepts=("人工智能", "机器人"),
                dates=dates,
            ),
            "600002": _analysis(
                "600002",
                "样例乙",
                second_values,
                concepts=("人工智能", "新能源"),
                dates=dates,
            ),
        }

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, benchmark_values),
            {"complete": True},
            total_cost=100.0,
        )

        exposure = {
            row["name"]: row["weight_pct"]
            for row in result["concentration"]["theme_exposure"]
        }
        self.assertEqual(exposure["人工智能"], 100.0)
        self.assertEqual(exposure["机器人"], 60.0)
        self.assertEqual(exposure["新能源"], 40.0)
        self.assertEqual(sum(exposure.values()), 200.0)
        self.assertIn("可能超过100%", result["concentration"]["theme_overlap_note"])

    def test_replay_state_has_no_lookahead_and_requires_full_ten_day_future(self):
        dates = _dates()
        benchmark = _history(
            [100 + 0.1 * index for index in range(len(dates))], dates
        )
        base_values = [100 + index for index in range(len(dates))]
        changed_future = base_values[:61] + [50 - index for index in range(20)]

        base_rows = _replay_observations(_history(base_values, dates), benchmark)
        changed_rows = _replay_observations(
            _history(changed_future, dates), benchmark
        )
        changed_benchmark = _history(
            [100 + 0.1 * index for index in range(61)]
            + [220 - index for index in range(20)],
            dates,
        )
        changed_benchmark_rows = _replay_observations(
            _history(base_values, dates), changed_benchmark
        )

        self.assertEqual([row["date"] for row in base_rows], dates[60:71:5])
        self.assertEqual(base_rows[-1]["date"], dates[-11])
        self.assertNotIn(dates[-6], [row["date"] for row in base_rows])
        self.assertEqual(base_rows[0]["state"], changed_rows[0]["state"])
        self.assertEqual(
            base_rows[0]["state"], changed_benchmark_rows[0]["state"]
        )
        self.assertNotEqual(
            base_rows[0]["return_10d"], changed_rows[0]["return_10d"]
        )
        self.assertAlmostEqual(
            base_rows[-1]["return_10d"],
            (base_values[-1] / base_values[-11] - 1) * 100,
        )
        for row in base_rows:
            self.assertIn("return_5d", row)
            self.assertIn("return_10d", row)
            self.assertIn("excess_5d", row)
            self.assertIn("excess_10d", row)

    def test_aggregate_metrics_share_one_common_as_of_date(self):
        dates = _dates(90)
        benchmark_values = [100 + 0.2 * index for index in range(len(dates))]
        first_values = [60 + 0.4 * index for index in range(len(dates))]
        second_dates = dates[:-3]
        second_values = [80 + 0.3 * index for index in range(len(second_dates))]
        items = [
            _item("600001", "样例甲", 1, 55, first_values[-1]),
            _item("600002", "样例乙", 1, 45, second_values[-1]),
        ]
        analyses = {
            "600001": _analysis("600001", "样例甲", first_values, dates=dates),
            "600002": _analysis(
                "600002", "样例乙", second_values, dates=second_dates
            ),
        }

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, benchmark_values),
            {"complete": True},
            total_cost=100.0,
        )

        common_as_of = second_dates[-1]
        self.assertEqual(result["analysis_period"]["daily_data_through"], common_as_of)
        self.assertEqual(items[0]["history"]["data_end"], common_as_of)
        self.assertEqual(items[1]["history"]["data_end"], common_as_of)
        self.assertEqual(result["portfolio_trend"]["data_end"], common_as_of)

    def test_partial_pair_coverage_cannot_be_reported_as_low_correlation(self):
        dates = _dates(90)
        benchmark_values = [100 + 0.1 * index for index in range(len(dates))]
        full_first = [50 + index for index in range(len(dates))]
        full_second = [80 + 0.2 * index + (index % 3) for index in range(len(dates))]
        short_dates = dates[-20:]
        short_values = [30 + index for index in range(len(short_dates))]
        items = [
            _item("600001", "样例甲", 1, 40, full_first[-1]),
            _item("600002", "样例乙", 1, 35, full_second[-1]),
            _item("600003", "样例丙", 1, 25, short_values[-1]),
        ]
        analyses = {
            "600001": _analysis("600001", "样例甲", full_first, dates=dates),
            "600002": _analysis("600002", "样例乙", full_second, dates=dates),
            "600003": _analysis(
                "600003", "样例丙", short_values, dates=short_dates
            ),
        }

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, benchmark_values),
            {"complete": True},
            total_cost=100.0,
        )

        correlation = result["correlation"]
        self.assertEqual(correlation["expected_pair_count"], 3)
        self.assertEqual(correlation["valid_pair_count"], 1)
        self.assertLess(correlation["pair_coverage_pct"], 100)
        self.assertEqual(correlation["risk_level"], "数据不足")

    def test_incomplete_portfolio_replay_is_kept_out_of_complete_result(self):
        dates = _dates(90)
        values = [100 + index for index in range(len(dates))]
        items = [_item("600001", "样例甲", 1, 100, values[-1])]
        analyses = {
            "600001": _analysis("600001", "样例甲", values, dates=dates)
        }

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, values),
            {
                "complete": False,
                "holding_count": 2,
                "analyzed_count": 1,
            },
            total_cost=200.0,
        )

        replay = result["diagnostic_replay"]
        self.assertFalse(replay["coverage_complete"])
        self.assertEqual(replay["scope"], "可分析持仓子集")
        self.assertEqual(replay["portfolio_states"], [])
        self.assertTrue(replay["analyzed_subset_states"])

    def test_specific_industry_and_unique_themes_drive_exposure(self):
        dates = _dates(90)
        values = [100 + 0.2 * index for index in range(len(dates))]
        analysis = _analysis("600001", "样例甲", values, dates=dates)
        analysis["company_context"] = {
            "industry": "光学光电子",
            "industry_path": ["制造业", "电子", "光学光电子"],
            "concepts": ["AI眼镜", "AI眼镜", "消费电子"],
        }
        items = [_item("600001", "样例甲", 1, 100, values[-1])]

        result = build_portfolio_analytics(
            items,
            {"600001": analysis},
            _market_history(dates, values),
            {"complete": True},
            total_cost=100.0,
        )

        industries = result["concentration"]["industry_exposure"]
        themes = result["concentration"]["theme_exposure"]
        self.assertEqual(industries[0]["name"], "光学光电子")
        self.assertEqual(
            {row["name"]: row["weight_pct"] for row in themes},
            {"AI眼镜": 100.0, "消费电子": 100.0},
        )

    def test_parent_industry_concentration_is_not_hidden_by_diverse_leaves(self):
        dates = _dates(90)
        benchmark_values = [100 + 0.1 * index for index in range(len(dates))]
        paths = [
            ["信息技术", "电子", "半导体"],
            ["信息技术", "计算机", "软件开发"],
            ["金融", "银行", "股份制银行"],
            ["消费", "食品饮料", "白酒"],
        ]
        items = []
        analyses = {}
        for index, path in enumerate(paths):
            code = f"600{index:03d}"
            values = [80 + row * 0.1 + index for row in range(len(dates))]
            items.append(_item(code, f"样例{index}", 1, 25, values[-1]))
            analyses[code] = _analysis(
                code,
                f"样例{index}",
                values,
                industry=path[-1],
                industry_path=path,
                dates=dates,
            )

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, benchmark_values),
            {"complete": True},
            total_cost=sum(item["cost_value"] for item in items),
        )

        concentration = result["concentration"]
        driver = concentration["industry_concentration_driver"]
        self.assertEqual(concentration["industry_assessment"], "高")
        self.assertEqual(driver["level_name"], "1级行业")
        self.assertEqual(driver["top_name"], "信息技术")
        self.assertEqual(driver["top_weight_pct"], 50.0)
        self.assertEqual(concentration["industry_exposure"][0]["name"], "信息技术")
        self.assertEqual(
            {row["name"] for row in concentration["industry_leaf_exposure"]},
            {"半导体", "软件开发", "股份制银行", "白酒"},
        )
        flag = next(
            row
            for row in result["priority_flags"]
            if row["title"] == "组合集中度偏高"
        )
        self.assertIn("1级行业 信息技术 50.0%", flag["detail"])

    def test_parent_industry_low_assessment_requires_level_coverage(self):
        dates = _dates(90)
        benchmark_values = [100 + 0.1 * index for index in range(len(dates))]
        items = []
        analyses = {}
        for index in range(10):
            code = f"600{index:03d}"
            values = [80 + row * 0.1 + index for row in range(len(dates))]
            known = index < 5
            path = [f"一级行业{index}", f"细分行业{index}"] if known else []
            items.append(_item(code, f"样例{index}", 1, 10, values[-1]))
            analyses[code] = _analysis(
                code,
                f"样例{index}",
                values,
                industry=path[-1] if path else "",
                industry_path=path,
                dates=dates,
            )

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, benchmark_values),
            {"complete": True},
            total_cost=sum(item["cost_value"] for item in items),
        )

        concentration = result["concentration"]
        self.assertEqual(concentration["industry_assessment"], "数据不足")
        self.assertEqual(concentration["industry_coverage_pct"], 50.0)
        self.assertTrue(
            all(
                row["assessment"] == "数据不足"
                for row in concentration["industry_hierarchy_exposure"]
            )
        )

    def test_missing_benchmark_keeps_absolute_portfolio_metrics_and_contributions(self):
        dates = _dates(90)
        first_values = [100 + index for index in range(len(dates))]
        second_values = [200 + 0.5 * index for index in range(len(dates))]
        items = [
            _item("600001", "样例甲", 2, 50, first_values[-1] * 2),
            _item("600002", "样例乙", 1, 50, second_values[-1]),
        ]
        analyses = {
            "600001": _analysis("600001", "样例甲", first_values, dates=dates),
            "600002": _analysis("600002", "样例乙", second_values, dates=dates),
        }

        result = build_portfolio_analytics(
            items,
            analyses,
            {"indices": [], "sectors": []},
            {"complete": True},
            total_cost=sum(item["cost_value"] for item in items),
        )

        trend = result["portfolio_trend"]
        self.assertIsNotNone(trend["returns"]["20d"])
        self.assertIsNotNone(trend["drawdown"]["20d"])
        self.assertIsNotNone(trend["volatility"]["20d"])
        self.assertIsNone(trend["relative_market"]["20d"])
        self.assertEqual(trend["state"], "数据不足")
        self.assertAlmostEqual(
            sum(item["period_contribution_pct"]["20d"] for item in items),
            trend["returns"]["20d"],
            delta=0.02,
        )
        self.assertEqual(result["diagnostic_replay"]["portfolio_states"], [])

    def test_window_metrics_require_the_full_named_window(self):
        dates = _dates(61)
        values = [100 + index + (index % 2) * 0.5 for index in range(61)]

        twenty_short = series_metrics(_history(values[:20], dates[:20]))
        twenty_full = series_metrics(_history(values[:21], dates[:21]))
        sixty_short = series_metrics(_history(values[:60], dates[:60]))
        sixty_full = series_metrics(_history(values, dates))

        self.assertIsNone(twenty_short["drawdown"]["20d"])
        self.assertIsNone(twenty_short["volatility"]["20d"])
        self.assertIsNotNone(twenty_full["drawdown"]["20d"])
        self.assertIsNotNone(twenty_full["volatility"]["20d"])
        self.assertIsNone(sixty_short["drawdown"]["60d"])
        self.assertIsNone(sixty_short["volatility"]["60d"])
        self.assertIsNotNone(sixty_full["drawdown"]["60d"])
        self.assertIsNotNone(sixty_full["volatility"]["60d"])

    def test_recent_long_gap_blocks_state_correlation_and_replay_observation(self):
        dates = _dates(130)
        benchmark_values = [100 + 0.1 * index for index in range(len(dates))]
        first_values = [80 + 0.3 * index + (index % 3) for index in range(len(dates))]
        kept_indexes = [index for index in range(len(dates)) if not 90 <= index <= 100]
        second_dates = [dates[index] for index in kept_indexes]
        second_values = [120 + 0.2 * index for index in kept_indexes]
        items = [
            _item("600001", "样例甲", 1, 50, first_values[-1]),
            _item("600002", "样例乙", 1, 50, second_values[-1]),
        ]
        analyses = {
            "600001": _analysis("600001", "样例甲", first_values, dates=dates),
            "600002": _analysis(
                "600002", "样例乙", second_values, dates=second_dates
            ),
        }

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, benchmark_values),
            {"complete": True},
            total_cost=sum(item["cost_value"] for item in items),
        )

        second_history = items[1]["history"]
        self.assertEqual(second_history["state"], "数据不足")
        self.assertGreater(
            second_history["data_quality"]["windows"]["60d"]["longest_gap_days"],
            5,
        )
        self.assertIsNone(second_history["volatility"]["60d"])
        self.assertEqual(result["portfolio_trend"]["state"], "数据不足")
        self.assertEqual(result["correlation"]["valid_pair_count"], 0)
        replay_rows = _replay_observations(
            _history(second_values, second_dates),
            _history(benchmark_values, dates),
        )
        replay_dates = [row["date"] for row in replay_rows]
        self.assertIn(dates[60], replay_dates)
        self.assertNotIn(dates[100], replay_dates)

    def test_market_context_does_not_turn_missing_history_into_sideways(self):
        dates = _dates(130)
        benchmark_values = [100 + 0.1 * index for index in range(len(dates))]
        holding_values = [80 + 0.2 * index for index in range(len(dates))]
        removed = set(range(90, 101))
        sector_dates = [day for index, day in enumerate(dates) if index not in removed]
        sector_values = [120 + 0.15 * index for index in range(len(sector_dates))]
        short_dates = dates[-5:]
        items = [_item("600001", "样例甲", 1, 100, holding_values[-1])]
        analyses = {
            "600001": _analysis(
                "600001", "样例甲", holding_values, dates=dates
            )
        }
        market_history = _market_history(
            dates,
            benchmark_values,
            sectors=[
                {
                    "code": "S-GAP",
                    "name": "历史缺口板块",
                    "dates": sector_dates,
                    "closes": sector_values,
                },
                {
                    "code": "S-SHORT",
                    "name": "短历史板块",
                    "dates": short_dates,
                    "closes": [50, 51, 52, 53, 54],
                },
            ],
        )

        result = build_portfolio_analytics(
            items,
            analyses,
            market_history,
            {"complete": True},
            total_cost=items[0]["cost_value"],
        )

        sectors = {
            row["code"]: row for row in result["market_context"]["sectors"]
        }
        self.assertEqual(sectors["S-GAP"]["trend"], "数据不足")
        self.assertEqual(sectors["S-SHORT"]["trend"], "数据不足")
        self.assertIsNone(sectors["S-SHORT"]["rank_5d"])
        self.assertIsNone(sectors["S-SHORT"]["rank_20d"])

    def test_high_volatility_uses_benchmark_at_common_as_of(self):
        dates = _dates(90)
        common_dates = dates[:-3]
        common_benchmark_values = [100 + 0.1 * index for index in range(len(common_dates))]
        benchmark_values = common_benchmark_values + [200.0, 60.0, 180.0]
        first_values = [100 + 0.3 * index + (3 if index % 2 else -3) for index in range(len(dates))]
        second_values = [200 + 0.2 * index for index in range(len(common_dates))]
        items = [
            _item("600001", "样例甲", 1, 50, first_values[-1]),
            _item("600002", "样例乙", 1, 50, second_values[-1]),
        ]
        analyses = {
            "600001": _analysis("600001", "样例甲", first_values, dates=dates),
            "600002": _analysis(
                "600002", "样例乙", second_values, dates=common_dates
            ),
        }

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, benchmark_values),
            {"complete": True},
            total_cost=sum(item["cost_value"] for item in items),
        )

        expected = series_metrics(
            _history(common_benchmark_values, common_dates)
        )["volatility"]["20d"]
        latest = series_metrics(
            _history(benchmark_values, dates)
        )["volatility"]["20d"]
        concentration = result["concentration"]
        self.assertEqual(concentration["benchmark_volatility_20d"], expected)
        self.assertNotEqual(concentration["benchmark_volatility_20d"], latest)
        self.assertEqual(concentration["high_volatility_assessment"], "高")

    def test_high_volatility_low_or_medium_requires_effective_weight_coverage(self):
        dates = _dates(90)
        benchmark_values = [
            100 + 0.2 * index + (0.1 if index % 2 else 0)
            for index in range(len(dates))
        ]
        first_values = list(benchmark_values)
        removed = set(range(75, 89))
        second_dates = [day for index, day in enumerate(dates) if index not in removed]
        second_values = [150 + 0.15 * index for index in range(len(second_dates))]
        items = [
            _item("600001", "样例甲", 1, 70, first_values[-1]),
            _item("600002", "样例乙", 1, 30, second_values[-1]),
        ]
        analyses = {
            "600001": _analysis("600001", "样例甲", first_values, dates=dates),
            "600002": _analysis(
                "600002", "样例乙", second_values, dates=second_dates
            ),
        }

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, benchmark_values),
            {"complete": True},
            total_cost=sum(item["cost_value"] for item in items),
        )

        concentration = result["concentration"]
        self.assertEqual(concentration["volatility_coverage_pct"], 70.0)
        self.assertEqual(concentration["high_volatility_assessment"], "数据不足")

    def test_known_exposure_can_flag_risk_but_low_requires_label_coverage(self):
        weights = [10] * 10
        no_themes = [()] * 10
        known_high = _concentration_case(
            weights, ["电子"] * 4 + [""] * 6, no_themes
        )
        known_medium = _concentration_case(
            weights, ["电子"] * 3 + [""] * 7, no_themes
        )
        known_low = _concentration_case(
            weights,
            [f"行业{index}" for index in range(6)] + [""] * 4,
            no_themes,
        )
        unknown = _concentration_case(weights, [""] * 10, no_themes)

        self.assertEqual(known_high["industry_coverage_pct"], 40.0)
        self.assertEqual(known_high["position_assessment"], "低")
        self.assertEqual(known_high["industry_assessment"], "高")
        self.assertEqual(known_high["theme_assessment"], "数据不足")
        self.assertEqual(known_high["assessment"], "高")
        self.assertEqual(known_medium["industry_coverage_pct"], 30.0)
        self.assertEqual(known_medium["industry_assessment"], "中")
        self.assertEqual(known_medium["assessment"], "中")
        self.assertEqual(known_low["industry_coverage_pct"], 60.0)
        self.assertEqual(known_low["industry_assessment"], "低")
        self.assertEqual(unknown["industry_assessment"], "数据不足")
        self.assertEqual(unknown["theme_assessment"], "数据不足")
        self.assertFalse(unknown["exposure_coverage_sufficient"])
        self.assertEqual(unknown["assessment"], "数据不足")

    def test_position_assessment_has_independent_thresholds_and_complete_gate(self):
        industries = [f"行业{index}" for index in range(10)]
        themes = [(f"主题{index}",) for index in range(10)]
        high = _concentration_case(
            [30, 10, 10, 10, 10, 10, 5, 5, 5, 5], [""] * 10, [()] * 10
        )
        medium = _concentration_case(
            [20, 17, 17, 8, 8, 8, 7, 5, 5, 5], [""] * 10, [()] * 10
        )
        low = _concentration_case([10] * 10, industries, themes)
        incomplete = _concentration_case(
            [10] * 10, industries, themes, complete=False
        )

        self.assertEqual(high["position_assessment"], "高")
        self.assertEqual(high["industry_assessment"], "数据不足")
        self.assertEqual(high["theme_assessment"], "数据不足")
        self.assertEqual(high["assessment"], "高")
        self.assertEqual(medium["position_assessment"], "中")
        self.assertEqual(medium["industry_assessment"], "数据不足")
        self.assertEqual(medium["theme_assessment"], "数据不足")
        self.assertEqual(medium["assessment"], "中")
        self.assertEqual(low["position_assessment"], "低")
        self.assertTrue(low["exposure_coverage_sufficient"])
        self.assertEqual(low["assessment"], "低")
        self.assertEqual(incomplete["position_assessment"], "数据不足")
        self.assertEqual(incomplete["industry_assessment"], "数据不足")
        self.assertEqual(incomplete["theme_assessment"], "数据不足")
        self.assertEqual(incomplete["assessment"], "数据不足")

    def test_theme_assessment_flags_known_risk_and_requires_coverage_for_low(self):
        weights = [10] * 10
        industries = [f"行业{index}" for index in range(10)]
        known_high = _concentration_case(
            weights, industries, [("人工智能",)] * 4 + [()] * 6
        )
        known_medium = _concentration_case(
            weights, industries, [("人工智能",)] * 3 + [()] * 7
        )
        known_low = _concentration_case(
            weights,
            industries,
            [(f"主题{index}",) for index in range(6)] + [()] * 4,
        )
        insufficient = _concentration_case(
            weights,
            industries,
            [(f"主题{index}",) for index in range(5)] + [()] * 5,
        )

        self.assertEqual(known_high["theme_coverage_pct"], 40.0)
        self.assertEqual(known_high["theme_assessment"], "高")
        self.assertEqual(known_high["assessment"], "高")
        self.assertEqual(known_medium["theme_coverage_pct"], 30.0)
        self.assertEqual(known_medium["theme_assessment"], "中")
        self.assertEqual(known_medium["assessment"], "中")
        self.assertEqual(known_low["theme_coverage_pct"], 60.0)
        self.assertEqual(known_low["position_assessment"], "低")
        self.assertEqual(known_low["industry_assessment"], "低")
        self.assertEqual(known_low["theme_assessment"], "低")
        self.assertTrue(known_low["exposure_coverage_sufficient"])
        self.assertEqual(known_low["assessment"], "低")
        self.assertEqual(insufficient["theme_coverage_pct"], 50.0)
        self.assertEqual(insufficient["theme_assessment"], "数据不足")
        self.assertFalse(insufficient["exposure_coverage_sufficient"])
        self.assertEqual(insufficient["assessment"], "数据不足")

    def test_priority_flag_names_the_actual_concentration_source(self):
        dates = _dates(90)
        benchmark_values = [100 + 0.1 * index for index in range(len(dates))]
        items = []
        analyses = {}
        for index in range(10):
            code = f"600{index:03d}"
            values = [80 + row * 0.1 + index for row in range(len(dates))]
            items.append(_item(code, f"样例{index}", 1, 10, values[-1]))
            analyses[code] = _analysis(
                code,
                f"样例{index}",
                values,
                industry=f"行业{index}",
                concepts=("人工智能",) if index < 4 else (f"主题{index}",),
                dates=dates,
            )

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, benchmark_values),
            {"complete": True},
            total_cost=sum(item["cost_value"] for item in items),
        )

        flag = next(
            row
            for row in result["priority_flags"]
            if row["title"] == "组合集中度偏高"
        )
        self.assertIn("主题 人工智能 40.0%", flag["detail"])
        self.assertNotIn("Top1", flag["detail"])

    def test_loss_source_concentration_becomes_a_priority_problem(self):
        dates = _dates(90)
        values = [100.0] * len(dates)
        items = []
        analyses = {}
        losses = [-50, -1, -1, -1, -1]
        for index, profit_amount in enumerate(losses):
            code = f"600{index:03d}"
            items.append(
                _item(code, f"样例{index}", 1, 20, values[-1], profit_amount)
            )
            analyses[code] = _analysis(
                code,
                f"样例{index}",
                values,
                industry=f"行业{index}",
                concepts=(f"主题{index}",),
                dates=dates,
            )

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, values),
            {"complete": True},
            total_cost=sum(item["cost_value"] for item in items),
        )

        loss_concentration = result["pnl_sources"]["loss_concentration"]
        self.assertEqual(loss_concentration["assessment"], "高")
        self.assertEqual(loss_concentration["largest_source_name"], "样例0")
        self.assertGreater(loss_concentration["largest_source_share_pct"], 90)
        flag = next(
            row
            for row in result["priority_flags"]
            if row["title"] == "累计亏损来源集中"
        )
        self.assertIn("样例0占累计亏损总额", flag["detail"])

    def test_priority_sort_keeps_severe_correlation_and_combines_trend_signals(self):
        dates = _dates(90)
        benchmark_values = [
            100 + 0.02 * index + (0.02 if index % 3 == 0 else 0)
            for index in range(len(dates))
        ]
        base_values = [
            200 - 1.1 * index + (4 if index % 2 else -4)
            for index in range(len(dates))
        ]
        items = []
        analyses = {}
        for index in range(4):
            code = f"600{index:03d}"
            values = [value + index * 10 for value in base_values]
            items.append(_item(code, f"样例{index}", 1, 25, values[-1]))
            analyses[code] = _analysis(
                code,
                f"样例{index}",
                values,
                industry=f"行业{index}",
                concepts=(f"主题{index}",),
                dates=dates,
            )

        result = build_portfolio_analytics(
            items,
            analyses,
            _market_history(dates, benchmark_values),
            {"complete": True},
            total_cost=sum(item["cost_value"] for item in items),
        )

        flags = result["priority_flags"]
        titles = [row["title"] for row in flags]
        self.assertEqual(len(flags), 3)
        self.assertIn("高相关仓位成组", titles)
        trend_flag = next(row for row in flags if row["title"] == "组合整体处于弱势")
        self.assertIn("最大回撤", trend_flag["detail"])
        self.assertIn("跑输沪深300", trend_flag["detail"])
        self.assertEqual(result["correlation"]["risk_level"], "高")

    def test_stale_common_as_of_blocks_current_strength_and_adds_data_flag(self):
        dates = _dates(90)
        holding_dates = dates[:-5]
        values = [100 + index for index in range(len(holding_dates))]
        benchmark_values = [100 + 0.1 * index for index in range(len(dates))]
        items = [_item("600001", "样例甲", 1, 100, values[-1])]

        result = build_portfolio_analytics(
            items,
            {"600001": _analysis("600001", "样例甲", values, dates=holding_dates)},
            _market_history(dates, benchmark_values),
            {"complete": True},
            total_cost=items[0]["cost_value"],
        )

        period = result["analysis_period"]
        self.assertEqual(period["common_as_of_lag_trading_days"], 5)
        self.assertTrue(period["common_as_of_stale"])
        self.assertEqual(items[0]["strength_state"], "数据不足")
        self.assertEqual(result["portfolio_trend"]["state"], "数据不足")
        self.assertIsNotNone(result["portfolio_trend"]["returns"]["20d"])
        self.assertEqual(
            result["concentration"]["high_volatility_assessment"], "数据不足"
        )
        self.assertIn(
            "组合日线明显滞后",
            [flag["title"] for flag in result["priority_flags"]],
        )

    def test_missing_benchmark_is_a_priority_data_problem(self):
        dates = _dates(90)
        values = [100 + index for index in range(len(dates))]
        items = [_item("600001", "样例甲", 1, 100, values[-1])]
        result = build_portfolio_analytics(
            items,
            {"600001": _analysis("600001", "样例甲", values, dates=dates)},
            {"indices": [], "sectors": []},
            {"complete": True},
            total_cost=100.0,
        )

        self.assertEqual(result["priority_flags"][0]["title"], "沪深300历史不可用")


if __name__ == "__main__":
    unittest.main()
