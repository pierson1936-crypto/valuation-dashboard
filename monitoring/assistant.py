from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Callable

import app

from monitoring.config import MonitorConfig
from monitoring.db import MonitorRepository
from monitoring.presets import DEFAULT_CONFIRM_COUNT, DEFAULT_COOLDOWN_SECONDS
from monitoring.rules import (
    ALLOWED_METRICS,
    ALLOWED_OPERATORS,
    QUOTE_METRICS,
    validate_rule_spec,
)


RULE_DRAFT_VERSION = "strict-v4-staged"
DEFAULT_HYSTERESIS = 0
HISTORICAL_LOW_ACTION = "derive_historical_low_risk"
HISTORICAL_LOW_QUESTION = (
    "持有逻辑已保存。是否根据历史行情自动识别前低，"
    "并生成一条待确认的风险规则？"
)

SYSTEM_PROMPT_TEMPLATE = """你是量化监控规则整理助手。

你的唯一职责，是把用户已经明确表达的买卖逻辑、关注条件和风险条件，
整理成可由程序验证的监控规则草案。

你不是研究助手、行情预测助手或交易决策助手。
不得替用户判断收益，不得承诺涨跌，不得要求用户立即买入或卖出。
direction中的buy、sell、alert仅表示规则意图分类，不代表实际交易指令。

只允许使用输入中明确提供的数据、价格和指标。
不得编造价格、指标值、估值、支撑位、目标位、时间或其他数字。
不得使用输入中未提供的新闻、财务数据或市场信息。
没有足够信息时，应指出缺失内容，不得自行补全。

输出必须是严格合法的JSON对象，不得输出Markdown、代码块、解释文字或JSON之外的内容。

顶层字段：

summary: 字符串；
assumptions: 字符串数组；
risks: 字符串数组；
needs_confirmation: 字符串数组；
rules: 数组。

rules中的每一项必须包含：

name: 字符串；
direction: buy、sell或alert；
metric: 必须从允许的metric列表选择；
operator: 必须从允许的operator列表选择；
threshold: 数字；
confirm_count: 正整数；
cooldown_seconds: 非负整数；
hysteresis: 非负数字；
reason: 字符串。

允许的metric：
{{ALLOWED_METRICS}}

允许的operator：
{{ALLOWED_OPERATORS}}

系统默认运行参数：
confirm_count = {{DEFAULT_CONFIRM_COUNT}}
cooldown_seconds = {{DEFAULT_COOLDOWN_SECONDS}}
hysteresis = {{DEFAULT_HYSTERESIS}}

用户没有明确指定运行参数时，只能使用上述系统默认值。

规则要求：

1. 根据用户明确提供的信息生成0至3条核心规则。
2. 不得为了凑齐3条规则而编造阈值。
3. 优先把已有逻辑整理为关注价、风险价、目标价规则，但只有用户明确提供相应价格时才可创建。
4. 用户只提供一个有效条件时，只生成一条规则。
5. 估值、RSI、MACD、资金流等指标只有在输入数据明确提供，并且用户明确要求高级模式时，才可以新增为规则。
6. 输入中提供的高级指标可以用于解释，但不得推断未提供的指标值。
7. 无法生成可验证规则时，rules输出空数组，并在needs_confirmation中说明缺少什么信息。
8. 所有待用户确认的价格、阈值或含糊条件都必须写入needs_confirmation。
9. reason只解释规则与用户原始逻辑的关系，不得给出确定性交易建议。
10. 避免生成互相冲突、重复或无法由当前系统执行的规则。
11. 行业利润、产品价格、行业景气、财报利润和现金流等不能由分钟行情可靠验证的
条件，只作为人工或定期复核事项，不得追问其数值，也不得生成分钟规则。
12. 每轮最多保留一个最关键的待确认问题，不得一次要求用户补齐一组参数。"""


