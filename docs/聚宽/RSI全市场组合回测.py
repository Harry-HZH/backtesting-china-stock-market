"""
聚宽（JoinQuant）：RSI14反转 + ATR风控，共享资金池版

这是单资金账户的组合策略：
- 每天扫描当时已经上市的全部A股；
- 排除停牌和数据不足股票，是否排除ST可配置；
- 最多持有 g.max_positions 只，按目标仓位等权买入；
- 股票资金池最多使用账户资产的 g.capital_pool_limit_ratio，单只股票不超过
  g.max_single_position_ratio；候选不足时保留现金，不提高单票上限；
- 每天独立统计全市场原始RSI信号，达到 g.market_signal_threshold 才允许开仓；
- 次日开盘相对信号日收盘高开超过 g.max_opening_gap_pct 时放弃；
- 信号数量超过空位时，按本地资金池一致的低评分优先选择；
- 每只股票独立执行初始止损、跟踪止盈和时间退出，不使用冷静期。
- 可选量能比过滤：信号日前5日平均成交量 / 更早30日平均成交量，默认关闭。
"""

import numpy as np


def initialize(context):
    # 共享资金池初始资金：1000万元。
    set_subportfolios([SubPortfolioConfig(cash=10_000_000, type="stock")])

    g.max_positions = 100
    # 当前资金池：总仓位100%，单票1%，最多100只。
    g.capital_pool_limit_ratio = 1.0
    g.max_single_position_ratio = 0.01
    g.scan_chunk_size = 1000
    g.rsi_warmup_bars = 250

    # 最新版本使用 RSI14；可手动改成其他周期进行对照。
    g.rsi_period = 14
    g.rsi_entry = 30.0
    g.rebound_from_low = 0.02
    g.atr_period = 14
    g.initial_atr_multiple = 1.5
    g.min_stop_pct = 0.04
    g.max_stop_pct = 0.05
    g.trailing_activation_pct = 0.08
    g.trailing_atr_multiple = 2.0
    g.min_trailing_pct = 0.04
    g.max_trailing_pct = 0.06
    g.time_exit_days = 20
    g.time_exit_min_return = 0.03

    # 当前资金池开仓过滤参数。0表示关闭相应数值门槛。
    # 可手动调整；设为0关闭全市场信号门槛。
    g.market_signal_threshold = 350
    g.max_opening_gap_pct = 5.0
    g.base_slippage_bps = 5.0
    g.minimum_average_turnover = 0.0
    # 可选量价过滤：信号日前5日均量 / 更早30日均量；设为0关闭。
    g.volume_ratio_threshold = 0.0
    g.require_above_ma200 = False
    g.require_ma200_rising = False
    g.exclude_st = False

    # security -> 持仓风控状态
    g.position_states = {}
    # security -> RSI递推状态。首次出现时预热一次，之后每天只更新一根收盘价。
    g.rsi_states = {}
    g.buy_candidates = []
    g.sell_signals = {}
    g.daily_raw_signal_count = 0
    g.initial_total_value = float(context.portfolio.total_value)
    g.benchmark_start_close = None

    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_slippage(PriceRelatedSlippage(g.base_slippage_bps / 10000.0))
    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0,
            open_commission=0.0003,
            close_commission=0.0003,
            close_today_commission=0,
            min_commission=0,
        ),
        type="stock",
    )

    run_daily(prepare_signals, time="before_open")
    run_daily(execute_orders, time="open")
    run_daily(after_close_tasks, time="after_close")


