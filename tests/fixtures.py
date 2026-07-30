from datetime import date, timedelta


def stock_meta(code="600000"):
    return {
        "code": code,
        "name": "固定样例",
        "secid": "1." + code,
        "prefix": "sh",
        "classify": "AStock",
        "type_name": "股票",
        "is_stock": True,
        "secucode": code + ".SH",
    }


def kline_rows(count=80):
    start = date(2025, 10, 1)
    rows = []
    for index in range(count):
        close = 10.0 + index * 0.1
        rows.append(
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "open": close - 0.05,
                "close": close,
                "high": close + 0.1,
                "low": close - 0.1,
                "vol": 1000 + index,
            }
        )
    return rows


def valuation_rows():
    return [
        {
            "date": "2025-01-01",
            "close": 10,
            "pe": 10.0,
            "pb": 1.0,
            "cap": 100,
            "name": "固定样例",
            "board_code": None,
            "board_name": None,
        },
        {
            "date": "2026-01-01",
            "close": 18,
            "pe": 20.0,
            "pb": 2.0,
            "cap": 120,
            "name": "固定样例",
            "board_code": None,
            "board_name": None,
        },
    ]
