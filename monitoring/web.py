from __future__ import annotations

import math
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from monitoring.config import MonitorConfig
from monitoring.db import MonitorRepository
from monitoring.presets import RISK_RULE_NAME, save_simple_setup, simple_rule_summary
from monitoring.rules import format_trigger_message


def _unique_text(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        cleaned = str(item or "").strip()[:500]
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _payload_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, "", 0):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _holding_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{label}必须是非负数")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是非负数") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label}必须是非负数")
    return number


class MonitorWebController:
    def __init__(
        self,
        config: MonitorConfig | None = None,
        repository: MonitorRepository | None = None,
        service_factory: Callable[[], Any] | None = None,
        explanation_factory: Callable[[], Any] | None = None,
        rule_assistant_factory: Callable[[], Any] | None = None,
        holding_ocr_factory: Callable[[], Any] | None = None,
        portfolio_report_factory: Callable[[], Any] | None = None,
    ):
        self.config = config or MonitorConfig.from_env()
        self.repository = repository or MonitorRepository(self.config.db_path)
        self.repository.initialize()
        self.service_factory = service_factory
        self.explanation_factory = explanation_factory
        self.rule_assistant_factory = rule_assistant_factory
        self.holding_ocr_factory = holding_ocr_factory
        self.portfolio_report_factory = portfolio_report_factory
        self._service = None
        self._explanation_assistant = None
        self._rule_assistant = None
        self._holding_ocr_assistant = None
        self._portfolio_report_assistant = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._started_at = ""
        self._last_summary: dict[str, Any] | None = None
        self._last_error = ""

    def _get_service(self):
        if self._service is None:
            if self.service_factory:
                self._service = self.service_factory()
            else:
                from monitoring.notifications import build_notifier
                from monitoring.service import MonitorService

                self._service = MonitorService(
                    self.config,
                    self.repository,
                    notifier=build_notifier(self.config),
                )
        return self._service

    def _get_explanation_assistant(self):
        if self._explanation_assistant is None:
            if self.explanation_factory:
                self._explanation_assistant = self.explanation_factory()
            else:
                from monitoring.explanations import EventExplanationAssistant

                self._explanation_assistant = EventExplanationAssistant(
                    self.config, self.repository
                )
        return self._explanation_assistant

    def _get_rule_assistant(self):
        if self._rule_assistant is None:
            if self.rule_assistant_factory:
                self._rule_assistant = self.rule_assistant_factory()
            else:
                from monitoring.assistant import RuleDraftAssistant

                self._rule_assistant = RuleDraftAssistant(
                    self.config, self.repository
                )
        return self._rule_assistant

    def _get_holding_ocr_assistant(self):
        if self._holding_ocr_assistant is None:
            if self.holding_ocr_factory:
                self._holding_ocr_assistant = self.holding_ocr_factory()
            else:
                from monitoring.holding_ocr import HoldingOCRAssistant

                self._holding_ocr_assistant = HoldingOCRAssistant(self.repository)
        return self._holding_ocr_assistant

    def _get_portfolio_report_assistant(self):
        if self._portfolio_report_assistant is None:
            if self.portfolio_report_factory:
                self._portfolio_report_assistant = self.portfolio_report_factory()
            else:
                from monitoring.portfolio import PortfolioReportAssistant

                self._portfolio_report_assistant = PortfolioReportAssistant(
                    self.config, self.repository
                )
        return self._portfolio_report_assistant

    def runtime_status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "started_at": self._started_at,
                "last_summary": self._last_summary,
                "last_error": self._last_error,
            }

    def _record_summary(self, summary: Any) -> dict[str, Any]:
        payload = summary.to_dict() if hasattr(summary, "to_dict") else dict(summary)
        with self._state_lock:
            self._last_summary = payload
            self._last_error = ""
        return payload

    def run_once(self) -> dict[str, Any]:
        with self._run_lock:
            try:
                return self._record_summary(self._get_service().run_once())
            except Exception as exc:
                error = "%s: %s" % (type(exc).__name__, str(exc)[:300])
                with self._state_lock:
                    self._last_error = error
                raise

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                pass
            if self._stop_event.wait(self.config.poll_seconds):
                break

    def start(self) -> dict[str, Any]:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                already_running = True
            else:
                already_running = False
                self._stop_event.clear()
                self._started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._last_error = ""
                self._thread = threading.Thread(
                    target=self._loop,
                    name="web-monitor",
                    daemon=True,
                )
                self._thread.start()
        if already_running:
            return self.runtime_status()
        return self.runtime_status()

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        return self.runtime_status()

    def save_setup(self, payload: dict[str, Any]) -> dict[str, Any]:
        code = str(payload.get("code") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("请输入 6 位代码")
        try:
            watch = float(payload["watch_price"])
            risk = float(payload["risk_price"])
            target_raw = payload.get("target_price")
            target = None if target_raw in (None, "") else float(target_raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("请填写有效的关注价和风险价") from exc
        result = save_simple_setup(
            self.repository,
            code,
            str(payload.get("name") or "").strip(),
            watch,
            risk,
            target,
        )
        return result

    def list_holdings(self) -> dict[str, Any]:
        return {
            "holdings": [
                {
                    "code": item["code"],
                    "name": item["name"],
                    "quantity": item["quantity"],
                    "cost_price": item["cost_price"],
                    "enabled": bool(item["enabled"]),
                    "updated_at": item["updated_at"],
                }
                for item in self.repository.list_holdings()
            ]
        }

    def recognize_holding_screenshot(
        self, payload: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("截图识别请求必须是对象")
        return self._get_holding_ocr_assistant().recognize(
            payload.get("image_data_url"),
            api_key,
            payload.get("base_url"),
        )

    def latest_portfolio_report(self) -> dict[str, Any]:
        return self._get_portfolio_report_assistant().latest()

    def portfolio_report_history(self) -> dict[str, Any]:
        return self._get_portfolio_report_assistant().history()

    def portfolio_scan(self) -> dict[str, Any]:
        return self._get_portfolio_report_assistant().scan()

    def generate_portfolio_report(
        self, payload: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("持仓报告请求必须是对象")
        return self._get_portfolio_report_assistant().generate(
            payload.get("user_judgment"),
            api_key,
            force=_payload_bool(payload.get("force")),
            provider=payload.get("provider") or "deepseek",
            deepseek_model=payload.get("deepseek_model") or "",
        )

    def upsert_holdings(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_holdings = payload.get("holdings") if isinstance(payload, dict) else None
        if not isinstance(raw_holdings, list):
            return {
                "saved": [],
                "errors": [
                    {
                        "index": None,
                        "code": "",
                        "field": "holdings",
                        "message": "持仓数据必须是数组",
                    }
                ],
            }
        if len(raw_holdings) > 100:
            return {
                "saved": [],
                "errors": [
                    {
                        "index": None,
                        "code": "",
                        "field": "holdings",
                        "message": "单次最多确认 100 条持仓",
                    }
                ],
            }

        normalized: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        seen_codes: set[str] = set()
        for index, raw_item in enumerate(raw_holdings):
            if not isinstance(raw_item, dict):
                errors.append(
                    {
                        "index": index,
                        "code": "",
                        "field": "row",
                        "message": "每条持仓必须是对象",
                    }
                )
                continue

            code = str(raw_item.get("code") or "").strip()
            item_errors: list[dict[str, Any]] = []
            if not re.fullmatch(r"\d{6}", code):
                item_errors.append(
                    {
                        "index": index,
                        "code": code,
                        "field": "code",
                        "message": "代码必须是 6 位数字",
                    }
                )
            elif code in seen_codes:
                item_errors.append(
                    {
                        "index": index,
                        "code": code,
                        "field": "code",
                        "message": "同一批持仓中代码不能重复",
                    }
                )
            else:
                seen_codes.add(code)

            raw_name = raw_item.get("name", "")
            if not isinstance(raw_name, str):
                item_errors.append(
                    {
                        "index": index,
                        "code": code,
                        "field": "name",
                        "message": "名称必须是文本",
                    }
                )
                name = ""
            else:
                name = raw_name.strip()
                if len(name) > 80:
                    item_errors.append(
                        {
                            "index": index,
                            "code": code,
                            "field": "name",
                            "message": "名称不能超过 80 字",
                        }
                    )

            numbers: dict[str, float] = {}
            for field, label in (("quantity", "持仓数量"), ("cost_price", "成本价")):
                try:
                    numbers[field] = _holding_number(raw_item.get(field), label)
                except ValueError as exc:
                    item_errors.append(
                        {
                            "index": index,
                            "code": code,
                            "field": field,
                            "message": str(exc),
                        }
                    )

            errors.extend(item_errors)
            if not item_errors:
                normalized.append(
                    {
                        "code": code,
                        "name": name,
                        "quantity": numbers["quantity"],
                        "cost_price": numbers["cost_price"],
                    }
                )

        if errors:
            return {"saved": [], "errors": errors}

        self.repository.upsert_holdings(normalized)
        return {"saved": normalized, "errors": []}

    def save_logic(self, payload: dict[str, Any]) -> dict[str, Any]:
        code = str(payload.get("code") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("请输入 6 位代码")
        thesis = str(payload.get("thesis") or "").strip()
        invalidation = str(payload.get("invalidation") or "").strip()
        review_items = str(payload.get("review_items") or "").strip()
        if len(thesis) > 2000:
            raise ValueError("买入或持有逻辑不能超过 2000 字")
        if len(invalidation) > 1500:
            raise ValueError("失效条件不能超过 1500 字")
        if len(review_items) > 1500:
            raise ValueError("复核事项不能超过 1500 字")

        watches = self.repository.list_watch()
        existing = next((item for item in watches if item["code"] == code), None)
        name = str(payload.get("name") or "").strip()[:80]
        if existing is None:
            self.repository.upsert_watch(code, name)
        elif name and name != existing["name"]:
            self.repository.upsert_watch(
                code,
                name,
                existing["quantity"],
                existing["cost_price"],
                existing["notes"],
                bool(existing["enabled"]),
            )
        self.repository.save_watch_logic(
            code, thesis, invalidation, review_items
        )
        from monitoring.assistant import classify_review_items

        combined_logic = "\n".join(
            item for item in (thesis, invalidation, review_items) if item
        )
        manual_items, periodic_items = classify_review_items(combined_logic)
        explicit_review_items = [
            item.strip()
            for item in re.split(r"[\n；;。]+", review_items)
            if item.strip()
        ]
        manual_items = _unique_text(
            manual_items
            + [
                item
                for item in explicit_review_items
                if item not in periodic_items
            ]
        )
        return {
            "code": code,
            "name": name or (existing["name"] if existing else ""),
            "thesis": thesis,
            "invalidation": invalidation,
            "review_items": review_items,
            "configured": bool(thesis or invalidation or review_items),
            "logic_saved": True,
            "auto_rule_count": 0,
            "manual_review_items": manual_items,
            "periodic_review_items": periodic_items,
        }

    def draft_rules(
        self, payload: dict[str, Any], api_key: str = ""
    ) -> dict[str, Any]:
        code = str(payload.get("code") or "").strip()
        logic_text = str(payload.get("logic_text") or "").strip()
        stored = (
            self.repository.get_watch_logic(code)
            if re.fullmatch(r"\d{6}", code)
            else None
        )
        if not logic_text and stored:
            logic_text = "\n".join(
                str(stored.get(field) or "").strip()
                for field in ("thesis", "invalidation", "review_items")
                if str(stored.get(field) or "").strip()
            )
        confirmation = payload.get("confirmation")
        if confirmation is not None and not isinstance(confirmation, dict):
            raise ValueError("confirmation 必须是对象")
        result = self._get_rule_assistant().suggest(
            code,
            logic_text,
            _payload_bool(payload.get("force")),
            api_key=api_key,
            advanced_mode=_payload_bool(payload.get("advanced_mode")),
            confirmation=confirmation,
            deepseek_model=payload.get("deepseek_model") or "",
        )
        logic_saved = bool(
            stored
            and (
                stored.get("thesis")
                or stored.get("invalidation")
                or stored.get("review_items")
            )
        )
        if stored:
            from monitoring.assistant import classify_review_items

            stored_text = "\n".join(
                str(stored.get(field) or "")
                for field in ("thesis", "invalidation", "review_items")
            )
            stored_manual, stored_periodic = classify_review_items(stored_text)
            explicit_review_items = [
                item.strip()
                for item in re.split(
                    r"[\n；;。]+", str(stored.get("review_items") or "")
                )
                if item.strip()
            ]
            stored_manual = _unique_text(
                stored_manual
                + [
                    item
                    for item in explicit_review_items
                    if item not in stored_periodic
                ]
            )
        else:
            stored_manual, stored_periodic = [], []
        result["logic_saved"] = logic_saved
        result["auto_rule_count"] = len(result.get("rules") or [])
        result["manual_review_items"] = _unique_text(
            list(result.get("manual_review_items") or []) + stored_manual
        )
        result["periodic_review_items"] = _unique_text(
            list(result.get("periodic_review_items") or [])
            + stored_periodic
        )
        return result

    def explain_event(
        self,
        event_id: Any,
        api_key: str,
        force: bool = False,
        deepseek_model: str = "",
    ) -> dict[str, Any]:
        return self._get_explanation_assistant().explain(
            event_id,
            api_key,
            force=force,
            deepseek_model=deepseek_model,
        )

    def remove_watch(self, code: str) -> bool:
        cleaned = str(code or "").strip()
        if not re.fullmatch(r"\d{6}", cleaned):
            raise ValueError("请输入 6 位代码")
        return self.repository.delete_watch(cleaned)

    def simulate_watch(self, code: str, kind: str = "risk") -> dict[str, Any]:
        cleaned = str(code or "").strip()
        if not re.fullmatch(r"\d{6}", cleaned):
            raise ValueError("请输入 6 位代码")
        if kind != "risk":
            raise ValueError("目前只支持模拟风险提醒")

        watch = next(
            (item for item in self.repository.list_watch() if item["code"] == cleaned),
            None,
        )
        if watch is None:
            raise ValueError("未找到该盯盘标的")
        rule = next(
            (
                item
                for item in self.repository.list_rules([cleaned], enabled_only=True)
                if item["name"] == RISK_RULE_NAME
            ),
            None,
        )
        if rule is None:
            raise ValueError("该标的没有可用的风险价规则")

        observed_value = float(rule["threshold"])
        return {
            "simulated": True,
            "code": cleaned,
            "name": str(watch.get("name") or ""),
            "rule_name": rule["name"],
            "direction": rule["direction"],
            "metric": rule["metric"],
            "operator": rule["operator"],
            "threshold": observed_value,
            "observed_value": observed_value,
            "message": format_trigger_message(
                rule,
                str(watch.get("name") or cleaned),
                observed_value,
            ),
            "persisted": False,
            "notification_sent": False,
            "token_usage": 0,
        }

    def overview(self) -> dict[str, Any]:
        watches = self.repository.list_watch()
        logic_by_code = {
            item["code"]: item for item in self.repository.list_watch_logic()
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        all_rules = self.repository.list_rules()
        for rule in all_rules:
            grouped.setdefault(rule["code"], []).append(rule)

        watch_rows = []
        for watch in watches:
            rules = grouped.get(watch["code"], [])
            logic = logic_by_code.get(watch["code"]) or {
                "thesis": "",
                "invalidation": "",
                "review_items": "",
                "updated_at": "",
            }
            watch_rows.append(
                {
                    "code": watch["code"],
                    "name": watch["name"],
                    "enabled": bool(watch["enabled"]),
                    "advanced_rule_count": sum(
                        1
                        for rule in rules
                        if not rule["name"].startswith(("[三线]", "[自动异动]"))
                    ),
                    "logic": {
                        "thesis": logic["thesis"],
                        "invalidation": logic["invalidation"],
                        "review_items": logic["review_items"],
                        "updated_at": logic["updated_at"],
                        "configured": bool(
                            logic["thesis"]
                            or logic["invalidation"]
                            or logic["review_items"]
                        ),
                    },
                    **simple_rule_summary(rules),
                }
            )

        event_rows = []
        for event in self.repository.list_events(20):
            event_rows.append(
                {
                    "id": event["id"],
                    "code": event["code"],
                    "name": str(event["payload"].get("name") or ""),
                    "occurred_at": event["occurred_at"],
                    "direction": event["direction"],
                    "metric": event["metric"],
                    "observed_value": event["observed_value"],
                    "threshold": event["threshold"],
                    "message": event["message"],
                    "notification_status": event["notification_status"],
                }
            )

        channels = ["本地控制台"]
        if self.config.wecom_webhook_url:
            channels.append("企业微信群")
        if self.config.serverchan_sendkey:
            channels.append("个人微信")
        usage_date = datetime.now(
            timezone(timedelta(hours=8))
        ).date().isoformat()
        daily_usage = self.repository.get_ai_usage(
            usage_date, "event_explanation"
        )
        try:
            from monitoring.trading_calendar import TradingCalendar

            calendar_status = TradingCalendar.from_path(
                self.config.market_calendar_path
            ).status()
        except ValueError as exc:
            calendar_status = {
                "source": "invalid",
                "fallback": True,
                "error": str(exc),
                "closed_dates": 0,
                "open_dates": 0,
                "updated_at": "",
            }
        return {
            "runtime": self.runtime_status(),
            "settings": {
                "poll_seconds": self.config.poll_seconds,
                "minute_retention_days": self.config.minute_retention_days,
                "event_retention_days": self.config.event_retention_days,
                "channels": channels,
                "token_usage_per_poll": 0,
                "calendar": calendar_status,
                "event_explanation": {
                    "retention_days": self.config.event_explanation_retention_days,
                    "daily_call_limit": self.config.event_explanation_daily_call_limit,
                    "daily_token_limit": self.config.event_explanation_daily_token_limit,
                    "daily_usage": daily_usage,
                },
            },
            "counts": {
                "watches": len(watches),
                "rules": len(all_rules),
                "events": self.repository.count_rows("events"),
                "notification_jobs": self.repository.count_rows(
                    "notification_jobs"
                ),
            },
            "watches": watch_rows,
            "events": event_rows,
        }
