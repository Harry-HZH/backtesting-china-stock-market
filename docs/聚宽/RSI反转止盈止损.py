"""
聚宽（JoinQuant）策略：RSI反转 + ATR止盈止损

规则：
1. RSI14 从不高于30向上穿越30；
2. 信号日收阳，且收盘至少高于最近20日最低价2%；
3. 下一交易日开盘买入；
4. 初始止损为1.5×ATR14，限制为买入价的4%～5%；
5. 最高收盘收益达到8%后，启用2×ATR14跟踪止盈，回撤限制4%～6%；
6. 持仓达到20个交易日且收盘收益低于3%时退出；
7. 不设置止损后的冷静期；退出后下一交易日重新独立判断信号。

使用方法：在聚宽“策略研究/回测”中新建策略，粘贴本文件内容。
修改 initialize() 中的 g.security 即可更换股票。
"""

import numpy as np


def initialize(context):
    # 示例：贵州茅台。深市代码示例：000001.XSHE
    g.security = "600519.XSHG"

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

    # 递推指标需要较长历史作为预热，600根通常足以消除初值影响。
    g.warmup_bars = 600

    # 持仓状态。
    g.entry_price = None
    g.entry_date = None
    g.holding_days = 0
    g.peak_close = None
    g.stop_price = None
    g.trailing_active = False

    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_slippage(FixedSlippage(0))

    # 与当前本地回测保持一致：买卖双边万分之三，暂不计印花税和最低佣金。
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

    # 09:30运行时，attribute_history(include_now=False)只读取上一交易日及以前的数据，
    # 因而实现“T日收盘确认，T+1日开盘下单”。
    run_daily(trade_at_open, time="open")


def trade_at_open(context):
    security = g.security
    current_data = get_current_data()
    current = current_data[security]

    if current.paused:
        return
    if current.is_st:
        return

    history = attribute_history(
        security,
        g.warmup_bars,
        unit="1d",
        fields=["open", "high", "low", "close"],
        skip_paused=True,
        df=False,
        fq="pre",
    )
    if history is None or len(history["close"]) < 25:
        return

    opens = np.asarray(history["open"], dtype=float)
    highs = np.asarray(history["high"], dtype=float)
    lows = np.asarray(history["low"], dtype=float)
    closes = np.asarray(history["close"], dtype=float)
    rsi_values = tonghuashun_rsi(closes, g.rsi_period)
    atr_values = wilder_atr(highs, lows, closes, g.atr_period)

    previous_close = float(closes[-1])
    current_atr = float(atr_values[-1]) if np.isfinite(atr_values[-1]) else 0.0
    position = context.portfolio.positions[security]
    has_position = position.total_amount > 0

    if has_position:
        synchronize_entry_state(context, position)
        g.holding_days += 1
        g.peak_close = max(float(g.peak_close or g.entry_price), previous_close)
        close_return = previous_close / float(g.entry_price) - 1

        exit_reason = None
        stopped = False

        if previous_close <= float(g.stop_price):
            exit_reason = "ATR初始止损"
            stopped = True
        else:
            if g.peak_close >= float(g.entry_price) * (1 + g.trailing_activation_pct):
                g.trailing_active = True

            if g.trailing_active:
                if current_atr > 0:
                    trailing_pct = g.trailing_atr_multiple * current_atr / g.peak_close
                    trailing_pct = min(g.max_trailing_pct, max(g.min_trailing_pct, trailing_pct))
                else:
                    trailing_pct = 0.05
                trailing_stop = max(float(g.entry_price), g.peak_close * (1 - trailing_pct))
                if previous_close <= trailing_stop:
                    exit_reason = "ATR跟踪止盈"

            if (
                exit_reason is None
                and g.holding_days >= g.time_exit_days
                and close_return < g.time_exit_min_return
            ):
                exit_reason = "20日低收益退出"

        if exit_reason is not None:
            order_target_value(security, 0)
            log.info(
                "%s：%s，昨收=%.3f，买入价=%.3f，收益=%.2f%%，持仓=%d日"
                % (security, exit_reason, previous_close, g.entry_price, close_return * 100, g.holding_days)
            )
            clear_position_state()
        return

    previous_rsi = rsi_values[-2]
    current_rsi = rsi_values[-1]
    if not np.isfinite(previous_rsi) or not np.isfinite(current_rsi):
        return

    rsi_crossed = previous_rsi <= g.rsi_entry < current_rsi
    bullish_candle = closes[-1] > opens[-1]
    lowest_20 = float(np.min(lows[-20:]))
    recovered_from_low = closes[-1] >= lowest_20 * (1 + g.rebound_from_low)

    if not (rsi_crossed and bullish_candle and recovered_from_low):
        return

    order = order_target_value(security, context.portfolio.total_value)
    if order is None:
        return

    # 聚宽市价单通常在当前撮合点成交；优先读取成交后的真实持仓成本。
    position = context.portfolio.positions[security]
    estimated_open = float(current.day_open or current.last_price or closes[-1])
    entry_price = float(position.avg_cost) if position.total_amount > 0 and position.avg_cost > 0 else estimated_open
    signal_atr = current_atr
    risk_pct = g.initial_atr_multiple * signal_atr / entry_price if signal_atr > 0 else g.max_stop_pct
    risk_pct = min(g.max_stop_pct, max(g.min_stop_pct, risk_pct))

    g.entry_price = entry_price
    g.entry_date = context.current_dt.date()
    # 成交日计为第1个持仓交易日，与本地回测保持一致。
    g.holding_days = 1
    g.peak_close = entry_price
    g.stop_price = entry_price * (1 - risk_pct)
    g.trailing_active = False

    log.info(
        (
            "%s：RSI反转买入，前RSI=%.2f，当前RSI=%.2f，昨收=%.3f，20日低点=%.3f，"
            "开盘成交估算=%.3f，初始止损=%.3f"
        )
        % (security, previous_rsi, current_rsi, closes[-1], lowest_20, entry_price, g.stop_price)
    )


