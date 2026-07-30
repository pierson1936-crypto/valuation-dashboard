from __future__ import annotations

import importlib
import sys
from typing import Any


def _app_module():
    main = sys.modules.get("__main__")
    if main and callable(getattr(main, "fetch_watch_quotes", None)):
        return main
    return importlib.import_module("app")


def compact_analysis(result: dict[str, Any]) -> dict[str, Any]:
    tech = result.get("tech") or {}
    risk = result.get("risk") or {}
    moneyflow = result.get("moneyflow") or {}
    metrics = {
        "price": result.get("price"),
        "change_pct": result.get("chg"),
        "price_percentile": result.get("price_pct"),
        "pe": result.get("pe"),
        "pe_percentile": result.get("pe_pct"),
        "pb": result.get("pb"),
        "pb_percentile": result.get("pb_pct"),
        "rsi": tech.get("rsi"),
        "macd_hist": tech.get("macd_hist"),
        "volume_ratio": tech.get("vol_ratio"),
        "max_drawdown": tech.get("mdd"),
        "annual_volatility": tech.get("vola"),
        "risk_score": risk.get("score"),
        "main_flow_today": moneyflow.get("main_today"),
        "main_flow_5d": moneyflow.get("main_sum5"),
    }
    return {
        "code": result.get("code"),
        "name": result.get("name"),
        "trade_date": result.get("date"),
        "metrics": metrics,
        "sample": {
            "start": result.get("start"),
            "end": result.get("date"),
            "count": result.get("count"),
            "target_years": result.get("years"),
            "valuation_count": len((result.get("val_series") or {}).get("dates") or []),
        },
    }


class AppMarketDataProvider:
    def get_quotes(self, codes: list[str]) -> list[dict[str, Any]]:
        app = _app_module()
        rows = app.fetch_watch_quotes(codes)
        return [
            {
                "code": row.get("code"),
                "name": row.get("name", ""),
                "price": row.get("price"),
                "change_pct": row.get("chg"),
                "change_amount": row.get("change"),
            }
            for row in rows
        ]

    def get_analysis(self, code: str) -> dict[str, Any]:
        app = _app_module()
        result = app.analyze_cached(code)
        if result.get("error"):
            raise ValueError(result["error"])
        return compact_analysis(result)
