from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AnalyzeRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    market: Literal["自动", "A股", "港股", "美股"] = "自动"
    horizon: Literal["短线", "波段", "中长线"] = "波段"
    focus: str = Field(default="综合分析", max_length=500)
    period: Literal["3mo", "6mo", "1y", "2y", "5y"] = "6mo"

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


StrategyName = Literal["买入持有", "均线交叉", "RSI反转", "RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控", "多周期趋势跟随", "KDJ急跌首阳T+1", "建仓波J<0动态止盈", "MA20/60首次回踩", "MA20/55金叉后J<13且涨幅<15%", "MA120回踩5日不破", "MA200回踩5日不破"]
CapitalPoolStrategyName = Literal["均线交叉", "RSI反转", "RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控", "红利质量动量", "多因子月度轮动", "自适应趋势轮动", "纯A股ETF-V25", "纯A股ETF-14基线", "多周期趋势跟随", "KDJ急跌首阳T+1", "建仓波J<0动态止盈", "MA20/60首次回踩", "MA20/55金叉后J<13且涨幅<15%", "MA120回踩5日不破", "MA200回踩5日不破"]
AdjustmentMode = Literal["前复权", "后复权", "不复权", "动态前复权"]


class BacktestRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    market: Literal["自动", "A股", "港股", "美股"] = "自动"
    strategy: StrategyName = "均线交叉"
    initial_capital: float = Field(default=100000, ge=1000, le=100_000_000)
    fee_rate: float = Field(default=0.0003, ge=0, le=0.02)
    slippage_bps: float = Field(default=5, ge=0, le=100)
    period: Literal["3mo", "6mo", "1y", "2y", "5y"] = "2y"

    @field_validator("symbol")
    @classmethod
    def normalize_backtest_symbol(cls, value: str) -> str:
        return value.strip().upper()


class BatchBacktestRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=30)
    market: Literal["自动", "A股", "港股", "美股"] = "自动"
    strategies: list[StrategyName] = Field(default_factory=lambda: ["买入持有", "均线交叉", "RSI反转", "RSI反转+止盈止损", "多周期趋势跟随", "KDJ急跌首阳T+1", "建仓波J<0动态止盈", "MA20/60首次回踩", "MA20/55金叉后J<13且涨幅<15%", "MA120回踩5日不破", "MA200回踩5日不破"], min_length=1, max_length=11)
    initial_capital: float = Field(default=100000, ge=1000, le=100_000_000)
    fee_rate: float = Field(default=0.0003, ge=0, le=0.02)
    slippage_bps: float = Field(default=5, ge=0, le=100)
    period: Literal["3mo", "6mo", "1y", "2y", "5y"] = "5y"

    @field_validator("symbols")
    @classmethod
    def normalize_batch_symbols(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            symbol = value.strip().upper()
            if symbol and symbol not in normalized:
                normalized.append(symbol)
        if not normalized:
            raise ValueError("批量回测至少需要一个股票代码")
        return normalized

    @field_validator("strategies")
    @classmethod
    def deduplicate_strategies(cls, values: list[StrategyName]) -> list[StrategyName]:
        return list(dict.fromkeys(values))


class HistoricalBacktestRequest(BaseModel):
    name: str = Field(default="策略回测", min_length=1, max_length=100)
    symbols: list[str] = Field(min_length=1, max_length=30)
    market: Literal["自动", "A股", "港股", "美股"] = "自动"
    strategies: list[StrategyName] = Field(default_factory=list, max_length=11)
    strategy_code: str | None = Field(default=None, max_length=10_000)
    start_date: date = date(2020, 1, 1)
    end_date: date = Field(default_factory=date.today)
    initial_capital: float = Field(default=100000, ge=1000, le=100_000_000)
    fee_rate: float = Field(default=0.0003, ge=0, le=0.02)
    slippage_bps: float = Field(default=5, ge=0, le=100)
    refresh_data: bool = False
    fundamental_score_enabled: bool = False
    fundamental_score_threshold: float = Field(default=60, ge=0, le=100)
    adjustment_mode: AdjustmentMode = "前复权"

    @field_validator("symbols")
    @classmethod
    def normalize_history_symbols(cls, values: list[str]) -> list[str]:
        symbols = list(dict.fromkeys(value.strip().upper() for value in values if value.strip()))
        if not symbols:
            raise ValueError("至少需要一个股票代码")
        return symbols

    @field_validator("strategies")
    @classmethod
    def normalize_history_strategies(cls, values: list[StrategyName]) -> list[StrategyName]:
        return list(dict.fromkeys(values))


class MarketDataSyncRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, max_length=6000)
    start_date: date = date(2020, 1, 1)
    end_date: date = Field(default_factory=date.today)
    concurrency: int = Field(default=4, ge=1, le=4)
    mode: Literal["incremental", "full"] = "incremental"
    price_scope: Literal["all", "front_only"] = "all"


class FundamentalDataSyncRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, max_length=6000)
    concurrency: int = Field(default=2, ge=1, le=4)
    mode: Literal["incremental", "full"] = "incremental"


class DividendDataSyncRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, max_length=6000)
    concurrency: int = Field(default=2, ge=1, le=4)