def tonghuashun_rsi(closes, period=14):
    """同花顺：SMA(MAX(C-LC,0),N,1) / SMA(ABS(C-LC),N,1) × 100。"""
    result = np.full(len(closes), np.nan, dtype=float)
    if len(closes) < 2:
        return result

    first_change = closes[1] - closes[0]
    average_gain = max(first_change, 0.0)
    average_change = abs(first_change)
    result[1] = 50.0 if average_change == 0 else average_gain / average_change * 100

    for index in range(2, len(closes)):
        change = closes[index] - closes[index - 1]
        average_gain = (average_gain * (period - 1) + max(change, 0.0)) / period
        average_change = (average_change * (period - 1) + abs(change)) / period
        result[index] = 50.0 if average_change == 0 else average_gain / average_change * 100
    return result


def wilder_atr(highs, lows, closes, period=14):
    """与本地回测一致的 Wilder ATR14。"""
    result = np.full(len(closes), np.nan, dtype=float)
    if len(closes) < period:
        return result

    true_ranges = np.empty(len(closes), dtype=float)
    true_ranges[0] = highs[0] - lows[0]
    for index in range(1, len(closes)):
        true_ranges[index] = max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )

    current = float(np.mean(true_ranges[:period]))
    result[period - 1] = current
    for index in range(period, len(closes)):
        current = (current * (period - 1) + true_ranges[index]) / period
        result[index] = current
    return result


def synchronize_entry_state(context, position):
    """发生重载或成交价格与开盘估算不同时，用聚宽实际持仓成本修正状态。"""
    if g.entry_price is not None:
        return
    g.entry_price = float(position.avg_cost)
    g.entry_date = context.current_dt.date()
    g.holding_days = 0
    g.peak_close = g.entry_price
    g.stop_price = g.entry_price * (1 - g.max_stop_pct)
    g.trailing_active = False


def clear_position_state():
    g.entry_price = None
    g.entry_date = None
    g.holding_days = 0
    g.peak_close = None
    g.stop_price = None
    g.trailing_active = False
