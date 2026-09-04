from __future__ import annotations

import json
import math
import random
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

from agno.tools import tool
import akshare as ak
import httpx

from .indicators import rsi as tonghuashun_rsi


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_RAW_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"

ETF_SYMBOLS = {
    "510300.SS", "510050.SS", "510500.SS", "512100.SS", "159915.SZ", "159919.SZ",
    "159870.SZ", "515880.SS", "159992.SZ", "512480.SS", "588000.SS", "512690.SS",
    "512880.SS", "511880.SS",
    # 纯 A 股 ETF 趋势轮动复现池
    "512480.SS", "589100.SS", "515880.SS", "515980.SS", "515230.SS", "159890.SZ", "159779.SZ", "159869.SZ",
    "159857.SZ", "159755.SZ", "515030.SS", "159326.SZ", "562500.SS", "159227.SZ", "512660.SS", "512800.SS",
    "159892.SZ", "512010.SS", "159883.SZ", "561120.SS", "515170.SS", "515220.SS", "515210.SS", "561360.SS",
    "159980.SZ", "159713.SZ", "159934.SZ", "512200.SS", "516750.SS", "159867.SZ", "515080.SS",
}


def _repair_etf_split_jumps(frame: Any) -> Any:
    """修复部分 ETF 历史接口未处理的份额拆分价格断层。"""
    if frame is None or frame.empty:
        return frame
    date_col = "日期" if "日期" in frame.columns else "date"
    close_col = "收盘" if "收盘" in frame.columns else "close"
    if date_col not in frame.columns or close_col not in frame.columns:
        return frame
    frame = frame.copy()
    frame[date_col] = frame[date_col].astype(str)
    frame = frame.sort_values(date_col).reset_index(drop=True)
    price_cols = [column for column in ("开盘", "最高", "最低", "收盘", "open", "high", "low", "close") if column in frame.columns]
    for index in range(1, len(frame)):
        previous = float(frame.iloc[index - 1][close_col] or 0)
        current = float(frame.iloc[index][close_col] or 0)
        if previous <= 0 or current <= 0:
            continue
        ratio = current / previous
        if ratio < 0.65 or ratio > 1.6:
            # 以断层后的价格为基准，缩放断层之前所有 OHLC，保持收益连续。
            for column in price_cols:
                frame.loc[:index - 1, column] = frame.loc[:index - 1, column].astype(float) * ratio
    return frame
TENCENT_PAGE_SIZE = 640
A_SHARE_INDEX_SYMBOLS = {"000300.SS", "000985.SS"}
PERIOD_NOTES = {"3mo": "约 3 个月", "6mo": "约 6 个月", "1y": "约 1 年", "2y": "约 2 年", "5y": "约 5 年"}
ADJUSTMENT_MODES = ("前复权", "后复权", "不复权", "动态前复权")
AKSHARE_PRIMARY_FAILURE_THRESHOLD = 3
AKSHARE_PRIMARY_COOLDOWN_SECONDS = 300.0
_akshare_primary_lock = threading.Lock()
_akshare_primary_failures = 0
_akshare_primary_disabled_until = 0.0


class MarketDataError(RuntimeError):
    pass


def _akshare_primary_available() -> bool:
    """首选接口连续断连后短暂熔断，避免全市场每只股票都重复等待超时。"""
    global _akshare_primary_failures, _akshare_primary_disabled_until
    now = time.monotonic()
    with _akshare_primary_lock:
        if _akshare_primary_disabled_until and now >= _akshare_primary_disabled_until:
            _akshare_primary_failures = 0
            _akshare_primary_disabled_until = 0.0
        return _akshare_primary_disabled_until == 0.0


def _record_akshare_primary_result(succeeded: bool) -> None:
    global _akshare_primary_failures, _akshare_primary_disabled_until
    with _akshare_primary_lock:
        if succeeded:
            _akshare_primary_failures = 0
            return
        _akshare_primary_failures += 1
        if _akshare_primary_failures >= AKSHARE_PRIMARY_FAILURE_THRESHOLD:
            _akshare_primary_disabled_until = time.monotonic() + AKSHARE_PRIMARY_COOLDOWN_SECONDS


def _akshare_exchange(normalized: str) -> tuple[str, str]:
    if normalized.endswith(".SS"):
        return "上海证券交易所", "CNY"
    if normalized.endswith(".SZ"):
        return "深圳证券交易所", "CNY"
    if normalized.endswith(".BJ"):
        return "北京证券交易所", "CNY"
    raise MarketDataError("AKShare A 股接口仅支持沪深京股票代码")


