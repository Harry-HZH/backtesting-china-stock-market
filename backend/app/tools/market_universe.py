from __future__ import annotations

from typing import Any

import akshare as ak
import httpx

from .market_data import MarketDataError


# 趋势轮动专用资产池。它们会被行情同步缓存，但不计入普通 A 股股票池。
ETF_ROTATION_SYMBOLS = (
    "510300.SS", "510050.SS", "510500.SS", "512100.SS", "159915.SZ", "159919.SZ",
    "159870.SZ", "515880.SS", "159992.SZ", "512480.SS", "588000.SS", "512690.SS",
    "512880.SS", "511880.SS",
)

# 纯 A 股行业 ETF 轮动复现池（来源：A-Share ETF Rotation V25）。
# 不包含纳指、恒生等 QDII/海外 ETF；代码只作为行情同步和策略专用资产池使用。
ETF_V25_SYMBOLS = (
    "512480.SS", "589100.SS", "515880.SS", "515980.SS", "515230.SS", "159890.SZ", "159779.SZ", "159869.SZ",
    "159857.SZ", "159755.SZ", "515030.SS", "159326.SZ", "562500.SS", "159227.SZ", "512660.SS", "512800.SS",
    "512880.SS", "159892.SZ", "512010.SS", "159992.SZ", "159883.SZ", "561120.SS", "515170.SS", "512690.SS",
    "159980.SZ", "515220.SS", "515210.SS", "561360.SS", "159870.SZ", "159713.SZ", "159934.SZ", "512200.SS",
    "516750.SS", "159867.SZ", "515080.SS",
)

# 14 只纯 A 股 ETF 简化基线（20 日动量 + MA200 + 每 5 个交易日调仓）。
ETF_SIMPLE_ROTATION_SYMBOLS = (
    "510300.SS", "510500.SS", "510050.SS", "159915.SZ", "512480.SH", "515880.SH",
    "512010.SH", "512880.SH", "512690.SS", "159919.SZ", "515080.SH", "159934.SZ",
    "512800.SH", "512100.SS",
)


def fetch_a_share_universe(client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """通过 AKShare 获取沪深京全部 A 股代码与简称；client 参数仅保留调用兼容性。"""
    try:
        frame = ak.stock_info_a_code_name()
        if frame is None or frame.empty or not {"code", "name"}.issubset(frame.columns):
            raise MarketDataError("AKShare 股票列表为空或字段不完整")
        result: list[dict[str, Any]] = []
        for row in frame.to_dict("records"):
            code = str(row.get("code") or "").strip().zfill(6)
            if not code.isdigit() or len(code) != 6:
                continue
            suffix = "BJ" if code.startswith(("4", "8", "92")) else "SS" if code.startswith(("5", "6", "9")) else "SZ"
            exchange = {"SS": "上海证券交易所", "SZ": "深圳证券交易所", "BJ": "北京证券交易所"}[suffix]
            result.append({
                "symbol": f"{code}.{suffix}", "code": code,
                "name": str(row.get("name") or code).strip(), "market": "A股",
                "exchange": exchange, "currency": "CNY",
            })
        if not result:
            raise MarketDataError("AKShare 未返回有效 A 股代码")
        return list({item["symbol"]: item for item in result}.values())
    except MarketDataError:
        raise
    except Exception as exc:
        raise MarketDataError(f"AKShare A 股股票列表获取失败：{exc}") from exc
