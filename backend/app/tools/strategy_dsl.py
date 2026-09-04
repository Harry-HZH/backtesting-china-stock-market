from __future__ import annotations

import ast
import math
import statistics
from typing import Any

from .indicators import atr, boll, kdj, macd, rsi, sma
from .market_data import MarketDataError
from .backtest import _aligned_benchmark, _fundamental_score_at


ALLOWED_FUNCTIONS = {
    "MA", "RSI", "KDJ_K", "KDJ_D", "KDJ_J", "ATR", "MACD_DIFF", "MACD_DEA", "MACD_HIST",
    "BOLL_MID", "BOLL_UPPER", "BOLL_LOWER", "REF", "CROSS_UP", "CROSS_DOWN",
    "GT", "GE", "LT", "LE", "EQ", "ALL", "ANY", "NOT",
}
PRICE_NAMES = {"OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"}


class StrategyCodeError(MarketDataError):
    pass


def parse_strategy_code(code: str) -> tuple[str, ast.AST, ast.AST]:
    if len(code) > 10_000:
        raise StrategyCodeError("策略代码过长")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise StrategyCodeError(f"策略代码语法错误：第 {exc.lineno} 行 {exc.msg}") from exc
    assignments: dict[str, ast.AST] = {}
    name = "AI 自定义策略"
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
            raise StrategyCodeError("策略代码只允许 NAME、ENTRY、EXIT 三个赋值语句")
        target = statement.targets[0].id
        if target not in {"NAME", "ENTRY", "EXIT"}:
            raise StrategyCodeError(f"不允许的赋值目标：{target}")
        if target == "NAME":
            if not isinstance(statement.value, ast.Constant) or not isinstance(statement.value.value, str):
                raise StrategyCodeError("NAME 必须是字符串")
            name = statement.value.value[:100]
        else:
            _validate_expression(statement.value)
            assignments[target] = statement.value
    if "ENTRY" not in assignments or "EXIT" not in assignments:
        raise StrategyCodeError("策略代码必须同时定义 ENTRY 和 EXIT")
    return name, assignments["ENTRY"], assignments["EXIT"]


def _validate_expression(node: ast.AST) -> None:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if not isinstance(child.func, ast.Name) or child.func.id not in ALLOWED_FUNCTIONS:
                raise StrategyCodeError("策略包含未授权函数")
            if child.keywords:
                raise StrategyCodeError("策略函数不支持关键字参数")
        elif isinstance(child, ast.Name):
            if child.id not in ALLOWED_FUNCTIONS | PRICE_NAMES:
                raise StrategyCodeError(f"策略包含未授权名称：{child.id}")
        elif isinstance(child, (ast.Expression, ast.Load, ast.Constant)):
            continue
        elif not isinstance(child, (ast.Call, ast.Name)):
            raise StrategyCodeError(f"策略包含不支持的语法：{type(child).__name__}")


def _series(value: Any, length: int) -> list[Any]:
    return value if isinstance(value, list) else [value] * length