def _parse_akshare_history(
    frame: Any, normalized: str, adjustment: str, is_index: bool = False,
    source_api: str = "stock_zh_a_hist", volume_in_lots: bool = True,
) -> dict[str, Any]:
    if frame is None or frame.empty:
        raise MarketDataError("AKShare 没有返回有效日线行情")
    chinese_columns = {"date": "日期", "open": "开盘", "close": "收盘", "high": "最高", "low": "最低", "volume": "成交量"}
    english_columns = {key: key for key in ("date", "open", "close", "high", "low", "volume")}
    columns = chinese_columns if set(chinese_columns.values()).issubset(frame.columns) else english_columns
    if not set(columns.values()).issubset(frame.columns):
        raise MarketDataError("AKShare 没有返回有效日线行情")
    rows: list[dict[str, Any]] = []
    for item in frame.to_dict("records"):
        try:
            raw_date = item[columns["date"]]
            trade_date = raw_date.date() if hasattr(raw_date, "date") else datetime.fromisoformat(str(raw_date)).date()
            values = {key: float(item[columns[key]]) for key in ("open", "close", "high", "low")}
            volume = float(item[columns["volume"]])
            if not all(math.isfinite(value) for value in (*values.values(), volume)):
                continue
            rows.append({
                "timestamp": int(datetime.combine(trade_date, datetime.min.time(), tzinfo=timezone.utc).timestamp()),
                **values,
                # stock_zh_a_hist 为“手”，stock_zh_a_daily 为“股”；入库统一为股。
                "volume": volume * 100 if volume_in_lots and not is_index else volume,
            })
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    exchange, currency = (("中证全指指数" if normalized.startswith("000985") else "沪深300指数"), "CNY") if is_index else _akshare_exchange(normalized)
    source_adjustment = "指数点位" if is_index else adjustment
    snapshot = _build_snapshot(
        rows, normalized, normalized, normalized, exchange, currency,
        f"AKShare {source_api} {source_adjustment}",
        period_note="指定日期区间",
    )
    snapshot["adjustment"] = source_adjustment
    unit_note = "成交量已由手换算为股" if volume_in_lots and not is_index else "成交量单位为股"
    snapshot["data_note"] = f"AKShare {source_api} 日线，价格口径：{source_adjustment}；{unit_note}。"
    return snapshot