def _render_system_prompt(
    allowed_metrics: set[str], advanced_mode: bool
) -> str:
    prompt = (
        SYSTEM_PROMPT_TEMPLATE.replace(
            "{{ALLOWED_METRICS}}", "、".join(sorted(allowed_metrics))
        )
        .replace("{{ALLOWED_OPERATORS}}", "、".join(sorted(ALLOWED_OPERATORS)))
        .replace("{{DEFAULT_CONFIRM_COUNT}}", str(DEFAULT_CONFIRM_COUNT))
        .replace(
            "{{DEFAULT_COOLDOWN_SECONDS}}", str(DEFAULT_COOLDOWN_SECONDS)
        )
        .replace("{{DEFAULT_HYSTERESIS}}", str(DEFAULT_HYSTERESIS))
    )
    if advanced_mode:
        return prompt
    return (
        prompt.replace("、估值、支撑位", "、支撑位")
        .replace(
            "5. 估值、RSI、MACD、资金流等指标只有在输入数据明确提供，并且用户明确要求高级模式时，才可以新增为规则。\n"
            "6. 输入中提供的高级指标可以用于解释，但不得推断未提供的指标值。",
            "5. 基础模式只允许使用明确提供的价格、涨跌幅和涨跌额生成规则。\n"
            "6. 未提供的行情字段不得用于解释或生成规则。",
        )
    )


SYSTEM_PROMPT = _render_system_prompt(ALLOWED_METRICS, True)
BASIC_SYSTEM_PROMPT = _render_system_prompt(QUOTE_METRICS, False)

TOP_LEVEL_FIELDS = {
    "summary",
    "assumptions",
    "risks",
    "needs_confirmation",
    "rules",
}
RULE_FIELDS = {
    "name",
    "direction",
    "metric",
    "operator",
    "threshold",
    "confirm_count",
    "cooldown_seconds",
    "hysteresis",
    "reason",
}

_PERIODIC_REVIEW_KEYWORDS = (
    "财报",
    "财务",
    "现金流",
    "营收",
    "净利润",
    "毛利",
    "行业利润",
    "行业盈利",
    "产品价格",
    "成分股盈利",
)
_MANUAL_REVIEW_KEYWORDS = (
    "行业景气",
    "景气度",
    "产业景气",
    "放量",
    "缩量",
    "成交量",
    "长期无法收回",
    "多日无法收回",
    "多个交易日",
    "竞争格局",
    "管理层",
    "公司治理",
    "政策变化",
)


