from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any

from .indicators import atr, kdj, rsi as tonghuashun_rsi, sma
from .market_data import MarketDataError, fetch_stock_snapshot


STRATEGIES = ("买入持有", "均线交叉", "RSI反转", "RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控", "多周期趋势跟随", "KDJ急跌首阳T+1", "建仓波J<0动态止盈", "MA20/60首次回踩", "MA20/55金叉后J<13且涨幅<15%", "MA120回踩5日不破", "MA200回踩5日不破")
BENCHMARK_SYMBOL = "000300.SS"
BENCHMARK_NAME = "沪深300"


def _equal_weight_portfolio_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [row for row in rows if row.get("curve") and float(row.get("initial_capital") or 0) > 0]
    if not usable:
        return {"portfolio_samples": 0, "portfolio_return_pct": None, "portfolio_benchmark_return_pct": None, "portfolio_max_drawdown_pct": None, "portfolio_sharpe": None, "portfolio_curve": []}
    timestamps = sorted({int(point["timestamp"]) for row in usable for point in row["curve"]})
    if len(timestamps) < 2:
        return {"portfolio_samples": len(usable), "portfolio_return_pct": None, "portfolio_benchmark_return_pct": None, "portfolio_max_drawdown_pct": None, "portfolio_sharpe": None, "portfolio_curve": []}
    totals = [0.0] * len(timestamps)
    benchmark_totals = [0.0] * len(timestamps)
    for row in usable:
        curve = sorted(row["curve"], key=lambda point: int(point["timestamp"]))
        initial = float(row["initial_capital"])
        cursor = -1
        normalized_equity = 1.0
        normalized_benchmark = 1.0
        for index, timestamp in enumerate(timestamps):
            while cursor + 1 < len(curve) and int(curve[cursor + 1]["timestamp"]) <= timestamp:
                cursor += 1
                normalized_equity = float(curve[cursor]["equity"]) / initial
                normalized_benchmark = float(curve[cursor].get("benchmark", initial)) / initial
            totals[index] += normalized_equity
            benchmark_totals[index] += normalized_benchmark
    portfolio = [total / len(usable) for total in totals]
    portfolio_benchmark = [total / len(usable) for total in benchmark_totals]
    daily_returns = [current / previous - 1 for previous, current in zip(portfolio, portfolio[1:]) if previous > 0]
    daily_std = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.0
    sharpe = statistics.fmean(daily_returns) / daily_std * math.sqrt(252) if daily_std else 0.0
    peak = portfolio[0]
    drawdown = 0.0
    for value in portfolio:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    return {
        "portfolio_samples": len(usable),
        "portfolio_return_pct": round((portfolio[-1] - 1) * 100, 2),
        "portfolio_benchmark_return_pct": round((portfolio_benchmark[-1] - 1) * 100, 2),
        "portfolio_max_drawdown_pct": round(drawdown * 100, 2),
        "portfolio_sharpe": round(sharpe, 2),
        "portfolio_curve": [
            {"timestamp": timestamps[0] - 86_400, "equity": 100.0, "benchmark": 100.0},
            *[
                {
                    "timestamp": timestamp,
                    "equity": round(equity * 100, 4),
                    "benchmark": round(benchmark * 100, 4),
                }
                for timestamp, equity, benchmark in zip(timestamps, portfolio, portfolio_benchmark)
            ],
        ],
    }


def summarize_backtest_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    present = list(dict.fromkeys(str(row["strategy"]) for row in results))
    strategy_order = [strategy for strategy in STRATEGIES if strategy in present] + [strategy for strategy in present if strategy not in STRATEGIES]
    for strategy in strategy_order:
        rows = [row for row in results if row["strategy"] == strategy]
        if not rows:
            continue
        win_rates = [float(row["win_rate_pct"]) for row in rows if row["win_rate_pct"] is not None]
        holding_daily_returns = [float(row["holding_daily_return_pct"]) for row in rows if row["holding_daily_return_pct"] is not None]
        annualized_returns = [float(row["annualized_return_pct"]) for row in rows if row.get("annualized_return_pct") is not None]
        average = lambda key: sum(float(row[key]) for row in rows) / len(rows)
        portfolio_metrics = _equal_weight_portfolio_metrics(rows)
        summaries.append({
            "strategy": strategy,
            "samples": len(rows),
            "average_return_pct": round(average("total_return_pct"), 2),
            "average_annualized_return_pct": round(sum(annualized_returns) / len(annualized_returns), 2) if annualized_returns else None,
            "average_benchmark_return_pct": round(average("benchmark_return_pct"), 2),
            "average_excess_return_pct": round(average("excess_return_pct"), 2),
            "average_max_drawdown_pct": round(average("max_drawdown_pct"), 2),
            "average_sharpe": round(average("sharpe"), 2),
            "average_trades": round(average("trades"), 1),
            "average_holding_days": round(average("holding_days"), 1),
            "average_holding_daily_return_pct": round(sum(holding_daily_returns) / len(holding_daily_returns), 4) if holding_daily_returns else None,
            "average_win_rate_pct": round(sum(win_rates) / len(win_rates), 2) if win_rates else None,
            "profitable_rate_pct": round(sum(row["total_return_pct"] > 0 for row in rows) / len(rows) * 100, 2),
            "outperform_rate_pct": round(sum(row["excess_return_pct"] > 0 for row in rows) / len(rows) * 100, 2),
            **portfolio_metrics,
        })
    return summaries


def _sma(values: list[float], period: int, index: int) -> float | None:
    if index + 1 < period:
        return None
    return statistics.fmean(values[index + 1 - period:index + 1])


def _trade_date(row: dict[str, Any]) -> str:
    timestamp = row.get("timestamp")
    if timestamp is None:
        return "未知日期"
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).date().isoformat()


def _fundamental_score_at(reports: list[dict[str, Any]], timestamp: int | float) -> dict[str, Any] | None:
    signal_date = datetime.fromtimestamp(float(timestamp), tz=timezone.utc).date().isoformat()
    available = [row for row in reports if str(row.get("effective_date") or "") <= signal_date]
    return max(available, key=lambda row: (str(row.get("effective_date") or ""), str(row.get("report_date") or ""))) if available else None


def _kdj_j_values(rows: list[dict[str, Any]], period: int = 9) -> list[float]:
    """统一复用指标模块的 KDJ(N=9, M1=3, M2=3) 计算。"""
    return kdj(rows, period)[2]


def _snapshot_kdj_j_values(snapshot: dict[str, Any], rows: list[dict[str, Any]]) -> list[float]:
    warmup = snapshot.get("indicator_warmup_points") or []
    combined = [*warmup, *rows]
    return _kdj_j_values(combined)[-len(rows):]


def _snapshot_sma_values(snapshot: dict[str, Any], rows: list[dict[str, Any]], period: int) -> list[float | None]:
    warmup = snapshot.get("indicator_warmup_points") or []
    combined = [*warmup, *rows]
    values = sma([float(row["close"]) for row in combined], period)
    return values[-len(rows):]


def _snapshot_atr_values(snapshot: dict[str, Any], rows: list[dict[str, Any]], period: int = 14) -> list[float | None]:
    warmup = snapshot.get("indicator_warmup_points") or []
    combined = [*warmup, *rows]
    return atr(combined, period)[-len(rows):]


