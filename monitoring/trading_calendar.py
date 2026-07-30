from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TradingCalendar:
    closed_dates: frozenset[str] = frozenset()
    open_dates: frozenset[str] = frozenset()
    source: str = "weekday_fallback"
    updated_at: str = ""

    @classmethod
    def from_path(cls, path: Path | str | None) -> "TradingCalendar":
        if not path:
            return cls()
        calendar_path = Path(path)
        if not calendar_path.exists():
            return cls()
        try:
            payload = json.loads(calendar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("交易日历文件无法读取") from exc
        if not isinstance(payload, dict):
            raise ValueError("交易日历必须是 JSON 对象")

        closed = cls._date_set(payload.get("closed_dates"), "closed_dates")
        opened = cls._date_set(payload.get("open_dates"), "open_dates")
        overlap = closed & opened
        if overlap:
            raise ValueError("交易日历开市和休市日期冲突：%s" % sorted(overlap)[0])
        return cls(
            frozenset(closed),
            frozenset(opened),
            str(payload.get("source") or calendar_path.name)[:200],
            str(payload.get("updated_at") or "")[:40],
        )

    @staticmethod
    def _date_set(value: Any, field: str) -> set[str]:
        if value is None:
            return set()
        if not isinstance(value, list):
            raise ValueError("%s 必须是日期数组" % field)
        result = set()
        for item in value:
            try:
                normalized = date.fromisoformat(str(item)).isoformat()
            except ValueError as exc:
                raise ValueError("%s 包含无效日期" % field) from exc
            result.add(normalized)
        return result

    def is_trading_day(self, value: date) -> bool:
        current = value.isoformat()
        if current in self.open_dates:
            return True
        if current in self.closed_dates:
            return False
        return value.weekday() < 5

    def status(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "updated_at": self.updated_at,
            "closed_dates": len(self.closed_dates),
            "open_dates": len(self.open_dates),
            "fallback": self.source == "weekday_fallback",
        }