class FullMarketBacktestRequest(BaseModel):
    name: str = Field(default="A股全市场回测", min_length=1, max_length=100)
    strategies: list[StrategyName] = Field(min_length=1, max_length=11)
    start_date: date = date(2020, 1, 1)
    end_date: date = Field(default_factory=date.today)
    initial_capital: float = Field(default=100000, ge=1000, le=100_000_000)
    fee_rate: float = Field(default=0.0003, ge=0, le=0.02)
    slippage_bps: float = Field(default=5, ge=0, le=100)
    fundamental_score_enabled: bool = False
    fundamental_score_threshold: float = Field(default=60, ge=0, le=100)
    adjustment_mode: AdjustmentMode = "前复权"
    market_signal_threshold: int = Field(default=0, ge=0, le=6000)
    market_signal_rate_threshold: float = Field(default=0, ge=0, le=1)
    volume_ratio_threshold: float = Field(default=0, ge=0, le=20)
    benchmark_5d_drop_threshold: float = Field(default=0, ge=0, le=0.5)


class CapitalPoolBacktestRequest(BaseModel):
    name: str = Field(default="A股资金池回测", min_length=1, max_length=100)
    strategy: CapitalPoolStrategyName = "RSI反转+止盈止损"
    adjustment_mode: AdjustmentMode = "动态前复权"
    candidate_ranking: Literal["rsi_rebound_score", "ten_day_decline_rank_15_39", "multifactor_score"] = "rsi_rebound_score"
    start_date: date = date(2020, 1, 1)
    end_date: date = Field(default_factory=date.today)
    initial_capital: float = Field(default=1_000_000, ge=10_000, le=1_000_000_000)
    fee_rate: float = Field(default=0.0003, ge=0, le=0.02)
    exposure_limit: float = Field(default=1.0, gt=0, le=1)
    single_position_limit: float = Field(default=0.04, gt=0, le=1)
    max_positions: int = Field(default=25, ge=1, le=100)
    volume_participation_limit: float = Field(default=1, gt=0, le=1)
    minimum_turnover: float = Field(default=0, ge=0, le=10_000_000_000)
    max_opening_gap_pct: float = Field(default=3, ge=0, le=20)
    market_signal_threshold: int = Field(default=300, ge=0, le=6000)
    market_signal_rate_threshold: float = Field(default=0, ge=0, le=1)
    volume_ratio_threshold: float = Field(default=0, ge=0, le=20)
    benchmark_5d_drop_threshold: float = Field(default=0, ge=0, le=0.5)
    fundamental_score_enabled: bool = False
    fundamental_score_threshold: float = Field(default=60, ge=0, le=100)
    require_above_ma200: bool = False
    require_ma200_rising: bool = False
    base_slippage_bps: float = Field(default=5, ge=0, le=100)
    impact_bps: float = Field(default=0, ge=0, le=500)
    factor_weights: dict[str, float] = Field(default_factory=lambda: {
        "low_volatility_20": 30,
        "fundamental_score": 25,
        "momentum_60": 20,
        "roe": 15,
        "reversal_5": 10,
    })

    @field_validator("factor_weights")
    @classmethod
    def validate_factor_weights(cls, values: dict[str, float]) -> dict[str, float]:
        allowed = {
            "momentum_20", "momentum_60", "reversal_5", "low_volatility_20",
            "volume_ratio_5_20", "trend_strength_60", "fundamental_score", "roe", "growth",
        }
        normalized = {
            key: float(value) for key, value in values.items()
            if key in allowed and 0 < float(value) <= 100
        }
        if not normalized:
            raise ValueError("多因子策略至少需要一个权重大于0的有效因子")
        return normalized


class CapitalPoolPlanRequest(BaseModel):
    """生成下一交易日资金池执行计划，不进行历史回测。"""
    strategy: CapitalPoolStrategyName = "RSI反转+止盈止损"
    adjustment_mode: AdjustmentMode = "动态前复权"
    start_date: date = date(2020, 1, 1)
    end_date: date = Field(default_factory=date.today)
    max_positions: int = Field(default=100, ge=1, le=100)
    market_signal_threshold: int = Field(default=0, ge=0, le=6000)
    max_opening_gap_pct: float = Field(default=3, ge=0, le=20)


class MarketScanRequest(BaseModel):
    strategies: list[StrategyName] = Field(min_length=1, max_length=11)
    start_date: date = date(2020, 1, 1)
    end_date: date = Field(default_factory=date.today)


class StrategyGenerateRequest(BaseModel):
    description: str = Field(min_length=5, max_length=3000)


class StrategyExplainRequest(BaseModel):
    code: str = Field(min_length=10, max_length=10_000)


class BacktestCompareRequest(BaseModel):
    run_ids: list[str] = Field(min_length=2, max_length=10)

    @field_validator("run_ids")
    @classmethod
    def unique_run_ids(cls, values: list[str]) -> list[str]:
        unique = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if len(unique) < 2:
            raise ValueError("至少选择两条不同的回测记录")
        return unique


class FactorAnalysisRequest(BaseModel):
    factors: list[str] = Field(min_length=1, max_length=9)
    start_date: date = date(2020, 1, 1)
    end_date: date = Field(default_factory=date.today)
    forward_days: int = Field(default=20, ge=5, le=60)
    universe_limit: int = Field(default=1200, ge=100, le=6000)

    @field_validator("factors")
    @classmethod
    def unique_factors(cls, values: list[str]) -> list[str]:
        unique = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not unique:
            raise ValueError("至少选择一个有效因子")
        return unique


class NewsRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100)
    lookback: Literal["1d", "3d", "7d", "30d"] = "3d"
    limit: int = Field(default=12, ge=3, le=30)
    focus: str = Field(default="事件影响与风险", max_length=300)
