from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from monitoring.db import iso_utc


QUOTE_METRICS = {"price", "change_pct", "change_amount"}
ANALYSIS_METRICS = {
    "price_percentile",
    "pe",
    "pe_percentile",
    "pb",
    "pb_percentile",
    "rsi",
    "macd_hist",
    "volume_ratio",
    "max_drawdown",
    "annual_volatility",
    "risk_score",
    "main_flow_today",
    "main_flow_5d",
}
ALLOWED_METRICS = QUOTE_METRICS | ANALYSIS_METRICS
ALLOWED_OPERATORS = {"gt", "gte", "lt", "lte", "crosses_above", "crosses_below"}
ALLOWED_DIRECTIONS = {"buy", "sell", "alert"}

METRIC_LABELS = {
    "price": "现价",
    "change_pct": "当日涨跌幅",
    "change_amount": "当日涨跌额",
    "price_percentile": "价格历史分位",
    "pe": "PE-TTM",
    "pe_percentile": "PE 历史分位",
    "pb": "PB",
    "pb_percentile": "PB 历史分位",
    "rsi": "RSI(14)",
    "macd_hist": "MACD 柱",
    "volume_ratio": "量比",
    "max_drawdown": "最大回撤",
    "annual_volatility": "年化波动率",
    "risk_score": "规则风险分",
    "main_flow_today": "当日主力净流入",
    "main_flow_5d": "近 5 日主力净流入",
}

OPERATOR_LABELS = {
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "crosses_above": "上穿",
    "crosses_below": "下穿",
}

DIRECTION_LABELS = {
    "buy": "买入关注条件",
    "sell": "卖出关注条件",
    "alert": "异动条件",
}


def validate_rule_spec(spec: dict[str, Any]) -> dict[str, Any]:
    metric = str(spec.get("metric", "")).strip()
    operator = str(spec.get("operator", "")).strip()
    direction = str(spec.get("direction", "")).strip()
    if metric not in ALLOWED_METRICS:
        raise ValueError("不支持的指标：%s" % metric)
    if operator not in ALLOWED_OPERATORS:
        raise ValueError("不支持的运算符：%s" % operator)
    if direction not in ALLOWED_DIRECTIONS:
        raise ValueError("不支持的方向：%s" % direction)
    try:
        threshold = float(spec["threshold"])
        confirm_count = int(spec.get("confirm_count", 1))
        cooldown_seconds = int(spec.get("cooldown_seconds", 3600))
        hysteresis = float(spec.get("hysteresis", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("规则阈值和控制参数必须是数字") from exc
    if not 1 <= confirm_count <= 20:
        raise ValueError("连续确认次数必须在 1 到 20 之间")
    if operator.startswith("crosses_") and confirm_count != 1:
        raise ValueError("上穿/下穿规则的连续确认次数必须为 1")
    if cooldown_seconds < 0:
        raise ValueError("冷却时间不能为负数")
    if hysteresis < 0:
        raise ValueError("回差不能为负数")
    return {
        **spec,
        "metric": metric,
        "operator": operator,
        "direction": direction,
        "threshold": threshold,
        "confirm_count": confirm_count,
        "cooldown_seconds": cooldown_seconds,
        "hysteresis": hysteresis,
    }


@dataclass
class RuleEvaluation:
    condition_met: bool
    triggered: bool
    state: dict[str, Any]


class RuleEngine:
    @staticmethod
    def _condition(
        operator: str, value: float, threshold: float, previous: float | None
    ) -> bool:
        if operator == "gt":
            return value > threshold
        if operator == "gte":
            return value >= threshold
        if operator == "lt":
            return value < threshold
        if operator == "lte":
            return value <= threshold
        if operator == "crosses_above":
            return previous is not None and previous <= threshold < value
        if operator == "crosses_below":
            return previous is not None and previous >= threshold > value
        return False

    @staticmethod
    def _can_rearm(rule: dict[str, Any], value: float) -> bool:
        threshold = float(rule["threshold"])
        hysteresis = float(rule.get("hysteresis", 0))
        if rule["operator"] in {"gt", "gte", "crosses_above"}:
            return value <= threshold - hysteresis
        return value >= threshold + hysteresis

    @staticmethod
    def _cooldown_elapsed(
        state: dict[str, Any], rule: dict[str, Any], now: datetime
    ) -> bool:
        raw = state.get("last_triggered_at")
        if not raw:
            return True
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now.astimezone(timezone.utc) - last.astimezone(timezone.utc)).total_seconds() >= int(
            rule.get("cooldown_seconds", 0)
        )

    def evaluate(
        self,
        rule: dict[str, Any],
        state: dict[str, Any],
        value: float,
        now: datetime,
    ) -> RuleEvaluation:
        normalized = validate_rule_spec(rule)
        current = dict(state)
        current.setdefault("rule_id", rule["id"])
        current.setdefault("consecutive_hits", 0)
        current.setdefault("armed", 1)
        previous = current.get("last_value")

        if not current["armed"]:
            if self._can_rearm(normalized, value):
                current["armed"] = 1
                current["consecutive_hits"] = 0
            current["last_value"] = value
            current["last_evaluated_at"] = iso_utc(now)
            return RuleEvaluation(False, False, current)

        met = self._condition(
            normalized["operator"], value, normalized["threshold"], previous
        )
        current["consecutive_hits"] = (
            current["consecutive_hits"] + 1 if met else 0
        )
        triggered = (
            met
            and current["consecutive_hits"] >= normalized["confirm_count"]
            and self._cooldown_elapsed(current, normalized, now)
        )
        if triggered:
            current["armed"] = 0
            current["consecutive_hits"] = 0
            current["last_triggered_at"] = iso_utc(now)
        current["last_value"] = value
        current["last_evaluated_at"] = iso_utc(now)
        return RuleEvaluation(met, triggered, current)


def format_trigger_message(
    rule: dict[str, Any], name: str, value: float
) -> str:
    return (
        "【%s】%s（%s）\n"
        "规则：%s\n"
        "条件：%s %s %s\n"
        "当前值：%s\n"
        "这是用户自定义规则的触发记录，仅供研究，不构成投资建议。"
        % (
            DIRECTION_LABELS[rule["direction"]],
            name or rule["code"],
            rule["code"],
            rule["name"],
            METRIC_LABELS[rule["metric"]],
            OPERATOR_LABELS[rule["operator"]],
            rule["threshold"],
            round(value, 4),
        )
    )
