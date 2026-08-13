from __future__ import annotations

import math
from statistics import median
from typing import Any


WINDOWS = (5, 10, 20, 60)
BENCHMARK_CODE = "000300"
BENCHMARK_NAME = "沪深300"
DIAGNOSTIC_LOOKBACK = 252
DIAGNOSTIC_STEP = 5
MIN_CORRELATION_SAMPLES = 20
MIN_REPLAY_STATE_SAMPLES = 8
MAX_RECENT_GAP_DAYS = 5
MAX_RECENT_FILL_PCT = 20.0
MAX_RECENT_STALE_DAYS = 2
MAX_COMMON_AS_OF_LAG_DAYS = 3
MIN_VOLATILITY_COVERAGE_PCT = 80.0
MIN_EXPOSURE_COVERAGE_PCT = 60.0
MIN_PORTFOLIO_HISTORY_COVERAGE_PCT = 99.5
SIGNIFICANT_DRAWDOWN_PCT = {"20d": 8.0, "60d": 12.0}
HIGH_DRAWDOWN_PCT = {"20d": 12.0, "60d": 20.0}
SIGNIFICANT_UNDERPERFORMANCE_PCT = {"20d": -3.0, "60d": -5.0}
HIGH_UNDERPERFORMANCE_PCT = {"20d": -6.0, "60d": -10.0}
MIN_LOSS_SOURCE_SHARE_PCT = 50.0
HIGH_LOSS_SOURCE_SHARE_PCT = 70.0
MIN_LOSS_SOURCE_PORTFOLIO_IMPACT_PCT = 1.0
HIGH_LOSS_SOURCE_PORTFOLIO_IMPACT_PCT = 3.0

CONCENTRATION_THRESHOLDS = {
    "top1_high": 30.0,
    "top3_high": 70.0,
    "industry_high": 40.0,
    "theme_high": 40.0,
    "top1_medium": 20.0,
    "top3_medium": 55.0,
    "industry_medium": 30.0,
    "theme_medium": 30.0,
}

