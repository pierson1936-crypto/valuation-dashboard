from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as clock_time, timedelta, timezone
from typing import Any

from monitoring.config import MonitorConfig
from monitoring.data import AppMarketDataProvider
from monitoring.db import MonitorRepository, utc_now
from monitoring.notifications import CompositeNotifier, NotificationResult
from monitoring.rules import (
    QUOTE_METRICS,
    RuleEngine,
    format_trigger_message,
    validate_rule_spec,
)
from monitoring.trading_calendar import TradingCalendar


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


@dataclass
class MonitorRunSummary:
    observed_at: str
    skipped_reason: str = ""
    watch_count: int = 0
    quote_count: int = 0
    rule_count: int = 0
    evaluated_count: int = 0
    triggered_count: int = 0
    analysis_refresh_count: int = 0
    notification_retry_count: int = 0
    token_usage: int = 0
    cleanup: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_market_session(
    now: datetime, calendar: TradingCalendar | None = None
) -> bool:
    local = now.astimezone(SHANGHAI)
    active_calendar = calendar or TradingCalendar()
    if not active_calendar.is_trading_day(local.date()):
        return False
    current = local.time().replace(tzinfo=None)
    return (
        clock_time(9, 30) <= current <= clock_time(11, 30)
        or clock_time(13, 0) <= current <= clock_time(15, 0)
    )


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class MonitorService:
    def __init__(
        self,
        config: MonitorConfig,
        repository: MonitorRepository,
        provider: object | None = None,
        notifier: CompositeNotifier | None = None,
        calendar: TradingCalendar | None = None,
    ):
        self.config = config
        self.repository = repository
        self.provider = provider or AppMarketDataProvider()
        self.notifier = notifier or CompositeNotifier([])
        self.calendar = calendar or TradingCalendar.from_path(
            self.config.market_calendar_path
        )
        self.engine = RuleEngine()

    def _analysis_metrics(
        self, code: str, now: datetime, summary: MonitorRunSummary
    ) -> dict[str, Any]:
        snapshot = self.repository.latest_daily_snapshot(code)
        if snapshot:
            age = (
                now.astimezone(timezone.utc)
                - _parse_time(snapshot["observed_at"]).astimezone(timezone.utc)
            ).total_seconds()
            if age < self.config.analysis_refresh_seconds:
                return snapshot["payload"].get("metrics") or {}
        analysis = self.provider.get_analysis(code)
        trade_date = analysis.get("trade_date") or now.astimezone(SHANGHAI).date().isoformat()
        self.repository.upsert_daily_snapshot(code, trade_date, analysis, now)
        summary.analysis_refresh_count += 1
        return analysis.get("metrics") or {}

    @staticmethod
    def _notification_status(
        results: list[NotificationResult],
    ) -> tuple[str, str | None]:
        external = [result for result in results if result.channel != "console"]
        if not external:
            return "local_only", None
        failures = [result for result in external if not result.success]
        if not failures:
            return "sent", None
        success_count = len(external) - len(failures)
        status = "partial" if success_count else "failed"
        error = "; ".join(
            "%s:%s" % (result.channel, result.error) for result in failures
        )
        return status, error

    def _send_channel(
        self, channel: str, title: str, content: str
    ) -> NotificationResult:
        if hasattr(self.notifier, "send_to_channel"):
            return self.notifier.send_to_channel(channel, title, content)
        results = self.notifier.send(title, content)
        return next(
            (result for result in results if result.channel == channel),
            NotificationResult(channel, False, "通知渠道未配置"),
        )

    def _retry_notifications(
        self, current: datetime, summary: MonitorRunSummary
    ) -> None:
        jobs = self.repository.list_due_notification_jobs(
            current,
            self.config.notification_max_attempts,
        )
        for job in jobs:
            result = self._send_channel(
                job["channel"], job["title"], job["content"]
            )
            attempts = int(job["attempts"]) + 1
            if result.success:
                job_status = "sent"
                next_attempt = current
                error = ""
            else:
                job_status = (
                    "dead"
                    if attempts >= self.config.notification_max_attempts
                    else "failed"
                )
                delay = self.config.notification_retry_seconds * (
                    2 ** max(0, attempts - 1)
                )
                next_attempt = current + timedelta(seconds=delay)
                error = result.error
            self.repository.update_notification_job(
                job["id"],
                job_status,
                attempts,
                next_attempt,
                error,
                current,
            )
            event = self.repository.get_event(job["event_id"])
            if event:
                event_jobs = self.repository.list_notification_jobs(event["id"])
                pending = [
                    item
                    for item in event_jobs
                    if item["status"] in {"pending", "failed"}
                ]
                dead = [item for item in event_jobs if item["status"] == "dead"]
                if not pending and not dead:
                    event_status = "sent"
                    event_error = None
                    notified_at = current
                else:
                    event_status = (
                        "partial"
                        if event["notification_status"] == "partial"
                        else "failed"
                    )
                    event_error = "; ".join(
                        "%s:%s" % (item["channel"], item["last_error"])
                        for item in [*pending, *dead]
                    )
                    notified_at = (
                        current if event_status == "partial" else None
                    )
                self.repository.update_event_notification(
                    event["id"],
                    event_status,
                    int(event["notification_attempts"]) + 1,
                    event_error,
                    notified_at,
                )
            summary.notification_retry_count += 1

    def run_once(
        self, force: bool = False, now: datetime | None = None
    ) -> MonitorRunSummary:
        current = now or utc_now()
        summary = MonitorRunSummary(observed_at=current.isoformat())
        if self.repository.cleanup_due(current):
            summary.cleanup = self.repository.cleanup(
                self.config.minute_retention_days,
                self.config.event_retention_days,
                current,
            )
        self._retry_notifications(current, summary)
        watches = self.repository.list_watch(enabled_only=True)
        summary.watch_count = len(watches)
        if not watches:
            summary.skipped_reason = "自选股为空"
            return summary
        if not force:
            local = current.astimezone(SHANGHAI)
            if not self.calendar.is_trading_day(local.date()):
                summary.skipped_reason = "当前不是沪深交易日"
                return summary
            if not is_market_session(current, self.calendar):
                summary.skipped_reason = "当前不在沪深交易时段"
                return summary

        codes = [watch["code"] for watch in watches]
        names = {watch["code"]: watch["name"] for watch in watches}
        rules = self.repository.list_rules(codes, enabled_only=True)
        summary.rule_count = len(rules)
        if not rules:
            summary.skipped_reason = "没有启用的监控规则"
            return summary

        try:
            quote_rows = self.provider.get_quotes(codes)
        except Exception as exc:
            summary.errors.append("行情批量获取失败：%s" % type(exc).__name__)
            return summary
        quotes = {row["code"]: row for row in quote_rows if row.get("code")}
        for quote in quote_rows:
            if quote.get("code") and quote.get("price") is not None:
                self.repository.add_minute_quote(quote, current)
                summary.quote_count += 1

        rules_by_code: dict[str, list[dict[str, Any]]] = {}
        for rule in rules:
            rules_by_code.setdefault(rule["code"], []).append(rule)

        for code, code_rules in rules_by_code.items():
            quote = quotes.get(code) or {}
            metrics = {
                key: quote.get(key)
                for key in QUOTE_METRICS
                if quote.get(key) is not None
            }
            if any(rule["metric"] not in QUOTE_METRICS for rule in code_rules):
                try:
                    analysis_metrics = self._analysis_metrics(code, current, summary)
                    for key, value in analysis_metrics.items():
                        if key not in QUOTE_METRICS or key not in metrics:
                            metrics[key] = value
                except Exception as exc:
                    summary.errors.append(
                        "%s 完整分析失败：%s" % (code, type(exc).__name__)
                    )
            for rule in code_rules:
                try:
                    normalized = validate_rule_spec(rule)
                except ValueError as exc:
                    summary.errors.append("规则 %s 无效：%s" % (rule["id"], exc))
                    continue
                raw_value = metrics.get(normalized["metric"])
                if raw_value is None:
                    summary.errors.append(
                        "规则 %s 缺少指标 %s" % (rule["id"], normalized["metric"])
                    )
                    continue
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    summary.errors.append(
                        "规则 %s 指标不是数字" % normalized["id"]
                    )
                    continue
                state = self.repository.get_rule_state(normalized["id"])
                evaluation = self.engine.evaluate(normalized, state, value, current)
                self.repository.save_rule_state(evaluation.state)
                summary.evaluated_count += 1
                if not evaluation.triggered:
                    continue

                name = quote.get("name") or names.get(code) or code
                message = format_trigger_message(normalized, name, value)
                event_id = self.repository.add_event(
                    normalized,
                    value,
                    message,
                    {"metrics": metrics, "name": name},
                    current,
                )
                results = self.notifier.send("自选股盯盘提醒", message)
                status, error = self._notification_status(results)
                self.repository.update_event_notification(
                    event_id,
                    status,
                    len(results),
                    error,
                    current if status in {"sent", "partial", "local_only"} else None,
                )
                for result in results:
                    if result.channel == "console" or result.success:
                        continue
                    self.repository.enqueue_notification_job(
                        event_id,
                        result.channel,
                        "自选股盯盘提醒",
                        message,
                        current
                        + timedelta(
                            seconds=self.config.notification_retry_seconds
                        ),
                        result.error,
                        current,
                    )
                summary.triggered_count += 1
        return summary

    def watch(self, on_run=None) -> None:
        while True:
            started = time.monotonic()
            summary = self.run_once()
            if on_run:
                on_run(summary)
            elapsed = time.monotonic() - started
            time.sleep(max(1, self.config.poll_seconds - elapsed))