def _snapshot_rsi_values(snapshot: dict[str, Any], rows: list[dict[str, Any]], period: int = 14) -> list[float | None]:
    warmup = snapshot.get("indicator_warmup_points") or []
    combined = [*warmup, *rows]
    values = tonghuashun_rsi([float(row["close"]) for row in combined], period)
    return values[-len(rows):]


def _rsi_risk_entry_setup(
    rows: list[dict[str, Any]], closes: list[float], rsi_values: list[float | None], index: int,
) -> bool:
    if index <= 0 or len(rsi_values) <= index:
        return False
    rsi = rsi_values[index]
    previous_rsi = rsi_values[index - 1]
    if rsi is None or previous_rsi is None:
        return False
    open_price = float(rows[index].get("open") or closes[index])
    lowest_20 = min(float(row["low"]) for row in rows[max(0, index - 19):index + 1])
    return previous_rsi <= 30 < rsi and closes[index] > open_price and closes[index] >= lowest_20 * 1.02


def rsi_risk_raw_signal_timestamps(snapshot: dict[str, Any], period: int = 14) -> list[int]:
    """逐日独立扫描原始RSI入场条件，不受任何影子持仓、退出或冷静期影响。"""
    rows = snapshot.get("points") or []
    if len(rows) < 2:
        return []
    closes = [float(row["close"]) for row in rows]
    rsi_values = _snapshot_rsi_values(snapshot, rows, period)
    return [
        int(rows[index]["timestamp"])
        for index in range(1, len(rows))
        if _rsi_risk_entry_setup(rows, closes, rsi_values, index)
    ]


def _aligned_benchmark(
    rows: list[dict[str, Any]], benchmark_snapshot: dict[str, Any] | None,
) -> tuple[list[float], float, str, str]:
    benchmark_rows = (benchmark_snapshot or {}).get("points") or rows
    ordered = sorted(benchmark_rows, key=lambda row: int(row["timestamp"]))
    if not ordered:
        raise MarketDataError("沪深300基准行情不足")
    aligned: list[float] = []
    selected_indices: list[int] = []
    cursor = 0
    for row in rows:
        timestamp = int(row["timestamp"])
        while cursor + 1 < len(ordered) and int(ordered[cursor + 1]["timestamp"]) <= timestamp:
            cursor += 1
        selected_indices.append(cursor)
        aligned.append(float(ordered[cursor]["close"]))
    first_row = ordered[selected_indices[0]]
    first_open = float(first_row.get("open") or first_row["close"])
    if first_open <= 0:
        first_open = float(first_row["close"])
    name = str((benchmark_snapshot or {}).get("name") or BENCHMARK_NAME)
    symbol = str((benchmark_snapshot or {}).get("symbol") or BENCHMARK_SYMBOL)
    return aligned, first_open, symbol, name


def _aligned_benchmark_sma(
    rows: list[dict[str, Any]], benchmark_snapshot: dict[str, Any] | None, period: int,
) -> list[float | None]:
    benchmark_rows = (benchmark_snapshot or {}).get("points") or rows
    warmup = (benchmark_snapshot or {}).get("indicator_warmup_points") or []
    ordered = sorted([*warmup, *benchmark_rows], key=lambda row: int(row["timestamp"]))
    values = sma([float(row["close"]) for row in ordered], period)
    aligned: list[float | None] = []
    cursor = 0
    for row in rows:
        timestamp = int(row["timestamp"])
        while cursor + 1 < len(ordered) and int(ordered[cursor + 1]["timestamp"]) <= timestamp:
            cursor += 1
        aligned.append(values[cursor])
    return aligned


def _red_fat_green_thin(rows: list[dict[str, Any]], index: int, window: int = 10, ratio: float = 1.3) -> tuple[bool, float, float]:
    if index + 1 < window:
        return False, 0.0, 0.0
    bullish_volumes: list[float] = []
    bearish_volumes: list[float] = []
    for row in rows[index + 1 - window:index + 1]:
        open_price = float(row.get("open") or row["close"])
        close_price = float(row["close"])
        volume = float(row.get("volume") or 0)
        if volume <= 0:
            continue
        if close_price > open_price:
            bullish_volumes.append(volume)
        elif close_price < open_price:
            bearish_volumes.append(volume)
    if not bullish_volumes or not bearish_volumes:
        return False, 0.0, 0.0
    bullish_average = statistics.fmean(bullish_volumes)
    bearish_average = statistics.fmean(bearish_volumes)
    return bullish_average >= bearish_average * ratio, bullish_average, bearish_average


def _kdj_rapid_drop_setup(
    rows: list[dict[str, Any]], kdj_j: list[float], ma200_values: list[float | None], index: int,
) -> bool:
    if index < 9 or len(kdj_j) <= index:
        return False
    average_daily_drop = (kdj_j[index - 3] - kdj_j[index]) / 3
    volume_pattern, _, _ = _red_fat_green_thin(rows, index)
    ma200 = ma200_values[index] if len(ma200_values) > index else None
    above_ma200 = ma200 is not None and float(rows[index]["close"]) > ma200
    return kdj_j[index] < 13 and average_daily_drop >= 18 and volume_pattern and above_ma200


