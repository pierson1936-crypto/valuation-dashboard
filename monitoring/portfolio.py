from __future__ import annotations

import hashlib
import json
import math
import os
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import app

from monitoring.config import MonitorConfig
from monitoring.db import MonitorRepository, utc_now
from monitoring.portfolio_analysis import build_portfolio_analytics


REPORT_VERSION = "portfolio-report-v6-evidence-bound"
USAGE_FEATURE = "portfolio_report"
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_JUDGMENT_LENGTH = 4000
REPORT_HISTORY_LIMIT = 7
INDEPENDENT_MAX_TOKENS = 3200
COMPARISON_MAX_TOKENS = 1800
CONFIRMED_FACT_CATALOG_LIMIT = 160
GPT_PORTFOLIO_MODEL = os.environ.get("PORTFOLIO_GPT_MODEL", "gpt-5.6-sol")

# GPT-5.6 supports JSON mode, but JSON Schema is the documented structured-output
# path and makes this report's existing output contract explicit at the API edge.
GPT_INDEPENDENT_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "why_important": {"type": "string"},
                    "confirmed_fact_ids": {"type": "array", "items": {"type": "string"}},
                    "data_inferences": {"type": "array", "items": {"type": "string"}},
                    "missing_information": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "title", "why_important", "confirmed_fact_ids",
                    "data_inferences", "missing_information",
                ],
                "additionalProperties": False,
            },
        },
        "overall_missing_information": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "issues", "overall_missing_information"],
    "additionalProperties": False,
}

GPT_COMPARISON_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "agreements": {"type": "array", "items": {"type": "string"}},
        "disagreements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "user_view": {"type": "string"},
                    "ai_view": {"type": "string"},
                    "evidence_boundary": {"type": "string"},
                },
                "required": ["topic", "user_view", "ai_view", "evidence_boundary"],
                "additionalProperties": False,
            },
        },
        "possible_omissions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["agreements", "disagreements", "possible_omissions"],
    "additionalProperties": False,
}

INDEPENDENT_SYSTEM_PROMPT = """你是持仓组合独立复核助手。

输入只包含程序已经计算完成的组合趋势、盈亏来源、集中度、相关性、持仓强弱、历史
诊断重放，以及必要的行情、估值、技术和资金流数据。第一轮输入中不会提供用户观点、
买入理由或担忧，你也不得假设这些内容存在。

不要按固定指标顺序机械罗列，也不要为了凑数量加入次要事项。请自行选择当前最重要的
2 至 5 个问题，优先解释组合趋势、主要盈亏来源、集中度、相关性和持仓强弱；只有真正
重要时才补充估值、量价、资金流或数据缺口。

程序给出的趋势状态和指标是确定性计算结果，不得擅自重算或改写。组合历史是“按当前
持仓数量静态回看”，不是账户真实收益；历史诊断重放不是交易回测。没有可靠板块代理时，
不得猜测所属板块或宣称产业周期已经得到确认。

不得编造新闻、公告、财务数据、价格、指标、行业结论、用户买入理由或未来涨跌。
每个问题必须明确区分：
1. confirmed_fact_ids：输入中 confirmed_fact_catalog 已确认事实的 evidence_id；
2. data_inferences：基于这些事实的有限推断；
3. missing_information：作出更强结论仍缺少的信息。

confirmed_fact_ids 只能逐字引用 confirmed_fact_catalog 中存在的 evidence_id。不得自行
改写、概括或补充“已确认事实”；程序会按 evidence_id 还原展示文字。目录中没有对应
证据时，只能写入 data_inferences 或 missing_information。每个问题至少引用 1 个、
最多引用 2 个 evidence_id。

不得给出确定性买卖指令，不得承诺收益。输出必须是严格合法的 JSON 对象，不得输出
Markdown、代码块或 JSON 之外的内容。

顶层字段必须且只能是：
summary: 字符串；
issues: 数组，必须包含 2 至 5 项；
overall_missing_information: 字符串数组。

issues 每项必须且只能包含：
title: 字符串；
why_important: 字符串；
confirmed_fact_ids: 字符串数组；
data_inferences: 字符串数组；
missing_information: 字符串数组。

严格控制长度：
1. summary 可按当前问题自由组织为 1 至 3 个短段落，不超过 500 个汉字；
2. title 不超过 20 个汉字，why_important 不超过 80 个汉字；
3. 每个 confirmed_fact_ids、data_inferences、missing_information 最多 2 项；
4. 每一项不超过 60 个汉字；overall_missing_information 最多 3 项；
5. 不要重复解释指标，不要写背景知识、方法说明或结尾建议。"""

COMPARISON_SYSTEM_PROMPT = """你是用户观点与独立持仓分析的对照助手。

输入包含三部分：用户已经锁定的原始判断、第一轮独立 AI 分析、程序从同一持仓快照
提取的精简核对事实。第一轮独立分析已经完成，不得改写、补做或伪装成事先读过用户
判断。

只比较双方已经表达的内容，并用结构化快照核对事实边界。不得编造新闻、数据、用户
买入理由或未来涨跌；不得把缺失信息写成事实；不得给出确定性买卖指令。

输出必须是严格合法的 JSON 对象，不得输出 Markdown、代码块或额外文字。
顶层字段必须且只能是：
agreements: 字符串数组，双方明确一致之处；
disagreements: 数组，双方判断不同或证据强度不同之处；
possible_omissions: 字符串数组，用户或 AI 可能遗漏且需要补充信息验证的事项。

disagreements 每项必须且只能包含：
topic: 字符串；
user_view: 字符串；
ai_view: 字符串；
evidence_boundary: 字符串，说明已知事实和缺失信息。

严格控制长度：agreements、disagreements、possible_omissions 各最多 3 项；普通数组
每项不超过 80 个汉字；disagreements 每个字段不超过 100 个汉字。只写差异核对结果，
不要重复第一轮完整分析，不要增加背景知识或结尾建议。"""


def _round_number(value: Any, digits: int = 2) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _latest_fundamental(rows: Any) -> dict[str, Any] | None:
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    return {
        key: row.get(key)
        for key in ("date", "eps", "roe", "gross", "debt", "rev_yoy")
    }


