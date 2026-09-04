from __future__ import annotations

from typing import Any
import akshare as ak

from .market_data import MarketDataError, normalize_symbol


def fetch_dividend_events(symbol: str) -> list[dict[str, Any]]:
    normalized = normalize_symbol(symbol, "A股")
    try:
        frame = ak.stock_dividend_cninfo(symbol=normalized.split(".")[0])
    except Exception as exc:
        raise MarketDataError(f"{normalized} 分红数据获取失败：{exc}") from exc
    if frame is None or frame.empty:
        return []
    result: list[dict[str, Any]] = []
    for row in frame.to_dict("records"):
        announcement = str(row.get("实施方案公告日期") or "")[:10]
        try:
            cash = float(row.get("派息比例") or 0)
        except (TypeError, ValueError):
            continue
        if announcement and cash > 0:
            result.append({
                "announcement_date": announcement,
                "ex_date": str(row.get("除权日") or "")[:10] or None,
                "payment_date": str(row.get("派息日") or "")[:10] or None,
                "cash_per_10": cash, "raw": {key: str(value) for key, value in row.items()},
            })
    return result