def evaluate_strategy(code: str, rows: list[dict[str, Any]]) -> tuple[str, list[bool], list[bool]]:
    name, entry_ast, exit_ast = parse_strategy_code(code)
    context = {key: [float(row[key.lower()] or 0) for row in rows] for key in PRICE_NAMES}
    cache: dict[str, Any] = {}

    def evaluate(node: ast.AST) -> Any:
        key = ast.dump(node)
        if key in cache:
            return cache[key]
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return context[node.id]
        assert isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        function = node.func.id
        arguments = [evaluate(argument) for argument in node.args]
        closes = context["CLOSE"]
        if function == "MA":
            value = sma(closes, int(arguments[0]))
        elif function == "RSI":
            value = rsi(closes, int(arguments[0]) if arguments else 14)
        elif function in {"KDJ_K", "KDJ_D", "KDJ_J"}:
            value = kdj(rows, int(arguments[0]) if arguments else 9)[{"KDJ_K": 0, "KDJ_D": 1, "KDJ_J": 2}[function]]
        elif function == "ATR":
            value = atr(rows, int(arguments[0]) if arguments else 14)
        elif function.startswith("MACD_"):
            value = macd(closes)[{"MACD_DIFF": 0, "MACD_DEA": 1, "MACD_HIST": 2}[function]]
        elif function.startswith("BOLL_"):
            value = boll(closes, int(arguments[0]) if arguments else 20)[{"BOLL_MID": 0, "BOLL_UPPER": 1, "BOLL_LOWER": 2}[function]]
        elif function == "REF":
            source, periods = _series(arguments[0], len(rows)), int(arguments[1])
            if periods < 0:
                raise StrategyCodeError("REF 周期不能为负数")
            value = [None] * len(rows) if periods >= len(rows) else [None] * periods + source[:len(rows) - periods] if periods else source
        elif function in {"GT", "GE", "LT", "LE", "EQ"}:
            left, right = _series(arguments[0], len(rows)), _series(arguments[1], len(rows))
            operators = {
                "GT": lambda a, b: a > b, "GE": lambda a, b: a >= b, "LT": lambda a, b: a < b,
                "LE": lambda a, b: a <= b, "EQ": lambda a, b: a == b,
            }
            value = [False if a is None or b is None else operators[function](a, b) for a, b in zip(left, right)]
        elif function in {"CROSS_UP", "CROSS_DOWN"}:
            left, right = _series(arguments[0], len(rows)), _series(arguments[1], len(rows))
            value = [False]
            for index in range(1, len(rows)):
                current_valid = left[index] is not None and right[index] is not None
                previous_valid = left[index - 1] is not None and right[index - 1] is not None
                if not current_valid or not previous_valid:
                    value.append(False)
                elif function == "CROSS_UP":
                    value.append(left[index] > right[index] and left[index - 1] <= right[index - 1])
                else:
                    value.append(left[index] < right[index] and left[index - 1] >= right[index - 1])
        elif function in {"ALL", "ANY"}:
            inputs = [_series(argument, len(rows)) for argument in arguments]
            reducer = all if function == "ALL" else any
            value = [reducer(bool(series[index]) for series in inputs) for index in range(len(rows))]
        elif function == "NOT":
            value = [not bool(item) for item in _series(arguments[0], len(rows))]
        else:
            raise StrategyCodeError(f"尚未实现函数：{function}")
        cache[key] = value
        return value

    return name, [bool(value) for value in evaluate(entry_ast)], [bool(value) for value in evaluate(exit_ast)]


def describe_strategy_code(code: str) -> str:
    name, entry, exit_signal = parse_strategy_code(code)
    return f"策略“{name}”：当 `{ast.unparse(entry)}` 成立时，在下一交易日开盘买入；当 `{ast.unparse(exit_signal)}` 成立时，在下一交易日开盘卖出。行情与指标使用回测时选择的日线复权口径。"