def prepare_signals(context):
    g.buy_candidates = []
    g.sell_signals = {}
    g.daily_raw_signal_count = 0

    held = [
        security
        for security, position in context.portfolio.positions.items()
        if position.total_amount > 0
    ]
    prepare_exit_signals(context, held)

    universe = current_a_share_universe(context)
    # 先扫描完整股票池。市场信号数量必须独立于持仓状态和资金池空位。
    rsi_signals = update_rsi_states(universe)
    if not rsi_signals:
        log.info("全市场扫描完成：RSI上穿30候选为0")
        return

    candidate_stocks = [item[0] for item in rsi_signals]
    opens = batch_history(candidate_stocks, 1, "open", g.scan_chunk_size)
    lows = batch_history(candidate_stocks, 20, "low", g.scan_chunk_size)
    raw_confirmed = []
    for security, previous_rsi, current_rsi, close_price in rsi_signals:
        open_values = finite_values(opens.get(security))
        low_values = finite_values(lows.get(security))
        if not len(open_values) or len(low_values) < 20:
            continue
        bullish = close_price > open_values[-1]
        lowest_20 = float(np.min(low_values[-20:]))
        rebound_pct = close_price / lowest_20 - 1 if lowest_20 > 0 else 0
        if bullish and rebound_pct >= g.rebound_from_low:
            raw_confirmed.append({
                "security": security,
                "previous_rsi": previous_rsi,
                "current_rsi": current_rsi,
                "signal_close": close_price,
                "lowest_20": lowest_20,
                "rebound_pct": rebound_pct,
            })

    # 与本地回测一致：这里统计的是不受持仓、退出和资金限制影响的原始入场信号。
    g.daily_raw_signal_count = len(raw_confirmed)
    if g.daily_raw_signal_count < g.market_signal_threshold:
        log.info(
            "全市场原始RSI信号=%d，未达到开仓门槛=%d，当日不新增持仓"
            % (g.daily_raw_signal_count, g.market_signal_threshold)
        )
        return

    eligible = set(universe) - set(held)
    confirmed = [item for item in raw_confirmed if item["security"] in eligible]
    if not confirmed:
        log.info("全市场原始RSI信号=%d，但没有未持仓候选" % g.daily_raw_signal_count)
        return

    confirmed = apply_optional_entry_filters(confirmed)
    if not confirmed:
        log.info("全市场原始RSI信号=%d，附加开仓条件过滤后为0" % g.daily_raw_signal_count)
        return

    confirmed_stocks = [item["security"] for item in confirmed]
    highs = batch_history(confirmed_stocks, 100, "high", g.scan_chunk_size)
    atr_lows = batch_history(confirmed_stocks, 100, "low", g.scan_chunk_size)
    atr_closes = batch_history(confirmed_stocks, 100, "close", g.scan_chunk_size)

    final_candidates = []
    for item in confirmed:
        security = item["security"]
        current_atr = latest_atr(
            highs.get(security), atr_lows.get(security), atr_closes.get(security), g.atr_period
        )
        if current_atr is None:
            continue
        item["signal_atr"] = current_atr
        # 对齐本地资金池：反弹越少、RSI修复越低，评分越低并越优先。
        item["rank_score"] = item["rebound_pct"] * 100 + item["current_rsi"] / 20
        final_candidates.append(item)

    g.buy_candidates = sorted(
        final_candidates,
        key=lambda item: (item["rank_score"], item["current_rsi"]),
    )
    log.info(
        "全市场扫描完成：股票池=%d，原始RSI信号=%d，最终买入候选=%d"
        % (len(universe), g.daily_raw_signal_count, len(g.buy_candidates))
    )


def apply_optional_entry_filters(candidates):
    """附加条件只过滤个股候选，不改变全市场原始RSI信号数量。"""
    if not candidates:
        return []
    if (
        g.minimum_average_turnover <= 0
        and g.volume_ratio_threshold <= 0
        and not g.require_above_ma200
        and not g.require_ma200_rising
    ):
        return candidates

    stocks = [item["security"] for item in candidates]
    close_count = 205 if g.require_ma200_rising else 200 if g.require_above_ma200 else 20
    closes = batch_history(stocks, close_count, "close", g.scan_chunk_size)
    volumes = (
        batch_history(stocks, 35, "volume", g.scan_chunk_size)
        if g.minimum_average_turnover > 0 or g.volume_ratio_threshold > 0
        else {}
    )
    result = []
    for item in candidates:
        security = item["security"]
        close_values = finite_values(closes.get(security))
        if (g.require_above_ma200 or g.require_ma200_rising) and len(close_values) < close_count:
            continue
        if g.require_above_ma200:
            current_ma200 = float(np.mean(close_values[-200:]))
            if item["signal_close"] <= current_ma200:
                continue
        if g.require_ma200_rising:
            current_ma200 = float(np.mean(close_values[-200:]))
            prior_ma200 = float(np.mean(close_values[-205:-5]))
            if current_ma200 <= prior_ma200:
                continue
        if g.minimum_average_turnover > 0:
            close_source = closes.get(security)
            volume_source = volumes.get(security)
            if close_source is None or volume_source is None:
                continue
            raw_closes = np.asarray(close_source, dtype=float)[-20:]
            raw_volumes = np.asarray(volume_source, dtype=float)[-20:]
            if len(raw_closes) < 20 or len(raw_volumes) < 20:
                continue
            valid = np.isfinite(raw_closes) & np.isfinite(raw_volumes)
            if not np.any(valid):
                continue
            average_turnover = float(np.mean(raw_closes[valid] * raw_volumes[valid]))
            if average_turnover < g.minimum_average_turnover:
                continue
        if g.volume_ratio_threshold > 0:
            volume_values = finite_values(volumes.get(security))
            if len(volume_values) < 35:
                continue
            prior_average = float(np.mean(volume_values[-35:-5]))
            recent_average = float(np.mean(volume_values[-5:]))
            volume_ratio = recent_average / prior_average if prior_average > 0 else 0.0
            if volume_ratio < g.volume_ratio_threshold:
                continue
            item["volume_ratio"] = volume_ratio
        result.append(item)
    return result


