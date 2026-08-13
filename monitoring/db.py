from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS watchlist (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    quantity REAL,
    cost_price REAL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL REFERENCES watchlist(code) ON DELETE CASCADE,
    name TEXT NOT NULL,
    direction TEXT NOT NULL,
    metric TEXT NOT NULL,
    operator TEXT NOT NULL,
    threshold REAL NOT NULL,
    confirm_count INTEGER NOT NULL DEFAULT 1,
    cooldown_seconds INTEGER NOT NULL DEFAULT 3600,
    hysteresis REAL NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rules_code_enabled
ON rules(code, enabled);

CREATE TABLE IF NOT EXISTS rule_state (
    rule_id INTEGER PRIMARY KEY REFERENCES rules(id) ON DELETE CASCADE,
    consecutive_hits INTEGER NOT NULL DEFAULT 0,
    armed INTEGER NOT NULL DEFAULT 1,
    last_value REAL,
    last_evaluated_at TEXT,
    last_triggered_at TEXT
);

CREATE TABLE IF NOT EXISTS minute_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    price REAL,
    change_pct REAL,
    change_amount REAL,
    UNIQUE(code, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_minute_quotes_time
ON minute_quotes(observed_at);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(code, trade_date)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER REFERENCES rules(id) ON DELETE SET NULL,
    code TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    direction TEXT NOT NULL,
    metric TEXT NOT NULL,
    observed_value REAL NOT NULL,
    threshold REAL NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    notification_status TEXT NOT NULL DEFAULT 'pending',
    notification_attempts INTEGER NOT NULL DEFAULT 0,
    last_notification_error TEXT,
    notified_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_time
ON events(occurred_at);

CREATE TABLE IF NOT EXISTS ai_rule_drafts (
    cache_key TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    logic_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watch_logic (
    code TEXT PRIMARY KEY REFERENCES watchlist(code) ON DELETE CASCADE,
    thesis TEXT NOT NULL DEFAULT '',
    invalidation TEXT NOT NULL DEFAULT '',
    review_items TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_explanations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key TEXT NOT NULL UNIQUE,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    token_usage INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_explanations_event
ON event_explanations(event_id, created_at);

CREATE TABLE IF NOT EXISTS ai_usage (
    usage_date TEXT NOT NULL,
    feature TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(usage_date, feature)
);

CREATE TABLE IF NOT EXISTS notification_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_notification_jobs_due
ON notification_jobs(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS holding_ocr_cache (
    cache_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    token_usage INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_judgment TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    cache_key TEXT,
    snapshot_hash TEXT,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    token_usage INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_portfolio_reports_cache
ON portfolio_reports(cache_key, status, expires_at);

CREATE INDEX IF NOT EXISTS idx_portfolio_reports_created
ON portfolio_reports(created_at DESC);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class MonitorRepository:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode = WAL")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_watch(
        self,
        code: str,
        name: str = "",
        quantity: float | None = None,
        cost_price: float | None = None,
        notes: str = "",
        enabled: bool = True,
        now: datetime | None = None,
    ) -> None:
        stamp = iso_utc(now)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO watchlist
                    (code, name, enabled, quantity, cost_price, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    enabled = excluded.enabled,
                    quantity = excluded.quantity,
                    cost_price = excluded.cost_price,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    code,
                    name,
                    int(enabled),
                    quantity,
                    cost_price,
                    notes,
                    stamp,
                    stamp,
                ),
            )

    def delete_watch(self, code: str) -> bool:
        with self.connect() as conn:
            result = conn.execute("DELETE FROM watchlist WHERE code = ?", (code,))
            return result.rowcount > 0

    def list_watch(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM watchlist"
        params: tuple[Any, ...] = ()
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY code"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def list_holdings(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT code, name, enabled, quantity, cost_price, updated_at
                    FROM watchlist
                    WHERE quantity IS NOT NULL OR cost_price IS NOT NULL
                    ORDER BY code
                    """
                ).fetchall()
            ]

    def upsert_holdings(
        self,
        holdings: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> None:
        stamp = iso_utc(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                """
                INSERT INTO watchlist
                    (code, name, enabled, quantity, cost_price, notes,
                     created_at, updated_at)
                VALUES (?, ?, 1, ?, ?, '', ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name = CASE
                        WHEN excluded.name <> '' THEN excluded.name
                        ELSE watchlist.name
                    END,
                    quantity = excluded.quantity,
                    cost_price = excluded.cost_price,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        item["code"],
                        item["name"],
                        item["quantity"],
                        item["cost_price"],
                        stamp,
                        stamp,
                    )
                    for item in holdings
                ],
            )

    def get_cached_holding_ocr(
        self, cache_key: str, now: datetime | None = None
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT model, created_at, expires_at, token_usage, payload_json
                FROM holding_ocr_cache
                WHERE cache_key = ? AND expires_at > ?
                """,
                (cache_key, iso_utc(now)),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def save_holding_ocr(
        self,
        cache_key: str,
        model: str,
        payload: dict[str, Any],
        retention_days: int,
        token_usage: int = 0,
        now: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO holding_ocr_cache
                    (cache_key, model, created_at, expires_at,
                     token_usage, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    model,
                    iso_utc(current),
                    iso_utc(current + timedelta(days=retention_days)),
                    max(0, int(token_usage)),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def create_portfolio_report(
        self,
        user_judgment: str,
        model: str,
        retention_days: int,
        now: datetime | None = None,
        provider: str = "deepseek",
    ) -> int:
        current = now or utc_now()
        stamp = iso_utc(current)
        initial_payload = json.dumps(
            {"provider": str(provider or "deepseek").strip().lower()},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO portfolio_reports
                    (user_judgment, status, model, created_at, updated_at,
                     expires_at, payload_json)
                VALUES (?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    user_judgment,
                    model,
                    stamp,
                    stamp,
                    iso_utc(current + timedelta(days=retention_days)),
                    initial_payload,
                ),
            )
            return int(cursor.lastrowid)

    def get_cached_portfolio_report(
        self, cache_key: str, now: datetime | None = None
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, user_judgment, status, cache_key, snapshot_hash,
                       model, created_at, updated_at, expires_at, token_usage,
                       payload_json, error
                FROM portfolio_reports
                WHERE cache_key = ? AND status = 'complete' AND expires_at > ?
                ORDER BY id DESC LIMIT 1
                """,
                (cache_key, iso_utc(now)),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def complete_portfolio_report(
        self,
        report_id: int,
        cache_key: str,
        snapshot_hash: str,
        payload: dict[str, Any],
        token_usage: int,
        now: datetime | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE portfolio_reports
                SET status = 'complete', cache_key = ?, snapshot_hash = ?,
                    updated_at = ?, token_usage = ?, payload_json = ?, error = ''
                WHERE id = ?
                """,
                (
                    cache_key,
                    snapshot_hash,
                    iso_utc(now),
                    max(0, int(token_usage)),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    int(report_id),
                ),
            )

    def fail_portfolio_report(
        self, report_id: int, error: str, now: datetime | None = None
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE portfolio_reports
                SET status = 'failed', updated_at = ?, error = ?
                WHERE id = ? AND status = 'pending'
                """,
                (iso_utc(now), str(error or "")[:300], int(report_id)),
            )

    def get_latest_portfolio_report(
        self, now: datetime | None = None
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, user_judgment, status, cache_key, snapshot_hash,
                       model, created_at, updated_at, expires_at, token_usage,
                       payload_json, error
                FROM portfolio_reports
                WHERE expires_at > ?
                ORDER BY id DESC LIMIT 1
                """,
                (iso_utc(now),),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def list_portfolio_reports(
        self, limit: int = 7, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 7))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_judgment, status, cache_key, snapshot_hash,
                       model, created_at, updated_at, expires_at, token_usage,
                       payload_json, error
                FROM portfolio_reports
                WHERE status = 'complete'
                ORDER BY id DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        reports = []
        for row in rows:
            report = dict(row)
            report["payload"] = json.loads(report.pop("payload_json"))
            reports.append(report)
        return reports

    def trim_portfolio_reports(self, keep_complete: int = 7) -> int:
        safe_keep = max(1, min(int(keep_complete), 7))
        with self.connect() as conn:
            return conn.execute(
                """
                DELETE FROM portfolio_reports
                WHERE status = 'complete' AND id NOT IN (
                    SELECT id FROM portfolio_reports
                    WHERE status = 'complete'
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (safe_keep,),
            ).rowcount

    def save_watch_logic(
        self,
        code: str,
        thesis: str,
        invalidation: str,
        review_items: str,
        now: datetime | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO watch_logic
                    (code, thesis, invalidation, review_items, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    thesis = excluded.thesis,
                    invalidation = excluded.invalidation,
                    review_items = excluded.review_items,
                    updated_at = excluded.updated_at
                """,
                (code, thesis, invalidation, review_items, iso_utc(now)),
            )

    def get_watch_logic(self, code: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM watch_logic WHERE code = ?", (code,)
            ).fetchone()
        return dict(row) if row else None

    def list_watch_logic(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM watch_logic ORDER BY code"
                ).fetchall()
            ]

    def add_rule(
        self,
        code: str,
        name: str,
        direction: str,
        metric: str,
        operator: str,
        threshold: float,
        confirm_count: int = 1,
        cooldown_seconds: int = 3600,
        hysteresis: float = 0,
        enabled: bool = True,
        now: datetime | None = None,
    ) -> int:
        stamp = iso_utc(now)
        with self.connect() as conn:
            result = conn.execute(
                """
                INSERT INTO rules
                    (code, name, direction, metric, operator, threshold,
                     confirm_count, cooldown_seconds, hysteresis, enabled,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    name,
                    direction,
                    metric,
                    operator,
                    threshold,
                    confirm_count,
                    cooldown_seconds,
                    hysteresis,
                    int(enabled),
                    stamp,
                    stamp,
                ),
            )
            rule_id = int(result.lastrowid)
            conn.execute(
                "INSERT INTO rule_state(rule_id) VALUES (?)",
                (rule_id,),
            )
            return rule_id

    def replace_managed_rules(
        self,
        code: str,
        managed_names: set[str],
        rules: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> list[int]:
        if not managed_names:
            raise ValueError("系统规则名称不能为空")
        if any(rule.get("name") not in managed_names for rule in rules):
            raise ValueError("只能替换指定的系统规则")

        stamp = iso_utc(now)
        placeholders = ",".join("?" for _ in managed_names)
        ordered_names = sorted(managed_names)
        rule_ids: list[int] = []
        with self.connect() as conn:
            watch = conn.execute(
                "SELECT 1 FROM watchlist WHERE code = ?", (code,)
            ).fetchone()
            if not watch:
                raise ValueError("自选股不存在：%s" % code)
            conn.execute(
                "DELETE FROM rules WHERE code = ? AND name IN (%s)" % placeholders,
                [code, *ordered_names],
            )
            for rule in rules:
                result = conn.execute(
                    """
                    INSERT INTO rules
                        (code, name, direction, metric, operator, threshold,
                         confirm_count, cooldown_seconds, hysteresis, enabled,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        code,
                        rule["name"],
                        rule["direction"],
                        rule["metric"],
                        rule["operator"],
                        rule["threshold"],
                        rule["confirm_count"],
                        rule["cooldown_seconds"],
                        rule["hysteresis"],
                        int(rule.get("enabled", True)),
                        stamp,
                        stamp,
                    ),
                )
                rule_id = int(result.lastrowid)
                rule_ids.append(rule_id)
                conn.execute("INSERT INTO rule_state(rule_id) VALUES (?)", (rule_id,))
        return rule_ids

    def set_rule_enabled(self, rule_id: int, enabled: bool) -> bool:
        with self.connect() as conn:
            result = conn.execute(
                "UPDATE rules SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), iso_utc(), rule_id),
            )
            return result.rowcount > 0

    def delete_rule(self, rule_id: int) -> bool:
        with self.connect() as conn:
            result = conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
            return result.rowcount > 0

    def list_rules(
        self, codes: list[str] | None = None, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if codes:
            conditions.append("code IN (%s)" % ",".join("?" for _ in codes))
            params.extend(codes)
        if enabled_only:
            conditions.append("enabled = 1")
        sql = "SELECT * FROM rules"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY code, id"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def get_rule_state(self, rule_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rule_state WHERE rule_id = ?", (rule_id,)
            ).fetchone()
            if row:
                return dict(row)
            conn.execute("INSERT INTO rule_state(rule_id) VALUES (?)", (rule_id,))
            return {
                "rule_id": rule_id,
                "consecutive_hits": 0,
                "armed": 1,
                "last_value": None,
                "last_evaluated_at": None,
                "last_triggered_at": None,
            }

    def save_rule_state(self, state: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO rule_state
                    (rule_id, consecutive_hits, armed, last_value,
                     last_evaluated_at, last_triggered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    consecutive_hits = excluded.consecutive_hits,
                    armed = excluded.armed,
                    last_value = excluded.last_value,
                    last_evaluated_at = excluded.last_evaluated_at,
                    last_triggered_at = excluded.last_triggered_at
                """,
                (
                    state["rule_id"],
                    state["consecutive_hits"],
                    state["armed"],
                    state.get("last_value"),
                    state.get("last_evaluated_at"),
                    state.get("last_triggered_at"),
                ),
            )

    def add_minute_quote(
        self, quote: dict[str, Any], observed_at: datetime | None = None
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO minute_quotes
                    (code, observed_at, name, price, change_pct, change_amount)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    quote["code"],
                    iso_utc(observed_at),
                    quote.get("name", ""),
                    quote.get("price"),
                    quote.get("change_pct"),
                    quote.get("change_amount"),
                ),
            )

    def upsert_daily_snapshot(
        self,
        code: str,
        trade_date: str,
        payload: dict[str, Any],
        observed_at: datetime | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_snapshots(code, trade_date, observed_at, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(code, trade_date) DO UPDATE SET
                    observed_at = excluded.observed_at,
                    payload_json = excluded.payload_json
                """,
                (
                    code,
                    trade_date,
                    iso_utc(observed_at),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def latest_daily_snapshot(self, code: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM daily_snapshots
                WHERE code = ?
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                (code,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def add_event(
        self,
        rule: dict[str, Any],
        observed_value: float,
        message: str,
        payload: dict[str, Any],
        occurred_at: datetime | None = None,
    ) -> int:
        with self.connect() as conn:
            result = conn.execute(
                """
                INSERT INTO events
                    (rule_id, code, occurred_at, direction, metric,
                     observed_value, threshold, message, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule["id"],
                    rule["code"],
                    iso_utc(occurred_at),
                    rule["direction"],
                    rule["metric"],
                    observed_value,
                    rule["threshold"],
                    message,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            return int(result.lastrowid)

    def update_event_notification(
        self,
        event_id: int,
        status: str,
        attempts: int,
        error: str | None = None,
        notified_at: datetime | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE events
                SET notification_status = ?, notification_attempts = ?,
                    last_notification_error = ?, notified_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    attempts,
                    error,
                    iso_utc(notified_at) if notified_at else None,
                    event_id,
                ),
            )

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY occurred_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def get_cached_event_explanation(
        self, cache_key: str, now: datetime | None = None
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT model, created_at, expires_at, token_usage, payload_json
                FROM event_explanations
                WHERE cache_key = ? AND expires_at > ?
                """,
                (cache_key, iso_utc(now)),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def save_event_explanation(
        self,
        cache_key: str,
        event_id: int,
        model: str,
        payload: dict[str, Any],
        token_usage: int,
        retention_days: int,
        now: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO event_explanations
                    (cache_key, event_id, model, created_at, expires_at,
                     token_usage, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    event_id,
                    model,
                    iso_utc(current),
                    iso_utc(current + timedelta(days=retention_days)),
                    max(0, int(token_usage)),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def get_ai_usage(self, usage_date: str, feature: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT usage_date, feature, calls, tokens, updated_at
                FROM ai_usage WHERE usage_date = ? AND feature = ?
                """,
                (usage_date, feature),
            ).fetchone()
        if row:
            return dict(row)
        return {
            "usage_date": usage_date,
            "feature": feature,
            "calls": 0,
            "tokens": 0,
            "updated_at": "",
        }

    def reserve_ai_usage(
        self,
        usage_date: str,
        feature: str,
        tokens: int,
        call_limit: int,
        token_limit: int,
        now: datetime | None = None,
        calls: int = 1,
    ) -> dict[str, Any]:
        requested = max(0, int(tokens))
        requested_calls = max(1, int(calls))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT calls, tokens FROM ai_usage WHERE usage_date = ? AND feature = ?",
                (usage_date, feature),
            ).fetchone()
            calls = int(row["calls"]) if row else 0
            used_tokens = int(row["tokens"]) if row else 0
            if call_limit > 0 and calls + requested_calls > call_limit:
                raise ValueError("今日 AI 解释次数已达上限")
            if token_limit > 0 and used_tokens + requested > token_limit:
                raise ValueError("今日 AI 解释 Token 预算不足")
            calls += requested_calls
            used_tokens += requested
            stamp = iso_utc(now)
            conn.execute(
                """
                INSERT INTO ai_usage
                    (usage_date, feature, calls, tokens, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(usage_date, feature) DO UPDATE SET
                    calls = excluded.calls,
                    tokens = excluded.tokens,
                    updated_at = excluded.updated_at
                """,
                (usage_date, feature, calls, used_tokens, stamp),
            )
        return {
            "usage_date": usage_date,
            "feature": feature,
            "calls": calls,
            "tokens": used_tokens,
            "updated_at": stamp,
        }

    def adjust_ai_usage(
        self,
        usage_date: str,
        feature: str,
        calls_delta: int = 0,
        tokens_delta: int = 0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT calls, tokens FROM ai_usage WHERE usage_date = ? AND feature = ?",
                (usage_date, feature),
            ).fetchone()
            calls = max(0, (int(row["calls"]) if row else 0) + calls_delta)
            tokens = max(0, (int(row["tokens"]) if row else 0) + tokens_delta)
            stamp = iso_utc(now)
            conn.execute(
                """
                INSERT INTO ai_usage
                    (usage_date, feature, calls, tokens, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(usage_date, feature) DO UPDATE SET
                    calls = excluded.calls,
                    tokens = excluded.tokens,
                    updated_at = excluded.updated_at
                """,
                (usage_date, feature, calls, tokens, stamp),
            )
        return {
            "usage_date": usage_date,
            "feature": feature,
            "calls": calls,
            "tokens": tokens,
            "updated_at": stamp,
        }

    def enqueue_notification_job(
        self,
        event_id: int,
        channel: str,
        title: str,
        content: str,
        next_attempt_at: datetime,
        error: str = "",
        now: datetime | None = None,
    ) -> None:
        stamp = iso_utc(now)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO notification_jobs
                    (event_id, channel, title, content, status, attempts,
                     next_attempt_at, last_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                ON CONFLICT(event_id, channel) DO UPDATE SET
                    title = excluded.title,
                    content = excluded.content,
                    status = 'pending',
                    next_attempt_at = excluded.next_attempt_at,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    event_id,
                    channel,
                    title,
                    content,
                    iso_utc(next_attempt_at),
                    error,
                    stamp,
                    stamp,
                ),
            )

    def list_due_notification_jobs(
        self,
        now: datetime,
        max_attempts: int,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM notification_jobs
                    WHERE status IN ('pending', 'failed')
                      AND attempts < ?
                      AND next_attempt_at <= ?
                    ORDER BY next_attempt_at, id
                    LIMIT ?
                    """,
                    (max_attempts, iso_utc(now), max(1, min(limit, 100))),
                ).fetchall()
            ]

    def update_notification_job(
        self,
        job_id: int,
        status: str,
        attempts: int,
        next_attempt_at: datetime,
        error: str = "",
        now: datetime | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE notification_jobs
                SET status = ?, attempts = ?, next_attempt_at = ?,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    attempts,
                    iso_utc(next_attempt_at),
                    error,
                    iso_utc(now),
                    job_id,
                ),
            )

    def list_notification_jobs(self, event_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM notification_jobs
                    WHERE event_id = ? ORDER BY id
                    """,
                    (event_id,),
                ).fetchall()
            ]

    def get_cached_draft(
        self, cache_key: str, now: datetime | None = None
    ) -> dict[str, Any] | None:
        stamp = iso_utc(now)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM ai_rule_drafts
                WHERE cache_key = ? AND expires_at > ?
                """,
                (cache_key, stamp),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def save_draft(
        self,
        cache_key: str,
        code: str,
        logic_text: str,
        payload: dict[str, Any],
        retention_days: int,
        now: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO ai_rule_drafts
                    (cache_key, code, logic_text, created_at, expires_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    code,
                    logic_text,
                    iso_utc(current),
                    iso_utc(current + timedelta(days=retention_days)),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def cleanup(
        self,
        minute_retention_days: int,
        event_retention_days: int,
        now: datetime | None = None,
    ) -> dict[str, int]:
        current = now or utc_now()
        minute_cutoff = iso_utc(current - timedelta(days=minute_retention_days))
        event_cutoff = iso_utc(current - timedelta(days=event_retention_days))
        stamp = iso_utc(current)
        with self.connect() as conn:
            minute_count = conn.execute(
                "DELETE FROM minute_quotes WHERE observed_at < ?", (minute_cutoff,)
            ).rowcount
            event_count = conn.execute(
                "DELETE FROM events WHERE occurred_at < ?", (event_cutoff,)
            ).rowcount
            draft_count = conn.execute(
                "DELETE FROM ai_rule_drafts WHERE expires_at <= ?", (stamp,)
            ).rowcount
            explanation_count = conn.execute(
                "DELETE FROM event_explanations WHERE expires_at <= ?", (stamp,)
            ).rowcount
            holding_ocr_count = conn.execute(
                "DELETE FROM holding_ocr_cache WHERE expires_at <= ?", (stamp,)
            ).rowcount
            portfolio_report_count = conn.execute(
                """
                DELETE FROM portfolio_reports
                WHERE expires_at <= ?
                  AND (
                      status != 'complete'
                      OR id NOT IN (
                          SELECT id FROM portfolio_reports
                          WHERE status = 'complete'
                          ORDER BY id DESC LIMIT 7
                      )
                  )
                """,
                (stamp,),
            ).rowcount
            usage_count = conn.execute(
                "DELETE FROM ai_usage WHERE usage_date < ?",
                ((current - timedelta(days=90)).date().isoformat(),),
            ).rowcount
            conn.execute(
                """
                INSERT OR REPLACE INTO metadata(key, value)
                VALUES ('last_cleanup_at', ?)
                """,
                (stamp,),
            )
        return {
            "minute_quotes": minute_count,
            "events": event_count,
            "ai_rule_drafts": draft_count,
            "event_explanations": explanation_count,
            "holding_ocr_cache": holding_ocr_count,
            "portfolio_reports": portfolio_report_count,
            "ai_usage": usage_count,
        }

    def cleanup_due(self, now: datetime | None = None) -> bool:
        current = now or utc_now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key = 'last_cleanup_at'"
            ).fetchone()
        if not row:
            return True
        try:
            last = datetime.fromisoformat(row["value"])
        except ValueError:
            return True
        return last.date() < current.astimezone(timezone.utc).date()

    def count_rows(self, table: str) -> int:
        allowed = {
            "watchlist",
            "rules",
            "rule_state",
            "minute_quotes",
            "daily_snapshots",
            "events",
            "ai_rule_drafts",
            "watch_logic",
            "event_explanations",
            "ai_usage",
            "notification_jobs",
            "holding_ocr_cache",
            "portfolio_reports",
        }
        if table not in allowed:
            raise ValueError("不允许读取该表")
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0])
