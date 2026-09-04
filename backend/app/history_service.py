from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .database import get_bar_coverage, load_snapshot, save_snapshot
from .tools.market_data import MarketDataError, fetch_stock_history_with_adjustments, normalize_symbol


def get_stock_history(
    symbol: str,
    market: str = "自动",
    start_date: str = "2020-01-01",
    end_date: str | None = None,
    refresh: bool = False,
    adjustment_mode: str = "前复权",
) -> dict[str, Any]:
    end_date = end_date or date.today().isoformat()
    normalized = normalize_symbol(symbol, market)
    if adjustment_mode != "前复权" and not normalized.endswith((".SS", ".SZ", ".BJ")):
        raise MarketDataError("后复权、不复权和动态前复权目前仅支持 A 股")
    coverage = get_bar_coverage(symbol, market)
    # 周末、节假日和当日未收盘会自然形成数日空档，因此尾部容忍 7 天。
    covered = bool(
        coverage
        and coverage[0] <= start_date
        and date.fromisoformat(coverage[1]) >= date.fromisoformat(end_date) - timedelta(days=7)
    )
    if refresh or not covered:
        warmup_start = (date.fromisoformat(start_date) - timedelta(days=400)).isoformat()
        snapshot = fetch_stock_history_with_adjustments(symbol, market, warmup_start, end_date)
        save_snapshot(snapshot, market)
    try:
        stored = load_snapshot(symbol, start_date, end_date, market, adjustment_mode=adjustment_mode)
    except MarketDataError:
        if adjustment_mode == "前复权":
            raise
        # 个股回测按需补齐双价格；全市场任务仍要求先统一同步，避免回测时访问数千次远端接口。
        warmup_start = (date.fromisoformat(start_date) - timedelta(days=400)).isoformat()
        snapshot = fetch_stock_history_with_adjustments(symbol, market, warmup_start, end_date)
        save_snapshot(snapshot, market)
        stored = load_snapshot(symbol, start_date, end_date, market, adjustment_mode=adjustment_mode)
    if stored is not None:
        stored["data_source"] = f"本地行情数据库（原始来源：{stored['data_source']}）"
        return stored
    warmup_start = (date.fromisoformat(start_date) - timedelta(days=400)).isoformat()
    snapshot = fetch_stock_history_with_adjustments(symbol, market, warmup_start, end_date)
    save_snapshot(snapshot, market)
    stored = load_snapshot(symbol, start_date, end_date, market, adjustment_mode=adjustment_mode)
    return stored or snapshot