def _fetch_akshare_history(normalized: str, start_date: str, end_date: str, adjust: str = "qfq") -> dict[str, Any]:
    code = normalized.split(".")[0]
    start_compact, end_compact = start_date.replace("-", ""), end_date.replace("-", "")
    adjustment = {"qfq": "前复权", "hfq": "后复权", "": "不复权"}.get(adjust)
    if adjustment is None:
        raise MarketDataError(f"AKShare 不支持的复权参数：{adjust}")
    # stock_zh_a_hist 不覆盖 ETF；ETF 使用 fund_etf_hist_em，三种复权口径字段一致。
    if normalized in ETF_SYMBOLS:
        try:
            try:
                frame = ak.fund_etf_hist_em(
                    symbol=code, period="daily", start_date=start_compact, end_date=end_compact,
                    adjust=adjust,
                )
                source_api = "fund_etf_hist_em"
            except Exception:
                # 东方财富 ETF 接口触发断连时，回退新浪 ETF 日线；新浪不区分复权，
                # 由上层统一按 ETF 因子 1.0 处理。
                prefix = "sh" if normalized.endswith(".SS") else "sz"
                frame = ak.fund_etf_hist_sina(symbol=f"{prefix}{code}")
                date_column = "日期" if "日期" in frame.columns else "date"
                dates = frame[date_column].astype(str).str.slice(0, 10)
                frame = frame[(dates >= start_date) & (dates <= end_date)]
                source_api = "fund_etf_hist_sina"
            frame = _repair_etf_split_jumps(frame)
            return _parse_akshare_history(
                frame, normalized, adjustment, source_api=source_api, volume_in_lots=False,
            )
        except Exception as exc:
            raise MarketDataError(f"AKShare ETF {code} {adjustment}历史行情不可用：{exc}") from exc
    last_error: Exception | None = None
    if _akshare_primary_available():
        for attempt in range(2):
            try:
                if normalized == "000985.SS":
                    # 中证指数官网接口，覆盖 000985 的完整历史；东财的
                    # index_zh_a_hist 在代理/限流时经常返回空数据。
                    frame = ak.stock_zh_index_hist_csindex(
                        symbol=code, start_date=start_compact, end_date=end_compact,
                    )
                    snapshot = _parse_akshare_history(
                        frame, normalized, "指数点位", is_index=True,
                        source_api="stock_zh_index_hist_csindex", volume_in_lots=False,
                    )
                elif normalized in A_SHARE_INDEX_SYMBOLS:
                    frame = ak.index_zh_a_hist(
                        symbol=code, period="daily", start_date=start_compact, end_date=end_compact,
                    )
                    snapshot = _parse_akshare_history(
                        frame, normalized, "指数点位", is_index=True,
                        source_api="index_zh_a_hist", volume_in_lots=False,
                    )
                else:
                    frame = ak.stock_zh_a_hist(
                        symbol=code, period="daily", start_date=start_compact, end_date=end_compact,
                        adjust=adjust, timeout=20,
                    )
                    snapshot = _parse_akshare_history(frame, normalized, adjustment)
                _record_akshare_primary_result(True)
                return snapshot
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.15 + random.uniform(0, 0.1))
        _record_akshare_primary_result(False)
    else:
        last_error = MarketDataError("AKShare 首选接口连续断连，已临时切换备用接口")
    # AKShare 的首选历史接口底层为东方财富；代理或上游断连时，回退到 AKShare 新浪日线。
    # 新浪文档提示高频访问可能限流，因此只在首选接口连续失败后调用一次。
    try:
        if normalized in A_SHARE_INDEX_SYMBOLS:
            frame = ak.stock_zh_index_daily(symbol=f"sh{code}")
            date_column = "date" if "date" in frame.columns else "日期"
            parsed_dates = frame[date_column].astype(str).str.slice(0, 10)
            frame = frame[
                (parsed_dates >= start_date)
                & (parsed_dates <= end_date)
            ]
            return _parse_akshare_history(
                frame, normalized, "指数点位", is_index=True,
                source_api="stock_zh_index_daily", volume_in_lots=False,
            )
        prefix = "sh" if normalized.endswith(".SS") else "sz" if normalized.endswith(".SZ") else "bj"
        if normalized == "689009.SS":
            raise MarketDataError("新浪接口的 CDR 日线不提供复权参数")
        frame = ak.stock_zh_a_daily(
            symbol=f"{prefix}{code}", start_date=start_compact, end_date=end_compact, adjust=adjust,
        )
        return _parse_akshare_history(
            frame, normalized, adjustment, source_api="stock_zh_a_daily", volume_in_lots=False,
        )
    except Exception as fallback_error:
        raise MarketDataError(
            f"AKShare {code} {adjustment}历史行情不可用：首选接口={last_error}；新浪回退={fallback_error}"
        ) from fallback_error


def normalize_symbol(symbol: str, market: str = "自动") -> str:
    raw = symbol.strip().upper().replace(" ", "")
    if not raw:
        raise MarketDataError("股票代码不能为空")
    if "." in raw:
        return raw
    if market == "港股":
        digits = raw.removeprefix("HK")
        if not digits.isdigit():
            raise MarketDataError("港股代码应为数字，例如 00700")
        return f"{digits[-4:].zfill(4)}.HK"
    if market == "A股" or (market == "自动" and raw.isdigit() and len(raw) == 6):
        suffix = "BJ" if raw.startswith(("4", "8", "92")) else "SS" if raw.startswith(("5", "6", "9")) else "SZ"
        return f"{raw}.{suffix}"
    if market == "自动" and raw.isdigit() and 4 <= len(raw) <= 5:
        return f"{raw[-4:].zfill(4)}.HK"
    return raw


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _mean(values: list[float], count: int) -> float | None:
    window = values[-count:]
    return statistics.fmean(window) if window else None


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < 2:
        return None
    return tonghuashun_rsi(closes, period)[-1]


def _max_drawdown(closes: list[float]) -> float | None:
    if not closes:
        return None
    peak = closes[0]
    worst = 0.0
    for price in closes:
        peak = max(peak, price)
        worst = min(worst, price / peak - 1)
    return worst * 100


