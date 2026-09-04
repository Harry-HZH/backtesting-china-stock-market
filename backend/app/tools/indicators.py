from __future__ import annotations

import math
import statistics
from typing import Any


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def sma(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        result.append(running / period if index + 1 >= period else None)
    return result


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """同花顺 RSI：SMA(MAX(C-LC,0),N,1) / SMA(ABS(C-LC),N,1) * 100。"""
    result: list[float | None] = [None] * len(values)
    if len(values) < 2:
        return result
    first_change = values[1] - values[0]
    average_gain = max(first_change, 0)
    average_change = abs(first_change)
    result[1] = 50.0 if average_change == 0 else average_gain / average_change * 100
    for index in range(2, len(values)):
        change = values[index] - values[index - 1]
        average_gain = (average_gain * (period - 1) + max(change, 0)) / period
        average_change = (average_change * (period - 1) + abs(change)) / period
        result[index] = 50.0 if average_change == 0 else average_gain / average_change * 100
    return result


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float], list[float], list[float]]:
    fast_values, slow_values = ema(values, fast), ema(values, slow)
    diff = [fast_value - slow_value for fast_value, slow_value in zip(fast_values, slow_values)]
    dea = ema(diff, signal)
    histogram = [(diff_value - dea_value) * 2 for diff_value, dea_value in zip(diff, dea)]
    return diff, dea, histogram


def kdj(rows: list[dict[str, Any]], period: int = 9) -> tuple[list[float], list[float], list[float]]:
    k_value = 50.0
    d_value = 50.0
    k_values, d_values, j_values = [], [], []
    for index, row in enumerate(rows):
        window = rows[max(0, index + 1 - period):index + 1]
        lowest = min(float(item["low"]) for item in window)
        highest = max(float(item["high"]) for item in window)
        rsv = (float(row["close"]) - lowest) / (highest - lowest) * 100 if highest > lowest else 50.0
        k_value = k_value * 2 / 3 + rsv / 3
        d_value = d_value * 2 / 3 + k_value / 3
        k_values.append(k_value)
        d_values.append(d_value)
        j_values.append(3 * k_value - 2 * d_value)
    return k_values, d_values, j_values


def atr(rows: list[dict[str, Any]], period: int = 14) -> list[float | None]:
    true_ranges = []
    for index, row in enumerate(rows):
        high, low = float(row["high"]), float(row["low"])
        previous_close = float(rows[index - 1]["close"]) if index else float(row["close"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    result: list[float | None] = [None] * len(rows)
    if len(rows) < period:
        return result
    current = statistics.fmean(true_ranges[:period])
    result[period - 1] = current
    for index in range(period, len(rows)):
        current = (current * (period - 1) + true_ranges[index]) / period
        result[index] = current
    return result


def boll(values: list[float], period: int = 20, deviations: float = 2) -> tuple[list[float | None], list[float | None], list[float | None]]:
    middle = sma(values, period)
    upper: list[float | None] = []
    lower: list[float | None] = []
    for index, center in enumerate(middle):
        if center is None:
            upper.append(None)
            lower.append(None)
            continue
        deviation = statistics.pstdev(values[index + 1 - period:index + 1])
        upper.append(center + deviations * deviation)
        lower.append(center - deviations * deviation)
    return middle, upper, lower


def nine_turn(values: list[float]) -> list[int]:
    result = [0] * len(values)
    direction = 0
    count = 0
    for index in range(4, len(values)):
        current_direction = 1 if values[index] > values[index - 4] else -1 if values[index] < values[index - 4] else 0
        if current_direction == 0:
            direction, count = 0, 0
        elif current_direction == direction:
            count = count % 9 + 1
        else:
            direction, count = current_direction, 1
        result[index] = direction * count
    return result


def calculate_indicators(rows: list[dict[str, Any]], requested: list[str] | None = None) -> dict[str, list[float | int | None]]:
    names = {name.upper() for name in (requested or ["MA", "MACD", "RSI", "KDJ", "ATR", "九转", "BOLL"])}
    closes = [float(row["close"]) for row in rows]
    output: dict[str, list[float | int | None]] = {}
    if "MA" in names:
        for period in (5, 10, 20, 55, 60, 120, 200, 250):
            output[f"ma{period}"] = [_round(value) for value in sma(closes, period)]
    if "MACD" in names:
        diff, dea, histogram = macd(closes)
        output.update(macd_diff=[_round(value) for value in diff], macd_dea=[_round(value) for value in dea], macd_hist=[_round(value) for value in histogram])
    if "RSI" in names:
        output["rsi14"] = [_round(value) for value in rsi(closes)]
    if "KDJ" in names:
        k_values, d_values, j_values = kdj(rows)
        output.update(kdj_k=[_round(value) for value in k_values], kdj_d=[_round(value) for value in d_values], kdj_j=[_round(value) for value in j_values])
    if "ATR" in names:
        output["atr14"] = [_round(value) for value in atr(rows)]
    if "九转" in names or "NINE" in names:
        output["nine_turn"] = nine_turn(closes)
    if "BOLL" in names:
        middle, upper, lower = boll(closes)
        output.update(boll_mid=[_round(value) for value in middle], boll_upper=[_round(value) for value in upper], boll_lower=[_round(value) for value in lower])
    return output


def attach_indicators(
    rows: list[dict[str, Any]],
    requested: list[str] | None = None,
    warmup_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    warmup = warmup_rows or []
    combined = [*warmup, *rows]
    values = calculate_indicators(combined, requested)
    offset = len(warmup)
    return [
        {**row, **{name: series[offset + index] for name, series in values.items()}}
        for index, row in enumerate(rows)
    ]