def prepare_exit_signals(context, held):
    if not held:
        return
    highs = batch_history(held, 100, "high", g.scan_chunk_size)
    lows = batch_history(held, 100, "low", g.scan_chunk_size)
    closes = batch_history(held, 100, "close", g.scan_chunk_size)

    for security in held:
        position = context.portfolio.positions[security]
        state = ensure_position_state(context, security, position)
        close_values = finite_values(closes.get(security))
        if not len(close_values):
            continue
        previous_close = float(close_values[-1])
        current_atr = latest_atr(
            highs.get(security), lows.get(security), closes.get(security), g.atr_period
        )
        current_atr = float(current_atr or 0)

        state["holding_days"] += 1
        state["peak_close"] = max(float(state["peak_close"]), previous_close)
        close_return = previous_close / float(state["entry_price"]) - 1
        reason = None

        if previous_close <= float(state["stop_price"]):
            reason = "ATR初始止损"
        else:
            if state["peak_close"] >= state["entry_price"] * (1 + g.trailing_activation_pct):
                state["trailing_active"] = True
            if state["trailing_active"]:
                if current_atr > 0:
                    trailing_pct = g.trailing_atr_multiple * current_atr / state["peak_close"]
                    trailing_pct = min(g.max_trailing_pct, max(g.min_trailing_pct, trailing_pct))
                else:
                    trailing_pct = 0.05
                trailing_stop = max(
                    float(state["entry_price"]),
                    state["peak_close"] * (1 - trailing_pct),
                )
                if previous_close <= trailing_stop:
                    reason = "ATR跟踪止盈"
            if (
                reason is None
                and state["holding_days"] >= g.time_exit_days
                and close_return < g.time_exit_min_return
            ):
                reason = "20日低收益退出"

        if reason:
            g.sell_signals[security] = {
                "reason": reason,
                "close": previous_close,
                "return_pct": close_return * 100,
                "holding_days": state["holding_days"],
            }