def _build_snapshot(
    rows: list[dict[str, Any]],
    requested_symbol: str,
    symbol: str,
    name: str,
    exchange: str | None,
    currency: str | None,
    data_source: str,
    period_note: str = "约 6 个月",
) -> dict[str, Any]:
    if len(rows) < 2:
        raise MarketDataError("有效行情数据不足")
    closes = [row["close"] for row in rows]
    returns = [current / previous - 1 for previous, current in zip(closes[:-1], closes[1:]) if previous]
    latest, previous = closes[-1], closes[-2]
    volumes = [float(row["volume"]) for row in rows[-20:] if row["volume"] is not None]
    volatility = statistics.stdev(returns) * math.sqrt(252) * 100 if len(returns) > 1 else None
    return {
        "requested_symbol": requested_symbol,
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "currency": currency,
        "price": _round(latest),
        "change": _round(latest - previous),
        "change_pct": _round((latest / previous - 1) * 100),
        "period_return_pct": _round((latest / closes[0] - 1) * 100),
        "ma5": _round(_mean(closes, 5)),
        "ma20": _round(_mean(closes, 20)),
        "ma60": _round(_mean(closes, 60)),
        "rsi14": _round(_rsi(closes)),
        "annualized_volatility_pct": _round(volatility),
        "max_drawdown_pct": _round(_max_drawdown(closes)),
        "period_high": _round(max(closes)),
        "period_low": _round(min(closes)),
        "avg_volume20": round(statistics.fmean(volumes)) if volumes else None,
        "points": rows,
        "data_source": data_source,
        "adjustment": "前复权",
        "data_note": f"{period_note}日线，统一采用前复权；价格可能延迟，不含完整财务报表与公司公告。",
    }


def parse_chart_payload(
    payload: dict[str, Any], requested_symbol: str, period_note: str = "约 6 个月",
) -> dict[str, Any]:
    chart = payload.get("chart") or {}
    error = chart.get("error")
    if error:
        raise MarketDataError(error.get("description") or "行情接口返回错误")
    results = chart.get("result") or []
    if not results:
        raise MarketDataError("没有找到该股票的行情数据")
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    adjclose = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or quote.get("close") or []
    raw_closes = quote.get("close") or []

    def raw_value(field: str, index: int) -> Any:
        values = quote.get(field) or []
        return values[index] if index < len(values) else None

    rows = []
    for index, timestamp in enumerate(timestamps):
        close = adjclose[index] if index < len(adjclose) else None
        raw_close = raw_closes[index] if index < len(raw_closes) else None
        if close is None or raw_close in (None, 0):
            continue
        adjustment_factor = float(close) / float(raw_close)

        def adjusted(field: str) -> float | None:
            value = raw_value(field, index)
            return round(float(value) * adjustment_factor, 4) if value is not None else None

        rows.append({
            "timestamp": int(timestamp),
            "open": adjusted("open"),
            "high": adjusted("high"),
            "low": adjusted("low"),
            "close": round(float(close), 4),
            "volume": raw_value("volume", index),
        })
    meta = result.get("meta") or {}
    return _build_snapshot(
        rows=rows,
        requested_symbol=requested_symbol,
        symbol=str(meta.get("symbol") or requested_symbol),
        name=str(meta.get("longName") or meta.get("shortName") or meta.get("symbol") or requested_symbol),
        exchange=meta.get("exchangeName") or meta.get("fullExchangeName"),
        currency=meta.get("currency"),
        data_source="Yahoo Finance Chart API（备用）",
        period_note=period_note,
    )


def _eastmoney_secids(normalized: str) -> list[tuple[str, str, str]]:
    if normalized.endswith(".SS"):
        return [(f"1.{normalized.removesuffix('.SS')}", "上海证券交易所", "CNY")]
    if normalized.endswith(".SZ"):
        return [(f"0.{normalized.removesuffix('.SZ')}", "深圳证券交易所", "CNY")]
    if normalized.endswith(".BJ"):
        return [(f"0.{normalized.removesuffix('.BJ')}", "北京证券交易所", "CNY")]
    if normalized.endswith(".HK"):
        code = normalized.removesuffix(".HK").zfill(5)
        return [(f"116.{code}", "香港交易所", "HKD")]
    return [(f"105.{normalized}", "NASDAQ", "USD"), (f"106.{normalized}", "NYSE", "USD")]


def _tencent_symbol(normalized: str) -> tuple[str, str, str]:
    if normalized.endswith(".SS"):
        return f"sh{normalized.removesuffix('.SS')}", "上海证券交易所", "CNY"
    if normalized.endswith(".SZ"):
        return f"sz{normalized.removesuffix('.SZ')}", "深圳证券交易所", "CNY"
    if normalized.endswith(".BJ"):
        return f"bj{normalized.removesuffix('.BJ')}", "北京证券交易所", "CNY"
    raise MarketDataError("腾讯行情当前仅用于 A 股代码")


