from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from itertools import groupby
from typing import Any

from ..database import connection


FACTOR_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "momentum_20",
        "name": "20日动量",
        "category": "动量",
        "formula": "Close(t) / Close(t-20) - 1",
        "direction": "越高越好",
        "data": "前复权日线",
    },
    {
        "id": "momentum_60",
        "name": "60日动量",
        "category": "动量",
        "formula": "Close(t) / Close(t-60) - 1",
        "direction": "越高越好",
        "data": "前复权日线",
    },
    {
        "id": "reversal_5",
        "name": "5日反转",
        "category": "反转",
        "formula": "-(Close(t) / Close(t-5) - 1)",
        "direction": "越高代表近期跌幅越大",
        "data": "前复权日线",
    },
    {
        "id": "low_volatility_20",
        "name": "20日低波动",
        "category": "低风险",
        "formula": "-Std(日收益率, 20)",
        "direction": "越高越稳定",
        "data": "前复权日线",
    },
    {
        "id": "volume_ratio_5_20",
        "name": "量能比 5/20",
        "category": "量价",
        "formula": "ln(近5日均量 / 此前20日均量)",
        "direction": "越高代表近期放量越明显",
        "data": "成交量",
    },
    {
        "id": "trend_strength_60",
        "name": "60日趋势强度",
        "category": "趋势",
        "formula": "Close(t) / MA60(t) - 1",
        "direction": "越高越强",
        "data": "前复权日线",
    },
    {
        "id": "fundamental_score",
        "name": "基本面综合分",
        "category": "质量",
        "formula": "财报盈利、增长、现金流与负债综合评分",
        "direction": "越高越好",
        "data": "按公告生效日匹配的历史财报",
    },
    {
        "id": "roe",
        "name": "ROE",
        "category": "质量",
        "formula": "最近一期已公告财报 ROE",
        "direction": "越高越好",
        "data": "按公告生效日匹配的历史财报",
    },
    {
        "id": "growth",
        "name": "成长质量",
        "category": "成长",
        "formula": "(营收同比增长 + 净利润同比增长) / 2",
        "direction": "越高越好",
        "data": "按公告生效日匹配的历史财报",
    },
)

FACTOR_IDS = {item["id"] for item in FACTOR_CATALOG}


def factor_catalog() -> list[dict[str, Any]]:
    return [dict(item) for item in FACTOR_CATALOG]


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        average_rank = (cursor + end - 1) / 2 + 1
        for position in range(cursor, end):
            ranks[order[position]] = average_rank
        cursor = end
    return ranks


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    left_mean, right_mean = statistics.fmean(left), statistics.fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_sum = sum((x - left_mean) ** 2 for x in left)
    right_sum = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_sum * right_sum)
    return numerator / denominator if denominator else None


def _spearman(left: list[float], right: list[float]) -> float | None:
    return _pearson(_rank(left), _rank(right))


def _winsorized_zscores(values: list[float]) -> list[float]:
    if not values:
        return []
    ordered = sorted(values)
    lower = ordered[max(0, int((len(ordered) - 1) * 0.025))]
    upper = ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.975))]
    clipped = [min(upper, max(lower, value)) for value in values]
    mean = statistics.fmean(clipped)
    deviation = statistics.pstdev(clipped)
    return [(value - mean) / deviation for value in clipped] if deviation else [0.0] * len(values)


def _latest_report(reports: list[dict[str, Any]], effective_dates: list[str], signal_date: str) -> dict[str, Any] | None:
    index = bisect_right(effective_dates, signal_date) - 1
    return reports[index] if index >= 0 else None


def calculate_factor_values(
    rows: list[dict[str, Any]],
    index: int,
    report: dict[str, Any] | None,
) -> dict[str, float | None]:
    closes = [float(row["close"]) for row in rows]
    volumes = [float(row["volume"] or 0) for row in rows]
    daily_returns = [closes[position] / closes[position - 1] - 1 for position in range(index - 19, index + 1)]
    previous_volume = volumes[index - 24:index - 4]
    recent_volume = volumes[index - 4:index + 1]
    previous_mean = statistics.fmean(previous_volume) if previous_volume else 0
    volume_ratio = statistics.fmean(recent_volume) / previous_mean if previous_mean > 0 else None
    revenue_growth = report.get("revenue_growth") if report else None
    profit_growth = report.get("profit_growth") if report else None
    growth_values = [float(value) for value in (revenue_growth, profit_growth) if value is not None]
    return {
        "momentum_20": closes[index] / closes[index - 20] - 1,
        "momentum_60": closes[index] / closes[index - 60] - 1,
        "reversal_5": -(closes[index] / closes[index - 5] - 1),
        "low_volatility_20": -statistics.pstdev(daily_returns),
        "volume_ratio_5_20": math.log(volume_ratio) if volume_ratio and volume_ratio > 0 else None,
        "trend_strength_60": closes[index] / statistics.fmean(closes[index - 59:index + 1]) - 1,
        "fundamental_score": float(report["score"]) if report and report.get("score") is not None else None,
        "roe": float(report["roe"]) if report and report.get("roe") is not None else None,
        "growth": statistics.fmean(growth_values) if growth_values else None,
    }