def run_dsl_backtest(
    snapshot: dict[str, Any], code: str, initial_capital: float = 100_000,
    fee_rate: float = 0.0003, benchmark_snapshot: dict[str, Any] | None = None,
    fundamental_reports: list[dict[str, Any]] | None = None,
    fundamental_score_threshold: float | None = None,
    slippage_bps: float = 0,
) -> dict[str, Any]:
    rows = snapshot.get("points") or []
    if len(rows) < 25:
        raise StrategyCodeError("自定义策略回测至少需要 25 个交易日")
    name, entries, exits = evaluate_strategy(code, rows)
    closes = [float(row["close"]) for row in rows]
    benchmark_closes, benchmark_first_open, benchmark_symbol, benchmark_name = _aligned_benchmark(rows, benchmark_snapshot)
    equity = float(initial_capital)
    slippage_rate = max(0.0, float(slippage_bps)) / 10_000
    first_open = float(rows[0].get("open") or closes[0])
    benchmark = initial_capital * (1 - fee_rate) * benchmark_closes[0] / benchmark_first_open
    position = 0
    holding_days = 0
    entry_equity: float | None = None
    wins = closed_trades = 0
    curve = [{"timestamp": rows[0]["timestamp"], "equity": round(equity, 2), "benchmark": round(benchmark, 2)}]
    trades: list[dict[str, Any]] = []
    reports = fundamental_reports or []
    fundamental_filter_enabled = fundamental_score_threshold is not None
    fundamental_filtered_entries = fundamental_missing_entries = 0
    daily_returns: list[float] = []
    for index in range(1, len(rows)):
        before = equity
        target = 0 if position and exits[index - 1] else 1 if not position and entries[index - 1] else position
        entry_fundamental: dict[str, Any] | None = None
        if position == 0 and target == 1 and fundamental_filter_enabled:
            entry_fundamental = _fundamental_score_at(reports, rows[index - 1]["timestamp"])
            if entry_fundamental is None:
                target = 0
                fundamental_missing_entries += 1
            elif float(entry_fundamental["score"]) < float(fundamental_score_threshold):
                target = 0
                fundamental_filtered_entries += 1
        if position or target:
            holding_days += 1
        execution_open = float(rows[index].get("open") or closes[index])
        if target != position:
            side = "B" if target else "S"
            execution_price = execution_open * (
                1 + slippage_rate if side == "B" else 1 - slippage_rate
            )
            if position:
                equity *= execution_price / closes[index - 1]
            equity *= 1 - fee_rate
            if target:
                entry_equity = equity
            else:
                closed_trades += 1
                wins += int(entry_equity is not None and equity > entry_equity)
                entry_equity = None
            reason = "ENTRY 条件成立" if target else "EXIT 条件成立"
            if target and entry_fundamental is not None:
                reason += f"；基本面评分={float(entry_fundamental['score']):.0f}，达到阈值{float(fundamental_score_threshold):.0f}"
            trades.append({
                "side": side, "timestamp": rows[index]["timestamp"], "price": round(execution_price, 4),
                "signal_timestamp": rows[index - 1]["timestamp"], "signal_price": round(closes[index - 1], 4),
                "equity": round(equity, 2), "curve_index": index,
                "fundamental_score": float(entry_fundamental["score"]) if target and entry_fundamental else None,
                "reason": reason,
            })
            position = target
            if position:
                equity *= closes[index] / execution_price
        elif position:
            equity *= closes[index] / closes[index - 1]
        benchmark *= benchmark_closes[index] / benchmark_closes[index - 1]
        daily_returns.append(equity / before - 1 if before else 0)
        curve.append({"timestamp": rows[index]["timestamp"], "equity": round(equity, 2), "benchmark": round(benchmark, 2)})
    peak = curve[0]["equity"]
    drawdown = 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        drawdown = min(drawdown, point["equity"] / peak - 1)
    standard_deviation = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0
    sharpe = statistics.fmean(daily_returns) / standard_deviation * math.sqrt(252) if standard_deviation else 0
    daily_compound = (equity / initial_capital) ** (1 / holding_days) - 1 if holding_days else None
    elapsed_days = (int(rows[-1]["timestamp"]) - int(rows[0]["timestamp"])) / 86_400
    annualized_return = (equity / initial_capital) ** (365.2425 / elapsed_days) - 1 if elapsed_days > 0 and equity > 0 else None
    return {
        "symbol": snapshot["symbol"], "name": snapshot["name"], "strategy": name,
        "initial_capital": round(initial_capital, 2), "final_equity": round(equity, 2),
        "total_return_pct": round((equity / initial_capital - 1) * 100, 2),
        "annualized_return_pct": round(annualized_return * 100, 2) if annualized_return is not None else None,
        "holding_days": holding_days, "holding_daily_return_pct": round(daily_compound * 100, 4) if daily_compound is not None else None,
        "benchmark_return_pct": round((benchmark / initial_capital - 1) * 100, 2),
        "excess_return_pct": round((equity / initial_capital - benchmark / initial_capital) * 100, 2),
        "benchmark_symbol": benchmark_symbol, "benchmark_name": benchmark_name,
        "max_drawdown_pct": round(drawdown * 100, 2), "sharpe": round(sharpe, 2),
        "trades": len(trades), "closed_trades": closed_trades,
        "win_rate_pct": round(wins / closed_trades * 100, 2) if closed_trades else None,
        "position_open": bool(position), "period_points": len(rows), "curve": curve, "trade_events": trades,
        "data_source": snapshot["data_source"], "adjustment": snapshot.get("adjustment", "前复权"),
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
        "adjustment_note": snapshot.get("adjustment_note"),
        "warning": f"AI 策略代码已通过受限 DSL 执行；信号在收盘生成、下一交易日{snapshot.get('adjustment', '前复权')}开盘成交，买卖单边滑点={float(slippage_bps):.2f}bps。历史回测不代表未来表现。",
    }