def parse_tencent_payload(
    payload: dict[str, Any], requested_symbol: str, preferred_series: str = "qfqday",
) -> tuple[list[dict[str, Any]], str, str]:
    """解析腾讯 qfqday；北交所无 qfqday 时保留接口原始 day 并明确标注。"""
    normalized = normalize_symbol(requested_symbol)
    tencent_code, _, _ = _tencent_symbol(normalized)
    data = (payload.get("data") or {}).get(tencent_code)
    if not isinstance(data, dict):
        raise MarketDataError("腾讯财经没有返回该代码的日线行情")
    lines = data.get(preferred_series)
    adjustment = {"day": "不复权", "hfqday": "后复权"}.get(preferred_series, "前复权")
    source = {
        "day": "腾讯财经不复权日K接口",
        "hfqday": "腾讯财经后复权日K接口",
    }.get(preferred_series, "腾讯财经前复权日K接口")
    if not lines:
        lines = data.get("day") if preferred_series != "day" else None
        if normalized in A_SHARE_INDEX_SYMBOLS:
            adjustment = "指数点位（无需复权）"
            source = "腾讯财经指数日K接口"
        else:
            adjustment = "未复权（腾讯未提供前复权序列）"
            source = "腾讯财经日K接口（未提供前复权序列）"
    if not lines:
        raise MarketDataError("腾讯财经没有返回该代码的日线行情")
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not isinstance(line, list) or len(line) < 6:
            continue
        try:
            rows.append({
                "timestamp": int(datetime.fromisoformat(str(line[0])).replace(tzinfo=timezone.utc).timestamp()),
                "open": float(line[1]),
                "close": float(line[2]),
                "high": float(line[3]),
                "low": float(line[4]),
                "volume": float(line[5]),
            })
        except (TypeError, ValueError):
            continue
    if not rows:
        raise MarketDataError("腾讯财经返回的日线行情无法解析")
    return rows, adjustment, source


def _fetch_tencent(
    normalized: str,
    http: httpx.Client,
    start_date: str,
    end_date: str,
    use_stdlib_transport: bool = False,
    series: str = "qfq",
) -> dict[str, Any]:
    tencent_code, exchange, currency = _tencent_symbol(normalized)
    requested_start = date.fromisoformat(start_date)
    page_end = date.fromisoformat(end_date)
    rows_by_date: dict[str, dict[str, Any]] = {}
    name = normalized
    adjustment = "前复权"
    source = "腾讯财经前复权日K接口"
    last_error: Exception | None = None

    # 2020 年至今通常只需 3～4 页；64 页上限用于防御接口重复返回同一页。
    for _page in range(64):
        suffix = ",qfq" if series == "qfq" else ",hfq" if series == "hfq" else ""
        params = {"param": f"{tencent_code},day,{start_date},{page_end.isoformat()},{TENCENT_PAGE_SIZE}{suffix}"}
        endpoint = TENCENT_RAW_KLINE_URL if series == "raw" else TENCENT_KLINE_URL
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://gu.qq.com/",
        }
        page_rows: list[dict[str, Any]] | None = None
        page_adjustment = ""
        page_source = ""
        expected_adjustment = {"qfq": "前复权", "hfq": "后复权"}.get(series, "不复权")
        for attempt in range(4):
            try:
                if use_stdlib_transport:
                    url = f"{endpoint}?{urllib.parse.urlencode(params)}"
                    request = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(request, timeout=30) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                else:
                    response = http.get(endpoint, params=params, headers=headers)
                    response.raise_for_status()
                    payload = response.json()
                # 腾讯限流时经常仍返回 HTTP 200，但 data 中没有请求的代码。
                # 解析也必须放在重试循环中，不能把这种临时空响应当成永久失败。
                page_rows, page_adjustment, page_source = parse_tencent_payload(
                    payload, normalized, {"qfq": "qfqday", "hfq": "hfqday"}.get(series, "day"),
                )
                if page_adjustment != expected_adjustment and normalized not in A_SHARE_INDEX_SYMBOLS:
                    raise MarketDataError("腾讯财经暂未返回请求的复权序列")
                break
            except (httpx.HTTPError, urllib.error.URLError, OSError, ValueError, MarketDataError) as exc:
                last_error = exc
                page_rows = None
                if attempt < 3:
                    time.sleep((0.35 * (2 ** attempt)) + random.uniform(0.0, 0.2))
        if page_rows is None:
            raise MarketDataError(f"腾讯财经行情不可用：{last_error}")
        adjustment, source = page_adjustment, page_source
        qt = ((payload.get("data") or {}).get(tencent_code) or {}).get("qt") or {}
        quote = qt.get(tencent_code) if isinstance(qt, dict) else None
        if isinstance(quote, list) and len(quote) > 1 and quote[1]:
            name = str(quote[1])
        earliest: date | None = None
        for row in page_rows:
            row_date = datetime.fromtimestamp(int(row["timestamp"]), timezone.utc).date()
            rows_by_date[row_date.isoformat()] = row
            earliest = row_date if earliest is None else min(earliest, row_date)
        if earliest is None or earliest <= requested_start or len(page_rows) < TENCENT_PAGE_SIZE:
            break
        next_end = earliest - timedelta(days=1)
        if next_end >= page_end:
            raise MarketDataError("腾讯财经分页返回了重复日期，已停止读取")
        page_end = next_end

    rows = [rows_by_date[key] for key in sorted(rows_by_date) if start_date <= key <= end_date]
    if len(rows) < 2:
        raise MarketDataError(f"{start_date} 至 {end_date} 的有效行情不足")
    snapshot = _build_snapshot(
        rows, normalized, normalized, name, exchange, currency, source,
        period_note=f"{start_date} 至 {end_date}",
    )
    snapshot["adjustment"] = adjustment
    snapshot["data_note"] = f"{start_date} 至 {end_date} 日线，复权口径：{adjustment}；价格可能延迟。"
    return snapshot


