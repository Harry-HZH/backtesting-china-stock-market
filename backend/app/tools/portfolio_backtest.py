from __future__ import annotations

from collections import defaultdict
import math
import re
import statistics
from typing import Any

from .backtest import run_backtest
from .factor_analysis import FACTOR_IDS, calculate_factor_values
from .indicators import sma


def _report_at(reports: list[dict[str, Any]], timestamp: int) -> dict[str, Any] | None:
    from datetime import datetime, timezone
    day = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
    eligible = [item for item in reports if str(item.get("effective_date") or "") <= day]
    return eligible[-1] if eligible else None


def build_dividend_quality_candidates(
    snapshot: dict[str, Any], reports: list[dict[str, Any]], dividends: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """月末生成红利质量动量候选，次月首个交易日开盘成交。"""
    from datetime import datetime, timedelta, timezone
    points = snapshot.get("points") or []
    warmup = snapshot.get("indicator_warmup_points") or []
    combined = [*warmup, *points]
    offset = len(warmup)
    name = str(snapshot.get("name") or "")
    code = str(snapshot.get("symbol") or "").split(".")[0]
    if "ST" in name.upper() or "退" in name or code.startswith(("300", "301", "688", "689", "4", "8", "92")):
        return []
    if len(combined) < 250 or len(points) < 2:
        return []
    month_ends = [
        index for index in range(len(points) - 1)
        if datetime.fromtimestamp(int(points[index]["timestamp"]), timezone.utc).date().month
        != datetime.fromtimestamp(int(points[index + 1]["timestamp"]), timezone.utc).date().month
    ]
    candidates: list[dict[str, Any]] = []
    closes = [float(row["close"]) for row in combined]
    for local_index in month_ends:
        index = offset + local_index
        if index < 55:
            continue
        signal = points[local_index]
        signal_timestamp = int(signal["timestamp"])
        report = _report_at(reports, signal_timestamp)
        if report is None:
            continue
        raw = report.get("raw") or {}
        try:
            roe = float(report.get("roe"))
            roa = float(raw.get("ZZCJLL"))
            revenue_growth = float(report.get("revenue_growth"))
            profit_growth = float(report.get("profit_growth"))
            operating_cash = float(report.get("operating_cash"))
            net_profit = float(report.get("net_profit"))
            debt_ratio = float(report.get("debt_ratio"))
            eps = float(raw.get("EPSJB"))
            raw_close = float(signal.get("raw_close") or signal["close"])
        except (TypeError, ValueError):
            continue
        cash_quality = operating_cash / net_profit if net_profit > 0 else -1
        pe = raw_close / eps if eps > 0 else -1
        if not (roe > 8 and roa > 3 and cash_quality > 0.8 and debt_ratio < 70 and revenue_growth > -5 and profit_growth > -10 and pe > 0):
            continue
        signal_day = datetime.fromtimestamp(signal_timestamp, timezone.utc).date()
        dividend_start = (signal_day - timedelta(days=365)).isoformat()
        cash_per_share = sum(
            float(item["cash_per_10"]) / 10
            for item in dividends
            if dividend_start <= str(item.get("ex_date") or "") <= signal_day.isoformat()
            and str(item.get("announcement_date") or "") <= signal_day.isoformat()
        )
        paid_dividend_years = {
            str(item.get("ex_date"))[:4]
            for item in dividends
            if (signal_day - timedelta(days=730)).isoformat() <= str(item.get("ex_date") or "") <= signal_day.isoformat()
            and str(item.get("announcement_date") or "") <= signal_day.isoformat()
            and float(item.get("cash_per_10") or 0) > 0
        }
        dividend_yield = cash_per_share / raw_close if raw_close > 0 else 0
        if dividend_yield < 0.02 or len(paid_dividend_years) < 2:
            continue
        momentum = closes[index - 5] / closes[index - 55] - 1 if closes[index - 55] > 0 else -1
        if momentum < -0.10:
            continue
        entry_index = local_index + 1
        next_month_end = next((pos for pos in month_ends if pos > local_index), len(points) - 1)
        scheduled_exit = min(next_month_end + 1, len(points) - 1)
        entry_price = float(points[entry_index]["open"])
        exit_index = scheduled_exit
        exit_reason = "月度调仓退出"
        for pos in range(entry_index, scheduled_exit + 1):
            if float(points[pos]["close"]) <= entry_price * 0.85:
                exit_index = min(pos + 1, len(points) - 1)
                exit_reason = "收盘亏损达到15%，下一交易日开盘止损"
                break
        candidates.append({
            "symbol": snapshot["symbol"], "name": snapshot["name"],
            "entry_timestamp": int(points[entry_index]["timestamp"]), "entry_price": entry_price,
            "entry_volume": float(points[entry_index].get("volume") or 0),
            "exit_timestamp": int(points[exit_index]["timestamp"]),
            "exit_price": float(points[exit_index]["open"] if exit_index < len(points)-1 else points[exit_index]["close"]),
            "exit_volume": float(points[exit_index].get("volume") or 0),
            "score": 0, "dividend_yield": dividend_yield, "quality_score": float(report.get("score") or 0),
            "valuation_score": 1 / pe, "momentum_50": momentum, "average_turnover": 0,
            "opening_gap_pct": 0, "fundamental_score": report.get("score"), "signal_timestamp": signal_timestamp,
            "entry_reason": f"月末红利质量动量入选：股息率={dividend_yield*100:.2f}%，ROE={roe:.2f}%，ROA={roa:.2f}%，PE={pe:.2f}，50日动量={momentum*100:.2f}%",
            "exit_reason": exit_reason,
        })
    return candidates


def build_multifactor_candidates(
    snapshot: dict[str, Any],
    reports: list[dict[str, Any]],
    factors: list[str],
    max_opening_gap_pct: float = 3,
    minimum_turnover: float = 0,
    require_above_ma200: bool = False,
    require_ma200_rising: bool = False,
) -> list[dict[str, Any]]:
    """月末计算因子，次月首个交易日开盘买入，再下个月首日换仓。"""
    from datetime import datetime, timezone

    selected_factors = [factor for factor in factors if factor in FACTOR_IDS]
    if not selected_factors:
        return []
    points = snapshot.get("points") or []
    warmup = snapshot.get("indicator_warmup_points") or []
    combined = [*warmup, *points]
    offset = len(warmup)
    name = str(snapshot.get("name") or "")
    if "ST" in name.upper() or "退" in name or len(combined) < 82 or len(points) < 3:
        return []
    month_ends = [
        index for index in range(len(points) - 1)
        if datetime.fromtimestamp(int(points[index]["timestamp"]), timezone.utc).strftime("%Y-%m")
        != datetime.fromtimestamp(int(points[index + 1]["timestamp"]), timezone.utc).strftime("%Y-%m")
    ]
    ma200_values = sma([float(row["close"]) for row in combined], 200)
    candidates: list[dict[str, Any]] = []
    for position, local_index in enumerate(month_ends):
        index = offset + local_index
        if index < 60:
            continue
        signal = points[local_index]
        signal_timestamp = int(signal["timestamp"])
        report = _report_at(reports, signal_timestamp) if reports else None
        values = calculate_factor_values(combined, index, report)
        factor_values = {factor: values.get(factor) for factor in selected_factors}
        if any(value is None or not math.isfinite(float(value)) for value in factor_values.values()):
            continue
        entry_index = local_index + 1
        next_month_end = month_ends[position + 1] if position + 1 < len(month_ends) else len(points) - 1
        exit_index = min(next_month_end + 1, len(points) - 1)
        if exit_index <= entry_index:
            continue
        signal_close = float(signal["close"])
        entry_price = float(points[entry_index]["open"])
        opening_gap_pct = (entry_price / signal_close - 1) * 100 if signal_close > 0 else float("inf")
        if opening_gap_pct > max_opening_gap_pct + 1e-9:
            continue
        turnover_rows = points[max(0, local_index - 19):local_index + 1]
        average_turnover = statistics.fmean(
            float(row.get("volume") or 0) * float(row["close"]) for row in turnover_rows
        ) if turnover_rows else 0
        if average_turnover < minimum_turnover:
            continue
        signal_ma200 = ma200_values[index] if index < len(ma200_values) else None
        if require_above_ma200 and (signal_ma200 is None or signal_close <= float(signal_ma200)):
            continue
        if require_ma200_rising:
            prior_ma200 = ma200_values[index - 5] if index >= 5 else None
            if signal_ma200 is None or prior_ma200 is None or float(signal_ma200) <= float(prior_ma200):
                continue
        candidates.append({
            "symbol": snapshot["symbol"], "name": snapshot["name"],
            "entry_timestamp": int(points[entry_index]["timestamp"]), "entry_price": entry_price,
            "entry_volume": float(points[entry_index].get("volume") or 0),
            "exit_timestamp": int(points[exit_index]["timestamp"]),
            "exit_price": float(points[exit_index]["open"] if exit_index < len(points) - 1 else points[exit_index]["close"]),
            "exit_volume": float(points[exit_index].get("volume") or 0),
            "score": 0.0, "factor_values": {key: float(value) for key, value in factor_values.items()},
            "average_turnover": average_turnover, "opening_gap_pct": opening_gap_pct,
            "fundamental_score": report.get("score") if report else None,
            "signal_timestamp": signal_timestamp,
            "entry_reason": "月末多因子横截面入选",
            "exit_reason": "月度调仓退出",
        })
    return candidates


def build_trend_rotation_candidates(snapshot: dict[str, Any], benchmark_snapshot: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """固定参数的多资产趋势轮动：12-1 动量 + MA200 趋势过滤 + 低波动加分。

    信号在月末收盘形成，下一交易日开盘进入，下一月调仓退出。参数刻意固定，
    避免在前端暴露通用选股参数造成过拟合。
    """
    from datetime import datetime, timezone
    points = snapshot.get("points") or []
    warmup = snapshot.get("indicator_warmup_points") or []
    combined = [*warmup, *points]
    if len(combined) < 270 or len(points) < 2:
        return []
    closes = [float(row["close"]) for row in combined]
    ma200 = sma(closes, 200)
    month_ends: list[int] = []
    benchmark_closes = [float(row["close"]) for row in (benchmark_snapshot or {}).get("points") or []]
    benchmark_timestamps = [int(row["timestamp"]) for row in (benchmark_snapshot or {}).get("points") or []]
    benchmark_by_timestamp = dict(zip(benchmark_timestamps, benchmark_closes))
    for index in range(len(points) - 1):
        left = datetime.fromtimestamp(int(points[index]["timestamp"]), timezone.utc)
        right = datetime.fromtimestamp(int(points[index + 1]["timestamp"]), timezone.utc)
        if (left.year, left.month) != (right.year, right.month):
            month_ends.append(index)
    candidates: list[dict[str, Any]] = []
    for position, local_index in enumerate(month_ends[:-1]):
        combined_index = len(warmup) + local_index
        if combined_index < 253 or local_index + 1 >= len(points):
            continue
        close = closes[combined_index]
        trend = ma200[combined_index]
        if not trend or close <= 0:
            continue
        benchmark_close = benchmark_by_timestamp.get(int(points[local_index]["timestamp"]))
        benchmark_ma200 = None
        if benchmark_close is not None:
            benchmark_history = [value for timestamp, value in zip(benchmark_timestamps, benchmark_closes) if timestamp <= int(points[local_index]["timestamp"])]
            if len(benchmark_history) >= 200:
                benchmark_ma200 = statistics.fmean(benchmark_history[-200:])
        returns_252 = close / closes[combined_index - 252] - 1
        returns_126 = close / closes[combined_index - 126] - 1
        returns_63 = close / closes[combined_index - 63] - 1
        recent_returns = [closes[i] / closes[i - 1] - 1 for i in range(max(1, combined_index - 59), combined_index + 1) if closes[i - 1] > 0]
        volatility = statistics.pstdev(recent_returns) * math.sqrt(252) if len(recent_returns) > 1 else 1.0
        is_cash_proxy = str(snapshot.get("symbol") or "") == "511880.SS"
        if ((close < trend) or (benchmark_ma200 is not None and benchmark_close < benchmark_ma200)) and not is_cash_proxy:
            continue
        momentum = 0.5 * returns_252 + 0.3 * returns_126 + 0.2 * returns_63
        score = 0.0 if is_cash_proxy else momentum - 0.15 * volatility
        entry_index = local_index + 1
        next_month_end = month_ends[position + 1]
        exit_index = min(next_month_end + 1, len(points) - 1)
        if exit_index <= entry_index:
            continue
        candidates.append({
            "symbol": snapshot["symbol"], "name": snapshot.get("name") or snapshot["symbol"],
            "entry_timestamp": int(points[entry_index]["timestamp"]),
            "entry_price": float(points[entry_index].get("open") or points[entry_index]["close"]),
            "entry_volume": float(points[entry_index].get("volume") or 0),
            "exit_timestamp": int(points[exit_index]["timestamp"]),
            "exit_price": float(points[exit_index].get("open") or points[exit_index]["close"]),
            "exit_volume": float(points[exit_index].get("volume") or 0),
            "score": score, "momentum_12_1": returns_252, "volatility_60": volatility,
            "signal_timestamp": int(points[local_index]["timestamp"]),
            "entry_reason": f"趋势轮动：MA200上方；12-1动量={returns_252:.2%}；年化波动={volatility:.2%}",
            "exit_reason": "月度趋势轮动调仓",
        })
    return candidates


_PURE_A_ETF_GROUPS = {
    "512480": "半导体芯片", "589100": "半导体芯片", "515880": "通信光模块",
    "515980": "AI数字", "515230": "AI数字", "159890": "AI数字", "159779": "AI数字", "159869": "AI数字",
    "159857": "新能源", "159755": "新能源", "515030": "新能源", "159326": "新能源",
    "562500": "高端制造", "159227": "高端制造", "512660": "高端制造",
    "512800": "大金融", "512880": "大金融", "159892": "大金融",
    "512010": "医疗", "159992": "医疗", "159883": "医疗",
    "561120": "大消费", "515170": "大消费", "512690": "大消费",
    "159980": "周期资源", "515220": "周期资源", "515210": "周期资源", "561360": "周期资源",
    "159870": "周期资源", "159713": "周期资源", "159934": "周期资源",
    "512200": "地产链", "516750": "地产链", "159867": "农业", "515080": "红利",
}


def _pure_a_etf_group(symbol: str) -> str:
    return _PURE_A_ETF_GROUPS.get(str(symbol).split(".")[0], "其他")


def build_pure_a_etf_candidates(
    snapshot: dict[str, Any],
    variant: str,
    benchmark_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """复现公开的纯 A 股 ETF 轮动基线。

    ``v25`` 使用周频多周期动量/波动率评分，``simple14`` 使用 20 日动量和
    MA200 过滤。信号只读取观察日前数据，下一交易日开盘执行；横截面排名和
    最多持仓数量由资金池选择器完成。
    """
    from datetime import datetime, timezone

    points = snapshot.get("points") or []
    warmup = snapshot.get("indicator_warmup_points") or []
    combined = [*warmup, *points]
    offset = len(warmup)
    if len(combined) < (210 if variant == "simple14" else 130) or len(points) < 10:
        return []
    closes = [float(row["close"]) for row in combined]
    volumes = [float(row.get("volume") or 0) for row in combined]
    ma200 = sma(closes, 200)
    # 原项目固定周五收盘生成信号、下周一开盘执行，而不是从回测起点
    # 每 5 根 K 线滚动一次。节假日时仍取周五之后的第一个交易日成交。
    signal_indices = [
        index for index, row in enumerate(points[:-1])
        if datetime.fromtimestamp(int(row["timestamp"]), timezone.utc).weekday() == 4
    ]
    result: list[dict[str, Any]] = []
    for local_index in signal_indices:
        index = offset + local_index
        lookback = 20 if variant == "simple14" else 60
        if index < max(lookback, 120) or local_index + 1 >= len(points):
            continue
        close = closes[index]
        if close <= 0 or ma200[index] is None:
            continue
        # 公开简化版要求站上 MA200；V25 只在黄金门控关闭时允许黄金作为防御资产。
        if variant == "simple14" and close <= float(ma200[index]):
            continue
        ret20 = close / closes[index - 20] - 1
        ret60 = close / closes[index - 60] - 1
        ret120 = close / closes[index - 120] - 1
        recent = [closes[i] / closes[i - 1] - 1 for i in range(max(1, index - 39), index + 1) if closes[i - 1] > 0]
        volatility = statistics.pstdev(recent) * math.sqrt(252) if len(recent) > 1 else 1.0
        # V25 评分：4 周动量 + 风险调整动量 + 加速度，避免使用未来数据。
        score = ret20 if variant == "simple14" else (
            0.20 * ret20 + 0.30 * ret60 + 0.50 * ret120
            + 0.40 * (ret20 / max(volatility, 0.10))
            + 0.10 * (ret20 - ret60)
            - 0.05 * ret20
        )
        entry_index = local_index + 1
        next_signal = next((item for item in signal_indices if item > local_index), None)
        if next_signal is None:
            continue
        exit_index = min(next_signal + 1, len(points) - 1)
        if exit_index <= entry_index:
            continue
        market_gate = True
        benchmark_points = (benchmark_snapshot or {}).get("points") or []
        if benchmark_points:
            signal_timestamp = int(points[local_index]["timestamp"])
            benchmark_history = [
                float(row["close"]) for row in benchmark_points
                if int(row["timestamp"]) <= signal_timestamp
            ]
            # GitHub 使用中证全指 50 周均线；本地没有中证全指时，明确使用
            # 沪深300作为可审计代理，而不是偷偷使用未来数据。
            if len(benchmark_history) >= 250:
                market_gate = benchmark_history[-1] > statistics.fmean(benchmark_history[-250:])
        entry = points[entry_index]
        exit_row = points[exit_index]
        result.append({
            "symbol": snapshot["symbol"], "name": snapshot.get("name") or snapshot["symbol"],
            "entry_timestamp": int(entry["timestamp"]), "entry_price": float(entry.get("open") or entry["close"]),
            "entry_volume": float(entry.get("volume") or volumes[index]),
            "exit_timestamp": int(exit_row["timestamp"]), "exit_price": float(exit_row.get("open") or exit_row["close"]),
            "exit_volume": float(exit_row.get("volume") or 0), "score": float(score),
            "momentum_20": float(ret20), "momentum_60": float(ret60), "momentum_120": float(ret120),
            "volatility_40": float(volatility), "signal_timestamp": int(points[local_index]["timestamp"]),
            "group": _pure_a_etf_group(snapshot["symbol"]),
            "market_gate": market_gate,
            "entry_reason": (
                f"纯A股ETF轮动{variant}：20日动量={ret20:.2%}；"
                f"60日动量={ret60:.2%}；MA200过滤通过；评分={score:.4f}"
            ),
            "exit_reason": "周度轮动调仓（T+1开盘执行）",
        })
    return result

def _timestamp_rows(snapshot: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(row["timestamp"]): row for row in snapshot.get("points") or []}


def build_pool_candidates(
    snapshot: dict[str, Any],
    benchmark_snapshot: dict[str, Any],
    fee_rate: float,
    strategy: str,
    max_opening_gap_pct: float = 3,
    minimum_turnover: float = 0,
    require_above_ma200: bool = False,
    require_ma200_rising: bool = False,
    market_signal_counts: dict[int, int] | None = None,
    market_signal_threshold: int = 0,
    fundamental_reports: list[dict[str, Any]] | None = None,
    fundamental_score_threshold: float | None = None,
    volume_ratio_threshold: float = 0,
    benchmark_5d_drop_threshold: float = 0,
    factor_weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """复用单股策略信号，并在资金池入场前应用统一的开仓过滤条件。"""
    result = run_backtest(
        snapshot, strategy, 100_000, fee_rate, benchmark_snapshot,
        fundamental_reports=fundamental_reports,
        fundamental_score_threshold=fundamental_score_threshold,
        market_signal_counts=market_signal_counts,
        market_signal_threshold=market_signal_threshold,
        volume_ratio_threshold=volume_ratio_threshold,
        benchmark_5d_drop_threshold=benchmark_5d_drop_threshold,
    )
    points = snapshot.get("points") or []
    rows_by_timestamp = _timestamp_rows(snapshot)
    point_indices = {int(row["timestamp"]): index for index, row in enumerate(points)}
    warmup = snapshot.get("indicator_warmup_points") or []
    combined = [*warmup, *points]
    ma200_values = sma([float(row["close"]) for row in combined], 200)[-len(points):] if points else []
    candidates: list[dict[str, Any]] = []
    pending_buy: dict[str, Any] | None = None

    events = result.get("trade_events") or []
    for event in events:
        if event["side"] == "B":
            pending_buy = event
            continue
        if pending_buy is None:
            continue
        candidate = _candidate_from_pair(
            snapshot, pending_buy, event, points, rows_by_timestamp, point_indices,
            max_opening_gap_pct, minimum_turnover, require_above_ma200,
            require_ma200_rising, ma200_values,
        )
        if candidate:
            candidate = _attach_factor_values(candidate, snapshot, fundamental_reports or [], factor_weights or {})
            if candidate:
                candidates.append(candidate)
        pending_buy = None

    if pending_buy is not None and points:
        last = points[-1]
        forced_exit = {
            "timestamp": int(last["timestamp"]), "price": float(last["close"]),
            "reason": "回测结束强制平仓", "side": "S",
        }
        candidate = _candidate_from_pair(
            snapshot, pending_buy, forced_exit, points, rows_by_timestamp, point_indices,
            max_opening_gap_pct, minimum_turnover, require_above_ma200,
            require_ma200_rising, ma200_values,
        )
        if candidate:
            candidate = _attach_factor_values(candidate, snapshot, fundamental_reports or [], factor_weights or {})
            if candidate:
                candidates.append(candidate)
    return candidates


def build_rsi_pool_candidates(
    snapshot: dict[str, Any],
    benchmark_snapshot: dict[str, Any],
    fee_rate: float,
) -> list[dict[str, Any]]:
    """兼容原有调用：使用 RSI 反转策略和 3% 高开限制。"""
    return build_pool_candidates(
        snapshot, benchmark_snapshot, fee_rate, "RSI反转+止盈止损",
    )


def _candidate_from_pair(
    snapshot: dict[str, Any],
    buy: dict[str, Any],
    sell: dict[str, Any],
    points: list[dict[str, Any]],
    rows_by_timestamp: dict[int, dict[str, Any]],
    point_indices: dict[int, int],
    max_opening_gap_pct: float,
    minimum_turnover: float,
    require_above_ma200: bool,
    require_ma200_rising: bool,
    ma200_values: list[float | None],
) -> dict[str, Any] | None:
    entry_timestamp = int(buy["timestamp"])
    signal_timestamp = int(buy.get("signal_timestamp") or entry_timestamp)
    exit_timestamp = int(sell["timestamp"])
    entry_row = rows_by_timestamp.get(entry_timestamp)
    exit_row = rows_by_timestamp.get(exit_timestamp)
    signal_index = point_indices.get(signal_timestamp)
    if entry_row is None or signal_index is None or exit_timestamp <= entry_timestamp:
        return None
    signal_close = float(points[signal_index]["close"])
    entry_price = float(buy["price"])
    opening_gap_pct = entry_price / signal_close - 1 if signal_close > 0 else float("inf")
    if opening_gap_pct * 100 > max_opening_gap_pct + 1e-9:
        return None
    turnover_rows = points[max(0, signal_index - 19):signal_index + 1]
    average_turnover = statistics.fmean(
        float(row.get("volume") or 0) * float(row["close"]) for row in turnover_rows
    ) if turnover_rows else 0
    if average_turnover < minimum_turnover:
        return None
    signal_ma200 = ma200_values[signal_index] if signal_index < len(ma200_values) else None
    if require_above_ma200 and (signal_ma200 is None or signal_close <= signal_ma200):
        return None
    if require_ma200_rising:
        prior_index = signal_index - 5
        prior_ma200 = ma200_values[prior_index] if prior_index >= 0 else None
        if signal_ma200 is None or prior_ma200 is None or signal_ma200 <= prior_ma200:
            return None
    reason = str(buy.get("reason") or "")
    rsi_match = re.search(r"RSI=([\d.]+)", reason)
    rebound_match = re.search(r"最低价([\d.]+)%", reason)
    rsi_value = float(rsi_match.group(1)) if rsi_match else 30.0
    rebound = float(rebound_match.group(1)) if rebound_match else 2.0
    score = rebound + rsi_value / 20
    combined_points = [*(snapshot.get("indicator_warmup_points") or []), *points]
    combined_index = next(
        (index for index, row in enumerate(combined_points) if int(row["timestamp"]) == signal_timestamp),
        None,
    )
    ten_day_return_pct = None
    if combined_index is not None and combined_index >= 10:
        prior_close = float(combined_points[combined_index - 10]["close"])
        if prior_close > 0:
            ten_day_return_pct = (signal_close / prior_close - 1) * 100
    return {
        "symbol": snapshot["symbol"], "name": snapshot["name"],
        "entry_timestamp": entry_timestamp, "entry_price": entry_price,
        "entry_volume": float(entry_row.get("volume") or 0),
        "exit_timestamp": exit_timestamp, "exit_price": float(sell["price"]),
        "exit_volume": float((exit_row or entry_row).get("volume") or 0),
        "score": score, "ten_day_return_pct": ten_day_return_pct,
        "average_turnover": average_turnover,
        "opening_gap_pct": opening_gap_pct * 100,
        "fundamental_score": buy.get("fundamental_score"),
        "entry_reason": reason, "exit_reason": str(sell.get("reason") or "策略退出"),
        "signal_timestamp": signal_timestamp,
    }


def _attach_factor_values(
    candidate: dict[str, Any],
    snapshot: dict[str, Any],
    reports: list[dict[str, Any]],
    factor_weights: dict[str, float],
) -> dict[str, Any] | None:
    factors = [factor for factor, weight in factor_weights.items() if factor in FACTOR_IDS and float(weight) > 0]
    if not factors:
        return candidate
    combined = [*(snapshot.get("indicator_warmup_points") or []), *(snapshot.get("points") or [])]
    signal_timestamp = int(candidate["signal_timestamp"])
    index = next((position for position, row in enumerate(combined) if int(row["timestamp"]) == signal_timestamp), None)
    if index is None or index < 60:
        return None
    report = _report_at(reports, signal_timestamp) if reports else None
    values = calculate_factor_values(combined, index, report)
    selected = {factor: values.get(factor) for factor in factors}
    if any(value is None or not math.isfinite(float(value)) for value in selected.values()):
        return None
    candidate["factor_values"] = {factor: float(value) for factor, value in selected.items()}
    return candidate


def select_capital_pool_trades(
    candidates: list[dict[str, Any]],
    initial_capital: float,
    fee_rate: float,
    exposure_limit: float,
    single_position_limit: float,
    max_positions: int,
    volume_participation_limit: float,
    base_slippage_bps: float,
    impact_bps: float,
    candidate_ranking: str = "rsi_rebound_score",
    factor_weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    entries: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        entries[int(candidate["entry_timestamp"])].append(candidate)
    dates = sorted({*entries.keys(), *(int(item["exit_timestamp"]) for item in candidates)})
    cash = float(initial_capital)
    active: dict[str, dict[str, Any]] = {}
    selected: list[dict[str, Any]] = []

    for timestamp in dates:
        for symbol, position in list(active.items()):
            if int(position["exit_timestamp"]) != timestamp:
                continue
            sell_price = _impacted_price(
                float(position["exit_price"]), position["shares"], position.get("exit_volume") or position["entry_volume"],
                volume_participation_limit, base_slippage_bps, impact_bps, "sell",
            )
            proceeds = position["shares"] * sell_price * (1 - fee_rate)
            cash += proceeds
            position["actual_exit_price"] = sell_price
            position["sell_proceeds"] = proceeds
            active.pop(symbol)

        approximate_invested = sum(item["shares"] * item["actual_entry_price"] for item in active.values())
        approximate_equity = cash + approximate_invested
        slots = max(0, max_positions - len(active))
        daily_candidates = entries.get(timestamp, [])
        if candidate_ranking in {"pure_a_v25", "pure_a_simple14"}:
            # V25 复现：先按市场门控决定进攻/防御，再做 Leg A + Leg G
            # 横截面选择。所有排名仅使用同一信号日的候选，避免未来函数。
            if candidate_ranking == "pure_a_v25" and daily_candidates and not any(item.get("market_gate", True) for item in daily_candidates):
                daily_candidates = [item for item in daily_candidates if str(item["symbol"]).split(".")[0] == "159934"]
                for item in daily_candidates:
                    item["score"] = 1.0
                    item["entry_reason"] += "；CSI全指代理50周均线防御，切换黄金ETF"
            elif candidate_ranking == "pure_a_v25":
                def zrank(items: list[dict[str, Any]], key: str, reverse: bool = False) -> dict[str, float]:
                    ordered = sorted(items, key=lambda item: (float(item.get(key) or -999), item["symbol"]), reverse=reverse)
                    denominator = max(1, len(ordered) - 1)
                    return {item["symbol"]: index / denominator for index, item in enumerate(ordered)}
                # Leg A：V7 动量/成交量/宽度的可复现近似；Leg G：行业动量前四组各取一只。
                by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for item in daily_candidates:
                    by_group[str(item.get("group") or "其他")].append(item)
                group_scores = {
                    group: statistics.fmean(float(item.get("momentum_20") or 0) for item in items)
                    for group, items in by_group.items()
                }
                top_groups = {group for group, _ in sorted(group_scores.items(), key=lambda pair: (-pair[1], pair[0]))[:4]}
                leg_g = []
                for group in top_groups:
                    leg_g.append(max(by_group[group], key=lambda item: (float(item.get("momentum_20") or -999), item["symbol"])))
                z20 = zrank(daily_candidates, "momentum_20")
                z60 = zrank(daily_candidates, "momentum_60")
                z120 = zrank(daily_candidates, "momentum_120")
                zvol = zrank(daily_candidates, "volatility_40")
                leg_a = sorted(
                    daily_candidates,
                    key=lambda item: (-(0.20 * z20[item["symbol"]] + 0.30 * z60[item["symbol"]] + 0.50 * z120[item["symbol"]]
                                      + 0.40 * (z20[item["symbol"]] - zvol[item["symbol"]])
                                      + 0.10 * (z20[item["symbol"]] - z60[item["symbol"]])), item["symbol"]),
                )[:4]
                merged: dict[str, dict[str, Any]] = {item["symbol"]: item for item in leg_a}
                merged.update({item["symbol"]: item for item in leg_g})
                for item in merged.values():
                    item["score"] = 0.5 if item in leg_a else 0.0
                    if item in leg_g:
                        item["score"] += 0.5
                    item["entry_reason"] += f"；V25 LegA={'是' if item in leg_a else '否'}，LegG={'是' if item in leg_g else '否'}，行业={item.get('group')}"
                daily_candidates = sorted(merged.values(), key=lambda item: (-float(item.get("score") or 0), item["symbol"]))
            else:
                daily_candidates = sorted(daily_candidates, key=lambda item: (-float(item.get("score") or 0), item["symbol"]))
        elif candidate_ranking == "trend_rotation":
            daily_candidates = sorted(daily_candidates, key=lambda item: (-float(item.get("score") or 0), item["symbol"]))
        elif candidate_ranking in {"multifactor", "multifactor_score"}:
            usable_weights = {
                factor: float(weight) for factor, weight in (factor_weights or {}).items()
                if factor in FACTOR_IDS and float(weight) > 0
            }
            ranked_candidates = [
                item for item in daily_candidates
                if all((item.get("factor_values") or {}).get(factor) is not None for factor in usable_weights)
            ]
            factor_ranks: dict[str, dict[str, float]] = {}
            for factor in usable_weights:
                ordered = sorted(
                    ranked_candidates,
                    key=lambda item: (float(item["factor_values"][factor]), item["symbol"]),
                )
                denominator = max(1, len(ordered) - 1)
                factor_ranks[factor] = {item["symbol"]: index / denominator for index, item in enumerate(ordered)}
            total_weight = sum(usable_weights.values()) or 1.0
            for item in ranked_candidates:
                item["score"] = sum(
                    usable_weights[factor] * factor_ranks[factor][item["symbol"]]
                    for factor in usable_weights
                ) / total_weight
                components = "，".join(
                    f"{factor}={item['factor_values'][factor]:.4f}" for factor in usable_weights
                )
                ranking_label = "月末多因子综合排名" if candidate_ranking == "multifactor" else "原策略信号通过，多因子候选排名"
                original_reason = str(item.get("entry_reason") or "")
                item["entry_reason"] = f"{ranking_label}，得分={item['score']:.4f}；{components}；原信号：{original_reason}"
            daily_candidates = sorted(ranked_candidates, key=lambda item: (-float(item["score"]), item["symbol"]))
        elif candidate_ranking == "dividend_quality_momentum":
            def percentile(items: list[dict[str, Any]], key: str) -> dict[str, float]:
                ordered = sorted(items, key=lambda item: (float(item.get(key) or 0), item["symbol"]))
                denominator = max(1, len(ordered) - 1)
                return {item["symbol"]: index / denominator for index, item in enumerate(ordered)}
            dividend_rank = percentile(daily_candidates, "dividend_yield")
            quality_rank = percentile(daily_candidates, "quality_score")
            valuation_rank = percentile(daily_candidates, "valuation_score")
            momentum_rank = percentile(daily_candidates, "momentum_50")
            daily_candidates = sorted(daily_candidates, key=lambda item: (
                -(0.55 * dividend_rank[item["symbol"]] + 0.20 * quality_rank[item["symbol"]]
                  + 0.10 * valuation_rank[item["symbol"]] + 0.15 * momentum_rank[item["symbol"]]),
                item["symbol"],
            ))
        elif candidate_ranking == "ten_day_decline_rank_15_39":
            # 只使用信号日及以前的收盘价计算近10日收益。收益越低，跌幅排名越靠前；
            # 先在当日全部合格候选中确定名次，再排除已经持仓的股票。
            ranked = [item for item in daily_candidates if item.get("ten_day_return_pct") is not None]
            ranked.sort(key=lambda item: (item["ten_day_return_pct"], item["symbol"]))
            daily_candidates = [
                {**item, "candidate_rank": rank}
                for rank, item in enumerate(ranked, start=1)
                if 15 <= rank <= 39
            ]
        else:
            # RSI 反转策略优先选择刚完成止跌确认、尚未明显反弹的候选。
            daily_candidates = sorted(daily_candidates, key=lambda item: (item["score"], item["symbol"]))
        available = [item for item in daily_candidates if item["symbol"] not in active]
        available = available[:slots]
        day_exposure_limit = min(
            exposure_limit,
            float(available[0].get("market_exposure", exposure_limit)) if available else exposure_limit,
        )
        pool_remaining = max(0.0, approximate_equity * day_exposure_limit - approximate_invested)
        rotation_risk_budget = (
            sum(1.0 / max(0.05, float(item.get("volatility_60") or 1.0)) for item in available)
            if candidate_ranking == "trend_rotation" else 0.0
        )
        for index, candidate in enumerate(available):
            remaining_candidates = len(available) - index
            if remaining_candidates <= 0 or cash <= 0 or pool_remaining <= 0:
                break
            if rotation_risk_budget > 0:
                risk_weight = (1.0 / max(0.05, float(candidate.get("volatility_60") or 1.0))) / rotation_risk_budget
                budget = min(approximate_equity * single_position_limit, pool_remaining * risk_weight, cash)
            else:
                budget = min(
                    approximate_equity * single_position_limit,
                    pool_remaining / remaining_candidates,
                    cash / remaining_candidates,
                )
            volume = float(candidate["entry_volume"] or 0)
            volume_cap_shares = math.floor(volume * volume_participation_limit / 100) * 100
            estimated_price = float(candidate["entry_price"])
            shares = min(math.floor(budget / estimated_price / 100) * 100, volume_cap_shares)
            if shares < 100:
                continue
            buy_price = _impacted_price(
                estimated_price, shares, volume, volume_participation_limit,
                base_slippage_bps, impact_bps, "buy",
            )
            cost = shares * buy_price * (1 + fee_rate)
            while shares >= 100 and cost > cash:
                shares -= 100
                buy_price = _impacted_price(
                    estimated_price, shares, volume, volume_participation_limit,
                    base_slippage_bps, impact_bps, "buy",
                )
                cost = shares * buy_price * (1 + fee_rate)
            if shares < 100:
                continue
            accepted = {**candidate, "shares": shares, "actual_entry_price": buy_price, "buy_cost": cost}
            cash -= cost
            pool_remaining -= shares * buy_price
            active[candidate["symbol"]] = accepted
            selected.append(accepted)
    return selected


def _impacted_price(
    price: float,
    shares: int,
    volume: float,
    participation_limit: float,
    base_slippage_bps: float,
    impact_bps: float,
    side: str,
) -> float:
    participation = shares / volume if volume > 0 else participation_limit
    scale = math.sqrt(max(0.0, participation / max(participation_limit, 1e-9)))
    impact_rate = min(0.02, (base_slippage_bps + impact_bps * scale) / 10_000)
    return price * (1 + impact_rate if side == "buy" else 1 - impact_rate)


def build_capital_pool_result(
    selected: list[dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
    benchmark_snapshot: dict[str, Any],
    initial_capital: float,
    fee_rate: float,
    candidates_count: int,
    strategy: str = "RSI反转+止盈止损",
    cash_proxy_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    buys: dict[int, list[dict[str, Any]]] = defaultdict(list)
    sells: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for trade in selected:
        buys[int(trade["entry_timestamp"])].append(trade)
        sells[int(trade["exit_timestamp"])].append(trade)
    price_maps = {symbol: _timestamp_rows(snapshot) for symbol, snapshot in snapshots.items()}
    benchmark_rows = benchmark_snapshot.get("points") or []
    if len(benchmark_rows) < 2:
        raise RuntimeError("沪深300基准行情不足")
    cash = float(initial_capital)
    positions: dict[str, dict[str, Any]] = {}
    last_closes: dict[str, float] = {}
    curve: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    daily_returns: list[float] = []
    exposure_values: list[float] = []
    wins = 0
    closed = 0
    benchmark_start = float(benchmark_rows[0]["close"])
    proxy_prices = _timestamp_rows(cash_proxy_snapshot or {})
    previous_proxy_close: float | None = None
    proxy_value = 0.0

    for benchmark_row in benchmark_rows:
        timestamp = int(benchmark_row["timestamp"])
        before_equity = curve[-1]["equity"] if curve else initial_capital
        proxy_row = proxy_prices.get(timestamp)
        if proxy_row:
            proxy_close = float(proxy_row["close"])
            if previous_proxy_close and previous_proxy_close > 0 and proxy_value > 0:
                proxy_value *= proxy_close / previous_proxy_close
            previous_proxy_close = proxy_close
        # 配置现金代理的策略有股票买入机会时，先释放现金代理仓位。
        if buys.get(timestamp) and proxy_value > 0:
            cash += proxy_value
            proxy_value = 0.0
        for trade in sells.get(timestamp, []):
            position = positions.pop(trade["symbol"], None)
            if position is None:
                continue
            shares = int(position["shares"])
            proceeds = float(trade["sell_proceeds"])
            price = float(trade["actual_exit_price"])
            reason = trade["exit_reason"]
            cash += proceeds
            pnl = proceeds - float(position["buy_cost"])
            wins += int(pnl > 0)
            closed += 1
            events.append({
                "side": "S", "timestamp": timestamp, "symbol": trade["symbol"], "name": trade["name"],
                "price": round(price, 4), "shares": shares,
                "reason": reason, "pnl": round(pnl, 2),
            })
        for trade in buys.get(timestamp, []):
            cash -= float(trade["buy_cost"])
            positions[trade["symbol"]] = trade
            events.append({
                "side": "B", "timestamp": timestamp, "signal_timestamp": trade["signal_timestamp"],
                "symbol": trade["symbol"], "name": trade["name"],
                "price": round(float(trade["actual_entry_price"]), 4), "shares": trade["shares"],
                "reason": trade["entry_reason"], "pnl": None,
            })
        # 没有股票买入机会时，将剩余现金停放在策略专用现金代理；未配置则保留现金。
        if not buys.get(timestamp) and proxy_prices.get(timestamp) and cash > 0:
            proxy_value += cash
            cash = 0.0
        market_value = 0.0
        for symbol, position in positions.items():
            row = price_maps.get(symbol, {}).get(timestamp)
            if row:
                last_closes[symbol] = float(row["close"])
            close = last_closes.get(symbol, float(position["actual_entry_price"]))
            market_value += position["shares"] * close
        equity = cash + proxy_value + market_value
        benchmark_equity = initial_capital * float(benchmark_row["close"]) / benchmark_start
        exposure_pct = market_value / equity * 100 if equity > 0 else 0
        exposure_values.append(exposure_pct)
        if curve and before_equity > 0:
            daily_returns.append(equity / before_equity - 1)
        curve.append({
            "timestamp": timestamp, "equity": round(equity, 2),
            "benchmark": round(benchmark_equity, 2), "exposure_pct": round(exposure_pct, 2),
        })

    final_equity = float(curve[-1]["equity"])
    peak = float(curve[0]["equity"])
    max_drawdown = 0.0
    for point in curve:
        peak = max(peak, float(point["equity"]))
        max_drawdown = min(max_drawdown, float(point["equity"]) / peak - 1)
    daily_std = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0
    sharpe = statistics.fmean(daily_returns) / daily_std * math.sqrt(252) if daily_std else 0
    benchmark_return = float(curve[-1]["benchmark"]) / initial_capital - 1
    elapsed_days = (int(curve[-1]["timestamp"]) - int(curve[0]["timestamp"])) / 86_400
    annualized = (
        (final_equity / initial_capital) ** (365.2425 / elapsed_days) - 1
        if elapsed_days > 0 and final_equity > 0 else None
    )
    holding_days = sum(1 for value in exposure_values if value > 0)
    holding_daily = (
        (final_equity / initial_capital) ** (1 / holding_days) - 1
        if holding_days and final_equity > 0 else None
    )
    return {
        "symbol": "CAPITAL_POOL", "name": "A股资金池", "strategy": f"资金池·{strategy}",
        "initial_capital": round(initial_capital, 2), "final_equity": round(final_equity, 2),
        "cash": round(cash, 2), "cash_proxy_value": round(proxy_value, 2),
        "total_return_pct": round((final_equity / initial_capital - 1) * 100, 2),
        "benchmark_return_pct": round(benchmark_return * 100, 2),
        "excess_return_pct": round((final_equity / initial_capital - 1 - benchmark_return) * 100, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2), "sharpe": round(sharpe, 2),
        "trades": len(events), "closed_trades": closed,
        "win_rate_pct": round(wins / closed * 100, 2) if closed else None,
        "average_exposure_pct": round(statistics.fmean(exposure_values), 2) if exposure_values else 0,
        "candidate_trades": candidates_count, "selected_trades": len(selected),
        "holding_days": holding_days,
        "holding_daily_return_pct": round(holding_daily * 100, 4) if holding_daily is not None else None,
        "annualized_return_pct": round(annualized * 100, 2) if annualized is not None else None,
        "position_open": bool(positions), "period_points": len(curve),
        "curve": curve, "trade_events": events,
        "data_source": "本地 AKShare 动态前复权日线", "adjustment": "动态前复权",
        "adjustment_note": "共享现金账户；流动性、成交量参与率及冲击成本约束已启用",
        "cash_proxy": str(cash_proxy_snapshot.get("symbol")) if cash_proxy_snapshot else None,
    }


def run_pure_a_v25_weight_backtest(
    snapshots: dict[str, dict[str, Any]],
    benchmark_snapshot: dict[str, Any],
    csi_snapshot: dict[str, Any],
    initial_capital: float,
    fee_rate: float,
    slippage_bps: float = 5.0,
) -> dict[str, Any]:
    """V25 独立目标权重回测引擎。

    与普通共享资金池不同：先在周五收盘计算 Leg A/Leg G 目标权重，
    下周第一个交易日执行；随后按上一周组合波动率缩放至 12% 目标，
    组合回撤和交易成本均在组合层计算。
    """
    from datetime import datetime, timezone

    if not snapshots or not benchmark_snapshot or not csi_snapshot:
        raise RuntimeError("V25 目标权重回测缺少 ETF、沪深300或中证全指行情")
    rows_by_symbol = {
        symbol: {int(row["timestamp"]): row for row in snapshot.get("points") or []}
        for symbol, snapshot in snapshots.items()
    }
    calendar = [int(row["timestamp"]) for row in benchmark_snapshot.get("points") or []]
    calendar = sorted(calendar)
    if len(calendar) < 260:
        raise RuntimeError("V25 回测交易日不足 260 天")
    csi_rows = {int(row["timestamp"]): float(row["close"]) for row in csi_snapshot.get("points") or []}
    groups = {symbol: _pure_a_etf_group(symbol) for symbol in rows_by_symbol}
    gold = next((symbol for symbol in rows_by_symbol if symbol.split(".")[0] == "159934"), None)
    if gold is None:
        raise RuntimeError("V25 资产池缺少黄金 ETF 159934")

    # 周五信号日及下一交易日执行日。
    signal_dates = [
        timestamp for timestamp in calendar
        if datetime.fromtimestamp(timestamp, timezone.utc).weekday() == 4
    ]
    signal_to_exec: dict[int, int] = {}
    for signal in signal_dates:
        execution = next((timestamp for timestamp in calendar if timestamp > signal), None)
        if execution is not None:
            signal_to_exec[execution] = signal

    target_by_exec: dict[int, dict[str, float]] = {}
    regime_state = False
    regime_weeks = 0
    weekly_portfolio_returns: list[float] = []
    previous_targets: dict[str, float] = {}
    diagnostics: list[dict[str, Any]] = []

    def value_at(symbol: str, timestamp: int, field: str = "close") -> float | None:
        row = rows_by_symbol.get(symbol, {}).get(timestamp)
        try:
            value = float(row[field]) if row else 0.0
            return value if math.isfinite(value) and value > 0 else None
        except (TypeError, ValueError):
            return None

    # 构造目标权重：CSI 全指 4 周-13 周加速度，连续 8 周滞后确认。
    for signal in signal_dates:
        available = []
        for symbol in rows_by_symbol:
            close = value_at(symbol, signal)
            history = [t for t in calendar if t <= signal]
            if close is None or len(history) < 121:
                continue
            p20 = value_at(symbol, history[-21])
            p60 = value_at(symbol, history[-61])
            p120 = value_at(symbol, history[-121])
            if not p20 or not p60 or not p120:
                continue
            returns = [value_at(symbol, t) / value_at(symbol, history[i - 1]) - 1 for i, t in enumerate(history[-40:], start=max(1, len(history) - 39)) if value_at(symbol, history[i - 1])]
            vol = statistics.pstdev(returns) * math.sqrt(252) if len(returns) > 2 else 1.0
            available.append({"symbol": symbol, "group": groups[symbol], "m20": close / p20 - 1, "m60": close / p60 - 1, "m120": close / p120 - 1, "vol": max(vol, 0.05)})
        if len(available) < 4:
            continue
        csi_history = [t for t in calendar if t <= signal and t in csi_rows]
        accel = None
        if len(csi_history) >= 66:
            csi_now = csi_rows[csi_history[-1]]
            csi_20 = csi_rows[csi_history[-21]]
            csi_65 = csi_rows[csi_history[-66]]
            if csi_20 and csi_65:
                accel = (csi_now / csi_20 - 1) - (csi_now / csi_65 - 1)
        if accel is not None:
            if not regime_state and accel > 0.05 and regime_weeks >= 8:
                regime_state, regime_weeks = True, 0
            elif regime_state and accel < 0 and regime_weeks >= 8:
                regime_state, regime_weeks = False, 0
        regime_weeks += 1
        if not regime_state:
            target = {gold: 1.0}
            target_by_exec[next((t for t in calendar if t > signal), signal)] = target
            diagnostics.append({"signal": signal, "regime": "RISK-OFF", "acceleration": accel, "selected": [gold]})
            continue
        def rank01(key: str, reverse: bool = False) -> dict[str, float]:
            ordered = sorted(available, key=lambda item: (float(item[key]), item["symbol"]), reverse=reverse)
            denominator = max(1, len(ordered) - 1)
            return {item["symbol"]: index / denominator for index, item in enumerate(ordered)}
        r20, r60, r120, rv = (rank01(key) for key in ("m20", "m60", "m120", "vol"))
        for item in available:
            item["score_a"] = 0.20 * r20[item["symbol"]] + 0.30 * r60[item["symbol"]] + 0.50 * r120[item["symbol"]] + 0.40 * (r20[item["symbol"]] - rv[item["symbol"]]) + 0.10 * (r20[item["symbol"]] - r60[item["symbol"]])
        leg_a = sorted(available, key=lambda item: (-item["score_a"], item["symbol"]))[:4]
        group_scores: dict[str, float] = {}
        for item in available:
            group_scores.setdefault(item["group"], []).append(item["m20"])
        top_groups = sorted(group_scores, key=lambda group: (-statistics.fmean(group_scores[group]), group))[:4]
        leg_g = [max([item for item in available if item["group"] == group], key=lambda item: (item["m20"], item["symbol"])) for group in top_groups]
        target = defaultdict(float)
        for item in leg_a:
            target[item["symbol"]] += 0.5 / len(leg_a)
        for item in leg_g:
            target[item["symbol"]] += 0.5 / len(leg_g)
        target_by_exec[next((t for t in calendar if t > signal), signal)] = dict(target)
        diagnostics.append({"signal": signal, "regime": "RISK-ON", "acceleration": accel, "selected": sorted(target)})

    # 组合层净值：目标权重在执行日生效，周度历史波动超过 12% 时按比例降仓。
    cash = float(initial_capital)
    holdings: dict[str, float] = {}
    curve: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    weekly_returns: list[float] = []
    last_equity = cash
    previous_closes: dict[str, float] = {}
    for timestamp in calendar:
        target = target_by_exec.get(timestamp)
        if target is not None:
            realized_vol = statistics.pstdev(weekly_returns[-26:]) * math.sqrt(52) if len(weekly_returns) >= 8 else 0.0
            scale = min(1.0, 0.12 / realized_vol) if realized_vol > 0.12 else 1.0
            target = {symbol: weight * scale for symbol, weight in target.items()}
            total_turnover = sum(abs(target.get(symbol, 0.0) - previous_targets.get(symbol, 0.0)) for symbol in set(target) | set(previous_targets))
            cost = last_equity * total_turnover * (fee_rate + slippage_bps / 10_000)
            cash -= cost
            for symbol in set(target) | set(previous_targets):
                if abs(target.get(symbol, 0.0) - previous_targets.get(symbol, 0.0)) > 1e-8:
                    events.append({"side": "B" if target.get(symbol, 0.0) > previous_targets.get(symbol, 0.0) else "S", "timestamp": timestamp, "symbol": symbol, "weight": round(target.get(symbol, 0.0), 6), "reason": "V25目标权重调仓"})
            holdings = target
            previous_targets = target
        daily_return = 0.0
        for symbol, weight in holdings.items():
            close = value_at(symbol, timestamp)
            prior = previous_closes.get(symbol)
            if close and prior:
                daily_return += weight * (close / prior - 1)
            if close:
                previous_closes[symbol] = close
        equity = max(0.0, last_equity * (1 + daily_return))
        if curve and datetime.fromtimestamp(timestamp, timezone.utc).weekday() == 4:
            weekly_returns.append(equity / last_equity - 1 if last_equity else 0.0)
        benchmark_row = next((row for row in benchmark_snapshot.get("points") or [] if int(row["timestamp"]) == timestamp), None)
        benchmark_value = initial_capital if not benchmark_row else initial_capital * float(benchmark_row["close"]) / float((benchmark_snapshot.get("points") or [benchmark_row])[0]["close"])
        curve.append({"timestamp": timestamp, "equity": round(equity, 2), "benchmark": round(benchmark_value, 2), "exposure_pct": round(sum(holdings.values()) * 100, 2)})
        last_equity = equity
    if not curve:
        raise RuntimeError("V25 没有生成净值曲线")
    peak = curve[0]["equity"]
    mdd = 0.0
    daily = []
    for index, point in enumerate(curve):
        peak = max(peak, point["equity"])
        mdd = min(mdd, point["equity"] / peak - 1 if peak else 0.0)
        if index:
            daily.append(point["equity"] / curve[index - 1]["equity"] - 1)
    std = statistics.stdev(daily) if len(daily) > 1 else 0.0
    total_return = curve[-1]["equity"] / initial_capital - 1
    elapsed_days = max(1.0, (curve[-1]["timestamp"] - curve[0]["timestamp"]) / 86400)
    annualized = (1 + total_return) ** (365.2425 / elapsed_days) - 1 if total_return > -1 else -1.0
    return {"symbol": "CAPITAL_POOL", "name": "纯A股ETF-V25目标权重组合", "strategy": "纯A股ETF-V25（目标权重引擎）", "initial_capital": initial_capital, "final_equity": round(curve[-1]["equity"], 2), "cash": round(curve[-1]["equity"], 2), "total_return_pct": round(total_return * 100, 2), "benchmark_return_pct": round((curve[-1]["benchmark"] / initial_capital - 1) * 100, 2), "excess_return_pct": round((total_return - (curve[-1]["benchmark"] / initial_capital - 1)) * 100, 2), "max_drawdown_pct": round(mdd * 100, 2), "annualized_return_pct": round(annualized * 100, 2), "sharpe": round(statistics.fmean(daily) / std * math.sqrt(252), 2) if std else 0.0, "trades": len(events), "closed_trades": sum(1 for item in events if item["side"] == "S"), "win_rate_pct": None, "holding_days": sum(1 for item in curve if item["exposure_pct"] > 0), "holding_daily_return_pct": None, "average_exposure_pct": round(statistics.fmean(item["exposure_pct"] for item in curve), 2), "trade_events": events, "curve": curve, "regime_diagnostics": diagnostics, "data_source": "本地 AKShare ETF + 中证指数接口", "adjustment": "前复权"}


def attach_market_exposure(candidates: list[dict[str, Any]], benchmark_snapshot: dict[str, Any]) -> None:
    """按信号日前沪深300趋势，为月度红利候选设置股票仓位。"""
    combined = [*(benchmark_snapshot.get("indicator_warmup_points") or []), *(benchmark_snapshot.get("points") or [])]
    closes = [float(row["close"]) for row in combined]
    ma60, ma120, ma200 = (sma(closes, period) for period in (60, 120, 200))
    by_timestamp = {int(row["timestamp"]): index for index, row in enumerate(combined)}
    for candidate in candidates:
        index = by_timestamp.get(int(candidate["signal_timestamp"]))
        exposure = 0.80
        if index is not None and ma120[index] is not None:
            close = closes[index]
            if ma60[index] is not None and close > float(ma60[index]) and index >= 20 and ma60[index - 20] is not None and float(ma60[index]) > float(ma60[index - 20]):
                exposure = 1.0
            elif close < float(ma120[index]):
                exposure = 0.50
            if ma200[index] is not None and close < float(ma200[index]) and index >= 20 and ma200[index - 20] is not None and float(ma200[index]) < float(ma200[index - 20]):
                exposure = 0.30
        candidate["market_exposure"] = exposure
        candidate["entry_reason"] += f"；沪深300趋势股票仓位={exposure*100:.0f}%"