def _construction_wave_confirmations(rows: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    """识别放量上涨后2～8日缩量回落完成的建仓波；仅使用确认日及此前数据。"""
    confirmations: list[dict[str, Any] | None] = [None] * len(rows)
    for impulse_index in range(20, max(20, len(rows) - 1)):
        impulse = rows[impulse_index]
        impulse_open = float(impulse.get("open") or impulse["close"])
        impulse_close = float(impulse["close"])
        impulse_volume = float(impulse.get("volume") or 0)
        prior_volumes = [float(row.get("volume") or 0) for row in rows[impulse_index - 20:impulse_index]]
        prior_average = statistics.fmean(prior_volumes) if prior_volumes else 0
        rise_pct = (impulse_close / impulse_open - 1) * 100 if impulse_open > 0 else 0
        volume_ratio = impulse_volume / prior_average if prior_average > 0 else 0
        if rise_pct < 4 or volume_ratio < 2:
            continue
        for confirmation_index in range(impulse_index + 2, min(len(rows), impulse_index + 9)):
            pullback_rows = rows[impulse_index + 1:confirmation_index + 1]
            down_days = sum(
                float(rows[cursor]["close"]) < float(rows[cursor - 1]["close"])
                for cursor in range(impulse_index + 1, confirmation_index + 1)
            )
            pullback_average_volume = statistics.fmean(float(row.get("volume") or 0) for row in pullback_rows)
            confirmation_close = float(rows[confirmation_index]["close"])
            if (
                down_days >= 2
                and pullback_average_volume <= impulse_volume * 0.75
                and impulse_open <= confirmation_close < impulse_close
            ):
                wave = {
                    "impulse_index": impulse_index,
                    "confirmation_index": confirmation_index,
                    "rise_pct": rise_pct,
                    "volume_ratio": volume_ratio,
                    "pullback_days": len(pullback_rows),
                    "pullback_volume_ratio": pullback_average_volume / impulse_volume if impulse_volume > 0 else 0,
                }
                existing = confirmations[confirmation_index]
                if existing is None or impulse_index > int(existing["impulse_index"]):
                    confirmations[confirmation_index] = wave
                break
    return confirmations


def _target_position(
    strategy: str,
    rows: list[dict[str, Any]],
    closes: list[float],
    index: int,
    previous: int,
    state: dict[str, Any],
    kdj_j: list[float],
    ma200_values: list[float | None],
    atr_values: list[float | None],
    rsi_values: list[float | None],
    trend_values: dict[str, Any] | None = None,
) -> int:
    if strategy == "买入持有":
        return 1
    if strategy == "均线交叉":
        fast, slow = _sma(closes, 5, index), _sma(closes, 20, index)
        if fast is None or slow is None:
            return previous
        return 1 if fast > slow else 0
    if strategy == "多周期趋势跟随":
        values = trend_values or {}
        ma20 = values.get("ma20") or []
        ma60 = values.get("ma60") or []
        ma200 = values.get("ma200") or []
        benchmark_closes = values.get("benchmark_closes") or []
        benchmark_ma200 = values.get("benchmark_ma200") or []
        if index < 60 or any(len(series) <= index for series in (ma20, ma60, ma200, benchmark_closes, benchmark_ma200)):
            return previous
        current_ma20, current_ma60, current_ma200 = ma20[index], ma60[index], ma200[index]
        previous_ma20, previous_ma60 = ma20[index - 1], ma60[index - 1]
        old_ma200 = ma200[index - 20] if index >= 20 else None
        current_benchmark_ma200 = benchmark_ma200[index]
        old_benchmark_ma200 = benchmark_ma200[index - 20] if index >= 20 else None
        if None in (current_ma20, current_ma60, current_ma200, previous_ma20, previous_ma60, old_ma200, current_benchmark_ma200, old_benchmark_ma200):
            return previous
        if previous:
            return 0 if float(current_ma20) < float(current_ma60) else 1
        crossed = float(previous_ma20) <= float(previous_ma60) and float(current_ma20) > float(current_ma60)
        stock_relative_return = closes[index] / closes[index - 60] - 1 if closes[index - 60] > 0 else float("-inf")
        benchmark_relative_return = float(benchmark_closes[index]) / float(benchmark_closes[index - 60]) - 1 if float(benchmark_closes[index - 60]) > 0 else float("inf")
        stock_regime = float(current_ma60) > float(current_ma200) > float(old_ma200)
        market_regime = float(benchmark_closes[index]) > float(current_benchmark_ma200) > float(old_benchmark_ma200)
        return 1 if crossed and stock_regime and market_regime and stock_relative_return > benchmark_relative_return else 0
    if strategy in {"RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控"}:
        rsi = rsi_values[index] if len(rsi_values) > index else None
        previous_rsi = rsi_values[index - 1] if index > 0 and len(rsi_values) >= index else None
        if rsi is None or previous_rsi is None:
            return previous
        if previous:
            entry_price = float(state.get("rsi_risk_entry_price") or 0)
            entry_index = state.get("rsi_risk_entry_index")
            if entry_price <= 0 or entry_index is None:
                return previous
            current_atr = float(atr_values[index] or 0) if len(atr_values) > index else 0
            peak = max(float(state.get("rsi_risk_peak_close") or entry_price), closes[index])
            state["rsi_risk_peak_close"] = peak
            return_pct = (closes[index] / entry_price - 1) * 100
            holding_days = index - int(entry_index) + 1
            crash_mode = bool(state.get("rsi_risk_crash"))
            stop_price = float(state.get("rsi_risk_stop_price") or entry_price * (0.97 if crash_mode else 0.95))
            if closes[index] <= stop_price:
                state["rsi_risk_exit_reason"] = f"ATR初始止损：收盘={closes[index]:.2f}≤止损价={stop_price:.2f}，收益={return_pct:.2f}%"
                return 0
            activation = 0.10 if crash_mode else 0.08
            if peak >= entry_price * (1 + activation):
                state["rsi_risk_trailing_active"] = True
            if state.get("rsi_risk_trailing_active"):
                trailing_pct = min(0.05, max(0.035, 2 * current_atr / peak)) if crash_mode else min(0.06, max(0.04, 2 * current_atr / peak))
                trailing_stop = max(entry_price, peak * (1 - trailing_pct))
                if closes[index] <= trailing_stop:
                    state["rsi_risk_exit_reason"] = (
                        f"ATR跟踪止盈：最高收盘={peak:.2f}，回撤止盈价={trailing_stop:.2f}，"
                        f"当前收盘={closes[index]:.2f}，收益={return_pct:.2f}%"
                    )
                    return 0
            min_return = 5 if crash_mode else 3
            if holding_days >= 20 and return_pct < min_return:
                state["rsi_risk_exit_reason"] = f"时间退出：持仓{holding_days}个交易日且收益仅{return_pct:.2f}%（低于{min_return}%）"
                return 0
            return 1
        if _rsi_risk_entry_setup(rows, closes, rsi_values, index):
            state["rsi_risk_signal_atr"] = float(atr_values[index] or 0) if len(atr_values) > index else 0
            if strategy == "RSI反转+暴跌强化风控":
                recent_high = max(closes[max(0, index - 19):index + 1])
                state["rsi_risk_crash"] = recent_high > 0 and closes[index] <= recent_high * 0.85
            return 1
        return 0
    if strategy == "KDJ急跌首阳T+1":
        if previous:
            entry_price = float(state.get("kdj_entry_price") or 0)
            entry_index = state.get("kdj_entry_index")
            if entry_price <= 0 or entry_index is None:
                return previous
            loss_pct = (closes[index] / entry_price - 1) * 100
            if loss_pct <= -3:
                state["kdj_exit_reason"] = (
                    f"止损：{_trade_date(rows[index])} 信号日收盘价 {closes[index]:.2f}，"
                    f"相对买入开盘价 {entry_price:.2f} 收益 {loss_pct:.2f}%，达到 -3% 止损线"
                )
                return 0
            holding_days = index - int(entry_index) + 1
            if holding_days >= 5 and not state.get("kdj_first_five_checked", False):
                first_five = rows[int(entry_index):int(entry_index) + 5]
                bullish_pcts = [
                    (float(row["close"]) / float(row.get("open") or row["close"]) - 1) * 100
                    for row in first_five
                    if float(row.get("open") or row["close"]) > 0 and float(row["close"]) > float(row.get("open") or row["close"])
                ]
                has_four_pct_bullish = any(value >= 4 for value in bullish_pcts)
                state["kdj_first_five_checked"] = True
                if not has_four_pct_bullish:
                    max_bullish_pct = max(bullish_pcts, default=0)
                    state["kdj_exit_reason"] = (
                        f"5日退出：{_trade_date(rows[int(entry_index)])} 至 {_trade_date(rows[index])} 的"
                        f"前 5 个持仓交易日内，最大阳线涨幅仅 {max_bullish_pct:.2f}%，未达到 4%"
                    )
                    return 0
            if holding_days >= 20:
                state["kdj_exit_reason"] = (
                    f"到期退出：从 {_trade_date(rows[int(entry_index)])} 买入日起计算，"
                    f"至 {_trade_date(rows[index])} 已持仓 20 个交易日"
                )
                return 0
            return 1
        pending_index = state.get("kdj_pending_observation_index")
        if pending_index is not None:
            if index <= int(pending_index):
                return 0
            open_price = float(rows[index].get("open") or closes[index])
            bullish_pct = (closes[index] / open_price - 1) * 100 if open_price > 0 else 0
            if bullish_pct > 0:
                state.pop("kdj_pending_observation_index", None)
                if closes[index] > open_price * 1.04:
                    return 0
                state["kdj_entry_observation_index"] = int(pending_index)
                state["kdj_entry_bullish_index"] = index
                state["kdj_entry_bullish_pct"] = bullish_pct
                return 1
            return 0
        if _kdj_rapid_drop_setup(rows, kdj_j, ma200_values, index):
            state["kdj_pending_observation_index"] = index
            state["kdj_observation_ma200"] = ma200_values[index]
        return 0
    if strategy == "建仓波J<0动态止盈":
        values = trend_values or {}
        waves = values.get("construction_waves") or []
        current_wave = waves[index] if len(waves) > index else None
        if isinstance(current_wave, dict):
            state["construction_armed_wave"] = current_wave
        if previous:
            entry_price = float(state.get("construction_entry_price") or 0)
            entry_index = state.get("construction_entry_index")
            if entry_price <= 0 or entry_index is None:
                return previous
            peak = max(float(state.get("construction_peak_close") or entry_price), closes[index])
            state["construction_peak_close"] = peak
            return_pct = (closes[index] / entry_price - 1) * 100
            fixed_stop = entry_price * 0.92
            if closes[index] <= fixed_stop:
                state["construction_exit_reason"] = (
                    f"固定止损：收盘={closes[index]:.2f}≤买入价92%止损线={fixed_stop:.2f}，"
                    f"收益={return_pct:.2f}%"
                )
                return 0
            if peak >= entry_price * 1.08:
                state["construction_trailing_active"] = True
            if state.get("construction_trailing_active"):
                current_atr = float(atr_values[index] or 0) if len(atr_values) > index else 0
                trailing_pct = min(0.08, max(0.04, 2 * current_atr / peak)) if current_atr > 0 else 0.06
                trailing_stop = max(entry_price, peak * (1 - trailing_pct))
                if closes[index] <= trailing_stop:
                    state["construction_exit_reason"] = (
                        f"动态止盈：最高收盘={peak:.2f}，2ATR回撤比例={trailing_pct * 100:.2f}%，"
                        f"止盈线={trailing_stop:.2f}，当前收盘={closes[index]:.2f}，收益={return_pct:.2f}%"
                    )
                    return 0
            return 1
        armed_wave = state.get("construction_armed_wave")
        if not isinstance(armed_wave, dict):
            return 0
        confirmation_index = int(armed_wave["confirmation_index"])
        if index - confirmation_index > 60:
            state.pop("construction_armed_wave", None)
            return 0
        if index <= confirmation_index or len(kdj_j) <= index or kdj_j[index] >= 0:
            return 0
        # “下一次J<0”是一次性机会：即使MA200价格条件不满足，也不继续等待后续J<0。
        state.pop("construction_armed_wave", None)
        state["construction_consumed_wave_index"] = confirmation_index
        ma200 = ma200_values[index] if len(ma200_values) > index else None
        allowed = ma200 is not None and float(ma200) < closes[index] <= float(ma200) * 1.5
        if allowed:
            state["construction_entry_wave"] = armed_wave
            state["construction_entry_j"] = kdj_j[index]
            state["construction_entry_ma200"] = float(ma200)
            return 1
        return 0
    if strategy == "MA20/60首次回踩":
        fast, slow = _sma(closes, 20, index), _sma(closes, 60, index)
        previous_fast = _sma(closes, 20, index - 1)
        previous_slow = _sma(closes, 60, index - 1)
        if fast is None or slow is None:
            return previous
        if previous:
            return 0 if closes[index] < slow else 1
        if fast <= slow:
            state["pullback_armed"] = False
            return 0
        crossed = (
            previous_fast is not None
            and previous_slow is not None
            and previous_fast <= previous_slow
            and fast > slow
        )
        if crossed:
            state["pullback_armed"] = True
            return 0
        low = float(rows[index].get("low") or closes[index])
        if state.get("pullback_armed", False) and low <= fast and closes[index] >= fast:
            state["pullback_armed"] = False
            return 1
        return 0
    if strategy == "MA20/55金叉后J<13且涨幅<15%":
        fast, slow = _sma(closes, 20, index), _sma(closes, 55, index)
        previous_fast = _sma(closes, 20, index - 1)
        previous_slow = _sma(closes, 55, index - 1)
        slow_five_days_ago = _sma(closes, 55, index - 5)
        if fast is None or slow is None:
            return previous
        if previous:
            return 0 if closes[index] < slow else 1
        if fast <= slow:
            state["j_signal_armed"] = False
            state.pop("ma2055_cross_price", None)
            return 0
        crossed = (
            previous_fast is not None
            and previous_slow is not None
            and previous_fast <= previous_slow
            and fast > slow
        )
        if crossed:
            ma55_rising = slow_five_days_ago is not None and slow > slow_five_days_ago
            state["j_signal_armed"] = ma55_rising
            if ma55_rising:
                state["ma2055_cross_price"] = closes[index]
            else:
                state.pop("ma2055_cross_price", None)
            return 0
        cross_price = float(state.get("ma2055_cross_price") or closes[index])
        rise_pct = (closes[index] / cross_price - 1) * 100
        ma55_rising = slow_five_days_ago is not None and slow > slow_five_days_ago
        if state.get("j_signal_armed", False) and ma55_rising and kdj_j[index] < 13 and rise_pct < 15:
            state["j_signal_armed"] = False
            return 1
        return 0
    if strategy in {"MA120回踩5日不破", "MA200回踩5日不破"}:
        period = 120 if strategy.startswith("MA120") else 200
        support = _sma(closes, period, index)
        previous_support = _sma(closes, period, index - 1)
        ma250 = _sma(closes, 250, index) if period == 200 else None
        ma200_above_ma250 = period != 200 or (ma250 is not None and support is not None and support > ma250)
        if support is None:
            return previous
        if previous:
            return 0 if closes[index] < support else 1
        state_key = f"ma{period}_support_days"
        confirmed_days = int(state.get(state_key, 0))
        if confirmed_days:
            support_rising = previous_support is not None and support > previous_support
            high = float(rows[index].get("high") or closes[index])
            if high <= support or not support_rising or not ma200_above_ma250:
                state[state_key] = 0
                return 0
            confirmed_days += 1
            state[state_key] = confirmed_days
            if confirmed_days >= 5:
                state[state_key] = 0
                return 1 if closes[index] > support else 0
            return 0
        low = float(rows[index].get("low") or closes[index])
        touched_from_above = (
            previous_support is not None
            and support > previous_support
            and ma200_above_ma250
            and closes[index - 1] > previous_support
            and low <= support
        )
        if touched_from_above:
            state[state_key] = 1
        return 0
    value = rsi_values[index] if len(rsi_values) > index else None
    if value is None:
        return previous
    if value < 30:
        return 1
    if value > 70:
        return 0
    return previous


def _signal_reason(
    strategy: str,
    rows: list[dict[str, Any]],
    closes: list[float],
    index: int,
    target: int,
    kdj_j: list[float],
    rsi_values: list[float | None],
    state: dict[str, Any],
    trend_values: dict[str, Any] | None = None,
) -> str:
    if strategy == "买入持有":
        return "回测首日建立长期持仓"
    if strategy == "均线交叉":
        fast, slow = _sma(closes, 5, index), _sma(closes, 20, index)
        action = "MA5 高于 MA20" if target else "MA5 低于或等于 MA20"
        return f"{action}（{fast:.2f} / {slow:.2f}）" if fast is not None and slow is not None else action
    if strategy == "多周期趋势跟随":
        values = trend_values or {}
        ma20 = float((values.get("ma20") or [0])[index] or 0)
        ma60 = float((values.get("ma60") or [0])[index] or 0)
        ma200 = float((values.get("ma200") or [0])[index] or 0)
        if not target:
            return f"趋势退出：MA20跌破MA60（MA20={ma20:.2f}，MA60={ma60:.2f}），下一交易日开盘卖出"
        benchmark_closes = values.get("benchmark_closes") or []
        benchmark_ma200 = values.get("benchmark_ma200") or []
        stock_return = (closes[index] / closes[index - 60] - 1) * 100
        benchmark_return = (float(benchmark_closes[index]) / float(benchmark_closes[index - 60]) - 1) * 100
        benchmark_close = float(benchmark_closes[index])
        benchmark_ma = float(benchmark_ma200[index] or 0)
        return (
            f"趋势入场：MA20上穿MA60，MA60={ma60:.2f}>MA200={ma200:.2f}且MA200保持上升；"
            f"个股近60日收益={stock_return:.2f}%>沪深300={benchmark_return:.2f}%；"
            f"沪深300收盘={benchmark_close:.2f}>上升MA200={benchmark_ma:.2f}，下一交易日开盘买入"
        )
    if strategy in {"RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控"}:
        rsi = rsi_values[index] if len(rsi_values) > index else None
        if target:
            open_price = float(rows[index].get("open") or closes[index])
            signal_atr = float(state.get("rsi_risk_signal_atr") or 0)
            lowest_20 = min(float(row["low"]) for row in rows[max(0, index - 19):index + 1])
            rebound_from_low = (closes[index] / lowest_20 - 1) * 100 if lowest_20 > 0 else 0.0
            return f"RSI(14) 从30下方向上穿越且当日收阳，收盘已离开20日最低价{rebound_from_low:.2f}%（要求≥2%）（开盘={open_price:.2f}，收盘={closes[index]:.2f}，RSI={rsi:.2f}，ATR14={signal_atr:.2f}）；下一交易日开盘买入，采用1.5ATR初始止损与盈利8%后跟踪止盈"
        return str(state.get("rsi_risk_exit_reason") or "RSI止盈止损策略退出")
    if strategy == "KDJ急跌首阳T+1":
        if target:
            observation_index = int(state.get("kdj_entry_observation_index", max(0, index - 1)))
            bullish_index = int(state.get("kdj_entry_bullish_index", index))
            bullish_pct = float(state.get("kdj_entry_bullish_pct") or 0)
            average_daily_drop = (kdj_j[observation_index - 3] - kdj_j[observation_index]) / 3
            _, bullish_volume, bearish_volume = _red_fat_green_thin(rows, observation_index)
            volume_ratio = bullish_volume / bearish_volume if bearish_volume else 0
            observation_ma200 = float(state.get("kdj_observation_ma200") or 0)
            return (
                f"入场：{_trade_date(rows[observation_index])} 为观察日，J={kdj_j[observation_index]:.2f}、"
                f"收盘={closes[observation_index]:.2f} 高于 MA200={observation_ma200:.2f}；"
                f"近3个间隔J日均下降={average_daily_drop:.2f}；近10日红肥绿瘦，"
                f"阳线均量/阴线均量={volume_ratio:.2f}（要求≥1.30）；"
                f"{_trade_date(rows[bullish_index])} 出现观察后的首根阳线，涨幅={bullish_pct:.2f}%（不大于4%），"
                "下一交易日开盘买入"
            )
        return str(state.get("kdj_exit_reason") or "KDJ急跌红肥绿瘦策略退出")
    if strategy == "建仓波J<0动态止盈":
        if target:
            wave = state.get("construction_entry_wave") or {}
            impulse_index = int(wave.get("impulse_index", index))
            confirmation_index = int(wave.get("confirmation_index", index))
            ma200 = float(state.get("construction_entry_ma200") or 0)
            return (
                f"建仓波：{_trade_date(rows[impulse_index])} 放量上涨 {float(wave.get('rise_pct', 0)):.2f}%，"
                f"成交量为此前20日均量 {float(wave.get('volume_ratio', 0)):.2f} 倍；"
                f"随后 {int(wave.get('pullback_days', 0))} 日缩量回落，于 {_trade_date(rows[confirmation_index])} 确认；"
                f"确认后60日内首次J={float(state.get('construction_entry_j', 0)):.2f}<0，"
                f"收盘={closes[index]:.2f}，MA200={ma200:.2f}，处于MA200至1.5倍MA200之间；"
                "下一交易日开盘买入，固定止损-8%，盈利8%后启动2ATR动态止盈"
            )
        return str(state.get("construction_exit_reason") or "建仓波动态止盈策略退出")
    if strategy == "MA20/60首次回踩":
        fast, slow = _sma(closes, 20, index), _sma(closes, 60, index)
        if target:
            low = float(rows[index].get("low") or closes[index])
            return f"MA20 上穿 MA60 后首次回踩并收回 MA20（最低={low:.2f}，MA20={fast:.2f}）"
        return f"收盘价跌破 MA60（收盘={closes[index]:.2f}，MA60={slow:.2f}）"
    if strategy == "MA20/55金叉后J<13且涨幅<15%":
        fast, slow = _sma(closes, 20, index), _sma(closes, 55, index)
        if target:
            cross_price = float(state.get("ma2055_cross_price") or closes[index])
            rise_pct = (closes[index] / cross_price - 1) * 100
            slow_five_days_ago = _sma(closes, 55, index - 5)
            slope_pct = (slow / slow_five_days_ago - 1) * 100 if slow_five_days_ago else 0
            return f"MA20 上穿 MA55 后 J<13，且 MA55 向上、MA20 仍高于 MA55、距金叉涨幅<15%（MA20={fast:.2f}，MA55={slow:.2f}，MA55五日变化={slope_pct:.2f}%，J={kdj_j[index]:.2f}，涨幅={rise_pct:.2f}%）"
        return f"日线收盘确认跌破 MA55（收盘={closes[index]:.2f}，MA55={slow:.2f}）"
    if strategy in {"MA120回踩5日不破", "MA200回踩5日不破"}:
        period = 120 if strategy.startswith("MA120") else 200
        support = _sma(closes, period, index)
        if target:
            previous_support = _sma(closes, period, index - 1)
            slope_pct = (support / previous_support - 1) * 100 if previous_support else 0
            trend_filter = ""
            if period == 200:
                ma250 = _sma(closes, 250, index)
                trend_filter = f"，MA250={ma250:.2f}，MA200>MA250"
            return f"从上方回踩向上的 MA{period}，第 2～5 日最高价均过线且第 5 日收盘收回（收盘={closes[index]:.2f}，MA{period}={support:.2f}{trend_filter}，当日斜率={slope_pct:.3f}%）"
        return f"日线收盘跌破 MA{period}（收盘={closes[index]:.2f}，MA{period}={support:.2f}）"
    value = rsi_values[index] if len(rsi_values) > index else None
    action = "RSI 低于 30" if target else "RSI 高于 70"
    return f"{action}（RSI={value:.2f}）" if value is not None else action


def latest_strategy_signal(
    snapshot: dict[str, Any], strategy: str, benchmark_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """重放策略状态，返回最后一个交易日收盘生成、供下一交易日开盘执行的信号。"""
    if strategy not in STRATEGIES or strategy == "买入持有":
        return None
    rows = snapshot.get("points") or []
    if len(rows) < 25:
        return None
    closes = [float(row["close"]) for row in rows]
    if strategy == "多周期趋势跟随" and benchmark_snapshot is None:
        return None
    kdj_j = _snapshot_kdj_j_values(snapshot, rows) if strategy in {"MA20/55金叉后J<13且涨幅<15%", "KDJ急跌首阳T+1", "建仓波J<0动态止盈"} else []
    ma200_values = _snapshot_sma_values(snapshot, rows, 200) if strategy in {"KDJ急跌首阳T+1", "建仓波J<0动态止盈"} else []
    atr_values = _snapshot_atr_values(snapshot, rows) if strategy in {"RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控", "建仓波J<0动态止盈"} else []
    rsi_period = 17 if strategy == "RSI17反转+止盈止损" else 14
    rsi_values = _snapshot_rsi_values(snapshot, rows, rsi_period) if strategy in {"RSI反转", "RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控"} else []
    trend_values: dict[str, Any] = {}
    if strategy == "多周期趋势跟随":
        benchmark_closes, _, _, _ = _aligned_benchmark(rows, benchmark_snapshot)
        trend_values = {
            "ma20": _snapshot_sma_values(snapshot, rows, 20),
            "ma60": _snapshot_sma_values(snapshot, rows, 60),
            "ma200": _snapshot_sma_values(snapshot, rows, 200),
            "benchmark_closes": benchmark_closes,
            "benchmark_ma200": _aligned_benchmark_sma(rows, benchmark_snapshot, 200),
        }
    elif strategy == "建仓波J<0动态止盈":
        trend_values = {"construction_waves": _construction_wave_confirmations(rows)}
    state: dict[str, Any] = {"pullback_armed": False, "j_signal_armed": False}
    position = 0
    latest: dict[str, Any] | None = None
    for index in range(len(rows)):
        target = _target_position(strategy, rows, closes, index, position, state, kdj_j, ma200_values, atr_values, rsi_values, trend_values)
        if target != position:
            latest = {
                "side": "B" if target else "S",
                "timestamp": rows[index]["timestamp"],
                "price": closes[index],
                "reason": _signal_reason(strategy, rows, closes, index, target, kdj_j, rsi_values, state, trend_values),
            }
            if strategy == "KDJ急跌首阳T+1" and index + 1 < len(rows):
                if target:
                    execution_open = float(rows[index + 1].get("open") or closes[index + 1])
                    state["kdj_entry_price"] = execution_open
                    state["kdj_entry_index"] = index + 1
                    state["kdj_first_five_checked"] = False
                else:
                    for key in ("kdj_entry_price", "kdj_entry_index", "kdj_first_five_checked", "kdj_exit_reason", "kdj_entry_observation_index", "kdj_entry_bullish_index", "kdj_entry_bullish_pct"):
                        state.pop(key, None)
            if strategy in {"RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控"} and index + 1 < len(rows):
                if target:
                    entry_price = float(rows[index + 1].get("open") or closes[index + 1])
                    signal_atr = float(state.get("rsi_risk_signal_atr") or 0)
                    risk_pct = min(0.05, max(0.04, 1.5 * signal_atr / entry_price)) if signal_atr > 0 else 0.05
                    state["rsi_risk_entry_price"] = entry_price
                    state["rsi_risk_entry_index"] = index + 1
                    state["rsi_risk_peak_close"] = entry_price
                    state["rsi_risk_stop_price"] = entry_price * (1 - risk_pct)
                else:
                    for key in ("rsi_risk_entry_price", "rsi_risk_entry_index", "rsi_risk_exit_reason", "rsi_risk_peak_close", "rsi_risk_stop_price", "rsi_risk_trailing_active", "rsi_risk_signal_atr"):
                        state.pop(key, None)
            if strategy == "建仓波J<0动态止盈" and index + 1 < len(rows):
                if target:
                    entry_price = float(rows[index + 1].get("open") or closes[index + 1])
                    state["construction_entry_price"] = entry_price
                    state["construction_entry_index"] = index + 1
                    state["construction_peak_close"] = entry_price
                    state["construction_trailing_active"] = False
                else:
                    for key in ("construction_entry_price", "construction_entry_index", "construction_peak_close", "construction_trailing_active", "construction_exit_reason"):
                        state.pop(key, None)
            position = target
        elif index == len(rows) - 1:
            latest = None
    return latest


def run_backtest(
    snapshot: dict[str, Any],
    strategy: str,
    initial_capital: float = 100_000,
    fee_rate: float = 0.0003,
    benchmark_snapshot: dict[str, Any] | None = None,
    fundamental_reports: list[dict[str, Any]] | None = None,
    fundamental_score_threshold: float | None = None,
    market_signal_counts: dict[int, int] | None = None,
    market_signal_threshold: int = 0,
    slippage_bps: float = 0,
    volume_ratio_threshold: float = 0,
    benchmark_5d_drop_threshold: float = 0,
) -> dict[str, Any]:
    if strategy not in STRATEGIES:
        raise MarketDataError(f"不支持的回测策略：{strategy}")
    rows = snapshot.get("points") or []
    if len(rows) < 25:
        raise MarketDataError("回测至少需要 25 个交易日数据")
    if strategy == "MA20/60首次回踩" and len(rows) < 61:
        raise MarketDataError("MA20/60首次回踩策略至少需要 61 个交易日数据，请选择 6 个月或更长范围")
    if strategy == "MA20/55金叉后J<13且涨幅<15%" and len(rows) < 56:
        raise MarketDataError("MA20/55金叉后J<13且涨幅<15% 策略至少需要 56 个交易日数据，请选择 6 个月或更长范围")
    if strategy == "KDJ急跌首阳T+1" and len([*(snapshot.get("indicator_warmup_points") or []), *rows]) < 200:
        raise MarketDataError("KDJ急跌首阳T+1 策略需要至少 200 个交易日（可含预热数据）以计算 MA200，请选择 1 年或更长范围")
    if strategy == "建仓波J<0动态止盈" and len([*(snapshot.get("indicator_warmup_points") or []), *rows]) < 200:
        raise MarketDataError("建仓波J<0动态止盈策略需要至少 200 个交易日（可含预热数据）以计算 MA200")
    if strategy == "MA120回踩5日不破" and len(rows) < 126:
        raise MarketDataError("MA120回踩5日不破策略至少需要 126 个交易日数据，请选择 1 年或更长范围")
    if strategy == "MA200回踩5日不破" and len(rows) < 255:
        raise MarketDataError("MA200回踩5日不破策略至少需要 255 个交易日数据，以计算 MA250 趋势过滤，请选择 2 年或更长范围")
    if strategy == "多周期趋势跟随" and len(rows) < 61:
        raise MarketDataError("多周期趋势跟随至少需要61个回测交易日，并使用开始日前预热数据计算MA200")
    closes = [float(row["close"]) for row in rows]
    kdj_j = _snapshot_kdj_j_values(snapshot, rows) if strategy in {"MA20/55金叉后J<13且涨幅<15%", "KDJ急跌首阳T+1", "建仓波J<0动态止盈"} else []
    ma200_values = _snapshot_sma_values(snapshot, rows, 200) if strategy in {"KDJ急跌首阳T+1", "建仓波J<0动态止盈"} else []
    atr_values = _snapshot_atr_values(snapshot, rows) if strategy in {"RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控", "建仓波J<0动态止盈"} else []
    rsi_period = 17 if strategy == "RSI17反转+止盈止损" else 14
    rsi_values = _snapshot_rsi_values(snapshot, rows, rsi_period) if strategy in {"RSI反转", "RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控"} else []
    benchmark_closes, benchmark_first_open, benchmark_symbol, benchmark_name = _aligned_benchmark(rows, benchmark_snapshot)
    trend_values: dict[str, Any] = {}
    if strategy == "多周期趋势跟随":
        trend_values = {
            "ma20": _snapshot_sma_values(snapshot, rows, 20),
            "ma60": _snapshot_sma_values(snapshot, rows, 60),
            "ma200": _snapshot_sma_values(snapshot, rows, 200),
            "benchmark_closes": benchmark_closes,
            "benchmark_ma200": _aligned_benchmark_sma(rows, benchmark_snapshot, 200),
        }
    elif strategy == "建仓波J<0动态止盈":
        trend_values = {"construction_waves": _construction_wave_confirmations(rows)}
    equity = float(initial_capital)
    slippage_rate = max(0.0, float(slippage_bps)) / 10_000
    first_open = float(rows[0].get("open") or closes[0])
    if first_open <= 0:
        first_open = closes[0]
    benchmark = float(initial_capital) * (1 - fee_rate) * benchmark_closes[0] / benchmark_first_open
    position = 0
    wins = 0
    closed_trades = 0
    holding_days = 0
    entry_equity: float | None = None
    trade_events: list[dict[str, Any]] = []
    fundamental_filter_enabled = fundamental_score_threshold is not None
    reports = fundamental_reports or []
    fundamental_filtered_entries = 0
    fundamental_missing_entries = 0
    market_signal_filtered_entries = 0
    volume_ratio_filtered_entries = 0
    volume_ratio_threshold = max(0.0, float(volume_ratio_threshold))
    benchmark_5d_drop_threshold = max(0.0, float(benchmark_5d_drop_threshold))
    warmup_rows = snapshot.get("indicator_warmup_points") or []
    combined_rows = [*warmup_rows, *rows]
    volume_offset = len(warmup_rows)

    def signal_volume_ratio(row_index: int) -> float | None:
        combined_index = volume_offset + row_index
        recent_start = combined_index - 4
        prior_start = recent_start - 30
        if prior_start < 0:
            return None
        recent = [float(item.get("volume") or 0) for item in combined_rows[recent_start:combined_index + 1]]
        prior = [float(item.get("volume") or 0) for item in combined_rows[prior_start:recent_start]]
        if len(recent) != 5 or len(prior) != 30:
            return None
        prior_average = statistics.fmean(prior)
        return statistics.fmean(recent) / prior_average if prior_average > 0 else None
    strategy_state: dict[str, Any] = {"pullback_armed": False, "j_signal_armed": False}
    initial_fundamental = _fundamental_score_at(reports, rows[0]["timestamp"]) if fundamental_filter_enabled else None
    initial_allowed = not fundamental_filter_enabled or (
        initial_fundamental is not None and float(initial_fundamental["score"]) >= float(fundamental_score_threshold)
    )
    if strategy == "买入持有" and initial_allowed:
        initial_fill_price = first_open * (1 + slippage_rate)
        equity *= 1 - fee_rate
        position = 1
        entry_equity = equity
        trade_events.append({
            "side": "B", "timestamp": rows[0]["timestamp"], "price": round(initial_fill_price, 4),
            "signal_timestamp": rows[0]["timestamp"], "signal_price": round(first_open, 4),
            "equity": round(equity, 2), "curve_index": 0,
            "fundamental_score": float(initial_fundamental["score"]) if initial_fundamental else None,
            "reason": _signal_reason(strategy, rows, closes, 0, 1, kdj_j, rsi_values, strategy_state)
            + (f"；基本面评分={float(initial_fundamental['score']):.0f}，达到阈值{float(fundamental_score_threshold):.0f}" if initial_fundamental else ""),
        })
        equity *= closes[0] / initial_fill_price
        holding_days = 1
    curve = [{"timestamp": rows[0]["timestamp"], "equity": round(equity, 2), "benchmark": round(benchmark, 2)}]
    daily_returns: list[float] = []
    for index in range(1, len(rows)):
        before_day = equity
        target = _target_position(strategy, rows, closes, index - 1, position, strategy_state, kdj_j, ma200_values, atr_values, rsi_values, trend_values)
        signal_timestamp = int(rows[index - 1]["timestamp"])
        signal_count = int((market_signal_counts or {}).get(signal_timestamp, 0))
        entry_volume_ratio: float | None = None
        if position == 0 and target == 1 and market_signal_threshold > 0 and signal_count < market_signal_threshold:
            target = 0
            market_signal_filtered_entries += 1
        if position == 0 and target == 1 and benchmark_5d_drop_threshold > 0:
            benchmark_ok = False
            if index - 1 >= 5 and benchmark_closes[index - 6] > 0:
                benchmark_drop = benchmark_closes[index - 1] / benchmark_closes[index - 6] - 1
                benchmark_ok = benchmark_drop <= -benchmark_5d_drop_threshold
            if not benchmark_ok:
                target = 0
        if position == 0 and target == 1 and volume_ratio_threshold > 0:
            entry_volume_ratio = signal_volume_ratio(index - 1)
            if entry_volume_ratio is None or entry_volume_ratio + 1e-12 < volume_ratio_threshold:
                target = 0
                volume_ratio_filtered_entries += 1
        entry_fundamental: dict[str, Any] | None = None
        if position == 0 and target == 1 and fundamental_filter_enabled:
            entry_fundamental = _fundamental_score_at(reports, rows[index - 1]["timestamp"])
            if entry_fundamental is None:
                target = 0
                fundamental_missing_entries += 1
            elif float(entry_fundamental["score"]) < float(fundamental_score_threshold):
                target = 0
                fundamental_filtered_entries += 1
        if position == 1 or target == 1:
            holding_days += 1
        execution_index = index
        execution_row = rows[execution_index]
        execution_open = float(execution_row.get("open") or closes[execution_index])
        if execution_open <= 0:
            execution_open = closes[execution_index]
        if target != position:
            side = "B" if target == 1 else "S"
            execution_price = execution_open * (
                1 + slippage_rate if side == "B" else 1 - slippage_rate
            )
            if position == 1:
                equity *= execution_price / closes[index - 1]
            equity *= 1 - fee_rate
            if target == 1:
                entry_equity = equity
            else:
                closed_trades += 1
                if entry_equity is not None and equity > entry_equity:
                    wins += 1
                entry_equity = None
            reason = _signal_reason(strategy, rows, closes, index - 1, target, kdj_j, rsi_values, strategy_state, trend_values)
            if target == 1 and entry_fundamental is not None:
                reason += f"；基本面评分={float(entry_fundamental['score']):.0f}，达到阈值{float(fundamental_score_threshold):.0f}"
            if target == 1 and market_signal_threshold > 0:
                reason += f"；信号日全市场开仓信号={signal_count}，达到门槛{market_signal_threshold}"
            if target == 1 and volume_ratio_threshold > 0 and entry_volume_ratio is not None:
                reason += f"；量能比={entry_volume_ratio:.2f}，达到门槛{volume_ratio_threshold:.2f}"
            trade_events.append({
                "side": side,
                "timestamp": execution_row["timestamp"],
                "price": round(execution_price, 4),
                "signal_timestamp": rows[index - 1]["timestamp"],
                "signal_price": round(closes[index - 1], 4),
                "equity": round(equity, 2),
                "curve_index": index,
                "fundamental_score": float(entry_fundamental["score"]) if target == 1 and entry_fundamental else None,
                "reason": reason,
            })
            if strategy == "KDJ急跌首阳T+1":
                if target == 1:
                    strategy_state["kdj_entry_price"] = execution_price
                    strategy_state["kdj_entry_index"] = index
                    strategy_state["kdj_first_five_checked"] = False
                else:
                    for key in ("kdj_entry_price", "kdj_entry_index", "kdj_first_five_checked", "kdj_exit_reason", "kdj_entry_observation_index", "kdj_entry_bullish_index", "kdj_entry_bullish_pct"):
                        strategy_state.pop(key, None)
            if strategy in {"RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控"}:
                if target == 1:
                    signal_atr = float(strategy_state.get("rsi_risk_signal_atr") or 0)
                    crash_mode = bool(strategy_state.get("rsi_risk_crash")) if strategy == "RSI反转+暴跌强化风控" else False
                    risk_pct = min(0.03, max(0.03, 1.5 * signal_atr / execution_price)) if crash_mode else (min(0.05, max(0.04, 1.5 * signal_atr / execution_price)) if signal_atr > 0 else 0.05)
                    strategy_state["rsi_risk_entry_price"] = execution_price
                    strategy_state["rsi_risk_entry_index"] = index
                    strategy_state["rsi_risk_peak_close"] = execution_price
                    strategy_state["rsi_risk_stop_price"] = execution_price * (1 - risk_pct)
                    strategy_state["rsi_risk_trailing_active"] = False
                else:
                    for key in ("rsi_risk_entry_price", "rsi_risk_entry_index", "rsi_risk_exit_reason", "rsi_risk_peak_close", "rsi_risk_stop_price", "rsi_risk_trailing_active", "rsi_risk_signal_atr", "rsi_risk_crash"):
                        strategy_state.pop(key, None)
            if strategy == "建仓波J<0动态止盈":
                if target == 1:
                    strategy_state["construction_entry_price"] = execution_price
                    strategy_state["construction_entry_index"] = index
                    strategy_state["construction_peak_close"] = execution_price
                    strategy_state["construction_trailing_active"] = False
                else:
                    for key in ("construction_entry_price", "construction_entry_index", "construction_peak_close", "construction_trailing_active", "construction_exit_reason"):
                        strategy_state.pop(key, None)
            position = target
            if position == 1:
                equity *= closes[index] / execution_price
        elif position == 1:
            equity *= closes[index] / closes[index - 1]
        benchmark *= benchmark_closes[index] / benchmark_closes[index - 1]
        daily_returns.append(equity / before_day - 1 if before_day else 0)
        curve.append({"timestamp": rows[index]["timestamp"], "equity": round(equity, 2), "benchmark": round(benchmark, 2)})
    peak = curve[0]["equity"]
    max_drawdown = 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        max_drawdown = min(max_drawdown, point["equity"] / peak - 1)
    daily_std = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0
    sharpe = (statistics.fmean(daily_returns) / daily_std * math.sqrt(252)) if daily_std else 0
    holding_daily_return = (equity / initial_capital) ** (1 / holding_days) - 1 if holding_days else None
    elapsed_days = (int(rows[-1]["timestamp"]) - int(rows[0]["timestamp"])) / 86_400
    annualized_return = (equity / initial_capital) ** (365.2425 / elapsed_days) - 1 if elapsed_days > 0 and equity > 0 else None
    return {
        "symbol": snapshot["symbol"],
        "name": snapshot["name"],
        "strategy": strategy,
        "initial_capital": round(initial_capital, 2),
        "final_equity": round(equity, 2),
        "total_return_pct": round((equity / initial_capital - 1) * 100, 2),
        "annualized_return_pct": round(annualized_return * 100, 2) if annualized_return is not None else None,
        "holding_days": holding_days,
        "holding_daily_return_pct": round(holding_daily_return * 100, 4) if holding_daily_return is not None else None,
        "benchmark_return_pct": round((benchmark / initial_capital - 1) * 100, 2),
        "excess_return_pct": round((equity / initial_capital - benchmark / initial_capital) * 100, 2),
        "benchmark_symbol": benchmark_symbol,
        "benchmark_name": benchmark_name,
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "sharpe": round(sharpe, 2),
        "trades": len(trade_events),
        "closed_trades": closed_trades,
        "win_rate_pct": round(wins / closed_trades * 100, 2) if closed_trades else None,
        "position_open": bool(position),
        "period_points": len(rows),
        "curve": curve,
        "trade_events": trade_events,
        "data_source": snapshot["data_source"],
        "adjustment": snapshot.get("adjustment", "前复权"),
        "slippage_bps": round(float(slippage_bps), 4),
        "fundamental_filter": {
            "enabled": fundamental_filter_enabled,
            "threshold": float(fundamental_score_threshold) if fundamental_filter_enabled else None,
            "reports": len(reports),
            "filtered_entries": fundamental_filtered_entries,
            "missing_entries": fundamental_missing_entries,
            "source": reports[-1].get("source") if reports else None,
            "point_in_time": True,
        },
        "market_signal_filter": {
            "enabled": market_signal_threshold > 0,
            "threshold": market_signal_threshold if market_signal_threshold > 0 else None,
            "filtered_entries": market_signal_filtered_entries,
        },
        "volume_ratio_filter": {
            "enabled": volume_ratio_threshold > 0,
            "threshold": volume_ratio_threshold if volume_ratio_threshold > 0 else None,
            "definition": "信号日近5日均量/此前30日均量",
            "filtered_entries": volume_ratio_filtered_entries,
        },
        "adjustment_note": snapshot.get("adjustment_note"),
        "indicator_note": f"RSI(14) 使用同花顺 SMA(X,14,1) 递推口径；KDJ(9,3,3)、RSI 与 MA200 均使用{snapshot.get('adjustment', '前复权')}日线，开始日前预热 {len(snapshot.get('indicator_warmup_points') or [])} 根日线。",
        "warning": f"信号在前一交易日收盘生成、下一交易日{snapshot.get('adjustment', '前复权')}开盘成交；买卖单边滑点={float(slippage_bps):.2f}bps。历史回测不代表未来表现，仍未完整模拟印花税、停牌、涨跌停和成交容量约束。",
    }
