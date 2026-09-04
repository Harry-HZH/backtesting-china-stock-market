from __future__ import annotations

import json
import math
from typing import Any

import httpx

from .market_data import MarketDataError, normalize_symbol


EASTMONEY_FINANCE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
FUNDAMENTAL_SOURCE = "东方财富主要财务指标"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _points(value: float | None, bands: tuple[tuple[float, float], ...]) -> float:
    if value is None:
        return 0.0
    for threshold, score in bands:
        if value >= threshold:
            return score
    return 0.0


def calculate_fundamental_score(row: dict[str, Any]) -> tuple[float, dict[str, float]]:
    """计算0-100分绝对评分；保持独立于任一交易策略。"""
    roe = _number(row.get("ROEJQ"))
    revenue_growth = _number(row.get("TOTALOPERATEREVETZ"))
    profit_growth = _number(row.get("PARENTNETPROFITTZ"))
    debt_ratio = _number(row.get("ZCFZL"))
    operating_cash = _number(row.get("NETCASH_OPERATE_PK"))
    net_profit = _number(row.get("PARENTNETPROFIT"))

    profitability = _points(roe, ((15, 25), (8, 18), (0, 10)))
    growth = _points(revenue_growth, ((15, 20), (0, 12), (-10, 6)))
    profit_trend = _points(profit_growth, ((15, 20), (0, 12), (-20, 6)))
    if operating_cash is None or net_profit is None:
        cash_quality = 0.0
    elif net_profit > 0:
        cash_quality = _points(operating_cash / net_profit, ((1, 20), (0.8, 15), (0, 8)))
    else:
        cash_quality = 5.0 if operating_cash > 0 else 0.0
    financial_safety = 0.0 if debt_ratio is None else 15.0 if debt_ratio <= 40 else 10.0 if debt_ratio <= 60 else 5.0 if debt_ratio <= 75 else 0.0
    components = {
        "profitability": profitability,
        "growth": growth,
        "profit_trend": profit_trend,
        "cash_quality": cash_quality,
        "financial_safety": financial_safety,
    }
    return round(sum(components.values()), 2), components


def parse_fundamental_payload(payload: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    if not payload.get("success"):
        raise MarketDataError(str(payload.get("message") or "东方财富财务接口返回失败"))
    data = ((payload.get("result") or {}).get("data") or [])
    normalized = normalize_symbol(symbol, "A股")
    reports: list[dict[str, Any]] = []
    for row in data:
        report_date = str(row.get("REPORT_DATE") or "")[:10]
        notice_date = str(row.get("NOTICE_DATE") or "")[:10]
        update_date = str(row.get("UPDATE_DATE") or "")[:10]
        effective_date = max(value for value in (notice_date, update_date) if value) if notice_date or update_date else ""
        if not report_date or not effective_date:
            continue
        score, components = calculate_fundamental_score(row)
        reports.append({
            "symbol": normalized,
            "report_date": report_date,
            "effective_date": effective_date,
            "report_type": row.get("REPORT_TYPE") or row.get("REPORT_DATE_NAME"),
            "score": score,
            "roe": _number(row.get("ROEJQ")),
            "revenue_growth": _number(row.get("TOTALOPERATEREVETZ")),
            "profit_growth": _number(row.get("PARENTNETPROFITTZ")),
            "operating_cash": _number(row.get("NETCASH_OPERATE_PK")),
            "net_profit": _number(row.get("PARENTNETPROFIT")),
            "debt_ratio": _number(row.get("ZCFZL")),
            "components": components,
            "raw": row,
            "source": FUNDAMENTAL_SOURCE,
        })
    reports.sort(key=lambda item: (item["effective_date"], item["report_date"]))
    return reports


def fetch_fundamental_reports(symbol: str, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    normalized = normalize_symbol(symbol, "A股")
    if not normalized.endswith((".SS", ".SZ", ".BJ")):
        raise MarketDataError("基本面评分目前仅支持A股")
    own_client = client is None
    http = client or httpx.Client(timeout=20, follow_redirects=True)
    # 本地/Yahoo口径使用 .SS，东方财富财务接口的 SECUCODE 使用 .SH。
    # 若直接传 .SS，沪市 600/601/603/605/688 股票会统一返回空数据。
    eastmoney_secucode = normalized.removesuffix(".SS") + ".SH" if normalized.endswith(".SS") else normalized
    try:
        response = http.get(
            EASTMONEY_FINANCE_URL,
            params={
                "reportName": "RPT_F10_FINANCE_MAINFINADATA",
                "columns": "ALL",
                "filter": f'(SECUCODE="{eastmoney_secucode}")',
                "pageNumber": 1,
                "pageSize": 100,
                "sortTypes": -1,
                "sortColumns": "REPORT_DATE",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
        )
        response.raise_for_status()
        reports = parse_fundamental_payload(response.json(), normalized)
        if not reports:
            raise MarketDataError(f"{normalized} 暂无可用财务指标")
        return reports
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise MarketDataError(f"{normalized} 基本面数据获取失败：{exc}") from exc
    finally:
        if own_client:
            http.close()
