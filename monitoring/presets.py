from __future__ import annotations

import math
from typing import Any

from monitoring.rules import validate_rule_spec


WATCH_RULE_NAME = "[三线] 关注价"
RISK_RULE_NAME = "[三线] 风险价"
TARGET_RULE_NAME = "[三线] 目标价"
MOVE_UP_RULE_NAME = "[自动异动] 当日上涨"
MOVE_DOWN_RULE_NAME = "[自动异动] 当日下跌"

MANAGED_RULE_NAMES = {
    WATCH_RULE_NAME,
    RISK_RULE_NAME,
    TARGET_RULE_NAME,
    MOVE_UP_RULE_NAME,
    MOVE_DOWN_RULE_NAME,
}

DEFAULT_CONFIRM_COUNT = 2
DEFAULT_COOLDOWN_SECONDS = 60 * 60
DEFAULT_MOVE_PERCENT = 3.0


def _positive_number(label: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("%s必须是大于 0 的有限数字" % label)
    return number


def _price_rule(
    name: str,
    direction: str,
    operator: str,
    threshold: float,
) -> dict[str, Any]:
    return validate_rule_spec(
        {
            "name": name,
            "direction": direction,
            "metric": "price",
            "operator": operator,
            "threshold": threshold,
            "confirm_count": DEFAULT_CONFIRM_COUNT,
            "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
            "hysteresis": max(round(threshold * 0.003, 4), 0.01),
        }
    )


def build_simple_rules(
    watch_price: float,
    risk_price: float,
    target_price: float | None = None,
    move_percent: float = DEFAULT_MOVE_PERCENT,
) -> list[dict[str, Any]]:
    watch = _positive_number("关注价", watch_price)
    risk = _positive_number("风险价", risk_price)
    target = (
        _positive_number("目标价", target_price)
        if target_price is not None
        else None
    )
    move = _positive_number("异动幅度", move_percent)

    if risk >= watch:
        raise ValueError("风险价必须低于关注价")
    if target is not None and target <= watch:
        raise ValueError("目标价必须高于关注价")
    if move > 20:
        raise ValueError("异动幅度不能高于 20%")

    rules = [
        _price_rule(WATCH_RULE_NAME, "buy", "lte", watch),
        _price_rule(RISK_RULE_NAME, "sell", "lte", risk),
    ]
    if target is not None:
        rules.append(_price_rule(TARGET_RULE_NAME, "sell", "gte", target))

    for name, operator, threshold in (
        (MOVE_UP_RULE_NAME, "gte", move),
        (MOVE_DOWN_RULE_NAME, "lte", -move),
    ):
        rules.append(
            validate_rule_spec(
                {
                    "name": name,
                    "direction": "alert",
                    "metric": "change_pct",
                    "operator": operator,
                    "threshold": threshold,
                    "confirm_count": DEFAULT_CONFIRM_COUNT,
                    "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
                    "hysteresis": 0.5,
                }
            )
        )
    return rules


def simple_rule_summary(rules: list[dict[str, Any]]) -> dict[str, Any]:
    by_name = {rule["name"]: rule for rule in rules if rule["name"] in MANAGED_RULE_NAMES}

    def enabled_threshold(name: str) -> float | None:
        rule = by_name.get(name)
        if not rule or not rule.get("enabled", True):
            return None
        return float(rule["threshold"])

    move_up = enabled_threshold(MOVE_UP_RULE_NAME)
    move_down = enabled_threshold(MOVE_DOWN_RULE_NAME)
    return {
        "watch_price": enabled_threshold(WATCH_RULE_NAME),
        "risk_price": enabled_threshold(RISK_RULE_NAME),
        "target_price": enabled_threshold(TARGET_RULE_NAME),
        "move_percent": (
            move_up
            if move_up is not None
            and move_down is not None
            and move_up == abs(move_down)
            else None
        ),
        "configured": WATCH_RULE_NAME in by_name and RISK_RULE_NAME in by_name,
    }


def save_simple_setup(
    repository: Any,
    code: str,
    name: str,
    watch_price: float,
    risk_price: float,
    target_price: float | None = None,
    move_percent: float = DEFAULT_MOVE_PERCENT,
) -> dict[str, Any]:
    rules = build_simple_rules(
        watch_price,
        risk_price,
        target_price,
        move_percent,
    )
    existing = next(
        (item for item in repository.list_watch() if item["code"] == code),
        None,
    )
    saved_name = str(name or (existing["name"] if existing else "")).strip()[:80]
    repository.upsert_watch(
        code,
        saved_name,
        existing["quantity"] if existing else None,
        existing["cost_price"] if existing else None,
        existing["notes"] if existing else "",
        bool(existing["enabled"]) if existing else True,
    )
    repository.replace_managed_rules(code, MANAGED_RULE_NAMES, rules)
    return {
        "code": code,
        "name": saved_name,
        **simple_rule_summary(rules),
    }