# Only map labels that have a reasonably direct relationship to an existing ETF proxy.
SECTOR_PROXIES = (
    ("白酒", "512690", ("白酒", "酿酒")),
    ("半导体", "512480", ("半导体",)),
    ("芯片", "512760", ("芯片", "集成电路")),
    ("医药", "512010", ("医药生物", "化学制药", "中药", "生物制品")),
    ("医疗", "512170", ("医疗服务", "医疗器械")),
    ("券商", "512000", ("证券", "证券行业")),
    ("银行", "512800", ("银行",)),
    ("军工", "512660", ("国防军工", "军工")),
    ("新能源车", "515030", ("新能源汽车",)),
    ("光伏", "515790", ("光伏设备", "光伏")),
    ("地产", "512200", ("房地产", "房地产开发")),
    ("煤炭", "515220", ("煤炭", "煤炭开采")),
    ("养殖", "159865", ("养殖业", "畜牧业")),
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _round(value: Any, digits: int = 2) -> float | None:
    number = _number(value)
    return round(number, digits) if number is not None else None


def history_from_analysis(analysis: dict[str, Any]) -> list[tuple[str, float]]:
    chart = analysis.get("chart") if isinstance(analysis.get("chart"), dict) else {}
    dates = chart.get("dates") if isinstance(chart.get("dates"), list) else []
    candles = chart.get("candle") if isinstance(chart.get("candle"), list) else []
    history: list[tuple[str, float]] = []
    for trading_date, candle in zip(dates, candles):
        if not isinstance(candle, (list, tuple)) or len(candle) < 2:
            continue
        close = _number(candle[1])
        if close is not None and close > 0 and trading_date:
            history.append((str(trading_date), close))
    history.sort(key=lambda row: row[0])
    return history


def _window_return(values: list[float], days: int) -> float | None:
    if len(values) <= days or not values[-days - 1]:
        return None
    return (values[-1] / values[-days - 1] - 1) * 100


def _moving_average(values: list[float], days: int) -> float | None:
    if len(values) < days:
        return None
    return sum(values[-days:]) / days


def _max_drawdown(values: list[float], days: int) -> float | None:
    if len(values) < days + 1:
        return None
    sample = values[-(days + 1) :]
    peak = sample[0]
    drawdown = 0.0
    for value in sample:
        peak = max(peak, value)
        if peak:
            drawdown = max(drawdown, (peak - value) / peak)
    return drawdown * 100


def _annual_volatility(values: list[float], days: int) -> float | None:
    if len(values) < days + 1:
        return None
    sample = values[-(days + 1) :]
    returns = [
        sample[index] / sample[index - 1] - 1
        for index in range(1, len(sample))
        if sample[index - 1]
    ]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(244) * 100


def _align_histories(
    first: list[tuple[str, float]], second: list[tuple[str, float]]
) -> tuple[list[str], list[float], list[float]]:
    first_map = dict(first)
    second_map = dict(second)
    dates = sorted(set(first_map).intersection(second_map))
    return dates, [first_map[day] for day in dates], [second_map[day] for day in dates]


def _align_to_reference(
    history: list[tuple[str, float]], reference: list[tuple[str, float]]
) -> tuple[list[str], list[float], list[float]]:
    """Align to the reference trading calendar and carry a suspended asset forward."""
    if not history or not reference:
        return [], [], []
    source = dict(history)
    latest_common = min(history[-1][0], reference[-1][0])
    dates: list[str] = []
    values: list[float] = []
    reference_values: list[float] = []
    last_value = None
    for trading_date, reference_value in reference:
        if trading_date > latest_common:
            break
        if trading_date in source:
            last_value = source[trading_date]
        if last_value is None:
            continue
        dates.append(trading_date)
        values.append(last_value)
        reference_values.append(reference_value)
    return dates, values, reference_values


def _align_to_reference_with_tail(
    history: list[tuple[str, float]], reference: list[tuple[str, float]]
) -> tuple[list[str], list[float], list[float]]:
    """Carry the latest proxy value to the reference end so staleness is measurable."""
    if not history or not reference:
        return [], [], []
    source = dict(history)
    dates: list[str] = []
    values: list[float] = []
    reference_values: list[float] = []
    last_value = None
    for trading_date, reference_value in reference:
        if trading_date in source:
            last_value = source[trading_date]
        if last_value is None:
            continue
        dates.append(trading_date)
        values.append(last_value)
        reference_values.append(reference_value)
    return dates, values, reference_values


def _gap_stats(source_dates: set[str], dates: list[str]) -> tuple[int, int, int]:
    carried_forward_days = sum(day not in source_dates for day in dates)
    longest_gap = 0
    current_gap = 0
    for day in dates:
        if day in source_dates:
            current_gap = 0
        else:
            current_gap += 1
            longest_gap = max(longest_gap, current_gap)
    stale_days = 0
    for day in reversed(dates):
        if day in source_dates:
            break
        stale_days += 1
    return carried_forward_days, longest_gap, stale_days


def _window_alignment_quality(
    history: list[tuple[str, float]], aligned_dates: list[str], days: int
) -> dict[str, Any]:
    required_points = days + 1
    window_dates = aligned_dates[-required_points:]
    source_dates = {day for day, _ in history}
    carried_forward_days, longest_gap, stale_days = _gap_stats(
        source_dates, window_dates
    )
    fill_pct = (
        carried_forward_days / len(window_dates) * 100 if window_dates else None
    )
    full_window = len(window_dates) >= required_points
    reasons = []
    if not full_window:
        reasons.append(
            "少于60个共同交易日"
            if days == 60
            else f"不足{days}个完整收益日"
        )
    if longest_gap > MAX_RECENT_GAP_DAYS:
        reasons.append(f"连续缺口{longest_gap}日")
    if fill_pct is not None and fill_pct > MAX_RECENT_FILL_PCT:
        reasons.append(f"前值填充{round(fill_pct, 1)}%")
    if stale_days > MAX_RECENT_STALE_DAYS:
        reasons.append(f"末端陈旧{stale_days}日")
    return {
        "required_points": required_points,
        "sample_points": len(window_dates),
        "full_window": full_window,
        "carried_forward_days": carried_forward_days,
        "fill_pct": _round(fill_pct),
        "longest_gap_days": longest_gap,
        "stale_trading_days": stale_days,
        "usable": not reasons,
        "reason": "、".join(reasons),
    }


def _alignment_quality(
    history: list[tuple[str, float]], aligned_dates: list[str]
) -> dict[str, Any]:
    source_dates = {day for day, _ in history}
    carried_forward_days, longest_gap, stale_days = _gap_stats(
        source_dates, aligned_dates
    )
    last_observed_date = next(
        (day for day in reversed(aligned_dates) if day in source_dates), ""
    )
    windows = {
        f"{window}d": _window_alignment_quality(history, aligned_dates, window)
        for window in WINDOWS
    }
    recent = windows["60d"]
    return {
        "carried_forward_days": carried_forward_days,
        "longest_gap_days": longest_gap,
        "last_observed_date": last_observed_date,
        "stale_trading_days": stale_days,
        "windows": windows,
        "usable_for_trend": recent["usable"],
        "quality_issue": recent["reason"],
        "note": (
            "近期行情质量不足：" + recent["reason"]
            if not recent["usable"]
            else "停牌或缺失交易日按前值填充"
            if carried_forward_days
            else "未发生前值填充"
        ),
    }


def _relative_returns(
    history: list[tuple[str, float]], reference: list[tuple[str, float]]
) -> dict[str, float | None]:
    if not history or not reference:
        return {f"{window}d": None for window in WINDOWS}
    _, values, reference_values = _align_to_reference(history, reference)
    return {
        f"{window}d": _round(
            (_window_return(values, window) or 0)
            - (_window_return(reference_values, window) or 0)
        )
        if _window_return(values, window) is not None
        and _window_return(reference_values, window) is not None
        else None
        for window in WINDOWS
    }


def _classify_state(
    values: list[float],
    returns: dict[str, float | None],
    relative: dict[str, float | None],
) -> tuple[str, str]:
    ma20 = _moving_average(values, 20)
    ma60 = _moving_average(values, 60)
    r5, r20 = returns.get("5d"), returns.get("20d")
    er5, er20 = relative.get("5d"), relative.get("20d")
    if any(value is None for value in (ma20, ma60, r5, r20, er5, er20)):
        return "数据不足", "少于60个共同交易日或基准数据不足"
    close = values[-1]
    if close > ma20 > ma60 and r20 > 0 and er20 > 0:
        return "强势", "站上MA20/MA60，近20日上涨并跑赢沪深300"
    if close < ma20 < ma60 and r20 < 0 and er20 < 0:
        return "弱势", "跌破MA20/MA60，近20日下跌并跑输沪深300"
    if (close < ma20 and r5 < 0) or (r20 > 0 and r5 < 0 and er5 < 0):
        return "转弱", "跌破MA20且近5日下跌，或中期上涨但短期下跌并跑输沪深300"
    return "震荡", "趋势与相对强弱信号混合，未满足强势、转弱或弱势条件"


def series_metrics(
    history: list[tuple[str, float]],
    benchmark: list[tuple[str, float]] | None = None,
    sector: list[tuple[str, float]] | None = None,
    quality_reference: list[tuple[str, float]] | None = None,
) -> dict[str, Any]:
    effective_history = history
    benchmark_values: list[float] = []
    if benchmark:
        dates, values, benchmark_values = _align_to_reference(history, benchmark)
        effective_history = list(zip(dates, values))
    elif quality_reference:
        dates, values, _ = _align_to_reference(history, quality_reference)
        effective_history = list(zip(dates, values))
    else:
        dates = [day for day, _ in history]
        values = [value for _, value in history]
    quality = _alignment_quality(history, dates)

    def window_usable(window: int) -> bool:
        return bool(quality["windows"][f"{window}d"]["usable"])

    returns = {
        f"{window}d": (
            _round(_window_return(values, window)) if window_usable(window) else None
        )
        for window in WINDOWS
    }
    relative_market = {
        f"{window}d": _round(
            returns[f"{window}d"] - _window_return(benchmark_values, window)
        )
        if returns[f"{window}d"] is not None
        and _window_return(benchmark_values, window) is not None
        else None
        for window in WINDOWS
    }
    sector_history = sector or []
    sector_dates, sector_values, sector_reference_values = (
        _align_to_reference_with_tail(sector_history, effective_history)
    )
    sector_quality = _alignment_quality(sector_history, sector_dates)
    relative_sector = {}
    for window in WINDOWS:
        key = f"{window}d"
        sector_return = _window_return(sector_values, window)
        reference_return = _window_return(sector_reference_values, window)
        relative_sector[key] = (
            _round(reference_return - sector_return)
            if window_usable(window)
            and sector_quality["windows"][key]["usable"]
            and reference_return is not None
            and sector_return is not None
            else None
        )
    sector_quality = {
        **sector_quality,
        "available": bool(sector_history),
        "source_data_through": sector_history[-1][0] if sector_history else "",
        "comparison_data_through": sector_dates[-1] if sector_dates else "",
    }
    if not quality["usable_for_trend"]:
        state, state_reason = "数据不足", quality["quality_issue"] or "近期行情质量不足"
    else:
        state, state_reason = _classify_state(values, returns, relative_market)
    return {
        "data_start": effective_history[0][0] if effective_history else "",
        "data_end": effective_history[-1][0] if effective_history else "",
        "sample_count": len(effective_history),
        "latest_close": _round(values[-1], 3) if values else None,
        "returns": returns,
        "relative_market": relative_market,
        "relative_sector": relative_sector,
        "sector_data_quality": sector_quality,
        "ma20": _round(_moving_average(values, 20), 3) if window_usable(20) else None,
        "ma60": _round(_moving_average(values, 60), 3) if window_usable(60) else None,
        "drawdown": {
            "20d": _round(_max_drawdown(values, 20)) if window_usable(20) else None,
            "60d": _round(_max_drawdown(values, 60)) if window_usable(60) else None,
        },
        "volatility": {
            "20d": _round(_annual_volatility(values, 20)) if window_usable(20) else None,
            "60d": _round(_annual_volatility(values, 60)) if window_usable(60) else None,
        },
        "state": state,
        "state_reason": state_reason,
        "data_quality": quality,
    }


def _market_histories(market_history: dict[str, Any]) -> dict[str, list[tuple[str, float]]]:
    result: dict[str, list[tuple[str, float]]] = {}
    for group in ("indices", "sectors"):
        rows = market_history.get(group) if isinstance(market_history.get(group), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            dates = row.get("dates") if isinstance(row.get("dates"), list) else []
            closes = row.get("closes") if isinstance(row.get("closes"), list) else []
            history = []
            for trading_date, close_value in zip(dates, closes):
                close = _number(close_value)
                if close is not None and close > 0 and trading_date:
                    history.append((str(trading_date), close))
            if history:
                result[str(row.get("code") or "")] = history
    return result


def _sector_proxy(analysis: dict[str, Any]) -> dict[str, str] | None:
    code = str(analysis.get("code") or "")
    for name, proxy_code, _ in SECTOR_PROXIES:
        if code == proxy_code:
            return {"name": name, "code": proxy_code}
    context = analysis.get("company_context") if isinstance(analysis.get("company_context"), dict) else {}
    labels = [str(value).strip() for value in context.get("industry_path") or [] if str(value).strip()]
    industry = str(context.get("industry") or "").strip()
    if industry:
        labels.append(industry)
    for name, proxy_code, exact_labels in SECTOR_PROXIES:
        if any(label in exact_labels for label in labels):
            return {"name": name, "code": proxy_code}
    return None


def _primary_industry(analysis: dict[str, Any], proxy: dict[str, str] | None) -> str:
    path = _industry_hierarchy(analysis, proxy)
    return path[-1] if path else "未识别"


def _industry_hierarchy(
    analysis: dict[str, Any], proxy: dict[str, str] | None
) -> list[str]:
    context = analysis.get("company_context") if isinstance(analysis.get("company_context"), dict) else {}
    path = list(
        dict.fromkeys(
            str(value).strip()
            for value in context.get("industry_path") or []
            if str(value).strip()
        )
    )
    industry = str(context.get("industry") or "").strip()
    if industry and industry not in path:
        path.append(industry)
    if not path and proxy:
        path.append(proxy["name"])
    return path


def _exposure_rows(values: dict[str, float], limit: int = 8) -> list[dict[str, Any]]:
    return [
        {"name": name, "weight_pct": _round(weight)}
        for name, weight in sorted(values.items(), key=lambda row: row[1], reverse=True)[:limit]
    ]


def _pearson(first: list[float], second: list[float]) -> float | None:
    if len(first) != len(second) or len(first) < MIN_CORRELATION_SAMPLES:
        return None
    first_mean = sum(first) / len(first)
    second_mean = sum(second) / len(second)
    numerator = sum(
        (left - first_mean) * (right - second_mean)
        for left, right in zip(first, second)
    )
    first_scale = math.sqrt(sum((value - first_mean) ** 2 for value in first))
    second_scale = math.sqrt(sum((value - second_mean) ** 2 for value in second))
    if not first_scale or not second_scale:
        return None
    return numerator / (first_scale * second_scale)


def _correlation_summary(
    items: list[dict[str, Any]],
    histories: dict[str, list[tuple[str, float]]],
    coverage_complete: bool,
    quality_histories: dict[str, list[tuple[str, float]]] | None = None,
) -> dict[str, Any]:
    pairs = []
    correlations = []
    for left_index, left in enumerate(items):
        for right in items[left_index + 1 :]:
            dates, left_values, right_values = _align_histories(
                histories.get(left["code"], []), histories.get(right["code"], [])
            )
            if len(dates) < MIN_CORRELATION_SAMPLES + 1:
                continue
            quality_days = min(60, len(dates) - 1)
            source_histories = quality_histories or histories
            if any(
                not _window_alignment_quality(
                    source_histories.get(str(item["code"]), []),
                    dates,
                    quality_days,
                )["usable"]
                for item in (left, right)
            ):
                continue
            left_values = left_values[-61:]
            right_values = right_values[-61:]
            left_returns = [left_values[i] / left_values[i - 1] - 1 for i in range(1, len(left_values))]
            right_returns = [right_values[i] / right_values[i - 1] - 1 for i in range(1, len(right_values))]
            correlation = _pearson(left_returns, right_returns)
            if correlation is None:
                continue
            combined_weight = float(left.get("weight_pct") or 0) + float(right.get("weight_pct") or 0)
            correlations.append(correlation)
            pairs.append(
                {
                    "left_code": left["code"],
                    "left_name": left["name"],
                    "right_code": right["code"],
                    "right_name": right["name"],
                    "correlation": _round(correlation, 3),
                    "combined_weight_pct": _round(combined_weight),
                    "sample_count": len(left_returns),
                }
            )
    pairs.sort(
        key=lambda row: -2.0
        if row["correlation"] is None
        else float(row["correlation"]),
        reverse=True,
    )
    high_pairs = [row for row in pairs if float(row["correlation"] or 0) >= 0.75]
    expected_pair_count = len(items) * (len(items) - 1) // 2
    valid_pair_count = len(pairs)
    weights = {
        str(item["code"]): float(item.get("weight_pct") or 0) for item in items
    }
    expected_pair_weight = sum(
        weights[str(left["code"])] * weights[str(right["code"])]
        for left_index, left in enumerate(items)
        for right in items[left_index + 1 :]
    )
    valid_pair_weight = sum(
        weights[row["left_code"]] * weights[row["right_code"]] for row in pairs
    )
    covered_codes = {
        str(row["left_code"]) for row in pairs
    } | {str(row["right_code"]) for row in pairs}
    uncovered_codes = [
        str(item["code"]) for item in items if str(item["code"]) not in covered_codes
    ]
    uncovered_weight = sum(weights[code] for code in uncovered_codes)
    parents = {str(item["code"]): str(item["code"]) for item in items}

    def find(code: str) -> str:
        while parents[code] != code:
            parents[code] = parents[parents[code]]
            code = parents[code]
        return code

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for row in high_pairs:
        union(str(row["left_code"]), str(row["right_code"]))
    cluster_codes: dict[str, set[str]] = {}
    connected_codes = {
        str(row["left_code"]) for row in high_pairs
    } | {str(row["right_code"]) for row in high_pairs}
    for code in connected_codes:
        cluster_codes.setdefault(find(code), set()).add(code)
    item_by_code = {str(item["code"]): item for item in items}
    clusters = []
    for codes in cluster_codes.values():
        cluster_items = [item_by_code[code] for code in sorted(codes) if code in item_by_code]
        clusters.append(
            {
                "codes": [str(item["code"]) for item in cluster_items],
                "names": [str(item["name"]) for item in cluster_items],
                "weight_pct": _round(
                    sum(float(item.get("weight_pct") or 0) for item in cluster_items)
                ),
            }
        )
    clusters.sort(key=lambda row: float(row["weight_pct"] or 0), reverse=True)
    max_cluster_weight = float(clusters[0]["weight_pct"] or 0) if clusters else 0.0
    if not coverage_complete or expected_pair_count == 0 or not pairs:
        risk_level = "数据不足"
    elif max_cluster_weight >= 50:
        risk_level = "高"
    elif valid_pair_count < expected_pair_count:
        risk_level = "数据不足"
    elif high_pairs:
        risk_level = "中"
    else:
        risk_level = "低"
    return {
        "average_correlation": _round(sum(correlations) / len(correlations), 3) if correlations else None,
        "highest_pairs": pairs[:5],
        "high_correlation_pair_count": len(high_pairs),
        "high_correlation_clusters": clusters[:3],
        "max_high_correlation_cluster_weight_pct": _round(max_cluster_weight),
        "expected_pair_count": expected_pair_count,
        "valid_pair_count": valid_pair_count,
        "pair_coverage_pct": _round(
            valid_pair_weight / expected_pair_weight * 100
            if expected_pair_weight
            else None
        ),
        "uncovered_codes": uncovered_codes,
        "uncovered_weight_pct": _round(uncovered_weight),
        "risk_level": risk_level,
        "sample_window": "最近60个共同收益日；少于20个样本不判断；行情缺口超限的配对跳过",
        "cluster_note": "高相关连通组：组内通过高相关关系相连，并非每一对都必须达到阈值",
    }


def _static_portfolio_history(
    items: list[dict[str, Any]],
    histories: dict[str, list[tuple[str, float]]],
    benchmark: list[tuple[str, float]],
) -> tuple[list[tuple[str, float]], float]:
    eligible = [item for item in items if histories.get(item["code"]) and _number(item.get("quantity")) is not None]
    coverage = sum(float(item.get("weight_pct") or 0) for item in eligible)
    if not eligible:
        return [], coverage
    history_maps = {item["code"]: dict(histories[item["code"]]) for item in eligible}
    reference_dates = (
        [day for day, _ in benchmark]
        if benchmark
        else sorted({day for values in history_maps.values() for day in values})
    )
    if not reference_dates:
        return [], coverage
    latest_common = min(max(values) for values in history_maps.values())
    values_by_code: dict[str, dict[str, float]] = {item["code"]: {} for item in eligible}
    for item in eligible:
        source = history_maps[item["code"]]
        last = None
        for trading_date in reference_dates:
            if trading_date > latest_common:
                break
            if trading_date in source:
                last = source[trading_date]
            if last is not None:
                values_by_code[item["code"]][trading_date] = last
    dates = [
        trading_date
        for trading_date in reference_dates
        if trading_date <= latest_common
        and all(trading_date in values_by_code[item["code"]] for item in eligible)
    ]
    series = []
    for trading_date in dates:
        value = sum(
            float(item["quantity"]) * values_by_code[item["code"]][trading_date]
            for item in eligible
        )
        if value > 0:
            series.append((trading_date, value))
    return series, coverage


def _period_contributions(
    items: list[dict[str, Any]],
    histories: dict[str, list[tuple[str, float]]],
    portfolio_history: list[tuple[str, float]],
) -> None:
    if not portfolio_history:
        return
    portfolio_map = dict(portfolio_history)
    dates = [day for day, _ in portfolio_history]
    for item in items:
        aligned_dates, aligned_values, _ = _align_to_reference(
            histories.get(item["code"], []), portfolio_history
        )
        source = dict(zip(aligned_dates, aligned_values))
        contributions: dict[str, float | None] = {}
        for window in WINDOWS:
            if len(dates) <= window:
                contributions[f"{window}d"] = None
                continue
            start_date, end_date = dates[-window - 1], dates[-1]
            start_price, end_price = source.get(start_date), source.get(end_date)
            start_value = portfolio_map.get(start_date)
            if start_price is None or end_price is None or not start_value:
                contributions[f"{window}d"] = None
                continue
            contribution = float(item["quantity"]) * (end_price - start_price) / start_value * 100
            contributions[f"{window}d"] = _round(contribution)
        item["period_contribution_pct"] = contributions


def _state_at(values: list[float], benchmark_values: list[float], index: int) -> str:
    sample = values[: index + 1]
    reference = benchmark_values[: index + 1]
    returns = {f"{window}d": _window_return(sample, window) for window in WINDOWS}
    relative = {
        f"{window}d": (
            returns[f"{window}d"] - _window_return(reference, window)
            if returns[f"{window}d"] is not None and _window_return(reference, window) is not None
            else None
        )
        for window in WINDOWS
    }
    return _classify_state(sample, returns, relative)[0]


def _replay_observations(
    history: list[tuple[str, float]],
    benchmark: list[tuple[str, float]],
    quality_histories: list[list[tuple[str, float]]] | None = None,
) -> list[dict[str, Any]]:
    dates, values, benchmark_values = _align_to_reference(history, benchmark)
    start = max(60, len(dates) - DIAGNOSTIC_LOOKBACK)
    stop = len(dates) - 10
    observations = []
    quality_sources = quality_histories or [history]
    for index in range(start, stop, DIAGNOSTIC_STEP):
        observation_dates = dates[: index + 1]
        if any(
            not _window_alignment_quality(source, observation_dates, 60)["usable"]
            for source in quality_sources
        ):
            continue
        state = _state_at(values, benchmark_values, index)
        if state == "数据不足":
            continue
        row = {"date": dates[index], "state": state}
        for horizon in (5, 10):
            asset_return = (values[index + horizon] / values[index] - 1) * 100
            benchmark_return = (benchmark_values[index + horizon] / benchmark_values[index] - 1) * 100
            row[f"return_{horizon}d"] = asset_return
            row[f"excess_{horizon}d"] = asset_return - benchmark_return
        observations.append(row)
    return observations


def _aggregate_replay(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for state in ("强势", "震荡", "转弱", "弱势"):
        samples = [row for row in observations if row["state"] == state]
        if not samples:
            continue
        output: dict[str, Any] = {"state": state, "samples": len(samples)}
        output["sample_quality"] = (
            "可参考" if len(samples) >= MIN_REPLAY_STATE_SAMPLES else "样本不足"
        )
        for horizon in (5, 10):
            returns = [float(row[f"return_{horizon}d"]) for row in samples]
            excess = [float(row[f"excess_{horizon}d"]) for row in samples]
            sufficient = len(samples) >= MIN_REPLAY_STATE_SAMPLES
            output[f"median_return_{horizon}d"] = _round(median(returns)) if sufficient else None
            output[f"median_excess_{horizon}d"] = _round(median(excess)) if sufficient else None
            output[f"up_rate_{horizon}d"] = (
                _round(sum(value > 0 for value in returns) / len(returns) * 100)
                if sufficient
                else None
            )
            output[f"outperform_rate_{horizon}d"] = (
                _round(sum(value > 0 for value in excess) / len(excess) * 100)
                if sufficient
                else None
            )
        rows.append(output)
    return rows


def _market_context(
    market_history: dict[str, Any], histories: dict[str, list[tuple[str, float]]]
) -> dict[str, Any]:
    benchmark = histories.get(BENCHMARK_CODE, [])
    output: dict[str, Any] = {
        "benchmark": {"code": BENCHMARK_CODE, "name": BENCHMARK_NAME},
        "indices": [],
        "sectors": [],
    }
    for group in ("indices", "sectors"):
        rows = market_history.get(group) if isinstance(market_history.get(group), list) else []
        group_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "")
            history = histories.get(code, [])
            if not history:
                continue
            metrics = series_metrics(history, benchmark if code != BENCHMARK_CODE else None)
            ma20, ma60 = metrics["ma20"], metrics["ma60"]
            close = metrics["latest_close"]
            r5, r20 = metrics["returns"]["5d"], metrics["returns"]["20d"]
            trend_inputs = (close, ma20, ma60, r5, r20)
            if (
                not metrics["data_quality"]["usable_for_trend"]
                or any(value is None for value in trend_inputs)
            ):
                trend = "数据不足"
            elif close > ma20 > ma60 and r20 > 0:
                trend = "强势"
            elif close < ma20 < ma60 and r20 < 0:
                trend = "弱势"
            elif r5 < 0 and close < ma20:
                trend = "转弱"
            else:
                trend = "震荡"
            group_rows.append(
                {
                    "code": code,
                    "name": str(row.get("name") or code),
                    "data_end": metrics["data_end"],
                    "returns": metrics["returns"],
                    "relative_market": metrics["relative_market"],
                    "drawdown_60d": metrics["drawdown"]["60d"],
                    "volatility_20d": metrics["volatility"]["20d"],
                    "trend": trend,
                    "stale": bool(benchmark and metrics["data_end"] != benchmark[-1][0]),
                }
            )
        if group == "sectors":
            rankable_5 = [
                row
                for row in group_rows
                if not row["stale"] and row["returns"]["5d"] is not None
            ]
            rankable_20 = [
                row
                for row in group_rows
                if not row["stale"] and row["returns"]["20d"] is not None
            ]
            rank5 = {
                row["code"]: index + 1
                for index, row in enumerate(
                    sorted(
                        rankable_5,
                        key=lambda item: float(item["returns"]["5d"]),
                        reverse=True,
                    )
                )
            }
            rank20 = {
                row["code"]: index + 1
                for index, row in enumerate(
                    sorted(
                        rankable_20,
                        key=lambda item: float(item["returns"]["20d"]),
                        reverse=True,
                    )
                )
            }
            for row in group_rows:
                row["rank_5d"] = rank5.get(row["code"])
                row["rank_20d"] = rank20.get(row["code"])
                row["rank_change"] = (
                    rank20[row["code"]] - rank5[row["code"]]
                    if row["code"] in rank20 and row["code"] in rank5
                    else None
                )
            group_rows.sort(
                key=lambda item: -1e9
                if item["returns"]["20d"] is None
                else float(item["returns"]["20d"]),
                reverse=True,
            )
        output[group] = group_rows
    benchmark_metrics = next(
        (row for row in output["indices"] if row["code"] == BENCHMARK_CODE), None
    )
    if benchmark_metrics:
        output["benchmark"] = benchmark_metrics
    return output


def build_portfolio_analytics(
    items: list[dict[str, Any]],
    analyses: dict[str, dict[str, Any]],
    market_history: dict[str, Any],
    coverage: dict[str, Any],
    total_cost: float,
) -> dict[str, Any]:
    market_histories = _market_histories(market_history)
    benchmark = market_histories.get(BENCHMARK_CODE, [])
    histories = {
        code: history_from_analysis(analysis)
        for code, analysis in analyses.items()
    }
    portfolio_history, subset_history_coverage = _static_portfolio_history(
        items, histories, benchmark
    )
    common_as_of = portfolio_history[-1][0] if portfolio_history else ""
    common_benchmark = (
        [(day, value) for day, value in benchmark if day <= common_as_of]
        if common_as_of
        else benchmark
    )
    common_as_of_lag_days = (
        sum(1 for day, _ in benchmark if day > common_as_of)
        if benchmark and common_as_of
        else 0
    )
    common_as_of_stale = common_as_of_lag_days > MAX_COMMON_AS_OF_LAG_DAYS
    quality_calendar = common_benchmark or portfolio_history
    aligned_histories: dict[str, list[tuple[str, float]]] = {}
    for code, history in histories.items():
        if quality_calendar:
            dates, values, _ = _align_to_reference(history, quality_calendar)
            aligned_histories[code] = list(zip(dates, values))
        else:
            aligned_histories[code] = history
    industry_weights: dict[str, float] = {}
    industry_level_weights: list[dict[str, float]] = []
    industry_level_covered_weights: list[float] = []
    theme_weights: dict[str, float] = {}
    asset_type_weights: dict[str, float] = {}
    industry_covered_weight = 0.0
    theme_covered_weight = 0.0
    for item in items:
        analysis = analyses[item["code"]]
        proxy = _sector_proxy(analysis)
        industry_path = _industry_hierarchy(analysis, proxy)
        industry = industry_path[-1] if industry_path else "未识别"
        context = analysis.get("company_context") if isinstance(analysis.get("company_context"), dict) else {}
        themes = list(
            dict.fromkeys(
                str(value).strip()
                for value in context.get("concepts") or []
                if str(value).strip()
            )
        )[:4]
        weight = float(item.get("weight_pct") or 0)
        asset_type = str(
            analysis.get("type_name") or analysis.get("classify") or "未识别"
        ).strip() or "未识别"
        asset_type_weights[asset_type] = asset_type_weights.get(asset_type, 0.0) + weight
        if industry != "未识别":
            industry_covered_weight += weight
        industry_weights[industry] = industry_weights.get(industry, 0.0) + weight
        for level_index, label in enumerate(industry_path):
            while len(industry_level_weights) <= level_index:
                industry_level_weights.append({})
                industry_level_covered_weights.append(0.0)
            level_weights = industry_level_weights[level_index]
            level_weights[label] = level_weights.get(label, 0.0) + weight
            industry_level_covered_weights[level_index] += weight
        for theme in themes:
            theme_weights[theme] = theme_weights.get(theme, 0.0) + weight
        if themes:
            theme_covered_weight += weight
        sector_history = market_histories.get(proxy["code"], []) if proxy else []
        metrics = series_metrics(
            histories.get(item["code"], []),
            common_benchmark,
            sector_history,
            quality_reference=quality_calendar,
        )
        if common_as_of_stale:
            metrics["state"] = "数据不足"
            metrics["state_reason"] = (
                f"组合共同截止日较沪深300落后{common_as_of_lag_days}个交易日，"
                "暂不判断当前强弱"
            )
        item["industry_group"] = industry
        item["industry_path"] = industry_path
        item["themes"] = themes
        item["sector_proxy"] = proxy
        item["history"] = metrics
        item["strength_state"] = metrics["state"]
        item["strength_reason"] = metrics["state_reason"]

    _period_contributions(items, histories, portfolio_history)
    history_quality_coverage: dict[str, float] = {}
    for window in WINDOWS:
        key = f"{window}d"
        valid_weight = 0.0
        for item in items:
            window_quality = (
                item.get("history", {})
                .get("data_quality", {})
                .get("windows", {})
                .get(key, {})
            )
            if window_quality.get("usable"):
                valid_weight += float(item.get("weight_pct") or 0)
            elif isinstance(item.get("period_contribution_pct"), dict):
                item["period_contribution_pct"][key] = None
        history_quality_coverage[key] = valid_weight
    portfolio_metrics = series_metrics(portfolio_history, common_benchmark) if portfolio_history else {
        "data_start": "",
        "data_end": "",
        "sample_count": 0,
        "returns": {f"{window}d": None for window in WINDOWS},
        "relative_market": {f"{window}d": None for window in WINDOWS},
        "drawdown": {"20d": None, "60d": None},
        "volatility": {"20d": None, "60d": None},
        "relative_sector": {f"{window}d": None for window in WINDOWS},
        "latest_close": None,
        "ma20": None,
        "ma60": None,
        "state": "数据不足",
        "state_reason": "组合共同历史数据不足",
        "data_quality": _alignment_quality([], []),
    }
    portfolio_metrics["data_quality"]["component_coverage_pct"] = {
        key: _round(value) for key, value in history_quality_coverage.items()
    }
    for window in WINDOWS:
        key = f"{window}d"
        if history_quality_coverage.get(key, 0.0) >= MIN_PORTFOLIO_HISTORY_COVERAGE_PCT:
            continue
        portfolio_metrics["returns"][key] = None
        portfolio_metrics["relative_market"][key] = None
        if window in (20, 60):
            portfolio_metrics["drawdown"][key] = None
            portfolio_metrics["volatility"][key] = None
            portfolio_metrics[f"ma{window}"] = None
    if history_quality_coverage.get("60d", 0.0) < MIN_PORTFOLIO_HISTORY_COVERAGE_PCT:
        portfolio_metrics["state"] = "数据不足"
        portfolio_metrics["state_reason"] = "近期60日有效行情未覆盖完整组合"
    if common_as_of_stale:
        portfolio_metrics["state"] = "数据不足"
        portfolio_metrics["state_reason"] = (
            f"组合共同截止日较沪深300落后{common_as_of_lag_days}个交易日，"
            "暂不判断当前强弱"
        )
    history_cost = sum(
        float(item.get("cost_value") or 0)
        for item in items
        if histories.get(item["code"])
    )
    overall_history_coverage = (
        history_cost / total_cost * 100
        if total_cost
        else subset_history_coverage
    )
    history_complete = (
        bool(coverage.get("complete"))
        and overall_history_coverage >= 99.5
        and subset_history_coverage >= 99.5
        and bool(portfolio_history)
    )
    subset_state = portfolio_metrics.get("state")
    subset_state_reason = portfolio_metrics.get("state_reason")
    if not history_complete:
        portfolio_metrics["state"] = "数据不足"
        portfolio_metrics["state_reason"] = (
            "仅覆盖可分析持仓子集，不能代表完整组合趋势"
            if portfolio_history
            else "组合共同历史数据不足"
        )
    portfolio_metrics.update(
        {
            "mode": "按当前持仓数量静态回看"
            if history_complete
            else "按当前可分析持仓数量静态回看",
            "scope": "完整持仓" if history_complete else "可分析持仓子集",
            "history_coverage_pct": _round(overall_history_coverage),
            "coverage_complete": history_complete,
            "analyzed_subset_state": subset_state,
            "analyzed_subset_state_reason": subset_state_reason,
        }
    )

    weights = sorted((float(item.get("weight_pct") or 0) for item in items), reverse=True)
    top1 = weights[0] if weights else 0.0
    top3 = sum(weights[:3])
    hhi = sum((weight / 100) ** 2 for weight in weights)
    effective = 1 / hhi if hhi else None
    total_weight = sum(weights)
    industry_leaf_exposure = _exposure_rows(industry_weights)
    themes = _exposure_rows(theme_weights)
    asset_types = _exposure_rows(asset_type_weights)
    top_theme = float(themes[0]["weight_pct"] or 0) if themes else 0.0
    complete = bool(coverage.get("complete"))
    if not complete:
        position_assessment = "数据不足"
    elif (
        top1 >= CONCENTRATION_THRESHOLDS["top1_high"]
        or top3 >= CONCENTRATION_THRESHOLDS["top3_high"]
    ):
        position_assessment = "高"
    elif (
        top1 >= CONCENTRATION_THRESHOLDS["top1_medium"]
        or top3 >= CONCENTRATION_THRESHOLDS["top3_medium"]
    ):
        position_assessment = "中"
    else:
        position_assessment = "低"

    def exposure_assessment(
        top_weight: float, covered_weight: float, high: float, medium: float
    ) -> str:
        if not complete:
            return "数据不足"
        if top_weight >= high:
            return "高"
        if top_weight >= medium:
            return "中"
        return "低" if covered_weight >= MIN_EXPOSURE_COVERAGE_PCT else "数据不足"

    industry_hierarchy_exposure = []
    hierarchy_depth = len(industry_level_weights)
    for level_index, level_weights in enumerate(industry_level_weights):
        covered_weight = industry_level_covered_weights[level_index]
        display_weights = dict(level_weights)
        unknown_weight = max(total_weight - covered_weight, 0.0)
        if unknown_weight > 0.005:
            display_weights["未识别"] = unknown_weight
        exposure = _exposure_rows(display_weights)
        known_exposure = [row for row in exposure if row["name"] != "未识别"]
        top_weight = (
            float(known_exposure[0]["weight_pct"] or 0)
            if known_exposure
            else 0.0
        )
        level_name = f"{level_index + 1}级行业" if hierarchy_depth > 1 else "行业"
        industry_hierarchy_exposure.append(
            {
                "level": level_index + 1,
                "level_name": level_name,
                "coverage_pct": _round(covered_weight),
                "assessment": exposure_assessment(
                    top_weight,
                    covered_weight,
                    CONCENTRATION_THRESHOLDS["industry_high"],
                    CONCENTRATION_THRESHOLDS["industry_medium"],
                ),
                "top_name": known_exposure[0]["name"] if known_exposure else "",
                "top_weight_pct": _round(top_weight),
                "exposure": exposure,
            }
        )

    level_assessments = [
        row["assessment"] for row in industry_hierarchy_exposure
    ]
    if not complete:
        industry_assessment = "数据不足"
    elif "高" in level_assessments:
        industry_assessment = "高"
    elif "中" in level_assessments:
        industry_assessment = "中"
    elif level_assessments and all(value == "低" for value in level_assessments):
        industry_assessment = "低"
    else:
        industry_assessment = "数据不足"

    matching_levels = [
        row
        for row in industry_hierarchy_exposure
        if row["assessment"] == industry_assessment
    ]
    if matching_levels:
        industry_driver = max(
            matching_levels,
            key=lambda row: (
                float(row["top_weight_pct"] or 0),
                int(row["level"]),
            ),
        )
    elif industry_hierarchy_exposure:
        industry_driver = industry_hierarchy_exposure[-1]
    else:
        industry_driver = {
            "level": None,
            "level_name": "行业",
            "coverage_pct": 0.0,
            "assessment": "数据不足",
            "top_name": "",
            "top_weight_pct": 0.0,
            "exposure": industry_leaf_exposure,
        }
    industries = industry_driver["exposure"]
    top_industry = float(industry_driver["top_weight_pct"] or 0)
    theme_assessment = exposure_assessment(
        top_theme,
        theme_covered_weight,
        CONCENTRATION_THRESHOLDS["theme_high"],
        CONCENTRATION_THRESHOLDS["theme_medium"],
    )
    exposure_coverage_sufficient = (
        industry_covered_weight >= MIN_EXPOSURE_COVERAGE_PCT
        and theme_covered_weight >= MIN_EXPOSURE_COVERAGE_PCT
    )
    component_assessments = (
        position_assessment,
        industry_assessment,
        theme_assessment,
    )
    if not complete:
        concentration_level = "数据不足"
    elif "高" in component_assessments:
        concentration_level = "高"
    elif "中" in component_assessments:
        concentration_level = "中"
    elif all(value == "低" for value in component_assessments):
        concentration_level = "低"
    else:
        concentration_level = "数据不足"

    benchmark_vol = None
    if common_benchmark:
        benchmark_vol = series_metrics(common_benchmark)["volatility"]["20d"]
    high_volatility_items = []
    valid_volatility_weight = sum(
        float(item.get("weight_pct") or 0)
        for item in items
        if item.get("history", {}).get("volatility", {}).get("20d") is not None
    )
    if benchmark_vol is not None and benchmark_vol > 0:
        for item in items:
            item_vol = item.get("history", {}).get("volatility", {}).get("20d")
            if item_vol is not None and item_vol >= benchmark_vol * 1.5:
                high_volatility_items.append(item)
    high_volatility_weight = sum(float(item.get("weight_pct") or 0) for item in high_volatility_items)
    if (
        not complete
        or benchmark_vol is None
        or benchmark_vol <= 0
        or common_as_of_stale
    ):
        high_volatility_assessment = "数据不足"
    elif high_volatility_weight >= 40:
        high_volatility_assessment = "高"
    elif valid_volatility_weight < MIN_VOLATILITY_COVERAGE_PCT:
        high_volatility_assessment = "数据不足"
    elif high_volatility_weight >= 25:
        high_volatility_assessment = "中"
    else:
        high_volatility_assessment = "低"
    state_weights: dict[str, float] = {}
    state_counts: dict[str, int] = {}
    for item in items:
        state = str(item.get("strength_state") or "数据不足")
        state_weights[state] = state_weights.get(state, 0.0) + float(item.get("weight_pct") or 0)
        state_counts[state] = state_counts.get(state, 0) + 1
    state_exposure = [
        {
            "state": state,
            "weight_pct": _round(state_weights.get(state, 0.0)),
            "holding_count": state_counts.get(state, 0),
        }
        for state in ("强势", "震荡", "转弱", "弱势", "数据不足")
        if state_counts.get(state, 0)
    ]

    for item in items:
        profit = _number(item.get("profit_amount"))
        item["profit_contribution_pct"] = (
            _round(profit / total_cost * 100) if profit is not None and total_cost else None
        )
    gains = sorted(
        (item for item in items if float(item.get("profit_amount") or 0) > 0),
        key=lambda item: float(item.get("profit_amount") or 0),
        reverse=True,
    )
    losses = sorted(
        (item for item in items if float(item.get("profit_amount") or 0) < 0),
        key=lambda item: float(item.get("profit_amount") or 0),
    )
    gross_loss_amount = sum(
        abs(float(item.get("profit_amount") or 0)) for item in losses
    )
    largest_loss_amount = (
        abs(float(losses[0].get("profit_amount") or 0)) if losses else 0.0
    )
    largest_loss_share = (
        largest_loss_amount / gross_loss_amount * 100 if gross_loss_amount else None
    )
    largest_loss_impact = (
        largest_loss_amount / total_cost * 100
        if total_cost and largest_loss_amount
        else None
    )
    if not complete or not total_cost:
        loss_concentration_assessment = "数据不足"
    elif (
        largest_loss_share is not None
        and largest_loss_impact is not None
        and largest_loss_share >= HIGH_LOSS_SOURCE_SHARE_PCT
        and largest_loss_impact >= HIGH_LOSS_SOURCE_PORTFOLIO_IMPACT_PCT
    ):
        loss_concentration_assessment = "高"
    elif (
        largest_loss_share is not None
        and largest_loss_impact is not None
        and largest_loss_share >= MIN_LOSS_SOURCE_SHARE_PCT
        and largest_loss_impact >= MIN_LOSS_SOURCE_PORTFOLIO_IMPACT_PCT
    ):
        loss_concentration_assessment = "中"
    else:
        loss_concentration_assessment = "低"
    source_fields = (
        "code",
        "name",
        "profit_amount",
        "profit_pct",
        "profit_contribution_pct",
        "weight_pct",
    )
    pnl_sources = {
        "scope": "完整持仓" if complete else "成功分析持仓子集",
        "top_gains": [{key: item.get(key) for key in source_fields} for item in gains[:3]],
        "top_losses": [{key: item.get(key) for key in source_fields} for item in losses[:3]],
        "loss_concentration": {
            "assessment": loss_concentration_assessment,
            "gross_loss_amount": _round(gross_loss_amount),
            "largest_source_code": losses[0]["code"] if losses else "",
            "largest_source_name": losses[0]["name"] if losses else "",
            "largest_source_share_pct": _round(largest_loss_share),
            "largest_source_portfolio_impact_pct": _round(largest_loss_impact),
            "rule": "最大亏损来源占累计亏损至少50%，且对组合成本影响至少1%",
        },
        "period_20d": [
            {
                "code": item["code"],
                "name": item["name"],
                "contribution_pct": item.get("period_contribution_pct", {}).get("20d"),
            }
            for item in sorted(
                items,
                key=lambda row: abs(float((row.get("period_contribution_pct") or {}).get("20d") or 0)),
                reverse=True,
            )[:5]
        ],
    }

    holding_observations = []
    holding_replay = []
    for item in items:
        observations = _replay_observations(
            histories.get(item["code"], []), common_benchmark
        )
        for row in observations:
            row["code"] = item["code"]
        holding_observations.extend(observations)
        holding_replay.append(
            {
                "code": item["code"],
                "name": item["name"],
                "current_weight_pct": item.get("weight_pct"),
                "states": _aggregate_replay(observations),
            }
        )
    portfolio_observations = _replay_observations(
        portfolio_history,
        common_benchmark,
        quality_histories=[
            histories[item["code"]]
            for item in items
            if histories.get(item["code"])
        ],
    )
    portfolio_state_rows = _aggregate_replay(portfolio_observations)
    replay_dates = [row["date"] for row in holding_observations + portfolio_observations]
    replay = {
        "mode": "历史诊断重放（非交易回测）",
        "sample_rule": "最近约250个交易日，每5个交易日取样；状态只使用当时及以前数据，行情缺口超限的观察日跳过",
        "sample_start": min(replay_dates) if replay_dates else "",
        "sample_end": max(replay_dates) if replay_dates else "",
        "holding_states": _aggregate_replay(holding_observations),
        "holding_state_scope": "各标的历史样本等权汇总，不代表组合权重",
        "holdings": holding_replay,
        "scope": "完整持仓" if history_complete else "可分析持仓子集",
        "portfolio_states": portfolio_state_rows if history_complete else [],
        "analyzed_subset_states": portfolio_state_rows if not history_complete else [],
        "coverage_complete": history_complete,
        "limitations": [
            "仅回看当前仍持有的标的，存在持仓选择偏差",
            "不包含真实历史仓位、买卖、现金、手续费、税费或分红",
            "5日与10日观察窗口可能重叠，不代表可交易策略收益",
            "单一状态少于8个观察样本时不输出胜率或中位收益",
        ],
    }

    concentration = {
        "assessment": concentration_level,
        "position_assessment": position_assessment,
        "industry_assessment": industry_assessment,
        "theme_assessment": theme_assessment,
        "top1_weight_pct": _round(top1),
        "top3_weight_pct": _round(top3),
        "hhi": _round(hhi, 4),
        "effective_holding_count": _round(effective),
        "industry_exposure": industries,
        "industry_coverage_pct": industry_driver["coverage_pct"],
        "industry_concentration_driver": {
            key: value
            for key, value in industry_driver.items()
            if key != "exposure"
        },
        "industry_hierarchy_exposure": industry_hierarchy_exposure,
        "industry_leaf_exposure": industry_leaf_exposure,
        "industry_leaf_coverage_pct": _round(industry_covered_weight),
        "asset_type_exposure": asset_types,
        "theme_exposure": themes,
        "theme_coverage_pct": _round(theme_covered_weight),
        "exposure_coverage_sufficient": exposure_coverage_sufficient,
        "theme_overlap_note": "主题标签可重叠，各主题权重合计可能超过100%",
        "high_volatility_weight_pct": _round(high_volatility_weight),
        "volatility_coverage_pct": _round(valid_volatility_weight),
        "benchmark_volatility_20d": _round(benchmark_vol),
        "high_volatility_assessment": high_volatility_assessment,
        "high_volatility_codes": [item["code"] for item in high_volatility_items],
        "high_volatility_rule": "20日年化波动达到沪深300的1.5倍",
        "threshold_note": "首版观察线：Top1 30%、Top3 70%、任一行业层级或单一主题40%",
        "coverage_complete": complete,
    }
    correlation = _correlation_summary(
        items,
        aligned_histories,
        complete and not common_as_of_stale,
        quality_histories=histories,
    )
    priority_candidates: list[dict[str, Any]] = []

    def add_priority(
        level: str,
        title: str,
        detail: str,
        importance: float,
        tie_breaker: int = 0,
    ) -> None:
        priority_candidates.append(
            {
                "level": level,
                "title": title,
                "detail": detail,
                "_severity": {"数据": 4, "高": 3, "中": 2}.get(level, 1),
                "_importance": importance,
                "_tie_breaker": tie_breaker,
            }
        )

    if not complete:
        add_priority(
            "数据",
            "组合覆盖不完整",
            f"当前只分析到{coverage.get('analyzed_count', len(items))}/{coverage.get('holding_count', len(items))}只持仓，暂不判断整体集中度。",
            100.0,
        )
    if not benchmark:
        add_priority(
            "数据",
            "沪深300历史不可用",
            "绝对趋势仍可计算；暂不能计算相对强弱、当前强弱分类或历史诊断重放。",
            95.0,
        )
    elif common_as_of_stale:
        add_priority(
            "数据",
            "组合日线明显滞后",
            (
                f"组合共同截止日为{common_as_of}，较沪深300落后"
                f"{common_as_of_lag_days}个交易日，暂不判断当前强弱。"
            ),
            95.0,
        )
    elif complete and history_quality_coverage.get("60d", 0.0) < MIN_PORTFOLIO_HISTORY_COVERAGE_PCT:
        add_priority(
            "数据",
            "近期行情质量不足",
            "部分持仓最近60日存在长缺口、较多前值填充或末端陈旧，暂不形成完整组合强弱结论。",
            90.0,
        )
    elif complete and (
        not history_complete or portfolio_metrics.get("analyzed_subset_state") == "数据不足"
    ):
        add_priority(
            "数据",
            "组合共同历史不足",
            "当前持仓缺少足够的共同交易日，暂不形成完整组合趋势结论。",
            90.0,
        )

    trend_state = str(portfolio_metrics.get("state") or "数据不足")
    drawdown_signals = [
        (key, float(value))
        for key, value in portfolio_metrics.get("drawdown", {}).items()
        if _number(value) is not None
        and float(value) >= SIGNIFICANT_DRAWDOWN_PCT.get(key, math.inf)
    ]
    relative_signals = [
        (key, float(value))
        for key, value in portfolio_metrics.get("relative_market", {}).items()
        if key in SIGNIFICANT_UNDERPERFORMANCE_PCT
        and _number(value) is not None
        and float(value) <= SIGNIFICANT_UNDERPERFORMANCE_PCT[key]
    ]
    drawdown_signal = (
        max(
            drawdown_signals,
            key=lambda row: row[1] / SIGNIFICANT_DRAWDOWN_PCT[row[0]],
        )
        if drawdown_signals
        else None
    )
    relative_signal = (
        max(
            relative_signals,
            key=lambda row: abs(row[1])
            / abs(SIGNIFICANT_UNDERPERFORMANCE_PCT[row[0]]),
        )
        if relative_signals
        else None
    )
    if history_complete and trend_state != "数据不足" and (
        trend_state in ("转弱", "弱势")
        or drawdown_signal is not None
        or relative_signal is not None
    ):
        trend_details = []
        if trend_state in ("转弱", "弱势"):
            trend_details.append(f"当前组合分类为{trend_state}")
        if drawdown_signal is not None:
            trend_details.append(
                f"近{drawdown_signal[0][:-1]}日最大回撤{_round(drawdown_signal[1])}%"
            )
        if relative_signal is not None:
            trend_details.append(
                f"近{relative_signal[0][:-1]}日跑输沪深300 {_round(abs(relative_signal[1]))}个百分点"
            )
        trend_is_high = (
            trend_state == "弱势"
            or any(
                value >= HIGH_DRAWDOWN_PCT[key]
                for key, value in drawdown_signals
            )
            or any(
                value <= HIGH_UNDERPERFORMANCE_PCT[key]
                for key, value in relative_signals
            )
        )
        if trend_state == "弱势":
            trend_title = "组合整体处于弱势"
        elif trend_state == "转弱":
            trend_title = "组合整体正在转弱"
        elif drawdown_signal is not None:
            trend_title = "组合近期回撤显著"
        else:
            trend_title = "组合相对大盘明显跑输"
        add_priority(
            "高" if trend_is_high else "中",
            trend_title,
            "；".join(trend_details) + "。",
            100.0,
            tie_breaker=50,
        )

    if concentration_level == "高":
        concentration_details = []
        concentration_importance = 0.0
        if position_assessment == "高":
            concentration_details.extend(
                [f"Top1 {_round(top1)}%", f"Top3 {_round(top3)}%"]
            )
            concentration_importance = max(concentration_importance, top1, top3)
        if industry_assessment == "高" and industry_driver.get("top_name"):
            concentration_details.append(
                f"{industry_driver['level_name']} {industry_driver['top_name']} {_round(top_industry)}%"
            )
            concentration_importance = max(concentration_importance, top_industry)
        if theme_assessment == "高" and themes:
            concentration_details.append(
                f"主题 {themes[0]['name']} {_round(top_theme)}%"
            )
            concentration_importance = max(concentration_importance, top_theme)
        add_priority(
            "高",
            "组合集中度偏高",
            "、".join(concentration_details) + "，触及首版观察线。",
            concentration_importance,
            tie_breaker=40,
        )
    weak_weight = state_weights.get("转弱", 0.0) + state_weights.get("弱势", 0.0)
    if complete and weak_weight >= 30:
        add_priority(
            "高" if weak_weight >= 50 else "中",
            "转弱与弱势仓位偏高",
            f"转弱和弱势持仓合计{_round(weak_weight)}%。",
            weak_weight,
            tie_breaker=30,
        )
    if high_volatility_assessment == "高":
        add_priority(
            "中",
            "高波动资产集中",
            f"相对沪深300的高波动持仓合计{_round(high_volatility_weight)}%。",
            high_volatility_weight,
            tie_breaker=10,
        )
    if correlation["risk_level"] == "高":
        add_priority(
            "高",
            "高相关仓位成组",
            f"最大高相关连通组权重{correlation['max_high_correlation_cluster_weight_pct']}%。",
            float(correlation["max_high_correlation_cluster_weight_pct"] or 0),
            tie_breaker=60,
        )
    if loss_concentration_assessment in ("高", "中") and losses:
        add_priority(
            loss_concentration_assessment,
            "累计亏损来源集中",
            (
                f"{losses[0]['name']}占累计亏损总额{_round(largest_loss_share)}%，"
                f"对组合成本影响-{_round(largest_loss_impact)}%。"
            ),
            float(largest_loss_impact or 0),
            tie_breaker=20,
        )
    priority_candidates.sort(
        key=lambda row: (
            -int(row["_severity"]),
            -float(row["_importance"]),
            -int(row["_tie_breaker"]),
            str(row["title"]),
        )
    )
    priority_flags = [
        {key: row[key] for key in ("level", "title", "detail")}
        for row in priority_candidates[:3]
    ]
    return {
        "analysis_period": {
            "window_days": list(WINDOWS),
            "history_start": portfolio_metrics.get("data_start") or "",
            "history_end": portfolio_metrics.get("data_end") or "",
            "daily_data_through": common_as_of,
            "market_data_through": benchmark[-1][0] if benchmark else "",
            "common_as_of_lag_trading_days": common_as_of_lag_days,
            "common_as_of_stale": common_as_of_stale,
            "mode": "按当前持仓数量静态回看",
        },
        "market_context": _market_context(market_history, market_histories),
        "portfolio_trend": portfolio_metrics,
        "pnl_sources": pnl_sources,
        "concentration": concentration,
        "state_exposure": state_exposure,
        "correlation": correlation,
        "priority_flags": priority_flags,
        "diagnostic_replay": replay,
    }
