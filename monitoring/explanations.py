from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import app

from monitoring.config import MonitorConfig
from monitoring.db import MonitorRepository, utc_now


EXPLANATION_VERSION = "event-review-v1"
USAGE_FEATURE = "event_explanation"
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")

SYSTEM_PROMPT = """你是盯盘事件复核助手。

你的职责是根据用户已经保存的买入逻辑、失效条件、复核事项，以及程序记录的单次
规则触发事件，帮助用户看清这次提醒与原逻辑的关系。

你不是行情预测助手或交易决策助手。不得预测涨跌、收益或目标价，不得要求用户立即
买入或卖出，不得自动修改任何监控规则。

只允许使用输入 JSON 中明确提供的内容。不得补充新闻、公告、财务数据、市场数据、
支撑位或其他数字。缺少信息时必须明确写入 limitations。

输出必须是严格合法的 JSON 对象，不得输出 Markdown、代码块或额外文字。

字段必须完整：
summary: 字符串，简要说明发生了什么；
relation: supports、contradicts 或 neutral；
logic_matches: 字符串数组，事件与原逻辑相符的部分；
invalidation_checks: 字符串数组，需要对照失效条件检查的部分；
review_questions: 字符串数组，用户下一步应核实的问题；
limitations: 字符串数组，当前输入缺少的信息。

所有表述都必须是复核提示，不得构成确定性交易建议。"""

TOP_LEVEL_FIELDS = {
    "summary",
    "relation",
    "logic_matches",
    "invalidation_checks",
    "review_questions",
    "limitations",
}
RELATIONS = {"supports", "contradicts", "neutral"}


def _strict_json(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text.strip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("模型没有返回严格 JSON 对象") from exc
    if not isinstance(payload, dict):
        raise ValueError("模型没有返回 JSON 对象")
    return payload


class EventExplanationAssistant:
    def __init__(
        self,
        config: MonitorConfig,
        repository: MonitorRepository,
        llm_call: Callable[[str, str, str], Any] | None = None,
    ):
        self.config = config
        self.repository = repository
        self._custom_llm = llm_call is not None
        self.llm_call = llm_call or self._call_llm

    @staticmethod
    def _estimated_tokens(system: str, user: str) -> int:
        return max(1, len(system) + len(user) + 700)

    @staticmethod
    def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
        missing = sorted(TOP_LEVEL_FIELDS - set(payload))
        if missing:
            raise ValueError("事件解释缺少字段：%s" % ",".join(missing))
        if not isinstance(payload["summary"], str):
            raise ValueError("summary 必须是字符串")
        if payload["relation"] not in RELATIONS:
            raise ValueError("relation 必须是 supports、contradicts 或 neutral")
        result = {
            "summary": payload["summary"][:1000],
            "relation": payload["relation"],
        }
        for field in (
            "logic_matches",
            "invalidation_checks",
            "review_questions",
            "limitations",
        ):
            value = payload[field]
            if not isinstance(value, list) or any(
                not isinstance(item, str) for item in value
            ):
                raise ValueError("%s 必须是字符串数组" % field)
            result[field] = [item[:500] for item in value[:8]]
        return result

    def _call_llm(
        self, system: str, user: str, api_key: str, deepseek_model: str = ""
    ) -> dict[str, Any]:
        model_name = app.resolve_deepseek_model(deepseek_model)
        body = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "max_tokens": 700,
        }
        headers = {
            "Authorization": "Bearer " + api_key,
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
        usage = response.get("usage") or {}
        return {
            "content": response["choices"][0]["message"]["content"],
            "token_usage": int(usage.get("total_tokens") or 0),
        }

    @staticmethod
    def _unpack_response(response: Any, estimated_tokens: int) -> tuple[str, int]:
        if isinstance(response, str):
            return response, estimated_tokens
        if isinstance(response, tuple) and len(response) == 2:
            return str(response[0]), max(0, int(response[1]))
        if isinstance(response, dict) and "content" in response:
            return (
                str(response["content"]),
                max(0, int(response.get("token_usage") or estimated_tokens)),
            )
        raise ValueError("模型调用返回格式不受支持")

    def explain(
        self,
        event_id: int,
        api_key: str,
        force: bool = False,
        now: datetime | None = None,
        deepseek_model: str = "",
    ) -> dict[str, Any]:
        try:
            normalized_event_id = int(event_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("事件编号无效") from exc
        key = str(api_key or "").strip()
        if not key:
            raise ValueError("缺少 DeepSeek Key")
        model_name = app.resolve_deepseek_model(deepseek_model)
        event = self.repository.get_event(normalized_event_id)
        if not event:
            raise ValueError("未找到该提醒事件")
        logic = self.repository.get_watch_logic(event["code"]) or {
            "thesis": "",
            "invalidation": "",
            "review_items": "",
            "updated_at": "",
        }
        current = now or utc_now()
        cache_source = json.dumps(
            {
                "version": EXPLANATION_VERSION,
                "model": model_name,
                "event": event,
                "logic": logic,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        cache_key = hashlib.sha256(cache_source.encode("utf-8")).hexdigest()
        if not force:
            cached = self.repository.get_cached_event_explanation(cache_key, current)
            if cached:
                usage_date = current.astimezone(SHANGHAI).date().isoformat()
                daily = self.repository.get_ai_usage(usage_date, USAGE_FEATURE)
                return {
                    **cached["payload"],
                    "event_id": normalized_event_id,
                    "model": cached["model"],
                    "token_usage": 0,
                    "cached": True,
                    "daily_usage": daily,
                }

        user = json.dumps(
            {
                "event": {
                    "id": event["id"],
                    "code": event["code"],
                    "occurred_at": event["occurred_at"],
                    "direction": event["direction"],
                    "metric": event["metric"],
                    "observed_value": event["observed_value"],
                    "threshold": event["threshold"],
                    "message": event["message"],
                    "payload": event["payload"],
                },
                "logic_card": {
                    "thesis": logic["thesis"],
                    "invalidation": logic["invalidation"],
                    "review_items": logic["review_items"],
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        estimated = self._estimated_tokens(SYSTEM_PROMPT, user)
        usage_date = current.astimezone(SHANGHAI).date().isoformat()
        self.repository.reserve_ai_usage(
            usage_date,
            USAGE_FEATURE,
            estimated,
            self.config.event_explanation_daily_call_limit,
            self.config.event_explanation_daily_token_limit,
            current,
        )
        try:
            raw, actual_tokens = self._unpack_response(
                self.llm_call(SYSTEM_PROMPT, user, key)
                if self._custom_llm
                else self.llm_call(SYSTEM_PROMPT, user, key, model_name),
                estimated,
            )
            payload = self._normalize(_strict_json(raw))
        except Exception:
            self.repository.adjust_ai_usage(
                usage_date,
                USAGE_FEATURE,
                calls_delta=-1,
                tokens_delta=-estimated,
                now=current,
            )
            raise

        daily = self.repository.adjust_ai_usage(
            usage_date,
            USAGE_FEATURE,
            tokens_delta=actual_tokens - estimated,
            now=current,
        )
        self.repository.save_event_explanation(
            cache_key,
            normalized_event_id,
            model_name,
            payload,
            actual_tokens,
            self.config.event_explanation_retention_days,
            current,
        )
        return {
            **payload,
            "event_id": normalized_event_id,
            "model": model_name,
            "token_usage": actual_tokens,
            "cached": False,
            "daily_usage": daily,
        }