def fetch_stock_history_with_adjustments(
    symbol: str,
    market: str = "自动",
    start_date: str = "2020-01-01",
    end_date: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """通过 AKShare 同时获取 A 股前复权、不复权与后复权行情，并生成累计复权因子。"""
    normalized = normalize_symbol(symbol, market)
    end_date = end_date or datetime.now(timezone.utc).date().isoformat()
    if not normalized.endswith((".SS", ".SZ", ".BJ")):
        return fetch_stock_history(symbol, market, start_date, end_date, client)
    try:
        qfq = _fetch_akshare_history(normalized, start_date, end_date, "qfq")
        if normalized in ETF_SYMBOLS:
            # ETF 的 fund_etf_hist_em 在部分时段只稳定提供 qfq；ETF 通常不存在
            # 个股式拆分/分红复权链条，统一以前复权价格作为三种口径，因子固定为 1。
            points = [{
                **row,
                **{f"raw_{key}": row[key] for key in ("open", "high", "low", "close")},
                **{f"hfq_{key}": row[key] for key in ("open", "high", "low", "close")},
                "adjustment_factor": 1.0,
            } for row in qfq["points"]]
            return {
                **qfq,
                "points": points,
                "data_source": "AKShare fund_etf_hist_em 前复权（ETF统一价格口径）",
                "has_adjustment_factors": True,
                "data_note": f"{start_date} 至 {end_date} ETF 日线；AKShare ETF 原始/后复权不可稳定提供，统一以前复权价格及因子1.0保存。",
            }
        if normalized in A_SHARE_INDEX_SYMBOLS:
            points = [{
                **row,
                **{f"raw_{key}": row[key] for key in ("open", "high", "low", "close")},
                **{f"hfq_{key}": row[key] for key in ("open", "high", "low", "close")},
                "adjustment_factor": 1.0,
            } for row in qfq["points"]]
            return {**qfq, "points": points, "has_adjustment_factors": True}
        raw = _fetch_akshare_history(normalized, start_date, end_date, "")
        hfq = _fetch_akshare_history(normalized, start_date, end_date, "hfq")
        raw_by_timestamp = {int(row["timestamp"]): row for row in raw["points"]}
        hfq_by_timestamp = {int(row["timestamp"]): row for row in hfq["points"]}
        points = []
        pending_timestamps: list[int] = []
        for row in qfq["points"]:
            raw_row = raw_by_timestamp.get(int(row["timestamp"]))
            hfq_row = hfq_by_timestamp.get(int(row["timestamp"]))
            if raw_row is None or hfq_row is None or float(raw_row.get("close") or 0) <= 0:
                pending_timestamps.append(int(row["timestamp"]))
                continue
            factor = float(hfq_row["close"]) / float(raw_row["close"])
            points.append({
                **row,
                **{f"raw_{key}": float(raw_row[key]) for key in ("open", "high", "low", "close")},
                **{f"hfq_{key}": float(hfq_row[key]) for key in ("open", "high", "low", "close")},
                "adjustment_factor": factor,
            })
        paired = len(points)
        if paired < 2:
            raise MarketDataError("AKShare 前复权、不复权与后复权行情无法按交易日对齐")
        # 三种复权口径在收盘后的更新时间可能存在短暂差异。只允许舍弃尚未补齐的末尾日期；
        # 中间交易日缺失意味着数据本身存在空洞，必须拒绝写库。
        latest_paired_timestamp = int(points[-1]["timestamp"])
        internal_missing = [timestamp for timestamp in pending_timestamps if timestamp < latest_paired_timestamp]
        if internal_missing:
            raise MarketDataError(
                f"AKShare 三套复权行情存在内部日期缺口：前复权 {len(qfq['points'])} 根，"
                f"共同完整 {paired} 根，内部缺失 {len(internal_missing)} 根"
            )
        pending_dates = [
            datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
            for timestamp in pending_timestamps
        ]
        return {
            **qfq,
            "points": points,
            "data_source": "AKShare 前复权/不复权/后复权日线",
            "has_adjustment_factors": True,
            "pending_adjustment_dates": pending_dates,
            "data_note": (
                f"{start_date} 至 {end_date} 日线，同时保存前复权、不复权、后复权价格及累计复权因子。"
                + (f"末尾 {len(pending_dates)} 个交易日复权数据尚未齐全，已留待下次增量同步。" if pending_dates else "")
            ),
        }
    except MarketDataError as exc:
        raise MarketDataError(f"AKShare 复权行情不可用：{exc}") from exc


def apply_price_adjustment(snapshot: dict[str, Any], mode: str) -> dict[str, Any]:
    """从同一份前复权/原始行情生成指定回测价格口径。"""
    if mode not in ADJUSTMENT_MODES:
        raise MarketDataError(f"不支持的复权模式：{mode}")
    if mode == "前复权":
        return {**snapshot, "adjustment": mode}

    combined = [*(snapshot.get("indicator_warmup_points") or []), *(snapshot.get("points") or [])]
    factors = [float(row["adjustment_factor"]) for row in combined if row.get("adjustment_factor") is not None]
    if not combined or any(row.get("raw_close") is None for row in combined):
        raise MarketDataError(f"{mode}需要不复权行情，请先完成全量行情同步")
    if mode in {"后复权", "动态前复权"} and (
        len(factors) != len(combined)
        or any(row.get("hfq_close") is None for row in combined)
        or not all(math.isfinite(factor) and factor > 0 for factor in factors)
    ):
        raise MarketDataError(f"{mode}需要完整且为正的后复权累计因子，请重新执行全量行情同步")

    def convert(row: dict[str, Any]) -> dict[str, Any]:
        if mode == "不复权":
            prices = {key: float(row[f"raw_{key}"]) for key in ("open", "high", "low", "close")}
        elif mode == "后复权":
            prices = {key: float(row[f"hfq_{key}"]) for key in ("open", "high", "low", "close")}
        else:
            # 后复权是只随已发生公司行为向前累积的正价格坐标。以回测末日因子做公共缩放，
            # 可得到末日价格等于真实价格的动态前复权序列；公共缩放不改变比例型指标和收益。
            end_factor = float((snapshot.get("points") or combined)[-1]["adjustment_factor"])
            prices = {key: float(row[f"hfq_{key}"]) / end_factor for key in ("open", "high", "low", "close")}
        return {**row, **{key: round(value, 6) for key, value in prices.items()}}

    note = {
        "不复权": "使用真实除权价格，不补偿现金分红和送转造成的价格缺口",
        "后复权": "使用 AKShare 后复权累计价格，只向前累积已经发生的公司行为",
        "动态前复权": "以 AKShare 后复权累计序列为价格坐标并按末日因子归一化；未来公司行为不会改变历史信号比例",
    }[mode]
    return {
        **snapshot,
        "points": [convert(row) for row in snapshot.get("points") or []],
        "indicator_warmup_points": [convert(row) for row in snapshot.get("indicator_warmup_points") or []],
        "adjustment": mode,
        "adjustment_note": note,
        "dynamic_adjustment": mode == "动态前复权",
    }


def parse_eastmoney_payload(
    payload: dict[str, Any], requested_symbol: str, exchange: str, currency: str,
) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("klines"):
        raise MarketDataError("东方财富没有返回该代码的日线行情")
    rows = []
    for line in data["klines"]:
        fields = str(line).split(",")
        if len(fields) < 6:
            continue
        rows.append({
            "timestamp": int(datetime.fromisoformat(fields[0]).replace(tzinfo=timezone.utc).timestamp()),
            "open": float(fields[1]),
            "close": float(fields[2]),
            "high": float(fields[3]),
            "low": float(fields[4]),
            "volume": float(fields[5]),
        })
    return _build_snapshot(
        rows=rows,
        requested_symbol=requested_symbol,
        symbol=str(data.get("code") or requested_symbol),
        name=str(data.get("name") or data.get("code") or requested_symbol),
        exchange=exchange,
        currency=currency,
        data_source="东方财富公开行情接口",
    )


def _fetch_eastmoney(
    normalized: str,
    http: httpx.Client,
    period: str = "6mo",
    start_date: str | None = None,
    end_date: str | None = None,
    use_stdlib_transport: bool = False,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for secid, exchange, currency in _eastmoney_secids(normalized):
        for attempt in range(3):
            try:
                params = {
                    "secid": secid, "klt": 101, "fqt": 1, "iscca": 1,
                    "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                    "lmt": {"3mo": 90, "6mo": 180, "1y": 260, "2y": 500, "5y": 1250, "max": 10000}.get(period, 180),
                    "beg": start_date.replace("-", "") if start_date else 0,
                    "end": end_date.replace("-", "") if end_date else 20500101,
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                }
                headers = {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                    "Accept": "application/json,text/plain,*/*",
                    "Referer": "https://quote.eastmoney.com/",
                    "Connection": "close",
                }
                if use_stdlib_transport:
                    url = f"{EASTMONEY_KLINE_URL}?{urllib.parse.urlencode(params)}"
                    request = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(request, timeout=30) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                else:
                    response = http.get(EASTMONEY_KLINE_URL, params=params, headers=headers)
                    response.raise_for_status()
                    payload = response.json()
                snapshot = parse_eastmoney_payload(payload, normalized, exchange, currency)
                snapshot["data_note"] = snapshot["data_note"].replace(
                    "约 6 个月", PERIOD_NOTES.get(period, "约 6 个月"),
                )
                return snapshot
            except (httpx.HTTPError, urllib.error.URLError, OSError, MarketDataError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))
    raise MarketDataError(f"东方财富行情不可用：{last_error}")


def fetch_stock_snapshot(
    symbol: str,
    market: str = "自动",
    client: httpx.Client | None = None,
    period: str = "6mo",
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol, market)
    owns_client = client is None
    # 保留系统代理设置；部分网络环境下直连行情源会被服务端断开。
    http = client or httpx.Client(timeout=20, follow_redirects=True, trust_env=True)
    try:
        if normalized.endswith((".SS", ".SZ", ".BJ")):
            end = date.today()
            days = {"3mo": 140, "6mo": 280, "1y": 500, "2y": 900, "5y": 2100}.get(period, 280)
            return _fetch_akshare_history(
                normalized, (end - timedelta(days=days)).isoformat(), end.isoformat(), "qfq",
            )
        try:
            return _fetch_eastmoney(normalized, http, period, use_stdlib_transport=owns_client)
        except MarketDataError as eastmoney_error:
            raise MarketDataError(f"东方财富行情不可用；系统已关闭 Yahoo 回退以保证统一口径：{eastmoney_error}") from eastmoney_error
    finally:
        if owns_client:
            http.close()


def fetch_stock_history(
    symbol: str,
    market: str = "自动",
    start_date: str = "2020-01-01",
    end_date: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """按日期获取完整日线历史；沪深京 A 股统一使用 AKShare 前复权。"""
    normalized = normalize_symbol(symbol, market)
    end_date = end_date or datetime.now(timezone.utc).date().isoformat()
    try:
        start = datetime.fromisoformat(start_date).date()
        end = datetime.fromisoformat(end_date).date()
    except ValueError as exc:
        raise MarketDataError("日期格式必须为 YYYY-MM-DD") from exc
    if start > end:
        raise MarketDataError("开始日期不能晚于结束日期")
    owns_client = client is None
    http = client or httpx.Client(timeout=30, follow_redirects=True, trust_env=True)
    try:
        if normalized.endswith((".SS", ".SZ", ".BJ")):
            return _fetch_akshare_history(normalized, start_date, end_date, "qfq")
        try:
            snapshot = _fetch_eastmoney(normalized, http, "max", start_date, end_date, use_stdlib_transport=owns_client)
        except MarketDataError as eastmoney_error:
            raise MarketDataError(f"东方财富历史行情不可用；系统已关闭 Yahoo 回退以保证统一口径：{eastmoney_error}") from eastmoney_error
        filtered = [
            row for row in snapshot["points"]
            if start <= datetime.fromtimestamp(int(row["timestamp"]), timezone.utc).date() <= end
        ]
        if len(filtered) < 2:
            raise MarketDataError(f"{start_date} 至 {end_date} 的有效行情不足")
        return {
            **snapshot,
            "points": filtered,
            "data_note": f"{start_date} 至 {end_date} 日线，统一采用前复权；价格可能延迟。",
        }
    finally:
        if owns_client:
            http.close()


@tool(name="get_stock_snapshot", description="获取 A 股、港股或美股近 6 个月行情与技术指标。")
def get_stock_snapshot(symbol: str, market: str = "自动") -> dict[str, Any]:
    return fetch_stock_snapshot(symbol, market)
