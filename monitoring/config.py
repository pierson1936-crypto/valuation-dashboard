from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("%s 必须是整数" % name) from exc
    if value < minimum:
        raise ValueError("%s 不能小于 %d" % (name, minimum))
    return value


@dataclass(frozen=True)
class MonitorConfig:
    db_path: Path
    market_calendar_path: Path | None = None
    poll_seconds: int = 60
    analysis_refresh_seconds: int = 1800
    minute_retention_days: int = 7
    event_retention_days: int = 365
    ai_draft_retention_days: int = 7
    event_explanation_retention_days: int = 7
    event_explanation_daily_call_limit: int = 0
    event_explanation_daily_token_limit: int = 0
    portfolio_report_retention_days: int = 7
    portfolio_report_daily_call_limit: int = 0
    portfolio_report_daily_token_limit: int = 0
    notification_timeout_seconds: int = 10
    notification_retry_seconds: int = 60
    notification_max_attempts: int = 3
    wecom_webhook_url: str = ""
    serverchan_sendkey: str = ""
    deepseek_api_key: str = ""

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "MonitorConfig":
        root = project_root or Path(__file__).resolve().parents[1]
        raw_path = os.environ.get("MONITOR_DB_PATH", "").strip()
        db_path = Path(raw_path).expanduser() if raw_path else root / "data" / "monitor.db"
        if not db_path.is_absolute():
            db_path = root / db_path
        raw_calendar_path = os.environ.get(
            "MONITOR_TRADING_CALENDAR_PATH", ""
        ).strip()
        market_calendar_path = (
            Path(raw_calendar_path).expanduser()
            if raw_calendar_path
            else root / "data" / "trading_calendar.json"
        )
        if not market_calendar_path.is_absolute():
            market_calendar_path = root / market_calendar_path
        return cls(
            db_path=db_path,
            market_calendar_path=market_calendar_path,
            poll_seconds=_env_int("MONITOR_POLL_SECONDS", 60, 15),
            analysis_refresh_seconds=_env_int(
                "MONITOR_ANALYSIS_REFRESH_SECONDS", 1800, 180
            ),
            minute_retention_days=_env_int("MONITOR_MINUTE_RETENTION_DAYS", 7, 1),
            event_retention_days=_env_int("MONITOR_EVENT_RETENTION_DAYS", 365, 1),
            ai_draft_retention_days=_env_int(
                "MONITOR_AI_DRAFT_RETENTION_DAYS", 7, 1
            ),
            event_explanation_retention_days=_env_int(
                "MONITOR_EVENT_EXPLANATION_RETENTION_DAYS", 7, 1
            ),
            event_explanation_daily_call_limit=_env_int(
                "MONITOR_EVENT_EXPLANATION_DAILY_CALL_LIMIT", 0, 0
            ),
            event_explanation_daily_token_limit=_env_int(
                "MONITOR_EVENT_EXPLANATION_DAILY_TOKEN_LIMIT", 0, 0
            ),
            portfolio_report_retention_days=_env_int(
                "MONITOR_PORTFOLIO_REPORT_RETENTION_DAYS", 7, 1
            ),
            portfolio_report_daily_call_limit=_env_int(
                "MONITOR_PORTFOLIO_REPORT_DAILY_CALL_LIMIT", 0, 0
            ),
            portfolio_report_daily_token_limit=_env_int(
                "MONITOR_PORTFOLIO_REPORT_DAILY_TOKEN_LIMIT", 0, 0
            ),
            notification_timeout_seconds=_env_int(
                "MONITOR_NOTIFICATION_TIMEOUT_SECONDS", 10, 3
            ),
            notification_retry_seconds=_env_int(
                "MONITOR_NOTIFICATION_RETRY_SECONDS", 60, 30
            ),
            notification_max_attempts=_env_int(
                "MONITOR_NOTIFICATION_MAX_ATTEMPTS", 3, 1
            ),
            wecom_webhook_url=os.environ.get(
                "MONITOR_WECOM_WEBHOOK_URL", ""
            ).strip(),
            serverchan_sendkey=os.environ.get(
                "MONITOR_SERVERCHAN_SENDKEY", ""
            ).strip(),
            deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
        )