def _monthly_schedule(trading_dates: list[str], start_date: str, end_date: str, horizon: int) -> list[tuple[str, str]]:
    month_ends: dict[str, str] = {}
    for trading_date in trading_dates:
        if start_date <= trading_date <= end_date:
            month_ends[trading_date[:7]] = trading_date
    date_index = {trading_date: index for index, trading_date in enumerate(trading_dates)}
    schedule = []
    for signal_date in month_ends.values():
        target_index = date_index[signal_date] + horizon
        if target_index < len(trading_dates):
            schedule.append((signal_date, trading_dates[target_index]))
    return schedule


def _factor_summary(factor_id: str, rows_by_date: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    ic_series: list[dict[str, Any]] = []
    long_short_series: list[dict[str, Any]] = []
    cumulative = 1.0
    coverage: list[int] = []
    for signal_date, rows in sorted(rows_by_date.items()):
        usable = [row for row in rows if row.get(factor_id) is not None]
        if len(usable) < 20:
            continue
        values = [float(row[factor_id]) for row in usable]
        returns = [float(row["forward_return"]) for row in usable]
        ic = _spearman(values, returns)
        if ic is None:
            continue
        ranked = sorted(zip(values, returns), key=lambda item: item[0])
        group_size = max(1, len(ranked) // 5)
        long_short = statistics.fmean(item[1] for item in ranked[-group_size:]) - statistics.fmean(item[1] for item in ranked[:group_size])
        cumulative *= max(0.01, 1 + long_short)
        coverage.append(len(usable))
        ic_series.append({"date": signal_date, "value": round(ic, 4)})
        long_short_series.append({"date": signal_date, "period_return_pct": round(long_short * 100, 3), "cumulative_pct": round((cumulative - 1) * 100, 3)})
    ics = [point["value"] for point in ic_series]
    mean_ic = statistics.fmean(ics) if ics else 0.0
    ic_std = statistics.pstdev(ics) if len(ics) > 1 else 0.0
    return {
        "factor": factor_id,
        "mean_ic": round(mean_ic, 4),
        "ic_ir": round(mean_ic / ic_std, 3) if ic_std else 0.0,
        "positive_ic_rate_pct": round(sum(value > 0 for value in ics) / len(ics) * 100, 2) if ics else 0.0,
        "long_short_return_pct": round((cumulative - 1) * 100, 2) if long_short_series else 0.0,
        "periods": len(ics),
        "average_coverage": round(statistics.fmean(coverage), 1) if coverage else 0.0,
        "ic_series": ic_series,
        "long_short_series": long_short_series,
    }


def analyze_factors(
    factors: list[str],
    start_date: str,
    end_date: str,
    forward_days: int = 20,
    universe_limit: int = 1200,
) -> dict[str, Any]:
    unknown = [factor for factor in factors if factor not in FACTOR_IDS]
    if unknown:
        raise ValueError(f"未知因子：{', '.join(unknown)}")
    if start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
    warmup_start = (date.fromisoformat(start_date) - timedelta(days=180)).isoformat()
    forward_end = (date.fromisoformat(end_date) + timedelta(days=max(60, forward_days * 3))).isoformat()
    needs_fundamental = bool({"fundamental_score", "roe", "growth"}.intersection(factors))
    observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with connection() as database:
        # 沪深300交易日历走(symbol, trade_date)主键，避免为取日历扫描数百万根个股K线。
        calendar = [str(row[0]) for row in database.execute(
            """SELECT trade_date FROM daily_bars WHERE symbol='000300.SS'
               AND trade_date BETWEEN ? AND ? ORDER BY trade_date""",
            (warmup_start, forward_end),
        )]
        if len(calendar) < 80:
            calendar = [str(row[0]) for row in database.execute(
                """SELECT DISTINCT trade_date FROM daily_bars
                   WHERE trade_date BETWEEN ? AND ? AND source LIKE '%AKShare%'
                   ORDER BY trade_date""",
                (warmup_start, forward_end),
            )]
        schedule = _monthly_schedule(calendar, start_date, end_date, forward_days)
        if not schedule:
            raise ValueError("所选日期范围没有足够行情，或缺少计算未来收益所需的后续交易日")
        available_instruments = database.execute(
            "SELECT symbol, name FROM instruments WHERE market='A股' ORDER BY symbol"
        ).fetchall()
        if len(available_instruments) > universe_limit:
            # 子样本均匀覆盖完整代码序列，避免LIMIT只取到沪深市场前段代码。
            if universe_limit == 1:
                instruments = [available_instruments[0]]
            else:
                indexes = [round(index * (len(available_instruments) - 1) / (universe_limit - 1)) for index in range(universe_limit)]
                instruments = [available_instruments[index] for index in indexes]
        else:
            instruments = available_instruments
        instrument_names = {str(row["symbol"]): str(row["name"]) for row in instruments}
        symbols = list(instrument_names)
        if not symbols:
            raise ValueError("本地数据库没有A股股票列表，请先同步市场数据")
        placeholders = ",".join("?" for _ in symbols)
        reports_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if needs_fundamental and symbols:
            report_cursor = database.execute(
                f"""SELECT symbol, effective_date, report_date, score, roe,
                           revenue_growth, profit_growth
                    FROM fundamental_reports WHERE symbol IN ({placeholders}) AND effective_date<=?
                    ORDER BY symbol, effective_date, report_date""",
                (*symbols, end_date),
            )
            for row in report_cursor:
                reports_by_symbol[str(row["symbol"])].append(dict(row))
        bar_cursor = database.execute(
            f"""SELECT symbol, trade_date, close, volume FROM daily_bars
                WHERE symbol IN ({placeholders}) AND trade_date BETWEEN ? AND ?
                  AND source LIKE '%AKShare%' AND close>0
                ORDER BY symbol, trade_date""",
            (*symbols, warmup_start, forward_end),
        )
        for symbol, grouped_rows in groupby(bar_cursor, key=lambda row: str(row["symbol"])):
            bars = [dict(row) for row in grouped_rows]
            if len(bars) < 82:
                continue
            index_by_date = {str(row["trade_date"]): index for index, row in enumerate(bars)}
            reports = reports_by_symbol.get(symbol, [])
            effective_dates = [str(row["effective_date"]) for row in reports]
            for signal_date, target_date in schedule:
                index = index_by_date.get(signal_date)
                target_index = index_by_date.get(target_date)
                if index is None or target_index is None or index < 60 or target_index <= index:
                    continue
                report = _latest_report(reports, effective_dates, signal_date) if reports else None
                factor_values = calculate_factor_values(bars, index, report)
                observations[signal_date].append({
                    "symbol": symbol,
                    "name": instrument_names.get(symbol, symbol),
                    "forward_return": float(bars[target_index]["close"]) / float(bars[index]["close"]) - 1,
                    **{factor: factor_values.get(factor) for factor in factors},
                })

    if not observations:
        raise ValueError("没有形成可分析的横截面样本，请先同步所选区间的前复权行情")

    analysis_factors = list(factors)
    if len(factors) > 1:
        analysis_factors.append("composite")
        for rows in observations.values():
            factor_zscores: dict[str, dict[int, float]] = {}
            for factor in factors:
                positions = [index for index, row in enumerate(rows) if row.get(factor) is not None]
                zscores = _winsorized_zscores([float(rows[index][factor]) for index in positions])
                factor_zscores[factor] = dict(zip(positions, zscores))
            for index, row in enumerate(rows):
                values = [factor_zscores[factor][index] for factor in factors if index in factor_zscores[factor]]
                row["composite"] = statistics.fmean(values) if len(values) == len(factors) else None

    summaries = [_factor_summary(factor, observations) for factor in analysis_factors]
    matrix: list[list[float | None]] = []
    for left in factors:
        matrix_row: list[float | None] = []
        for right in factors:
            correlations = []
            for rows in observations.values():
                usable = [row for row in rows if row.get(left) is not None and row.get(right) is not None]
                if len(usable) < 20:
                    continue
                correlation = _spearman([float(row[left]) for row in usable], [float(row[right]) for row in usable])
                if correlation is not None:
                    correlations.append(correlation)
            matrix_row.append(round(statistics.fmean(correlations), 3) if correlations else None)
        matrix.append(matrix_row)

    catalog_by_id = {item["id"]: item for item in FACTOR_CATALOG}
    labels = {factor: catalog_by_id[factor]["name"] for factor in factors}
    if "composite" in analysis_factors:
        labels["composite"] = "等权复合因子"
    return {
        "parameters": {
            "start_date": start_date,
            "end_date": end_date,
            "forward_days": forward_days,
            "rebalance": "月末",
            "universe_limit": universe_limit,
            "requested_factors": factors,
        },
        "universe": {
            "instruments": len(instruments),
            "periods": len(observations),
            "latest_sample_date": max(observations),
        },
        "labels": labels,
        "summaries": summaries,
        "correlation": {"factors": factors, "matrix": matrix},
        "methodology": {
            "factor_timing": "价格与成交量仅使用调仓日收盘及此前数据；财报仅在 effective_date（公告生效日）之后可见。",
            "return_timing": f"因子在月末收盘计算，标签为随后 {forward_days} 个全市场交易日的前复权收盘收益。",
            "preprocessing": "每个调仓日横截面2.5%/97.5%缩尾后标准化；IC使用Spearman秩相关。",
            "long_short": "每期做多因子最高20%、做空最低20%的等权理论收益；未计手续费、涨跌停和冲击成本。",
            "bias_warning": "股票池来自本地数据库在区间内有行情的标的；若历史退市股未同步，仍可能存在幸存者偏差。",
        },
    }