def _portfolio_item(holding: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    quantity = float(holding["quantity"])
    cost_price = float(holding["cost_price"])
    price = _round_number(analysis.get("price"), 3)
    cost_value = quantity * cost_price
    market_value = quantity * price if price is not None else None
    profit_amount = market_value - cost_value if market_value is not None else None
    profit_pct = profit_amount / cost_value * 100 if cost_value else None
    tech = analysis.get("tech") if isinstance(analysis.get("tech"), dict) else {}
    moneyflow = (
        analysis.get("moneyflow")
        if isinstance(analysis.get("moneyflow"), dict)
        else None
    )
    valuation = {
        key: analysis.get(key)
        for key in ("pe", "pb", "pe_pct", "pb_pct")
        if analysis.get(key) is not None
    }
    return {
        "code": str(holding["code"]),
        "name": str(analysis.get("name") or holding.get("name") or ""),
        "saved_name": str(holding.get("name") or ""),
        "type": str(analysis.get("type_name") or analysis.get("classify") or ""),
        "quantity": _round_number(quantity, 4),
        "cost_price": _round_number(cost_price, 4),
        "current_price": price,
        "cost_value": _round_number(cost_value),
        "market_value": _round_number(market_value),
        "profit_amount": _round_number(profit_amount),
        "profit_pct": _round_number(profit_pct),
        "day_change_pct": _round_number(analysis.get("chg")),
        "price_percentile": _round_number(analysis.get("price_pct"), 1),
        "from_history_high_pct": _round_number(analysis.get("from_hi"), 1),
        "from_history_low_pct": _round_number(analysis.get("from_lo"), 1),
        "valuation": valuation,
        "technical": {
            key: tech.get(key)
            for key in (
                "ma5",
                "ma20",
                "ma60",
                "rsi",
                "macd_hist",
                "vol_ratio",
                "mdd",
                "vola",
            )
            if tech.get(key) is not None
        },
        "moneyflow": {
            key: moneyflow.get(key)
            for key in ("main_today", "main_sum5", "streak", "streak_dir")
        }
        if moneyflow
        else None,
        "latest_fundamental": _latest_fundamental(analysis.get("fund")),
        "analysis_date": str(analysis.get("date") or ""),
        "realtime_at": str(analysis.get("rt_time") or ""),
    }


def build_portfolio_snapshot(
    repository: MonitorRepository,
    analyzer: Callable[[str], dict[str, Any]] | None = None,
    market_provider: Callable[[], dict[str, Any]] | None = None,
    market_history_provider: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    holdings = repository.list_holdings()
    if not holdings:
        raise ValueError("当前没有已保存持仓")
    incomplete_codes = [
        str(item.get("code") or "")
        for item in holdings
        if _positive_number(item.get("quantity")) is None
        or _positive_number(item.get("cost_price")) is None
    ]
    if incomplete_codes:
        raise ValueError(
            "请先补全以下持仓的数量和成本价：" + "、".join(incomplete_codes)
        )
    analyze_one = analyzer or app.analyze_cached
    fetch_market = market_provider or app.market_overview
    if market_history_provider is not None:
        fetch_market_history = market_history_provider
    elif market_provider is None:
        fetch_market_history = app.market_history
    else:
        # Tests and external adapters that only inject a current-market provider must not
        # unexpectedly make live history requests.
        fetch_market_history = lambda: {"indices": [], "sectors": [], "errors": []}
    analyses: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(3, len(holdings))) as executor:
        futures = {
            executor.submit(analyze_one, str(item["code"])): item
            for item in holdings
        }
        for future in as_completed(futures):
            holding = futures[future]
            code = str(holding["code"])
            try:
                result = future.result()
            except Exception as exc:
                failures.append({"code": code, "reason": type(exc).__name__})
                continue
            if not isinstance(result, dict) or result.get("error"):
                failures.append(
                    {"code": code, "reason": str((result or {}).get("error") or "分析失败")[:160]}
                )
                continue
            analyses[code] = result

    items = [
        _portfolio_item(holding, analyses[str(holding["code"])])
        for holding in holdings
        if str(holding["code"]) in analyses
    ]
    if not items:
        raise ValueError("持仓行情暂时都不可用，无法生成组合报告")
    total_cost = sum(
        float(holding["quantity"]) * float(holding["cost_price"])
        for holding in holdings
    )
    analyzed_cost = sum(float(item["cost_value"] or 0) for item in items)
    valued_items = [item for item in items if item["market_value"] is not None]
    analyzed_market = sum(float(item["market_value"] or 0) for item in valued_items)
    complete = len(items) == len(holdings) and len(valued_items) == len(holdings)
    total_market = analyzed_market if complete else None
    analyzed_profit = (
        analyzed_market - analyzed_cost if len(valued_items) == len(items) else None
    )
    total_profit = analyzed_profit if complete else None
    total_profit_pct = total_profit / total_cost * 100 if total_profit is not None and total_cost else None
    day_start_values: dict[str, float] = {}
    for item in valued_items:
        change_pct = item.get("day_change_pct")
        market_value = item.get("market_value")
        if change_pct is None or market_value is None:
            day_start_values = {}
            break
        change_factor = 1 + float(change_pct) / 100
        if change_factor <= 0:
            day_start_values = {}
            break
        day_start_values[str(item["code"])] = float(market_value) / change_factor
    day_start_market = (
        sum(day_start_values.values())
        if valued_items and len(day_start_values) == len(valued_items)
        else None
    )
    for item in items:
        market_value = item["market_value"]
        item["weight_pct"] = (
            _round_number(float(market_value) / analyzed_market * 100)
            if market_value is not None and analyzed_market
            else None
        )
        day_start_value = day_start_values.get(str(item["code"]))
        item["day_contribution_pct"] = (
            _round_number(
                (float(market_value) - day_start_value) / day_start_market * 100
            )
            if market_value is not None
            and day_start_value is not None
            and day_start_market
            else None
        )
    items.sort(key=lambda item: float(item["market_value"] or 0), reverse=True)

    try:
        market = fetch_market()
    except Exception as exc:
        market = {"error": type(exc).__name__, "indices": [], "sectors": [], "time": ""}
    if not isinstance(market, dict):
        market = {"indices": [], "sectors": [], "time": ""}
    market_snapshot = {
        "time": str(market.get("time") or ""),
        "indices": market.get("indices") if isinstance(market.get("indices"), list) else [],
        "sectors": market.get("sectors") if isinstance(market.get("sectors"), list) else [],
        "error": str(market.get("error") or ""),
    }
    try:
        market_history = fetch_market_history()
    except Exception as exc:
        market_history = {
            "indices": [],
            "sectors": [],
            "errors": [{"code": "market_history", "reason": type(exc).__name__}],
        }
    if not isinstance(market_history, dict):
        market_history = {"indices": [], "sectors": [], "errors": []}
    coverage = {
        "holding_count": len(holdings),
        "analyzed_count": len(items),
        "valued_count": len(valued_items),
        "analysis_count_pct": _round_number(len(items) / len(holdings) * 100),
        "cost_coverage_pct": _round_number(analyzed_cost / total_cost * 100)
        if total_cost
        else None,
        "complete": complete,
        "weight_scope": "完整持仓" if complete else "成功分析持仓子集",
    }
    analytics = build_portfolio_analytics(
        items,
        analyses,
        market_history,
        coverage,
        total_cost,
    )
    boundaries = [
        "当前市值和盈亏未计入手续费、税费、分红与现金仓位",
        "多日组合历史按当前持仓数量静态回看，不等于真实账户历史收益",
        "实时价格可能延迟，历史分位和技术指标按最近日线计算",
        "历史诊断重放不模拟买卖、调仓、手续费或参数优化",
        "输入不包含新闻、公告、用户买入理由或未来预测",
    ]
    if not complete:
        boundaries.insert(
            0,
            "部分持仓缺少分析或当前价格；权重只描述成功子集，组合集中度不作完整判断",
        )
    return {
        "generated_at": utc_now().isoformat(timespec="seconds"),
        "totals": {
            "holding_count": len(holdings),
            "analyzed_count": len(items),
            "valued_count": len(valued_items),
            "total_cost": _round_number(total_cost),
            "total_market_value": _round_number(total_market),
            "total_profit_amount": _round_number(total_profit),
            "total_profit_pct": _round_number(total_profit_pct),
            "analyzed_cost_value": _round_number(analyzed_cost),
            "analyzed_market_value": _round_number(analyzed_market),
            "analyzed_profit_amount": _round_number(analyzed_profit),
        },
        "coverage": coverage,
        "holdings": items,
        "market": market_snapshot,
        "analytics": analytics,
        "failures": failures,
        "data_boundaries": boundaries,
    }


def _display_number(value: Any, digits: int = 2) -> str | None:
    number = _round_number(value, digits)
    if number is None:
        return None
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _display_pct(value: Any) -> str | None:
    number = _display_number(value)
    return f"{number}%" if number is not None else None


def build_confirmed_fact_catalog(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """Build the only facts the model may label as confirmed."""
    catalog: list[dict[str, str]] = []
    evidence_ids: set[str] = set()

    def add(evidence_id: str, fact: str) -> None:
        clean_id = str(evidence_id or "").strip()
        clean_fact = str(fact or "").strip()
        if (
            not clean_id
            or not clean_fact
            or clean_id in evidence_ids
            or len(catalog) >= CONFIRMED_FACT_CATALOG_LIMIT
        ):
            return
        evidence_ids.add(clean_id)
        catalog.append({"evidence_id": clean_id, "fact": clean_fact})

    totals = snapshot.get("totals") if isinstance(snapshot.get("totals"), dict) else {}
    coverage = (
        snapshot.get("coverage") if isinstance(snapshot.get("coverage"), dict) else {}
    )
    add(
        "portfolio.coverage",
        "组合数据覆盖%s/%s只持仓，当前价格覆盖%s/%s只，完整组合：%s。"
        % (
            coverage.get("analyzed_count", totals.get("analyzed_count", 0)),
            coverage.get("holding_count", totals.get("holding_count", 0)),
            totals.get("valued_count", 0),
            coverage.get("holding_count", totals.get("holding_count", 0)),
            "是" if coverage.get("complete") else "否",
        ),
    )
    total_cost = _display_number(totals.get("total_cost"))
    total_market = _display_number(totals.get("total_market_value"))
    total_profit = _display_number(totals.get("total_profit_amount"))
    total_profit_pct = _display_pct(totals.get("total_profit_pct"))
    total_parts = []
    if total_cost is not None:
        total_parts.append(f"成本{total_cost}元")
    if total_market is not None:
        total_parts.append(f"市值{total_market}元")
    if total_profit is not None:
        total_parts.append(f"累计盈亏{total_profit}元")
    if total_profit_pct is not None:
        total_parts.append(f"累计收益率{total_profit_pct}")
    if total_parts:
        add("portfolio.pnl.total", "组合" + "，".join(total_parts) + "。")

    analytics = (
        snapshot.get("analytics")
        if isinstance(snapshot.get("analytics"), dict)
        else {}
    )
    period = (
        analytics.get("analysis_period")
        if isinstance(analytics.get("analysis_period"), dict)
        else {}
    )
    if period.get("daily_data_through"):
        add(
            "portfolio.analysis_period",
            "组合日线共同截止日为%s，市场数据截止日为%s，分析窗口为5/10/20/60个交易日。"
            % (
                period.get("daily_data_through"),
                period.get("market_data_through") or "未提供",
            ),
        )

    trend = (
        analytics.get("portfolio_trend")
        if isinstance(analytics.get("portfolio_trend"), dict)
        else {}
    )
    if trend.get("state"):
        state_fact = f"组合强弱状态为{trend['state']}"
        if trend.get("state_reason"):
            state_fact += f"；程序依据：{trend['state_reason']}"
        add("portfolio.trend.state", state_fact + "。")
    returns = trend.get("returns") if isinstance(trend.get("returns"), dict) else {}
    relatives = (
        trend.get("relative_market")
        if isinstance(trend.get("relative_market"), dict)
        else {}
    )
    for window in (5, 10, 20, 60):
        key = f"{window}d"
        absolute = _display_pct(returns.get(key))
        relative = _display_pct(relatives.get(key))
        parts = []
        if absolute is not None:
            parts.append(f"组合收益{absolute}")
        if relative is not None:
            parts.append(f"相对沪深300 {relative}")
        if parts:
            add(f"portfolio.trend.return.{key}", f"近{window}日" + "，".join(parts) + "。")
    for metric_key, label in (("drawdown", "最大回撤"), ("volatility", "年化波动")):
        metric = trend.get(metric_key) if isinstance(trend.get(metric_key), dict) else {}
        for window in (20, 60):
            value = _display_pct(metric.get(f"{window}d"))
            if value is not None:
                add(
                    f"portfolio.trend.{metric_key}.{window}d",
                    f"组合近{window}日{label}为{value}。",
                )

    pnl_sources = (
        analytics.get("pnl_sources")
        if isinstance(analytics.get("pnl_sources"), dict)
        else {}
    )
    for group, label in (("top_gains", "累计盈利来源"), ("top_losses", "累计亏损来源")):
        for index, row in enumerate(pnl_sources.get(group) or [], 1):
            if not isinstance(row, dict):
                continue
            parts = [f"{row.get('name') or row.get('code')}({row.get('code')})"]
            amount = _display_number(row.get("profit_amount"))
            profit_pct = _display_pct(row.get("profit_pct"))
            weight = _display_pct(row.get("weight_pct"))
            if amount is not None:
                parts.append(f"盈亏{amount}元")
            if profit_pct is not None:
                parts.append(f"持仓收益率{profit_pct}")
            if weight is not None:
                parts.append(f"权重{weight}")
            add(f"portfolio.pnl.{group}.{index}", label + "：" + "，".join(parts) + "。")
    for index, row in enumerate(pnl_sources.get("period_20d") or [], 1):
        if not isinstance(row, dict):
            continue
        contribution = _display_pct(row.get("contribution_pct"))
        if contribution is not None:
            add(
                f"portfolio.pnl.period_20d.{index}",
                "近20日价格贡献：%s(%s)对期初组合市值贡献%s。"
                % (row.get("name") or row.get("code"), row.get("code"), contribution),
            )

    concentration = (
        analytics.get("concentration")
        if isinstance(analytics.get("concentration"), dict)
        else {}
    )
    if concentration:
        add(
            "portfolio.concentration.summary",
            "组合集中度%s；仓位%s、行业%s、主题%s；Top1 %s，Top3 %s。"
            % (
                concentration.get("assessment") or "数据不足",
                concentration.get("position_assessment") or "数据不足",
                concentration.get("industry_assessment") or "数据不足",
                concentration.get("theme_assessment") or "数据不足",
                _display_pct(concentration.get("top1_weight_pct")) or "暂无",
                _display_pct(concentration.get("top3_weight_pct")) or "暂无",
            ),
        )
        for field, label in (("industry_exposure", "行业暴露"), ("theme_exposure", "主题暴露")):
            for index, row in enumerate(concentration.get(field) or [], 1):
                if not isinstance(row, dict):
                    continue
                weight = _display_pct(row.get("weight_pct"))
                if weight is not None:
                    add(
                        f"portfolio.concentration.{field}.{index}",
                        f"{label}：{row.get('name') or '未识别'}占组合{weight}。",
                    )
        high_vol_weight = _display_pct(concentration.get("high_volatility_weight_pct"))
        if high_vol_weight is not None:
            add(
                "portfolio.concentration.high_volatility",
                "高波动资产集中度%s，权重%s，有效波动覆盖%s。"
                % (
                    concentration.get("high_volatility_assessment") or "数据不足",
                    high_vol_weight,
                    _display_pct(concentration.get("volatility_coverage_pct")) or "暂无",
                ),
            )

    correlation = (
        analytics.get("correlation")
        if isinstance(analytics.get("correlation"), dict)
        else {}
    )
    if correlation:
        add(
            "portfolio.correlation.summary",
            "持仓相关性风险%s；有效配对%s/%s，配对权重覆盖%s，最大高相关连通组权重%s。"
            % (
                correlation.get("risk_level") or "数据不足",
                correlation.get("valid_pair_count", 0),
                correlation.get("expected_pair_count", 0),
                _display_pct(correlation.get("pair_coverage_pct")) or "暂无",
                _display_pct(correlation.get("max_high_correlation_cluster_weight_pct")) or "暂无",
            ),
        )
        for index, row in enumerate(correlation.get("highest_pairs") or [], 1):
            if not isinstance(row, dict):
                continue
            corr = _display_number(row.get("correlation"), 3)
            if corr is not None:
                add(
                    f"portfolio.correlation.pair.{index}",
                    "%s与%s最近共同收益日相关系数为%s，合计权重%s。"
                    % (
                        row.get("left_name") or row.get("left_code"),
                        row.get("right_name") or row.get("right_code"),
                        corr,
                        _display_pct(row.get("combined_weight_pct")) or "暂无",
                    ),
                )

    for row in analytics.get("state_exposure") or []:
        if not isinstance(row, dict) or not row.get("state"):
            continue
        add(
            f"portfolio.state.{row['state']}",
            "%s持仓共%s只，占组合%s。"
            % (
                row["state"],
                row.get("holding_count", 0),
                _display_pct(row.get("weight_pct")) or "暂无",
            ),
        )
    for index, row in enumerate(analytics.get("priority_flags") or [], 1):
        if isinstance(row, dict) and row.get("title"):
            add(
                f"portfolio.priority.{index}",
                "程序重点问题（%s）：%s。%s"
                % (row.get("level") or "观察", row["title"], row.get("detail") or ""),
            )

    for item in snapshot.get("holdings") or []:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        code = str(item["code"])
        name = str(item.get("name") or code)
        overview_parts = []
        for value, label, percent in (
            (item.get("weight_pct"), "权重", True),
            (item.get("profit_amount"), "累计盈亏", False),
            (item.get("profit_pct"), "持仓收益率", True),
            (item.get("day_contribution_pct"), "当日组合贡献", True),
        ):
            display = _display_pct(value) if percent else _display_number(value)
            if display is not None:
                overview_parts.append(f"{label}{display}{'' if percent else '元'}")
        if overview_parts:
            add(f"holding.{code}.overview", f"{name}({code})" + "，".join(overview_parts) + "。")
        history = item.get("history") if isinstance(item.get("history"), dict) else {}
        if item.get("strength_state"):
            add(
                f"holding.{code}.state",
                f"{name}({code})强弱状态为{item['strength_state']}；程序依据：{item.get('strength_reason') or '未提供'}。",
            )
        item_returns = history.get("returns") if isinstance(history.get("returns"), dict) else {}
        item_relative = history.get("relative_market") if isinstance(history.get("relative_market"), dict) else {}
        for window in (5, 20, 60):
            key = f"{window}d"
            absolute = _display_pct(item_returns.get(key))
            relative = _display_pct(item_relative.get(key))
            parts = []
            if absolute is not None:
                parts.append(f"收益{absolute}")
            if relative is not None:
                parts.append(f"相对沪深300 {relative}")
            if parts:
                add(
                    f"holding.{code}.return.{key}",
                    f"{name}({code})近{window}日" + "，".join(parts) + "。",
                )

    market_context = (
        analytics.get("market_context")
        if isinstance(analytics.get("market_context"), dict)
        else {}
    )
    for group in ("indices", "sectors"):
        for row in market_context.get(group) or []:
            if not isinstance(row, dict) or not row.get("code"):
                continue
            returns = row.get("returns") if isinstance(row.get("returns"), dict) else {}
            parts = []
            for window in (5, 20):
                value = _display_pct(returns.get(f"{window}d"))
                if value is not None:
                    parts.append(f"近{window}日{value}")
            if row.get("trend"):
                parts.append(f"趋势{row['trend']}")
            if row.get("rank_20d") is not None:
                parts.append(f"20日排名{row['rank_20d']}")
            if parts:
                add(
                    f"market.{group}.{row['code']}",
                    "%s(%s)，%s，数据截至%s。"
                    % (
                        row.get("name") or row["code"],
                        row["code"],
                        "，".join(parts),
                        row.get("data_end") or "未提供",
                    ),
                )

    replay = (
        analytics.get("diagnostic_replay")
        if isinstance(analytics.get("diagnostic_replay"), dict)
        else {}
    )
    if replay.get("mode"):
        add(
            "portfolio.replay.scope",
            "%s；范围%s，样本期%s至%s。"
            % (
                replay.get("mode"),
                replay.get("scope") or "未提供",
                replay.get("sample_start") or "未提供",
                replay.get("sample_end") or "未提供",
            ),
        )
    for row in replay.get("portfolio_states") or replay.get("analyzed_subset_states") or []:
        if not isinstance(row, dict) or not row.get("state"):
            continue
        add(
            f"portfolio.replay.state.{row['state']}",
            "历史诊断中组合%s状态有%s个样本，样本质量%s；随后10日中位表现%s，相对沪深300 %s。"
            % (
                row["state"],
                row.get("samples", 0),
                row.get("sample_quality") or "未提供",
                _display_pct(row.get("median_return_10d")) or "未输出",
                _display_pct(row.get("median_excess_10d")) or "未输出",
            ),
        )

    for index, boundary in enumerate(snapshot.get("data_boundaries") or [], 1):
        if str(boundary or "").strip():
            add(f"data.boundary.{index}", "数据边界：" + str(boundary).strip() + "。")
    for index, failure in enumerate(snapshot.get("failures") or [], 1):
        if isinstance(failure, dict):
            add(
                f"data.failure.{index}",
                "持仓%s分析失败，原因：%s。"
                % (failure.get("code") or "未知", failure.get("reason") or "未提供"),
            )
    return catalog


def _confirmed_fact_map(catalog: Any) -> dict[str, str]:
    if not isinstance(catalog, list) or not catalog:
        raise ValueError("已确认事实目录不能为空")
    result: dict[str, str] = {}
    for row in catalog:
        if not isinstance(row, dict) or set(row) != {"evidence_id", "fact"}:
            raise ValueError("已确认事实目录格式无效")
        evidence_id = str(row.get("evidence_id") or "").strip()
        fact = str(row.get("fact") or "").strip()
        if not evidence_id or not fact or evidence_id in result:
            raise ValueError("已确认事实目录包含空值或重复 ID")
        result[evidence_id] = fact
    return result


def _strict_json(text: str) -> dict[str, Any]:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("模型 JSON 包含重复字段")
            result[key] = value
        return result

    candidate = str(text or "").strip().lstrip("\ufeff")
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        opening = lines[0].strip().lower() if lines else ""
        if len(lines) >= 3 and opening in {"```", "```json"} and lines[-1].strip() == "```":
            candidate = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(candidate, object_pairs_hook=reject_duplicates)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("模型没有返回严格 JSON 对象") from exc
    if not isinstance(payload, dict):
        raise ValueError("模型没有返回 JSON 对象")
    return payload


def _string_list(
    value: Any, field: str, limit: int = 8, max_chars: int = 600
) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} 必须是字符串数组")
    return [item.strip()[:max_chars] for item in value[:limit] if item.strip()]


def _normalize_independent(
    payload: dict[str, Any], confirmed_fact_catalog: list[dict[str, str]]
) -> dict[str, Any]:
    confirmed_facts_by_id = _confirmed_fact_map(confirmed_fact_catalog)
    if set(payload) != {"summary", "issues", "overall_missing_information"}:
        raise ValueError("独立分析字段不完整或包含额外字段")
    if not isinstance(payload["summary"], str):
        raise ValueError("summary 必须是字符串")
    summary = payload["summary"].strip()
    if not summary:
        raise ValueError("summary 不能为空")
    issues = payload["issues"]
    if not isinstance(issues, list) or not 2 <= len(issues) <= 5:
        raise ValueError("独立分析必须包含 2 至 5 个重要问题")
    normalized_issues = []
    required = {
        "title",
        "why_important",
        "confirmed_fact_ids",
        "data_inferences",
        "missing_information",
    }
    for index, issue in enumerate(issues, 1):
        if not isinstance(issue, dict) or set(issue) != required:
            raise ValueError(f"第 {index} 个问题字段不完整或包含额外字段")
        if not isinstance(issue["title"], str) or not isinstance(issue["why_important"], str):
            raise ValueError(f"第 {index} 个问题标题和重要性必须是字符串")
        title = issue["title"].strip()
        why_important = issue["why_important"].strip()
        if not title or not why_important:
            raise ValueError(f"第 {index} 个问题标题和重要性不能为空")
        confirmed_fact_ids = _string_list(
            issue["confirmed_fact_ids"], "confirmed_fact_ids", 2, 80
        )
        if not confirmed_fact_ids:
            raise ValueError(f"第 {index} 个问题必须引用已确认事实")
        if len(set(confirmed_fact_ids)) != len(confirmed_fact_ids):
            raise ValueError(f"第 {index} 个问题包含重复证据 ID")
        unknown_ids = [
            evidence_id
            for evidence_id in confirmed_fact_ids
            if evidence_id not in confirmed_facts_by_id
        ]
        if unknown_ids:
            raise ValueError(
                f"第 {index} 个问题引用未知证据 ID：{unknown_ids[0]}"
            )
        normalized_issues.append(
            {
                "title": title[:40],
                "why_important": why_important[:120],
                "confirmed_fact_ids": confirmed_fact_ids,
                "confirmed_facts": [
                    confirmed_facts_by_id[evidence_id]
                    for evidence_id in confirmed_fact_ids
                ],
                "data_inferences": _string_list(
                    issue["data_inferences"], "data_inferences", 2, 100
                ),
                "missing_information": _string_list(
                    issue["missing_information"], "missing_information", 2, 100
                ),
            }
        )
    return {
        "summary": summary[:800],
        "issues": normalized_issues,
        "overall_missing_information": _string_list(
            payload["overall_missing_information"],
            "overall_missing_information",
            3,
            100,
        ),
    }


def _normalize_comparison(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"agreements", "disagreements", "possible_omissions"}:
        raise ValueError("观点对比字段不完整或包含额外字段")
    disagreements = payload["disagreements"]
    if not isinstance(disagreements, list):
        raise ValueError("disagreements 必须是数组")
    required = {"topic", "user_view", "ai_view", "evidence_boundary"}
    normalized_disagreements = []
    for index, item in enumerate(disagreements[:3], 1):
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError(f"第 {index} 个分歧字段不完整或包含额外字段")
        if any(not isinstance(item[field], str) for field in required):
            raise ValueError(f"第 {index} 个分歧字段必须是字符串")
        normalized_disagreements.append(
            {field: item[field].strip()[:180] for field in required}
        )
    return {
        "agreements": _string_list(payload["agreements"], "agreements", 3, 180),
        "disagreements": normalized_disagreements,
        "possible_omissions": _string_list(
            payload["possible_omissions"], "possible_omissions", 3, 180
        ),
    }


def _compact_market_context_row(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    returns = value.get("returns") if isinstance(value.get("returns"), dict) else {}
    return {
        "code": value.get("code"),
        "name": value.get("name"),
        "return_5d_pct": returns.get("5d"),
        "return_20d_pct": returns.get("20d"),
        "trend": value.get("trend"),
        "rank_5d": value.get("rank_5d"),
        "rank_20d": value.get("rank_20d"),
        "rank_change": value.get("rank_change"),
        "data_end": value.get("data_end"),
        "stale": bool(value.get("stale")),
    }


def build_comparison_facts(snapshot: dict[str, Any]) -> dict[str, Any]:
    holdings = []
    for item in snapshot.get("holdings") or []:
        if not isinstance(item, dict):
            continue
        holdings.append(
            {
                key: item.get(key)
                for key in (
                    "code",
                    "name",
                    "quantity",
                    "cost_price",
                    "current_price",
                    "profit_pct",
                    "day_change_pct",
                    "day_contribution_pct",
                    "weight_pct",
                    "price_percentile",
                    "valuation",
                    "analysis_date",
                    "industry_group",
                    "themes",
                    "strength_state",
                    "strength_reason",
                )
            }
        )
        history = item.get("history") if isinstance(item.get("history"), dict) else {}
        holdings[-1]["history"] = {
            key: history.get(key)
            for key in (
                "data_end",
                "returns",
                "relative_market",
                "relative_sector",
                "drawdown",
                "volatility",
                "data_quality",
            )
        }
    market = snapshot.get("market") if isinstance(snapshot.get("market"), dict) else {}
    analytics = (
        snapshot.get("analytics")
        if isinstance(snapshot.get("analytics"), dict)
        else {}
    )
    market_context = (
        analytics.get("market_context")
        if isinstance(analytics.get("market_context"), dict)
        else {}
    )
    replay = (
        analytics.get("diagnostic_replay")
        if isinstance(analytics.get("diagnostic_replay"), dict)
        else {}
    )
    compact_benchmark = _compact_market_context_row(market_context.get("benchmark"))
    compact_indices = [
        compact
        for compact in (
            _compact_market_context_row(row)
            for row in market_context.get("indices") or []
        )
        if compact is not None
    ]
    compact_sectors = [
        compact
        for compact in (
            _compact_market_context_row(row)
            for row in market_context.get("sectors") or []
        )
        if compact is not None
    ]
    return {
        "generated_at": snapshot.get("generated_at"),
        "totals": snapshot.get("totals"),
        "coverage": snapshot.get("coverage"),
        "holdings": holdings,
        "market": {
            "time": market.get("time"),
            "indices": market.get("indices") if isinstance(market.get("indices"), list) else [],
            "sectors": (
                market.get("sectors")[:5]
                if isinstance(market.get("sectors"), list)
                else []
            ),
            "error": market.get("error"),
        },
        "failures": snapshot.get("failures"),
        "analytics": {
            key: analytics.get(key)
            for key in (
                "analysis_period",
                "portfolio_trend",
                "pnl_sources",
                "concentration",
                "state_exposure",
                "correlation",
                "priority_flags",
            )
        },
        "market_context": {
            "benchmark": compact_benchmark,
            "indices": compact_indices,
            "sectors": compact_sectors,
        },
        "diagnostic_replay": {
            key: replay.get(key)
            for key in (
                "mode",
                "sample_start",
                "sample_end",
                "scope",
                "portfolio_states",
                "coverage_complete",
                "limitations",
            )
        },
        "data_boundaries": snapshot.get("data_boundaries"),
    }


class PortfolioReportAssistant:
    def __init__(
        self,
        config: MonitorConfig,
        repository: MonitorRepository,
        analyzer: Callable[[str], dict[str, Any]] | None = None,
        market_provider: Callable[[], dict[str, Any]] | None = None,
        llm_call: Callable[[str, str, str, int], Any] | None = None,
        market_history_provider: Callable[[], dict[str, Any]] | None = None,
    ):
        self.config = config
        self.repository = repository
        self.analyzer = analyzer
        self.market_provider = market_provider
        self.market_history_provider = market_history_provider
        self.llm_call = llm_call

    def _build_snapshot(self) -> dict[str, Any]:
        return build_portfolio_snapshot(
            self.repository,
            self.analyzer,
            self.market_provider,
            self.market_history_provider,
        )

    def scan(self) -> dict[str, Any]:
        return {**self._build_snapshot(), "ai_used": False, "token_usage": 0}

    @staticmethod
    def _call_llm(
        system: str,
        user: str,
        api_key: str,
        max_tokens: int,
        deepseek_model: str = "",
    ) -> dict[str, Any]:
        model_name = app.resolve_deepseek_model(deepseek_model)
        body = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        response = app.api_post(
            app.AGENT_BASE + "/chat/completions",
            {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            body,
            timeout=120,
            retries=3,
        )
        usage = response.get("usage") or {}
        if "choices" not in response:
            error = ValueError(response.get("error", {}).get("message") or "模型返回异常")
            error.returned_token_usage = int(usage.get("total_tokens") or 0)
            raise error
        try:
            choice = response["choices"][0]
            content = choice["message"]["content"]
        except (IndexError, KeyError, TypeError):
            error = ValueError("模型返回结构异常")
            error.returned_token_usage = int(usage.get("total_tokens") or 0)
            raise error
        return {
            "content": content,
            "token_usage": int(usage.get("total_tokens") or 0),
            "finish_reason": str(choice.get("finish_reason") or ""),
        }

    @staticmethod
    def _call_gpt(
        system: str, user: str, api_key: str, max_tokens: int, schema: dict[str, Any]
    ) -> dict[str, Any]:
        body = {
            "model": GPT_PORTFOLIO_MODEL,
            "instructions": system,
            "input": user,
            "reasoning": {"effort": "low"},
            "max_output_tokens": max_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "portfolio_report",
                    "schema": schema,
                    "strict": True,
                }
            },
            "store": False,
        }
        try:
            response = app.api_post(
                app.OAI_BASE + "/responses",
                {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                body,
                timeout=150,
                retries=3,
            )
        except HTTPError as exc:
            message = ""
            try:
                payload = json.loads(exc.read().decode("utf-8"))
                message = str((payload.get("error") or {}).get("message") or "").strip()
            except Exception:
                pass
            detail = f"：{message[:300]}" if message else ""
            raise ValueError(f"GPT 请求被拒（HTTP {exc.code}）{detail}") from exc
        usage = response.get("usage") or {}
        if response.get("error"):
            error = response.get("error") or {}
            returned_error = ValueError(error.get("message") or "GPT 返回异常")
            returned_error.returned_token_usage = int(usage.get("total_tokens") or 0)
            raise returned_error
        output_text = "".join(
            str(part.get("text") or "")
            for item in response.get("output") or []
            if isinstance(item, dict) and item.get("type") == "message"
            for part in item.get("content") or []
            if isinstance(part, dict) and part.get("type") == "output_text"
        )
        if not output_text:
            error = ValueError("GPT 没有返回可解析的文本")
            error.returned_token_usage = int(usage.get("total_tokens") or 0)
            raise error
        incomplete = response.get("incomplete_details") or {}
        finish_reason = (
            "length"
            if response.get("status") == "incomplete"
            and incomplete.get("reason") == "max_output_tokens"
            else str(response.get("status") or "")
        )
        return {
            "content": output_text,
            "token_usage": int(usage.get("total_tokens") or 0),
            "finish_reason": finish_reason,
        }

    @staticmethod
    def _unpack_response(response: Any, estimate: int) -> tuple[str, int, str]:
        if isinstance(response, str):
            return response, estimate, ""
        if isinstance(response, tuple) and len(response) == 2:
            return str(response[0]), max(0, int(response[1])), ""
        if isinstance(response, dict) and "content" in response:
            return (
                str(response["content"]),
                max(0, int(response.get("token_usage") or estimate)),
                str(response.get("finish_reason") or ""),
            )
        raise ValueError("模型调用返回格式不受支持")

    @staticmethod
    def _estimate_stage_tokens(system: str, user: str, max_tokens: int) -> int:
        return max(1, ((len(system) + len(user)) * 3 // 4) + max_tokens)

    def _run_stage(
        self,
        system: str,
        user: str,
        api_key: str,
        max_tokens: int,
        normalizer: Callable[[dict[str, Any]], dict[str, Any]],
        provider: str,
        usage: dict[str, int],
        model_name: str,
        gpt_schema: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        estimate = self._estimate_stage_tokens(system, user, max_tokens)
        try:
            if self.llm_call:
                response = self.llm_call(system, user, api_key, max_tokens)
            elif provider == "gpt":
                response = self._call_gpt(
                    system, user, api_key, max_tokens, gpt_schema or {}
                )
            else:
                response = self._call_llm(
                    system, user, api_key, max_tokens, model_name
                )
        except Exception as exc:
            if hasattr(exc, "returned_token_usage"):
                usage["calls"] += 1
                usage["tokens"] += max(
                    0, int(getattr(exc, "returned_token_usage", 0) or 0)
                )
            raise
        usage["calls"] += 1
        try:
            raw, actual, finish_reason = self._unpack_response(response, estimate)
        except Exception:
            if isinstance(response, dict):
                usage["tokens"] += max(0, int(response.get("token_usage") or 0))
            raise
        usage["tokens"] += actual
        if finish_reason == "length":
            raise ValueError("模型输出过长被截断，请重新分析")
        payload = normalizer(_strict_json(raw))
        return payload, actual

    def latest(self) -> dict[str, Any]:
        report = self.repository.get_latest_portfolio_report()
        if not report:
            return {"report": None}
        payload = report.pop("payload")
        return {"report": {**report, **payload}}

    def history(self) -> dict[str, Any]:
        reports = []
        for report in self.repository.list_portfolio_reports(REPORT_HISTORY_LIMIT):
            payload = report.pop("payload")
            reports.append({**report, **payload})
        return {"reports": reports, "limit": REPORT_HISTORY_LIMIT}

    def generate(
        self,
        user_judgment: Any,
        api_key: str,
        force: bool = False,
        now: datetime | None = None,
        provider: str = "deepseek",
        deepseek_model: str = "",
    ) -> dict[str, Any]:
        judgment = str(user_judgment or "").strip()
        if not judgment:
            raise ValueError("请先填写“我的当前判断”")
        if len(judgment) > MAX_JUDGMENT_LENGTH:
            raise ValueError("“我的当前判断”不能超过 4000 字")
        provider_name = str(provider or "deepseek").strip().lower()
        if provider_name not in {"deepseek", "gpt"}:
            raise ValueError("持仓报告模型只能选择 DeepSeek 或 GPT")
        model_name = (
            GPT_PORTFOLIO_MODEL
            if provider_name == "gpt"
            else app.resolve_deepseek_model(deepseek_model)
        )
        current = now or utc_now()
        report_id = self.repository.create_portfolio_report(
            judgment,
            model_name,
            self.config.portfolio_report_retention_days,
            current,
            provider=provider_name,
        )
        usage_date = current.astimezone(SHANGHAI).date().isoformat()
        actual_usage = {"calls": 0, "tokens": 0}
        reserved_tokens = 0
        usage_reserved = False
        usage_settled = False

        def settle_usage() -> dict[str, Any]:
            nonlocal usage_settled
            if not usage_reserved or usage_settled:
                return self.repository.get_ai_usage(usage_date, USAGE_FEATURE)
            daily_usage = self.repository.adjust_ai_usage(
                usage_date,
                USAGE_FEATURE,
                calls_delta=actual_usage["calls"] - 2,
                tokens_delta=actual_usage["tokens"] - reserved_tokens,
                now=current,
            )
            usage_settled = True
            return daily_usage

        try:
            snapshot = self._build_snapshot()
            cache_snapshot = {key: value for key, value in snapshot.items() if key != "generated_at"}
            snapshot_json = json.dumps(
                cache_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
            cache_source = json.dumps(
                {
                    "version": REPORT_VERSION,
                    "provider": provider_name,
                    "model": model_name,
                    "snapshot_hash": snapshot_hash,
                    "user_judgment": judgment,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            cache_key = hashlib.sha256(cache_source.encode("utf-8")).hexdigest()
            if not force:
                cached = self.repository.get_cached_portfolio_report(cache_key, current)
                if cached:
                    payload = cached["payload"]
                    self.repository.complete_portfolio_report(
                        report_id, cache_key, snapshot_hash, payload, 0, current
                    )
                    self.repository.trim_portfolio_reports(REPORT_HISTORY_LIMIT)
                    return {
                        "report_id": report_id,
                        "user_judgment": judgment,
                        **payload,
                        "model": model_name,
                        "provider": provider_name,
                        "token_usage": 0,
                        "cached": True,
                        "status": "complete",
                        "daily_usage": self.repository.get_ai_usage(
                            usage_date, USAGE_FEATURE
                        ),
                    }

            key = str(api_key or "").strip()
            if not key:
                raise ValueError(
                    "请先在页面顶部保存 OpenAI Key"
                    if provider_name == "gpt"
                    else "请先在页面顶部保存 DeepSeek Key"
                )
            confirmed_fact_catalog = build_confirmed_fact_catalog(snapshot)
            independent_user = json.dumps(
                {
                    "portfolio_snapshot": snapshot,
                    "confirmed_fact_catalog": confirmed_fact_catalog,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            comparison_facts = build_comparison_facts(snapshot)
            comparison_budget_user = json.dumps(
                {
                    "user_judgment": judgment,
                    "comparison_facts": comparison_facts,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            reserved_tokens = (
                self._estimate_stage_tokens(
                    INDEPENDENT_SYSTEM_PROMPT,
                    independent_user,
                    INDEPENDENT_MAX_TOKENS,
                )
                + self._estimate_stage_tokens(
                    COMPARISON_SYSTEM_PROMPT,
                    comparison_budget_user,
                    COMPARISON_MAX_TOKENS,
                )
                + INDEPENDENT_MAX_TOKENS
            )
            self.repository.reserve_ai_usage(
                usage_date,
                USAGE_FEATURE,
                reserved_tokens,
                self.config.portfolio_report_daily_call_limit,
                self.config.portfolio_report_daily_token_limit,
                current,
                calls=2,
            )
            usage_reserved = True
            independent, first_tokens = self._run_stage(
                INDEPENDENT_SYSTEM_PROMPT,
                independent_user,
                key,
                INDEPENDENT_MAX_TOKENS,
                lambda payload: _normalize_independent(
                    payload, confirmed_fact_catalog
                ),
                provider_name,
                actual_usage,
                model_name,
                GPT_INDEPENDENT_REPORT_SCHEMA if provider_name == "gpt" else None,
            )
            comparison_user = json.dumps(
                {
                    "user_judgment": judgment,
                    "independent_analysis": independent,
                    "comparison_facts": comparison_facts,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            comparison, second_tokens = self._run_stage(
                COMPARISON_SYSTEM_PROMPT,
                comparison_user,
                key,
                COMPARISON_MAX_TOKENS,
                _normalize_comparison,
                provider_name,
                actual_usage,
                model_name,
                GPT_COMPARISON_REPORT_SCHEMA if provider_name == "gpt" else None,
            )
            payload = {
                "provider": provider_name,
                "snapshot_summary": {
                    "generated_at": snapshot["generated_at"],
                    "totals": snapshot["totals"],
                    "coverage": snapshot["coverage"],
                    "analytics": snapshot["analytics"],
                    "failures": snapshot["failures"],
                    "data_boundaries": snapshot["data_boundaries"],
                },
                "independent_analysis": independent,
                "comparison": comparison,
            }
            total_tokens = first_tokens + second_tokens
            daily = settle_usage()
            self.repository.complete_portfolio_report(
                report_id, cache_key, snapshot_hash, payload, total_tokens, current
            )
            self.repository.trim_portfolio_reports(REPORT_HISTORY_LIMIT)
            return {
                "report_id": report_id,
                "user_judgment": judgment,
                **payload,
                "model": model_name,
                "provider": provider_name,
                "token_usage": total_tokens,
                "cached": False,
                "status": "complete",
                "daily_usage": daily,
            }
        except Exception as exc:
            if usage_reserved and not usage_settled:
                settle_usage()
            self.repository.fail_portfolio_report(report_id, str(exc)[:300], current)
            raise