def _extract_json(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text.strip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("模型没有返回严格 JSON 对象") from exc
    if not isinstance(payload, dict):
        raise ValueError("模型没有返回 JSON 对象")
    return payload


def _logic_items(text: str) -> list[str]:
    items = []
    for raw in re.split(r"[\n；;。]+", str(text or "")):
        item = re.sub(r"^[\s\-*•、，,]+", "", raw).strip()
        if item and item not in items:
            items.append(item[:500])
    return items


def classify_review_items(text: str) -> tuple[list[str], list[str]]:
    """Separate non-minute conditions without inventing thresholds."""
    manual: list[str] = []
    periodic: list[str] = []
    for item in _logic_items(text):
        if any(keyword in item for keyword in _PERIODIC_REVIEW_KEYWORDS):
            periodic.append(item)
        elif any(keyword in item for keyword in _MANUAL_REVIEW_KEYWORDS):
            manual.append(item)
    return manual, periodic


def _merge_unique(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for item in group:
            cleaned = str(item or "").strip()[:500]
            if cleaned and cleaned not in merged:
                merged.append(cleaned)
    return merged


def _as_positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _history_candidate_from_context(
    context: dict[str, Any],
) -> dict[str, Any] | None:
    for key in ("historical_low", "previous_low", "recent_low"):
        raw = context.get(key)
        if isinstance(raw, dict):
            price = _as_positive_number(
                raw.get("price", raw.get("low", raw.get("value")))
            )
            if price is not None:
                return {
                    "price": price,
                    "date": str(raw.get("date") or ""),
                    "method": str(raw.get("method") or key),
                }
        else:
            price = _as_positive_number(raw)
            if price is not None:
                return {"price": price, "date": "", "method": key}

    rows = next(
        (
            context.get(key)
            for key in ("rows", "kline", "klines", "history")
            if isinstance(context.get(key), list)
        ),
        [],
    )
    normalized = []
    for raw in rows[-120:]:
        if not isinstance(raw, dict):
            continue
        low = _as_positive_number(raw.get("low"))
        if low is not None:
            normalized.append(
                {"price": low, "date": str(raw.get("date") or "")}
            )
    if len(normalized) < 3:
        return None

    current = _as_positive_number(
        context.get("current_price", context.get("price"))
    )
    local_lows = [
        normalized[index]
        for index in range(1, len(normalized) - 1)
        if normalized[index]["price"] <= normalized[index - 1]["price"]
        and normalized[index]["price"] <= normalized[index + 1]["price"]
        and (
            current is None or normalized[index]["price"] < current
        )
    ]
    candidate = (
        local_lows[-1]
        if local_lows
        else min(normalized[:-1], key=lambda item: item["price"])
    )
    if current is not None and candidate["price"] >= current:
        return None
    return {**candidate, "method": "最近日线局部低点"}


def _default_historical_price_context(code: str) -> dict[str, Any]:
    meta = app._guess(code)
    rows, name, live = app.fetch_kline(meta["prefix"], code)
    if len(rows) < 3:
        return {"error": "历史行情不足，暂时无法识别前低"}
    current_price = _as_positive_number(
        (live or {}).get("price") if isinstance(live, dict) else None
    )
    if current_price is None:
        current_price = _as_positive_number(rows[-1].get("close"))
    context = {
        "code": code,
        "name": name or meta.get("name", ""),
        "current_price": current_price,
        "rows": rows,
    }
    candidate = _history_candidate_from_context(context)
    if candidate is None:
        return {"error": "历史行情中没有识别出低于现价的前低"}
    return {
        "code": code,
        "name": context["name"],
        "current_price": current_price,
        "historical_low": candidate,
    }


class RuleDraftAssistant:
    def __init__(
        self,
        config: MonitorConfig,
        repository: MonitorRepository,
        analyzer: Callable[[str], dict[str, Any]] | None = None,
        llm_call: Callable[[str, str], str] | None = None,
    ):
        self.config = config
        self.repository = repository
        self._custom_analyzer = analyzer is not None
        self.analyzer = analyzer or app._agent_summarize
        self._custom_llm = llm_call is not None
        self.llm_call = llm_call or self._call_llm

    def _call_llm(
        self, system: str, user: str, api_key: str = ""
    ) -> str:
        key = str(api_key or self.config.deepseek_api_key).strip()
        if not key:
            raise ValueError("未设置 DEEPSEEK_API_KEY")
        body = {
            "model": app.AGENT_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "max_tokens": 900,
        }
        headers = {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        }
        response = app.api_post(
            app.AGENT_BASE + "/chat/completions",
            headers,
            body,
            timeout=120,
            retries=3,
        )
        if "choices" not in response:
            raise ValueError(response.get("error", {}).get("message") or "模型返回异常")
        return response["choices"][0]["message"]["content"]

    @staticmethod
    def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
        missing_top = sorted(TOP_LEVEL_FIELDS - set(payload))
        if missing_top:
            raise ValueError("规则草案缺少顶层字段：%s" % ",".join(missing_top))
        if not isinstance(payload["summary"], str):
            raise ValueError("summary 必须是字符串")
        for field in ("assumptions", "risks", "needs_confirmation"):
            if not isinstance(payload[field], list) or any(
                not isinstance(item, str) for item in payload[field]
            ):
                raise ValueError("%s 必须是字符串数组" % field)

        rules = payload["rules"]
        if not isinstance(rules, list) or len(rules) > 3:
            raise ValueError("规则草案数量必须在 0 到 3 之间")
        normalized_rules = []
        for index, raw in enumerate(rules, 1):
            if not isinstance(raw, dict):
                raise ValueError("第 %d 条规则格式错误" % index)
            missing_rule = sorted(RULE_FIELDS - set(raw))
            if missing_rule:
                raise ValueError(
                    "第 %d 条规则缺少字段：%s"
                    % (index, ",".join(missing_rule))
                )
            if not isinstance(raw["name"], str) or not isinstance(
                raw["reason"], str
            ):
                raise ValueError(
                    "第 %d 条规则的 name 和 reason 必须是字符串" % index
                )
            rule = validate_rule_spec(raw)
            normalized_rules.append(
                {
                    "name": rule["name"][:80],
                    "direction": rule["direction"],
                    "metric": rule["metric"],
                    "operator": rule["operator"],
                    "threshold": rule["threshold"],
                    "confirm_count": rule["confirm_count"],
                    "cooldown_seconds": rule["cooldown_seconds"],
                    "hysteresis": rule["hysteresis"],
                    "reason": rule["reason"][:500],
                }
            )
        return {
            "summary": payload["summary"][:1000],
            "assumptions": [
                item[:500] for item in payload["assumptions"][:10]
            ],
            "risks": [item[:500] for item in payload["risks"][:10]],
            "needs_confirmation": [
                item[:500] for item in payload["needs_confirmation"][:10]
            ],
            "rules": normalized_rules,
        }

    def suggest(
        self,
        code: str,
        logic_text: str,
        force: bool = False,
        api_key: str = "",
        advanced_mode: bool = False,
        confirmation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        code = str(code).strip()
        logic = str(logic_text).strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("请输入 6 位代码")
        if not logic:
            raise ValueError("请提供你的买入、卖出或异动逻辑")
        if len(logic) > 2000:
            raise ValueError("逻辑描述不能超过 2000 字")
        confirmed_action = ""
        if isinstance(confirmation, dict) and confirmation.get("approved") is True:
            confirmed_action = str(confirmation.get("action") or "").strip()
        if confirmed_action and confirmed_action != HISTORICAL_LOW_ACTION:
            raise ValueError("不支持的确认动作：%s" % confirmed_action)
        cache_key = hashlib.sha256(
            (
                "%s\n%s\n%s\n%s\n%s\n%s"
                % (
                    RULE_DRAFT_VERSION,
                    app.AGENT_MODEL,
                    code,
                    logic,
                    int(bool(advanced_mode)),
                    confirmed_action,
                )
            ).encode("utf-8")
        ).hexdigest()
        if not force:
            cached = self.repository.get_cached_draft(cache_key)
            if cached:
                return {**cached, "cached": True}
        if (
            confirmed_action != HISTORICAL_LOW_ACTION
            and not self._custom_llm
            and not str(api_key or self.config.deepseek_api_key).strip()
        ):
            raise ValueError("未设置 DEEPSEEK_API_KEY")

        manual_items, periodic_items = classify_review_items(logic)
        if confirmed_action == HISTORICAL_LOW_ACTION:
            analysis = (
                self.analyzer(code)
                if self._custom_analyzer
                else _default_historical_price_context(code)
            )
            candidate = (
                None
                if analysis.get("error")
                else _history_candidate_from_context(analysis)
            )
            current_price = _as_positive_number(
                analysis.get("current_price", analysis.get("price"))
            )
            if (
                candidate is not None
                and current_price is not None
                and float(candidate["price"]) >= current_price
            ):
                candidate = None
                analysis = {
                    **analysis,
                    "error": "识别到的历史低点不低于现价，不能作为风险价草案",
                }
            if candidate is None:
                message = str(
                    analysis.get("error")
                    or "历史行情中没有识别出可验证的前低"
                )
                payload = {
                    "summary": "持仓逻辑已保留，但没有生成自动规则。",
                    "assumptions": [],
                    "risks": [message],
                    "needs_confirmation": [
                        "历史数据不足，请手工填写风险价。"
                    ],
                    "rules": [],
                    "code": code,
                    "model": "deterministic-history",
                    "manual_review_items": manual_items,
                    "periodic_review_items": periodic_items,
                    "auto_rule_count": 0,
                    "next_question": None,
                    "advanced_mode": bool(advanced_mode),
                    "token_usage": 0,
                }
            else:
                price = float(candidate["price"])
                date_text = str(candidate.get("date") or "").strip()
                date_reason = (
                    "%s 的" % date_text if date_text else "历史行情中的"
                )
                rule = validate_rule_spec(
                    {
                        "name": "历史前低风险线（待确认）",
                        "direction": "sell",
                        "metric": "price",
                        "operator": "lte",
                        "threshold": price,
                        "confirm_count": DEFAULT_CONFIRM_COUNT,
                        "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
                        "hysteresis": DEFAULT_HYSTERESIS,
                        "reason": (
                            "根据用户授权识别%s日线前低；"
                            "该价格仅为待确认草案，不会自动启用。"
                            % date_reason
                        ),
                    }
                )
                rule["draft_status"] = "pending_confirmation"
                payload = {
                    "summary": "已生成一条历史前低风险规则草案，等待确认。",
                    "assumptions": [
                        "前低识别仅使用已有历史日线，不代表未来支撑有效。"
                    ],
                    "risks": [
                        "历史前低可能失效，规则启用前仍需人工核对。"
                    ],
                    "needs_confirmation": [
                        "请确认是否采用%s前低 %.4g 作为风险价。"
                        % (date_reason, price)
                    ],
                    "rules": [rule],
                    "code": code,
                    "model": "deterministic-history",
                    "manual_review_items": manual_items,
                    "periodic_review_items": periodic_items,
                    "auto_rule_count": 1,
                    "next_question": None,
                    "advanced_mode": bool(advanced_mode),
                    "token_usage": 0,
                }
            self.repository.save_draft(
                cache_key,
                code,
                logic,
                payload,
                self.config.ai_draft_retention_days,
            )
            return {**payload, "cached": False}

        allowed_metrics = ALLOWED_METRICS if advanced_mode else QUOTE_METRICS
        user_lines = [
            "标的代码：%s" % code,
            "用户原始逻辑：%s" % logic,
            "运行模式：%s" % ("高级模式" if advanced_mode else "基础模式"),
        ]
        if advanced_mode:
            analysis = self.analyzer(code)
            if analysis.get("error"):
                raise ValueError(analysis["error"])
            user_lines.append(
                "当前压缩数据：%s"
                % json.dumps(
                    analysis,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        else:
            user_lines.append(
                "当前没有提供自动分析数据，只能使用用户原文中的明确数字。"
            )
        user_lines.extend(
            [
                "允许指标：%s" % ",".join(sorted(allowed_metrics)),
                "允许运算符：%s" % ",".join(sorted(ALLOWED_OPERATORS)),
                "请生成待用户确认的规则草案。",
            ]
        )
        user = "\n".join(user_lines)
        system_prompt = SYSTEM_PROMPT if advanced_mode else BASIC_SYSTEM_PROMPT
        raw = (
            self.llm_call(system_prompt, user)
            if self._custom_llm
            else self.llm_call(system_prompt, user, api_key)
        )
        payload = self._normalize_payload(_extract_json(raw))
        remaining_confirmations = []
        for item in payload["needs_confirmation"]:
            extra_manual, extra_periodic = classify_review_items(item)
            manual_items = _merge_unique(manual_items, extra_manual)
            periodic_items = _merge_unique(
                periodic_items, extra_periodic
            )
            if not extra_manual and not extra_periodic:
                remaining_confirmations.append(item)
        payload["needs_confirmation"] = remaining_confirmations[:1]
        for rule in payload["rules"]:
            rule["draft_status"] = "pending_confirmation"

        next_question = None
        if not payload["rules"]:
            next_question = {
                "id": "historical_low_risk",
                "text": HISTORICAL_LOW_QUESTION,
                "confirmation": {
                    "action": HISTORICAL_LOW_ACTION,
                    "approved": True,
                },
            }
            payload["needs_confirmation"] = [HISTORICAL_LOW_QUESTION]
        payload.update(
            {
                "code": code,
                "model": app.AGENT_MODEL,
                "manual_review_items": manual_items,
                "periodic_review_items": periodic_items,
                "auto_rule_count": len(payload["rules"]),
                "next_question": next_question,
                "advanced_mode": bool(advanced_mode),
            }
        )
        self.repository.save_draft(
            cache_key,
            code,
            logic,
            payload,
            self.config.ai_draft_retention_days,
        )
        return {**payload, "cached": False}