def execute_orders(context):
    current_data = get_current_data()
    initial_held_count = sum(
        1 for position in context.portfolio.positions.values() if position.total_amount > 0
    )
    sold = 0
    for security, signal in list(g.sell_signals.items()):
        current = current_data[security]
        if current.paused or current.last_price <= current.low_limit:
            continue
        order = order_target_value(security, 0)
        if order is None:
            continue
        sold += 1
        g.position_states.pop(security, None)
        log.info(
            "%s：%s，昨收=%.3f，收益=%.2f%%，持仓=%d日"
            % (
                security,
                signal["reason"],
                signal["close"],
                signal["return_pct"],
                signal["holding_days"],
            )
        )

    available_slots = max(0, g.max_positions - initial_held_count + sold)
    if available_slots <= 0:
        return

    executable = []
    for item in g.buy_candidates:
        security = item["security"]
        current = current_data[security]
        if current.paused or (g.exclude_st and current.is_st) or current.last_price >= current.high_limit:
            continue
        if context.portfolio.positions[security].total_amount > 0:
            continue
        estimated_open = float(current.day_open or current.last_price or item["signal_close"])
        opening_gap_pct = (
            estimated_open / item["signal_close"] - 1
            if item["signal_close"] > 0
            else float("inf")
        )
        if opening_gap_pct * 100 > g.max_opening_gap_pct:
            log.info(
                "%s：次日开盘高开%.2f%%，超过上限%.2f%%，放弃买入"
                % (security, opening_gap_pct * 100, g.max_opening_gap_pct)
            )
            continue
        item["estimated_open"] = estimated_open
        item["opening_gap_pct"] = opening_gap_pct
        executable.append(item)
        if len(executable) >= available_slots:
            break
    if not executable:
        return

    total_value = float(context.portfolio.total_value)
    # 同一开盘待卖出的仓位不再占用新一轮资金池预算；实际成交失败时，聚宽的
    # 可用现金约束仍会阻止超额买入。
    invested_value = sum(
        float(getattr(position, "value", 0) or 0)
        for security, position in context.portfolio.positions.items()
        if position.total_amount > 0 and security not in g.sell_signals
    )
    pool_limit = total_value * g.capital_pool_limit_ratio
    remaining_pool = max(0.0, pool_limit - invested_value)
    target_value = min(
        total_value * g.max_single_position_ratio,
        remaining_pool / len(executable),
    )
    if target_value <= 0:
        return

    bought = 0
    for item in executable:
        security = item["security"]
        current = current_data[security]
        order = order_target_value(security, target_value)
        if order is None:
            continue

        estimated_open = float(item["estimated_open"])
        risk_pct = g.initial_atr_multiple * item["signal_atr"] / estimated_open
        risk_pct = min(g.max_stop_pct, max(g.min_stop_pct, risk_pct))
        g.position_states[security] = {
            "entry_price": estimated_open,
            # 本地回测将成交日计为第1个持仓交易日。
            "holding_days": 1,
            "peak_close": estimated_open,
            "risk_pct": risk_pct,
            "stop_price": estimated_open * (1 - risk_pct),
            "trailing_active": False,
            "needs_cost_sync": True,
        }
        bought += 1
        log.info(
            "%s：资金池入场，全市场信号=%d，RSI %.2f→%.2f，离20日低点 %.2f%%，高开 %.2f%%，止损 %.2f%%"
            % (
                security,
                g.daily_raw_signal_count,
                item["previous_rsi"],
                item["current_rsi"],
                item["rebound_pct"] * 100,
                item["opening_gap_pct"] * 100,
                risk_pct * 100,
            )
        )


def after_close_tasks(context):
    synchronize_costs(context)
    record_performance(context)


def record_performance(context):
    """在聚宽结果页输出策略、沪深300累计收益及股票资金使用率。"""
    benchmark = attribute_history(
        "000300.XSHG",
        2,
        unit="1d",
        fields=["close"],
        skip_paused=True,
        df=False,
        fq="pre",
    )
    closes = finite_values(benchmark.get("close") if benchmark else None)
    if not len(closes):
        return
    if g.benchmark_start_close is None:
        g.benchmark_start_close = float(closes[-2] if len(closes) >= 2 else closes[-1])

    strategy_return = (
        float(context.portfolio.total_value) / g.initial_total_value - 1
    ) * 100
    benchmark_return = (float(closes[-1]) / g.benchmark_start_close - 1) * 100
    invested_value = sum(
        float(getattr(position, "value", 0) or 0)
        for position in context.portfolio.positions.values()
        if position.total_amount > 0
    )
    capital_usage = (
        invested_value / float(context.portfolio.total_value) * 100
        if context.portfolio.total_value > 0
        else 0
    )
    record(策略累计收益=strategy_return, 沪深300累计收益=benchmark_return, 资金使用率=capital_usage)


def synchronize_costs(context):
    for security, position in context.portfolio.positions.items():
        if position.total_amount <= 0:
            continue
        state = g.position_states.get(security)
        if not state or not state.get("needs_cost_sync") or position.avg_cost <= 0:
            continue
        actual_cost = float(position.avg_cost)
        state["entry_price"] = actual_cost
        state["peak_close"] = actual_cost
        state["stop_price"] = actual_cost * (1 - state["risk_pct"])
        state["needs_cost_sync"] = False


def current_a_share_universe(context):
    securities = get_all_securities(types=["stock"], date=context.previous_date)
    current_data = get_current_data()
    result = []
    for security in securities.index:
        current = current_data[security]
        if current.paused:
            continue
        name = current.name or ""
        if g.exclude_st and (current.is_st or "退" in name or name.startswith("退市")):
            continue
        result.append(security)
    return result


def batch_history(securities, count, field, chunk_size):
    result = {}
    for start in range(0, len(securities), chunk_size):
        chunk = securities[start:start + chunk_size]
        frame = history(
            count,
            unit="1d",
            field=field,
            security_list=chunk,
            df=True,
            skip_paused=False,
            fq="pre",
        )
        for security in chunk:
            if security in frame.columns:
                result[security] = np.asarray(frame[security].values, dtype=float)
    return result


def finite_values(values):
    if values is None:
        return np.asarray([], dtype=float)
    values = np.asarray(values, dtype=float)
    return values[np.isfinite(values)]


def update_rsi_states(universe):
    """首次为股票预热RSI，之后每天用最新一根收盘价做O(1)递推。"""
    signals = []
    missing = [security for security in universe if security not in g.rsi_states]
    existing = [security for security in universe if security in g.rsi_states]

    # 新出现的股票（包括回测首日的全市场）只做一次250日预热。
    if missing:
        histories = batch_history(
            missing, g.rsi_warmup_bars, "close", g.scan_chunk_size
        )
        for security in missing:
            closes = finite_values(histories.get(security))
            state = seed_rsi_state(closes, g.rsi_period)
            if state is None:
                continue
            g.rsi_states[security] = state
            if state["previous_rsi"] <= g.rsi_entry < state["current_rsi"]:
                signals.append((
                    security,
                    state["previous_rsi"],
                    state["current_rsi"],
                    state["last_close"],
                ))

    # 已预热股票每天只读取上一交易日的一根收盘价。
    if existing:
        latest = batch_history(existing, 1, "close", g.scan_chunk_size)
        for security in existing:
            values = finite_values(latest.get(security))
            if not len(values):
                continue
            close_price = float(values[-1])
            state = g.rsi_states[security]
            change = close_price - state["last_close"]
            state["average_gain"] = (
                state["average_gain"] * (g.rsi_period - 1) + max(change, 0.0)
            ) / g.rsi_period
            state["average_change"] = (
                state["average_change"] * (g.rsi_period - 1) + abs(change)
            ) / g.rsi_period
            previous_rsi = state["current_rsi"]
            current_rsi = (
                50.0
                if state["average_change"] == 0
                else state["average_gain"] / state["average_change"] * 100
            )
            state["previous_rsi"] = previous_rsi
            state["current_rsi"] = current_rsi
            state["last_close"] = close_price
            if previous_rsi <= g.rsi_entry < current_rsi:
                signals.append((security, previous_rsi, current_rsi, close_price))
    return signals


def seed_rsi_state(closes, period=14):
    if len(closes) < 25:
        return None
    first_change = closes[1] - closes[0]
    average_gain = max(first_change, 0.0)
    average_change = abs(first_change)
    current_rsi = 50.0 if average_change == 0 else average_gain / average_change * 100
    previous_rsi = current_rsi
    for index in range(2, len(closes)):
        change = closes[index] - closes[index - 1]
        average_gain = (average_gain * (period - 1) + max(change, 0.0)) / period
        average_change = (average_change * (period - 1) + abs(change)) / period
        previous_rsi = current_rsi
        current_rsi = 50.0 if average_change == 0 else average_gain / average_change * 100
    return {
        "average_gain": float(average_gain),
        "average_change": float(average_change),
        "previous_rsi": float(previous_rsi),
        "current_rsi": float(current_rsi),
        "last_close": float(closes[-1]),
    }


def latest_atr(highs, lows, closes, period=14):
    if highs is None or lows is None or closes is None:
        return None
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)
    valid = np.isfinite(highs) & np.isfinite(lows) & np.isfinite(closes)
    highs, lows, closes = highs[valid], lows[valid], closes[valid]
    if len(closes) < period:
        return None

    true_ranges = np.empty(len(closes), dtype=float)
    true_ranges[0] = highs[0] - lows[0]
    for index in range(1, len(closes)):
        true_ranges[index] = max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
    current = float(np.mean(true_ranges[:period]))
    for index in range(period, len(true_ranges)):
        current = (current * (period - 1) + true_ranges[index]) / period
    return current


def ensure_position_state(context, security, position):
    state = g.position_states.get(security)
    if state:
        return state
    entry_price = float(position.avg_cost)
    state = {
        "entry_price": entry_price,
        "holding_days": 0,
        "peak_close": entry_price,
        "risk_pct": g.max_stop_pct,
        "stop_price": entry_price * (1 - g.max_stop_pct),
        "trailing_active": False,
        "needs_cost_sync": False,
    }
    g.position_states[security] = state
    return state
