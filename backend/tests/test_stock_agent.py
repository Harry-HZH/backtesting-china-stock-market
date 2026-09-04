import asyncio
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
import statistics
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import httpx
import pandas as pd
from fastapi.testclient import TestClient

from backend.app.agent_service import stream_news_analysis, stream_stock_analysis
from backend.app.config import Settings
from backend.app.main import app
from backend.app.tools.market_data import (
    apply_price_adjustment,
    MarketDataError,
    fetch_stock_history_with_adjustments,
    fetch_stock_history,
    fetch_stock_snapshot,
    normalize_symbol,
    parse_chart_payload,
    parse_tencent_payload,
)
from backend.app.tools.backtest import (
    latest_strategy_signal, rsi_risk_raw_signal_timestamps, run_backtest, summarize_backtest_results,
)
from backend.app.tools.indicators import attach_indicators, calculate_indicators, rsi
from backend.app.tools.news_feed import fetch_stock_news, parse_news_rss
from backend.app.tools.portfolio_backtest import (
    build_capital_pool_result, build_dividend_quality_candidates, build_multifactor_candidates,
    build_pool_candidates, build_rsi_pool_candidates,
    select_capital_pool_trades,
)
from backend.app.tools.strategy_dsl import StrategyCodeError, describe_strategy_code, run_dsl_backtest
from backend.app.tools.market_universe import fetch_a_share_universe
from backend.app.tools.fundamentals import calculate_fundamental_score, parse_fundamental_payload
from backend.app.task_service import front_adjustment_changed, scan_market, sync_fundamental_data, sync_market_data
from backend.app.database import (
    adjustment_history_complete,
    complete_backtest_run,
    create_backtest_run,
    delete_backtest_run,
    get_backtest_run_summary,
    init_database,
    list_backtest_instruments,
    load_snapshot,
    list_backtest_result_summaries,
    list_backtest_runs,
    price_mode_coverage,
    save_backtest_result,
    save_snapshot,
    upsert_instruments,
)


DEMO_SETTINGS = Settings(
    OPENAI_API_KEY=None,
    OPENAI_MODEL="demo",
    OPENAI_BASE_URL=None,
    CORS_ORIGINS="http://localhost:5173",
)


def chart_payload() -> dict:
    closes = [100 + index * 0.5 for index in range(80)]
    timestamps = list(range(1_700_000_000, 1_700_000_000 + len(closes) * 86_400, 86_400))
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": "600519.SS", "longName": "测试公司", "currency": "CNY", "exchangeName": "SHH"},
        "timestamp": timestamps,
        "indicators": {
            "quote": [{
                "open": closes.copy(), "high": [value + 1 for value in closes],
                "low": [value - 1 for value in closes], "close": closes.copy(),
                "volume": [1_000_000] * len(closes),
            }],
            "adjclose": [{"adjclose": closes.copy()}],
        },
    }]}}


def kdj_t2_snapshot(length: int = 50, *, stop_index: int | None = None, surge_index: int | None = None) -> dict:
    rows = []
    for index in range(length):
        close = 101.0
        if index == stop_index:
            close = 96.0
        if index == surge_index:
            close = 105.0
        rows.append({
            "timestamp": 1_700_000_000 + index * 86_400,
            "open": 100.0,
            "high": max(101.5, close + 0.5),
            "low": min(99.0, close - 0.5),
            "close": close,
            "volume": 1_000_000,
        })
    # 观察日前10日构造“阳线放量、阴线缩量”，阳线均量/阴线均量=2.6。
    for index in range(15, min(25, length)):
        if index % 2:
            rows[index].update(close=99.0, high=101.5, low=98.5, volume=500_000)
        else:
            rows[index].update(close=101.0, high=101.5, low=99.0, volume=1_300_000)
    return {
        "symbol": "TEST.SS",
        "name": "KDJ T+2测试",
        "points": rows,
        "indicator_warmup_points": [
            {
                "timestamp": 1_700_000_000 - (210 - index) * 86_400,
                "open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0, "volume": 1_000_000,
            }
            for index in range(210)
        ],
        "data_source": "test",
        "adjustment": "前复权",
    }


def rapid_drop_j_values(length: int) -> list[float]:
    values = [50.0] * length
    values[21:25] = [70.0, 50.0, 30.0, 10.0]
    return values


def eastmoney_payload() -> dict:
    lines = []
    for index in range(80):
        value = 100 + index * 0.5
        lines.append(f"2026-01-{(index % 28) + 1:02d},{value},{value},{value + 1},{value - 1},1000000")
    return {"data": {"code": "600519", "name": "测试公司", "klines": lines}}


def tencent_payload(code: str, dates: list[date]) -> dict:
    lines = []
    for index, trade_date in enumerate(dates):
        value = 100 + index * 0.5
        lines.append([trade_date.isoformat(), str(value), str(value), str(value + 1), str(value - 1), "1000000"])
    return {"data": {code: {"qfqday": lines, "qt": {code: [code, "测试公司"]}}}}


def akshare_frame(dates: list[date], scale: float = 1.0) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "日期": trade_date,
            "开盘": (100 + index * 0.5) * scale,
            "收盘": (100 + index * 0.5) * scale,
            "最高": (101 + index * 0.5) * scale,
            "最低": (99 + index * 0.5) * scale,
            "成交量": 10_000,
        }
        for index, trade_date in enumerate(dates)
    ])


class SymbolTests(unittest.TestCase):
    def test_normalizes_a_share_hk_and_us_symbols(self) -> None:
        self.assertEqual(normalize_symbol("600519", "自动"), "600519.SS")
        self.assertEqual(normalize_symbol("000001", "A股"), "000001.SZ")
        self.assertEqual(normalize_symbol("830799", "A股"), "830799.BJ")
        self.assertEqual(normalize_symbol("920992", "A股"), "920992.BJ")
        self.assertEqual(normalize_symbol("00700", "港股"), "0700.HK")
        self.assertEqual(normalize_symbol("aapl", "自动"), "AAPL")


class MarketDataTests(unittest.TestCase):
    def test_market_scan_counts_only_buy_as_opening_signal(self) -> None:
        updates = []
        job = {"payload": {
            "strategies": ["RSI反转+止盈止损"],
            "start_date": "2026-01-01", "end_date": "2026-01-10",
        }}
        instruments = [{"symbol": "000001.SZ"}, {"symbol": "000002.SZ"}]
        signals = iter([
            {"side": "B", "timestamp": int(datetime(2026, 1, 9, tzinfo=timezone.utc).timestamp()), "price": 10, "reason": "建议买入"},
            {"side": "S", "timestamp": int(datetime(2026, 1, 9, tzinfo=timezone.utc).timestamp()), "price": 9, "reason": "卖出提醒"},
        ])
        with (
            patch("backend.app.task_service.get_job", return_value=job),
            patch("backend.app.task_service.list_instruments", return_value=instruments),
            patch("backend.app.task_service.latest_market_trade_date", return_value="2026-01-09"),
            patch("backend.app.task_service.load_snapshot", return_value={"points": [{}, {}]}),
            patch("backend.app.task_service.latest_strategy_signal", side_effect=lambda *args: next(signals)),
            patch("backend.app.task_service.save_scan_signal"),
            patch("backend.app.task_service.sync_strategy_position"),
            patch("backend.app.task_service.update_job", side_effect=lambda *args, **kwargs: updates.append(kwargs)),
        ):
            asyncio.run(scan_market("scan-job"))
        completed = next(item for item in reversed(updates) if item.get("status") == "completed")
        self.assertEqual(completed["result"]["signals"], 1)
        self.assertEqual(completed["result"]["buy_signals"], 1)
        self.assertEqual(completed["result"]["sell_signals"], 1)

    def test_dividend_quality_candidate_uses_only_announced_paid_dividends(self) -> None:
        from datetime import datetime, timezone
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        points = []
        for index in range(340):
            trade_day = start + timedelta(days=index)
            close = 10 + index * 0.01
            points.append({"timestamp": int(trade_day.timestamp()), "open": close, "high": close * 1.01,
                           "low": close * 0.99, "close": close, "volume": 1_000_000, "raw_close": close})
        snapshot = {"symbol": "600001.SS", "name": "红利测试", "points": points[250:],
                    "indicator_warmup_points": points[:250]}
        reports = [{"effective_date": "2024-01-01", "score": 80, "roe": 12,
                    "revenue_growth": 5, "profit_growth": 6, "operating_cash": 120,
                    "net_profit": 100, "debt_ratio": 40,
                    "raw": {"ZZCJLL": 5, "EPSJB": 1}}]
        dividends = [
            {"announcement_date": "2023-02-01", "ex_date": "2023-03-01", "cash_per_10": 5},
            {"announcement_date": "2024-02-01", "ex_date": "2024-03-01", "cash_per_10": 5},
            {"announcement_date": "2026-01-01", "ex_date": "2024-04-01", "cash_per_10": 50},
        ]
        candidates = build_dividend_quality_candidates(snapshot, reports, dividends)
        self.assertTrue(candidates)
        self.assertTrue(all(candidate["dividend_yield"] < 0.10 for candidate in candidates))

    def test_loaded_snapshot_excludes_zero_price_rows(self) -> None:
        from backend.app.database import connection, load_snapshot, save_snapshot

        symbol = "920999.BJ"
        snapshot = {
            "requested_symbol": symbol, "symbol": symbol, "name": "零价格测试",
            "exchange": "北京证券交易所", "currency": "CNY", "adjustment": "前复权",
            "data_source": "AKShare stock_zh_a_hist 前复权", "points": [
                {"timestamp": 1_700_000_000, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000},
                {"timestamp": 1_700_086_400, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 1000},
                {"timestamp": 1_700_172_800, "open": 11, "high": 12, "low": 10, "close": 11, "volume": 1000},
            ],
        }
        save_snapshot(snapshot, "A股")
        loaded = load_snapshot(symbol, "2023-11-01", "2023-12-01", "A股")
        self.assertIsNotNone(loaded)
        self.assertEqual([row["close"] for row in loaded["points"]], [10.0, 11.0])
        with connection() as database:
            database.execute("DELETE FROM daily_bars WHERE symbol=?", (symbol,))
            database.execute("DELETE FROM instruments WHERE symbol=?", (symbol,))

    def test_fundamental_fetch_maps_local_ss_suffix_to_eastmoney_sh(self) -> None:
        from backend.app.tools.fundamentals import fetch_fundamental_reports

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["filter"] = request.url.params.get("filter")
            return httpx.Response(200, json={
                "success": True,
                "result": {"data": [{
                    "SECUCODE": "600000.SH", "REPORT_DATE": "2025-12-31",
                    "NOTICE_DATE": "2026-03-30", "ROEJQ": 10,
                }]},
            })

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reports = fetch_fundamental_reports("600000.SS", client)
        client.close()
        self.assertEqual(captured["filter"], '(SECUCODE="600000.SH")')
        self.assertEqual(reports[0]["symbol"], "600000.SS")

    def test_capital_pool_request_accepts_twenty_five_positions_at_four_percent(self) -> None:
        from backend.app.schemas import CapitalPoolBacktestRequest

        request = CapitalPoolBacktestRequest(
            exposure_limit=1, single_position_limit=0.04, max_positions=25,
        )
        self.assertEqual(request.exposure_limit, 1)
        self.assertEqual(request.single_position_limit, 0.04)
        self.assertEqual(request.max_positions, 25)

    def test_multifactor_pool_ranks_higher_factor_score_first(self) -> None:
        common = {
            "entry_timestamp": 2, "entry_price": 10.0, "entry_volume": 10_000_000,
            "exit_timestamp": 3, "exit_price": 11.0, "exit_volume": 10_000_000,
            "score": 0, "average_turnover": 100_000_000, "opening_gap_pct": 0,
            "fundamental_score": 80, "signal_timestamp": 1,
            "entry_reason": "test", "exit_reason": "test",
        }
        candidates = [
            {**common, "symbol": "000001.SZ", "name": "低分", "factor_values": {"low_volatility_20": -0.05}},
            {**common, "symbol": "000002.SZ", "name": "高分", "factor_values": {"low_volatility_20": -0.01}},
        ]
        selected = select_capital_pool_trades(
            candidates, 1_000_000, 0, 1, 1, 1, 1, 0, 0,
            "multifactor", {"low_volatility_20": 100},
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["symbol"], "000002.SZ")
        self.assertIn("多因子综合排名", selected[0]["entry_reason"])

    def test_capital_pool_request_accepts_multifactor_weights(self) -> None:
        from backend.app.schemas import CapitalPoolBacktestRequest

        request = CapitalPoolBacktestRequest(
            strategy="多因子月度轮动",
            factor_weights={"low_volatility_20": 60, "fundamental_score": 40, "unknown": 50},
        )
        self.assertEqual(request.strategy, "多因子月度轮动")
        self.assertEqual(request.factor_weights, {"low_volatility_20": 60.0, "fundamental_score": 40.0})

    def test_fetches_qfq_and_raw_prices_to_derive_adjustment_factors(self) -> None:
        dates = [date(2026, 1, 5), date(2026, 1, 6)]

        def history(**kwargs):
            return akshare_frame(dates, {"qfq": 1.0, "": 2.0, "hfq": 4.0}[kwargs["adjust"]])

        with patch("backend.app.tools.market_data.ak.stock_zh_a_hist", side_effect=history) as fetch:
            snapshot = fetch_stock_history_with_adjustments("600519", "A股", "2026-01-05", "2026-01-06")
        self.assertEqual([call.kwargs["adjust"] for call in fetch.call_args_list], ["qfq", "", "hfq"])
        self.assertEqual(snapshot["points"][0]["raw_close"], 200.0)
        self.assertEqual(snapshot["points"][0]["hfq_close"], 400.0)
        self.assertEqual(snapshot["points"][0]["adjustment_factor"], 2.0)
        self.assertTrue(snapshot["has_adjustment_factors"])

    def test_adjustment_fetch_keeps_common_dates_when_only_trailing_date_is_pending(self) -> None:
        qfq_dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
        complete_dates = qfq_dates[:2]

        def history(**kwargs):
            dates = qfq_dates if kwargs["adjust"] == "qfq" else complete_dates
            return akshare_frame(dates)

        with patch("backend.app.tools.market_data.ak.stock_zh_a_hist", side_effect=history):
            snapshot = fetch_stock_history_with_adjustments("600519", "A股", "2026-01-05", "2026-01-07")
        self.assertEqual(len(snapshot["points"]), 2)
        self.assertEqual(snapshot["pending_adjustment_dates"], ["2026-01-07"])
        self.assertTrue(all(row.get("adjustment_factor") for row in snapshot["points"]))

    def test_adjustment_modes_generate_raw_hfq_and_dynamic_prices(self) -> None:
        points = [
            {"timestamp": 1, "open": 9, "high": 9, "low": 9, "close": 9, "volume": 1, "raw_open": 10, "raw_high": 10, "raw_low": 10, "raw_close": 10, "hfq_open": 10, "hfq_high": 10, "hfq_low": 10, "hfq_close": 10, "adjustment_factor": 1.0},
            {"timestamp": 2, "open": 9, "high": 9, "low": 9, "close": 9, "volume": 1, "raw_open": 9, "raw_high": 9, "raw_low": 9, "raw_close": 9, "hfq_open": 10, "hfq_high": 10, "hfq_low": 10, "hfq_close": 10, "adjustment_factor": 10 / 9},
        ]
        snapshot = {"points": points, "indicator_warmup_points": [], "adjustment_factor_base": 0.9}
        raw = apply_price_adjustment(snapshot, "不复权")
        hfq = apply_price_adjustment(snapshot, "后复权")
        dynamic = apply_price_adjustment(snapshot, "动态前复权")
        self.assertEqual([row["close"] for row in raw["points"]], [10.0, 9.0])
        self.assertEqual([row["close"] for row in hfq["points"]], [10.0, 10.0])
        self.assertEqual([row["close"] for row in dynamic["points"]], [9.0, 9.0])
        self.assertTrue(dynamic["dynamic_adjustment"])

    def test_front_adjustment_unchanged_when_overlap_prices_match(self) -> None:
        stored = {"points": [
            {"timestamp": 1_700_000_000 + index * 86_400, "close": close}
            for index, close in enumerate([100.0, 102.0, 104.0])
        ]}
        fresh = {"points": [dict(point) for point in stored["points"]]}
        changed, ratio = front_adjustment_changed(stored, fresh)
        self.assertFalse(changed)
        self.assertEqual(ratio, 1.0)

    def test_front_adjustment_detects_consistent_historical_rescaling(self) -> None:
        timestamps = [1_700_000_000 + index * 86_400 for index in range(3)]
        stored = {"points": [
            {"timestamp": timestamp, "close": close}
            for timestamp, close in zip(timestamps, [100.0, 102.0, 104.0])
        ]}
        fresh = {"points": [
            {"timestamp": timestamp, "close": close}
            for timestamp, close in zip(timestamps, [99.0, 100.98, 102.96])
        ]}
        changed, ratio = front_adjustment_changed(stored, fresh)
        self.assertTrue(changed)
        self.assertAlmostEqual(ratio or 0, 0.99)

    def test_parses_metrics(self) -> None:
        snapshot = parse_chart_payload(chart_payload(), "600519.SS")
        self.assertEqual(snapshot["symbol"], "600519.SS")
        self.assertEqual(snapshot["name"], "测试公司")
        self.assertGreater(snapshot["ma5"], snapshot["ma20"])
        self.assertGreater(snapshot["rsi14"], 50)
        self.assertEqual(len(snapshot["points"]), 80)
        self.assertEqual(snapshot["adjustment"], "前复权")

    def test_yahoo_adjusts_all_ohlc_fields_with_same_factor(self) -> None:
        payload = chart_payload()
        payload["chart"]["result"][0]["indicators"]["adjclose"][0]["adjclose"][0] = 50
        snapshot = parse_chart_payload(payload, "600519.SS")
        first = snapshot["points"][0]
        self.assertEqual(first["open"], 50)
        self.assertEqual(first["high"], 50.5)
        self.assertEqual(first["low"], 49.5)
        self.assertEqual(first["close"], 50)

    def test_fetch_uses_normalized_symbol(self) -> None:
        with patch(
            "backend.app.tools.market_data.ak.stock_zh_a_hist",
            return_value=akshare_frame([date.today() - timedelta(days=1), date.today()]),
        ) as fetch:
            snapshot = fetch_stock_snapshot("600519", "A股")
        self.assertEqual(fetch.call_args.kwargs["symbol"], "600519")
        self.assertEqual(fetch.call_args.kwargs["adjust"], "qfq")
        self.assertEqual(snapshot["requested_symbol"], "600519.SS")

    def test_history_fetch_sends_exact_date_range_to_akshare(self) -> None:
        with patch(
            "backend.app.tools.market_data.ak.stock_zh_a_hist",
            return_value=akshare_frame([date(2026, 1, 5), date(2026, 1, 20)]),
        ) as fetch:
            snapshot = fetch_stock_history("600519", "A股", "2026-01-05", "2026-01-20")
        self.assertEqual(fetch.call_args.kwargs, {
            "symbol": "600519", "period": "daily", "start_date": "20260105",
            "end_date": "20260120", "adjust": "qfq", "timeout": 20,
        })
        self.assertTrue(snapshot["points"])

    def test_beijing_history_uses_akshare_front_adjusted_data(self) -> None:
        with patch(
            "backend.app.tools.market_data.ak.stock_zh_a_hist",
            return_value=akshare_frame([date(2026, 1, 5), date(2026, 1, 6)]),
        ) as fetch:
            snapshot = fetch_stock_history("920000", "A股", "2026-01-05", "2026-01-06")
        self.assertEqual(fetch.call_args.kwargs["symbol"], "920000")
        self.assertEqual(snapshot["requested_symbol"], "920000.BJ")
        self.assertEqual(snapshot["adjustment"], "前复权")
        self.assertEqual(len(snapshot["points"]), 2)

    def test_akshare_history_retries_empty_primary_response(self) -> None:
        attempts = 0

        def history(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return pd.DataFrame()
            return akshare_frame([date(2026, 1, 5), date(2026, 1, 6)])

        with (
            patch("backend.app.tools.market_data.ak.stock_zh_a_hist", side_effect=history),
            patch("backend.app.tools.market_data.time.sleep"),
        ):
            snapshot = fetch_stock_history("600519", "A股", "2026-01-05", "2026-01-06")
        self.assertEqual(attempts, 2)
        self.assertEqual(len(snapshot["points"]), 2)

    def test_a_share_history_falls_back_to_akshare_sina_and_not_tencent(self) -> None:
        with (
            patch("backend.app.tools.market_data.ak.stock_zh_a_hist", side_effect=RuntimeError("上游断连")) as primary,
            patch(
                "backend.app.tools.market_data.ak.stock_zh_a_daily",
                return_value=akshare_frame([date(2023, 6, 5), date(2023, 6, 6)]).rename(columns={
                    "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
                    "最低": "low", "成交量": "volume",
                }),
            ) as fallback,
            patch("backend.app.tools.market_data.time.sleep"),
        ):
            snapshot = fetch_stock_history("000037", "A股", "2023-05-25", "2023-06-06")
        self.assertEqual(primary.call_count, 2)
        self.assertEqual(fallback.call_args.kwargs["symbol"], "sz000037")
        self.assertIn("stock_zh_a_daily", snapshot["data_source"])

    def test_tencent_payload_uses_qfq_ohlc_order(self) -> None:
        rows, adjustment, source = parse_tencent_payload(
            {"data": {"sz000037": {"qfqday": [["2023-06-02", "7.908", "8.688", "8.688", "7.908", "12345"]]}}},
            "000037.SZ",
        )
        self.assertEqual(rows[0]["open"], 7.908)
        self.assertEqual(rows[0]["close"], 8.688)
        self.assertEqual(rows[0]["high"], 8.688)
        self.assertEqual(rows[0]["low"], 7.908)
        self.assertEqual(adjustment, "前复权")
        self.assertIn("腾讯财经", source)

    def test_tencent_hs300_accepts_raw_index_points_without_stock_adjustment(self) -> None:
        rows, adjustment, source = parse_tencent_payload(
            {"data": {"sh000300": {"day": [["2026-01-05", "3900", "3920", "3930", "3890", "1000"]]}}},
            "000300.SS",
        )
        self.assertEqual(rows[0]["close"], 3920)
        self.assertEqual(adjustment, "指数点位（无需复权）")
        self.assertEqual(source, "腾讯财经指数日K接口")

    def test_akshare_volume_is_converted_from_lots_to_shares(self) -> None:
        with patch(
            "backend.app.tools.market_data.ak.stock_zh_a_hist",
            return_value=akshare_frame([date(2026, 1, 5), date(2026, 1, 6)]),
        ):
            snapshot = fetch_stock_history("000037", "A股", "2026-01-05", "2026-01-06")
        self.assertEqual(snapshot["points"][0]["volume"], 1_000_000)
        self.assertIn("AKShare", snapshot["data_source"])

    def test_adjustment_cache_must_reach_exact_target_trade_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "backend.app.database.database_path", return_value=Path(directory) / "test.db",
        ):
            init_database()
            upsert_instruments([{
                "symbol": "000037.SZ", "code": "000037", "name": "深南电A", "market": "A股",
                "exchange": "深圳证券交易所", "currency": "CNY",
            }])
            points = []
            for day in (10, 11):
                points.append({
                    "timestamp": int(datetime(2026, 8, day, tzinfo=timezone.utc).timestamp()),
                    "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100,
                    "raw_close": 10, "hfq_close": 10, "adjustment_factor": 1,
                })
            save_snapshot({
                "symbol": "000037.SZ", "points": points, "adjustment": "前复权",
                "data_source": "AKShare 前复权/不复权/后复权日线",
            }, "A股")
            self.assertTrue(adjustment_history_complete("000037.SZ", "2026-08-11"))
            self.assertFalse(adjustment_history_complete("000037.SZ", "2026-08-14"))

    def test_a_share_universe_uses_akshare_code_name(self) -> None:
        frame = pd.DataFrame({"code": ["600519", "000037", "920000"], "name": ["贵州茅台", "深南电A", "北交所测试"]})
        with patch("backend.app.tools.market_universe.ak.stock_info_a_code_name", return_value=frame) as fetch:
            universe = fetch_a_share_universe()
        fetch.assert_called_once_with()
        self.assertEqual([item["symbol"] for item in universe], ["600519.SS", "000037.SZ", "920000.BJ"])

    def test_a_share_universe_reports_akshare_error(self) -> None:
        with (
            patch("backend.app.tools.market_universe.ak.stock_info_a_code_name", side_effect=RuntimeError("断连")),
            self.assertRaisesRegex(MarketDataError, "AKShare A 股股票列表获取失败"),
        ):
            fetch_a_share_universe()

    def test_market_sync_uses_saved_universe_when_remote_list_disconnects(self) -> None:
        updates = []
        job = {"payload": {"start_date": "2026-01-01", "end_date": "2026-01-10", "mode": "incremental", "concurrency": 1}}
        snapshot = {
            "requested_symbol": "000037.SZ", "symbol": "000037.SZ", "name": "深南电A",
            "points": [{"timestamp": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}, {"timestamp": 2, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
            "data_source": "AKShare stock_zh_a_hist 前复权", "adjustment": "前复权",
        }
        with (
            patch("backend.app.task_service.get_job", return_value=job),
            patch("backend.app.task_service.list_instruments", return_value=[{"symbol": "000037.SZ"}]),
            patch("backend.app.task_service.fetch_a_share_universe", side_effect=MarketDataError("断连")),
            patch("backend.app.task_service.get_bar_coverage", return_value=None),
            patch("backend.app.task_service.fetch_stock_history", return_value=snapshot),
            patch("backend.app.task_service.fetch_stock_history_with_adjustments", return_value=snapshot),
            patch("backend.app.task_service.save_snapshot"),
            patch("backend.app.task_service.update_job", side_effect=lambda *args, **kwargs: updates.append(kwargs)),
        ):
            asyncio.run(sync_market_data("job-1"))
        completed = next(item for item in reversed(updates) if item.get("status") == "completed")
        self.assertEqual(completed["result"]["universe_source"], "本地已保存股票池")
        self.assertIn("沿用本地股票池", completed["message"])

    def test_incremental_sync_reloads_full_history_when_adjustment_changes(self) -> None:
        updates = []
        job = {"payload": {
            "symbols": ["000037.SZ"], "start_date": "2020-01-01", "end_date": "2026-01-10",
            "mode": "incremental", "concurrency": 1,
        }}
        timestamps = [1_768_089_600 + index * 86_400 for index in range(3)]
        stored = {"points": [
            {"timestamp": timestamp, "close": close}
            for timestamp, close in zip(timestamps, [100.0, 102.0, 104.0])
        ]}
        incremental = {
            "requested_symbol": "000037.SZ", "symbol": "000037.SZ", "name": "深南电A",
            "points": [
                {"timestamp": timestamp, "open": close, "high": close, "low": close, "close": close, "volume": 1}
                for timestamp, close in zip(timestamps, [99.0, 100.98, 102.96])
            ],
            "data_source": "AKShare stock_zh_a_hist 前复权", "adjustment": "前复权",
        }
        full = {**incremental, "points": [
            {"timestamp": 1_577_836_800, "open": 50, "high": 50, "low": 50, "close": 50, "volume": 1},
            *incremental["points"],
        ]}
        benchmark = {**incremental, "symbol": "000300.SS", "requested_symbol": "000300.SS"}
        calls = []

        def fetch(symbol, market, start_date, end_date, client=None):
            calls.append((symbol, start_date, end_date))
            if symbol == "000300.SS":
                return benchmark
            return incremental if start_date != "2020-01-01" else full

        with (
            patch("backend.app.task_service.get_job", return_value=job),
            patch("backend.app.task_service.get_bar_coverage", return_value=("2020-01-01", "2026-01-09")),
            patch("backend.app.task_service.adjustment_history_complete", return_value=True),
            patch("backend.app.task_service.load_snapshot", return_value=stored),
            patch("backend.app.task_service.fetch_stock_history", side_effect=fetch),
            patch("backend.app.task_service.fetch_stock_history_with_adjustments", side_effect=fetch),
            patch("backend.app.task_service.save_snapshot") as save,
            patch("backend.app.task_service.update_job", side_effect=lambda *args, **kwargs: updates.append(kwargs)),
        ):
            asyncio.run(sync_market_data("repair-job"))

        self.assertIn(("000037.SZ", "2020-01-01", "2026-01-10"), calls)
        save.assert_any_call(full, "A股")
        completed = next(item for item in reversed(updates) if item.get("status") == "completed")
        self.assertEqual(completed["result"]["adjustment_repairs"], 1)
        self.assertEqual(completed["result"]["adjustment_repair_samples"][0]["symbol"], "000037.SZ")

    def test_incremental_sync_skips_symbol_already_at_complete_market_cutoff(self) -> None:
        updates = []
        job = {"payload": {
            "symbols": ["000037.SZ"], "start_date": "2020-01-01", "end_date": "2026-01-10",
            "mode": "incremental", "concurrency": 2,
        }}
        timestamps = [
            int(datetime(2026, 1, day, tzinfo=timezone.utc).timestamp())
            for day in (8, 9)
        ]
        snapshot = {
            "requested_symbol": "000001.SZ", "symbol": "000001.SZ", "name": "平安银行",
            "points": [
                {"timestamp": timestamp, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
                for timestamp in timestamps
            ],
            "data_source": "AKShare 前复权/不复权/后复权日线", "adjustment": "前复权",
        }
        adjustment_calls = []

        def fetch_adjustments(symbol, *args):
            adjustment_calls.append(symbol)
            return snapshot

        with (
            patch("backend.app.task_service.get_job", return_value=job),
            patch("backend.app.task_service.list_instruments", return_value=[{"symbol": "000037.SZ", "name": "深南电A"}]),
            patch("backend.app.task_service.get_bar_coverage", return_value=("2020-01-01", "2026-01-09", 1000)),
            patch("backend.app.task_service.adjustment_history_complete", return_value=True),
            patch("backend.app.task_service.fetch_stock_history", return_value={**snapshot, "symbol": "000300.SS"}),
            patch("backend.app.task_service.fetch_stock_history_with_adjustments", side_effect=fetch_adjustments),
            patch("backend.app.task_service.save_snapshot"),
            patch("backend.app.task_service.update_job", side_effect=lambda *args, **kwargs: updates.append(kwargs)),
        ):
            asyncio.run(sync_market_data("current-job"))

        # 唯一一次三套行情请求是市场截止日探针，000037 本身没有再次访问 AKShare。
        self.assertEqual(adjustment_calls, ["000001.SZ"])
        completed = next(item for item in reversed(updates) if item.get("status") == "completed")
        self.assertEqual(completed["result"]["updated"], 0)
        self.assertEqual(completed["result"]["cached"], 1)
        self.assertEqual(completed["result"]["failed"], 0)
        self.assertEqual(completed["result"]["target_trade_date"], "2026-01-09")

    def test_front_only_sync_never_requests_raw_or_back_adjusted_prices(self) -> None:
        updates = []
        job = {"payload": {
            "symbols": ["000037.SZ"], "start_date": "2020-01-01", "end_date": "2026-01-10",
            "mode": "incremental", "price_scope": "front_only", "concurrency": 1,
        }}
        timestamps = [
            int(datetime(2026, 1, day, tzinfo=timezone.utc).timestamp())
            for day in (8, 9)
        ]
        snapshot = {
            "requested_symbol": "000037.SZ", "symbol": "000037.SZ", "name": "深南电A",
            "points": [
                {"timestamp": timestamp, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
                for timestamp in timestamps
            ],
            "data_source": "腾讯财经前复权日K接口", "adjustment": "前复权",
        }
        calls = []

        def fetch_front(symbol, *args):
            calls.append(symbol)
            return {**snapshot, "requested_symbol": symbol, "symbol": symbol}

        with (
            patch("backend.app.task_service.get_job", return_value=job),
            patch("backend.app.task_service.get_bar_coverage", return_value=None),
            patch("backend.app.task_service.fetch_stock_history", side_effect=fetch_front),
            patch("backend.app.task_service.fetch_stock_history_with_adjustments") as fetch_all,
            patch("backend.app.task_service.save_snapshot"),
            patch("backend.app.task_service.update_job", side_effect=lambda *args, **kwargs: updates.append(kwargs)),
        ):
            asyncio.run(sync_market_data("front-only-job"))

        fetch_all.assert_not_called()
        self.assertEqual(calls, ["000300.SS", "000001.SZ", "000037.SZ"])
        completed = next(item for item in reversed(updates) if item.get("status") == "completed")
        self.assertEqual(completed["result"]["price_scope"], "front_only")
        self.assertEqual(completed["result"]["updated"], 1)

    def test_market_sync_stops_when_eastmoney_failure_rate_is_too_high(self) -> None:
        updates = []
        job = {"payload": {"start_date": "2026-01-01", "end_date": "2026-01-10", "mode": "full", "concurrency": 1}}
        universe = [{"symbol": f"{index:06d}.SZ", "market": "A股"} for index in range(1, 51)]
        benchmark = {
            "requested_symbol": "000300.SS", "symbol": "000300.SS", "name": "沪深300",
            "points": [{"timestamp": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}, {"timestamp": 2, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
            "data_source": "腾讯财经前复权日K接口", "adjustment": "前复权",
        }
        fetch_effect = lambda symbol, *args: benchmark if symbol == "000300.SS" else (_ for _ in ()).throw(MarketDataError("K线断连"))
        with (
            patch("backend.app.task_service.get_job", return_value=job),
            patch("backend.app.task_service.list_instruments", return_value=universe),
            patch("backend.app.task_service.fetch_a_share_universe", side_effect=MarketDataError("列表断连")),
            patch("backend.app.task_service.adjustment_history_complete", return_value=False),
            patch("backend.app.task_service.fetch_stock_history", side_effect=fetch_effect),
            patch("backend.app.task_service.fetch_stock_history_with_adjustments", side_effect=fetch_effect),
            patch("backend.app.task_service.save_snapshot"),
            patch("backend.app.task_service.asyncio.sleep"),
            patch("backend.app.task_service.update_job", side_effect=lambda *args, **kwargs: updates.append(kwargs)),
        ):
            asyncio.run(sync_market_data("job-2"))
        failed = next(item for item in reversed(updates) if item.get("status") == "failed")
        self.assertIn("失败率熔断", failed["error"])
        self.assertIn("串行退避重试后", failed["error"])

    def test_market_sync_recovers_rate_limited_symbols_with_serial_retry(self) -> None:
        updates = []
        symbols = [f"{index:06d}.SZ" for index in range(1, 51)]
        job = {"payload": {
            "symbols": symbols, "start_date": "2026-01-01", "end_date": "2026-01-10",
            "mode": "incremental", "concurrency": 4,
        }}
        snapshot = {
            "requested_symbol": "000001.SZ", "symbol": "000001.SZ", "name": "测试股票",
            "points": [
                {"timestamp": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
                {"timestamp": 2, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            ],
            "data_source": "腾讯财经前复权日K接口", "adjustment": "前复权",
        }
        attempts: dict[str, int] = {}

        def fetch(symbol, *args):
            if symbol == "000300.SS":
                return {**snapshot, "requested_symbol": symbol, "symbol": symbol, "name": "沪深300"}
            # 增量任务先用平安银行探测三套复权行情的共同截止日；该请求不属于股票批次。
            if symbol == "000001.SZ" and len(args) >= 2 and args[1] == "2025-11-26":
                return {**snapshot, "requested_symbol": symbol, "symbol": symbol}
            attempts[symbol] = attempts.get(symbol, 0) + 1
            if attempts[symbol] == 1:
                raise MarketDataError("腾讯财经没有返回该代码的日线行情")
            return {**snapshot, "requested_symbol": symbol, "symbol": symbol}

        with (
            patch("backend.app.task_service.get_job", return_value=job),
            patch("backend.app.task_service.get_bar_coverage", return_value=None),
            patch("backend.app.task_service.fetch_stock_history", side_effect=fetch),
            patch("backend.app.task_service.fetch_stock_history_with_adjustments", side_effect=fetch),
            patch("backend.app.task_service.save_snapshot"),
            patch("backend.app.task_service.asyncio.sleep"),
            patch("backend.app.task_service.update_job", side_effect=lambda *args, **kwargs: updates.append(kwargs)),
        ):
            asyncio.run(sync_market_data("retry-job"))

        completed = next(item for item in reversed(updates) if item.get("status") == "completed")
        self.assertEqual(completed["result"]["succeeded"], 50)
        self.assertEqual(completed["result"]["failed"], 0)
        self.assertEqual(
            completed["result"]["retried_succeeded"] + completed["result"]["inactive_skipped"],
            50,
        )
        self.assertEqual(completed["result"]["requested_concurrency"], 4)
        self.assertEqual(completed["result"]["effective_concurrency"], 1)

    def test_fundamental_sync_persists_reports_before_backtest(self) -> None:
        updates = []
        job = {"payload": {"symbols": ["002714.SZ"], "mode": "incremental", "concurrency": 2}}
        reports = [{"symbol": "002714.SZ", "report_date": "2025-12-31", "effective_date": "2026-03-28", "score": 70}]
        with (
            patch("backend.app.task_service.get_job", return_value=job),
            patch("backend.app.task_service.fundamental_cache_is_fresh", return_value=False),
            patch("backend.app.task_service.fetch_fundamental_reports", return_value=reports),
            patch("backend.app.task_service.save_fundamental_reports", return_value=1) as save_reports,
            patch("backend.app.task_service.fundamental_coverage", return_value={"total": 1, "covered": 1, "missing": 0}),
            patch("backend.app.task_service.update_job", side_effect=lambda *args, **kwargs: updates.append(kwargs)),
        ):
            asyncio.run(sync_fundamental_data("fundamental-job"))
        save_reports.assert_called_once_with("002714.SZ", reports)
        completed = next(item for item in reversed(updates) if item.get("status") == "completed")
        self.assertEqual(completed["result"]["succeeded"], 1)
        self.assertEqual(completed["result"]["coverage"]["covered"], 1)

    def test_akshare_snapshot_removes_stale_other_source_rows_in_same_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "backend.app.database.database_path", return_value=Path(directory) / "test.db",
        ):
            init_database()
            base = {
                "requested_symbol": "000037",
                "symbol": "000037.SZ",
                "name": "深南电A",
                "exchange": "深圳证券交易所",
                "currency": "CNY",
                "adjustment": "前复权",
            }
            yahoo_rows = [
                {"timestamp": 1_685_404_800 + index * 86_400, "open": 8.0, "high": 14.0 if index == 1 else 8.2, "low": 7.9, "close": 12.0 if index == 1 else 8.1, "volume": 100}
                for index in range(3)
            ]
            save_snapshot({**base, "points": yahoo_rows, "data_source": "Yahoo Finance Chart API（备用）"}, "A股")
            akshare_rows = [yahoo_rows[0], yahoo_rows[2]]
            save_snapshot({**base, "points": akshare_rows, "data_source": "AKShare stock_zh_a_hist 前复权"}, "A股")
            stored = load_snapshot("000037", "2023-05-30", "2023-06-02", "A股")
            self.assertIsNotNone(stored)
            self.assertEqual(len(stored["points"]), 2)
            self.assertTrue(all(point["close"] != 12.0 for point in stored["points"]))

    def test_dynamic_adjustment_coverage_requires_every_row_in_requested_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "backend.app.database.database_path", return_value=Path(directory) / "test.db",
        ):
            init_database()
            upsert_instruments([
                {"symbol": "000001.SZ", "code": "000001", "name": "测试一", "market": "A股"},
                {"symbol": "000002.SZ", "code": "000002", "name": "测试二", "market": "A股"},
            ])
            base_timestamp = int(datetime(2019, 1, 2, tzinfo=timezone.utc).timestamp())
            for symbol, complete in (("000001.SZ", True), ("000002.SZ", False)):
                points = []
                for index in range(3):
                    raw = 10.0 + index
                    point = {
                        "timestamp": base_timestamp + index * 86_400,
                        "open": raw, "high": raw + 0.2, "low": raw - 0.2, "close": raw,
                        "volume": 100,
                        "raw_open": raw, "raw_high": raw + 0.2, "raw_low": raw - 0.2, "raw_close": raw,
                    }
                    if complete or index == 2:
                        point.update(
                            hfq_open=raw * 2, hfq_high=(raw + 0.2) * 2,
                            hfq_low=(raw - 0.2) * 2, hfq_close=raw * 2,
                            adjustment_factor=2.0,
                        )
                    points.append(point)
                save_snapshot({
                    "requested_symbol": symbol, "symbol": symbol, "name": symbol,
                    "exchange": "深圳证券交易所", "currency": "CNY", "adjustment": "前复权",
                    "data_source": "AKShare 前复权/不复权/后复权日线", "points": points,
                }, "A股")
            coverage = price_mode_coverage("动态前复权", "A股", "2019-01-01", "2020-12-31")
            self.assertEqual(coverage["total"], 2)
            self.assertEqual(coverage["covered"], 1)
            selected = list_backtest_instruments("2019-01-01", "2020-12-31", "A股", "动态前复权")
            self.assertEqual([item["symbol"] for item in selected], ["000001.SZ"])

    def test_backtest_results_support_search_sort_and_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "backend.app.database.database_path", return_value=Path(directory) / "test.db",
        ):
            init_database()
            run_id = create_backtest_run("查询测试", "market", "2020-01-01", "2026-01-01", ["均线交叉"], {})
            base_result = run_backtest(parse_chart_payload(chart_payload(), "600519.SS"), "均线交叉")
            first = {**base_result, "symbol": "600519.SS", "name": "贵州茅台", "total_return_pct": 10.0}
            second = {**base_result, "symbol": "000001.SZ", "name": "平安银行", "total_return_pct": 5.0}
            save_backtest_result(run_id, first)
            save_backtest_result(run_id, second)
            searched = list_backtest_result_summaries(run_id, 100, 0, "茅台", None, "return", "desc")
            self.assertEqual(searched["total"], 1)
            self.assertEqual(searched["items"][0]["symbol"], "600519.SS")
            paged = list_backtest_result_summaries(run_id, 1, 1, None, None, "return", "desc")
            self.assertEqual(paged["total"], 2)
            self.assertEqual(paged["items"][0]["symbol"], "000001.SZ")
            summary = {"strategies": [{"strategy": "均线交叉", "average_return_pct": 7.5}]}
            complete_backtest_run(run_id, summary)
            self.assertEqual(get_backtest_run_summary(run_id)["summary"], summary)
            second_run = create_backtest_run("PK测试", "market", "2021-01-01", "2026-01-01", ["均线交叉"], {})
            complete_backtest_run(second_run, {"strategies": [{"strategy": "均线交叉", "average_return_pct": 3.0}]})
            api = TestClient(app)
            comparison = api.post("/api/backtests/compare", json={"run_ids": [run_id, second_run]})
            self.assertEqual(comparison.status_code, 200)
            self.assertEqual([row["rank"] for row in comparison.json()["rows"]], [1, 2])
            deleted = api.delete(f"/api/backtests/records/{second_run}")
            self.assertEqual(deleted.status_code, 200)
            self.assertTrue(deleted.json()["recoverable"])
            self.assertTrue(delete_backtest_run(run_id))
            self.assertEqual(list_backtest_runs(), [])
            self.assertTrue(list_backtest_result_summaries(run_id)["not_found"])

    def test_backtest_returns_strategy_metrics_and_curve(self) -> None:
        snapshot = parse_chart_payload(chart_payload(), "600519.SS")
        result = run_backtest(snapshot, "均线交叉", 100_000, 0.0003)
        self.assertEqual(result["strategy"], "均线交叉")
        self.assertEqual(result["period_points"], 80)
        self.assertEqual(len(result["curve"]), 80)
        self.assertIn("max_drawdown_pct", result)
        self.assertIn("warning", result)
        self.assertTrue(result["trade_events"])
        self.assertEqual(result["trade_events"][0]["side"], "B")
        self.assertGreaterEqual(result["trade_events"][0]["curve_index"], 20)

    def test_rsi_risk_strategy_enters_on_bullish_rebound_and_takes_profit(self) -> None:
        rows = [{
            "timestamp": 1_700_000_000 + index * 86_400,
            "open": 100.0,
            "close": 101.0 if index == 21 else 112.0 if index == 23 else 105.0 if index == 24 else 100.0,
            "volume": 1_000_000,
        } for index in range(30)]
        for row in rows:
            row["high"], row["low"] = row["close"] + 1, row["close"] - 1
        snapshot = {"symbol": "TEST.SS", "name": "RSI风控测试", "points": rows, "data_source": "test", "adjustment": "前复权"}

        fake_rsi = [50.0] * len(rows)
        fake_rsi[20:22] = [25.0, 35.0]
        with patch("backend.app.tools.backtest._snapshot_rsi_values", return_value=fake_rsi):
            result = run_backtest(snapshot, "RSI反转+止盈止损")
        self.assertEqual([event["side"] for event in result["trade_events"]], ["B", "S"])
        self.assertEqual([event["curve_index"] for event in result["trade_events"]], [22, 25])
        self.assertIn("向上穿越且当日收阳", result["trade_events"][0]["reason"])
        self.assertIn("ATR跟踪止盈", result["trade_events"][1]["reason"])

    def test_fundamental_score_is_independent_optional_entry_filter(self) -> None:
        rows = [{
            "timestamp": 1_700_000_000 + index * 86_400,
            "open": 100.0,
            "close": 101.0 if index == 21 else 112.0 if index == 23 else 105.0 if index == 24 else 100.0,
            "volume": 1_000_000,
        } for index in range(30)]
        for row in rows:
            row["high"], row["low"] = row["close"] + 1, row["close"] - 1
        snapshot = {"symbol": "TEST.SS", "name": "基本面开关测试", "points": rows, "data_source": "test", "adjustment": "前复权"}
        fake_rsi = [50.0] * len(rows)
        fake_rsi[20:22] = [25.0, 35.0]
        effective_date = datetime.fromtimestamp(rows[20]["timestamp"], timezone.utc).date().isoformat()
        reports = [{"effective_date": effective_date, "report_date": effective_date, "score": 55.0, "source": "test"}]
        with patch("backend.app.tools.backtest._snapshot_rsi_values", return_value=fake_rsi):
            baseline = run_backtest(snapshot, "RSI反转+止盈止损")
            rejected = run_backtest(snapshot, "RSI反转+止盈止损", fundamental_reports=reports, fundamental_score_threshold=60)
            accepted = run_backtest(snapshot, "RSI反转+止盈止损", fundamental_reports=reports, fundamental_score_threshold=50)
        self.assertTrue(baseline["trade_events"])
        self.assertEqual(rejected["trade_events"], [])
        self.assertGreater(rejected["fundamental_filter"]["filtered_entries"], 0)
        self.assertEqual(accepted["trade_events"][0]["fundamental_score"], 55.0)
        self.assertIn("基本面评分=55", accepted["trade_events"][0]["reason"])
        self.assertFalse(baseline["fundamental_filter"]["enabled"])

    def test_fundamental_score_uses_financial_quality_components(self) -> None:
        score, components = calculate_fundamental_score({
            "ROEJQ": 16, "TOTALOPERATEREVETZ": 20, "PARENTNETPROFITTZ": 18,
            "NETCASH_OPERATE_PK": 120, "PARENTNETPROFIT": 100, "ZCFZL": 35,
        })
        self.assertEqual(score, 100)
        self.assertEqual(sum(components.values()), 100)
        parsed = parse_fundamental_payload({"success": True, "result": {"data": [{
            "REPORT_DATE": "2024-12-31 00:00:00", "NOTICE_DATE": "2025-03-01 00:00:00",
            "UPDATE_DATE": "2025-03-05 00:00:00", "ROEJQ": 16, "TOTALOPERATEREVETZ": 20,
            "PARENTNETPROFITTZ": 18, "NETCASH_OPERATE_PK": 120, "PARENTNETPROFIT": 100, "ZCFZL": 35,
        }]}}, "600519.SS")
        self.assertEqual(parsed[0]["effective_date"], "2025-03-05")

    def test_rsi_risk_strategy_stops_at_five_percent_close_loss(self) -> None:
        rows = [{
            "timestamp": 1_700_000_000 + index * 86_400,
            "open": 100.0,
            "close": 101.0 if index == 21 else 95.0 if index == 23 else 100.0,
            "volume": 1_000_000,
        } for index in range(30)]
        for row in rows:
            row["high"], row["low"] = row["close"] + 1, row["close"] - 1
        snapshot = {"symbol": "TEST.SS", "name": "RSI止损测试", "points": rows, "data_source": "test", "adjustment": "前复权"}
        fake_rsi = [50.0] * len(rows)
        fake_rsi[20:22] = [25.0, 35.0]
        with patch("backend.app.tools.backtest._snapshot_rsi_values", return_value=fake_rsi):
            result = run_backtest(snapshot, "RSI反转+止盈止损")
        self.assertEqual([event["curve_index"] for event in result["trade_events"]], [22, 24])
        self.assertIn("ATR初始止损", result["trade_events"][1]["reason"])

    def test_multi_timeframe_trend_requires_stock_and_market_regimes(self) -> None:
        rows = [{
            "timestamp": 1_700_000_000 + index * 86_400,
            "open": 100.0,
            "high": 122.0 if index >= 68 else 101.0,
            "low": 99.0,
            "close": 120.0 if index == 68 else 121.0 if index == 69 else 119.0 if index == 70 else 100.0,
            "volume": 1_000_000,
        } for index in range(71)]
        snapshot = {"symbol": "TEST.SS", "name": "趋势测试", "points": rows, "data_source": "test", "adjustment": "前复权"}
        benchmark_rows = [{**row, "close": 105.0 if index >= 68 else 100.0, "open": 100.0, "high": 106.0, "low": 99.0} for index, row in enumerate(rows)]
        benchmark = {"symbol": "000300.SS", "name": "沪深300", "points": benchmark_rows, "data_source": "test", "adjustment": "指数"}
        ma20 = [90.0] * 71
        ma20[67], ma20[68], ma20[69], ma20[70] = 99.0, 101.0, 90.0, 90.0
        ma60 = [100.0] * 71
        ma200 = [95.0] * 71
        ma200[48] = 94.0
        benchmark_ma200 = [90.0] * 71
        benchmark_ma200[48] = 89.0

        def stock_ma(_snapshot, _rows, period):
            return {20: ma20, 60: ma60, 200: ma200}[period]

        with (
            patch("backend.app.tools.backtest._snapshot_sma_values", side_effect=stock_ma),
            patch("backend.app.tools.backtest._aligned_benchmark_sma", return_value=benchmark_ma200),
        ):
            result = run_backtest(snapshot, "多周期趋势跟随", benchmark_snapshot=benchmark)
        self.assertEqual([event["side"] for event in result["trade_events"]], ["B", "S"])
        self.assertEqual([event["curve_index"] for event in result["trade_events"]], [69, 70])
        self.assertIn("个股近60日收益", result["trade_events"][0]["reason"])
        self.assertIn("MA20跌破MA60", result["trade_events"][1]["reason"])

    def test_rsi_risk_strategy_rejects_close_still_near_twenty_day_low(self) -> None:
        rows = [{
            "timestamp": 1_700_000_000 + index * 86_400,
            "open": 100.0, "high": 102.0, "low": 100.0,
            "close": 101.0 if index == 21 else 100.5,
            "volume": 1_000_000,
        } for index in range(30)]
        snapshot = {"symbol": "TEST.SS", "name": "RSI近低点过滤", "points": rows, "data_source": "test", "adjustment": "前复权"}
        fake_rsi = [50.0] * len(rows)
        fake_rsi[20:22] = [25.0, 35.0]
        with patch("backend.app.tools.backtest._snapshot_rsi_values", return_value=fake_rsi):
            result = run_backtest(snapshot, "RSI反转+止盈止损")
        self.assertEqual(result["trade_events"], [])

    def test_kdj_rapid_drop_strategy_buys_after_first_small_bullish_candle(self) -> None:
        snapshot = kdj_t2_snapshot(34)
        snapshot["points"][25]["close"] = 99.0
        with patch("backend.app.tools.backtest._kdj_j_values", return_value=rapid_drop_j_values(34)):
            result = run_backtest(snapshot, "KDJ急跌首阳T+1")
        self.assertEqual([event["side"] for event in result["trade_events"]], ["B", "S"])
        self.assertEqual(result["trade_events"][0]["curve_index"], 27)
        self.assertEqual(result["trade_events"][0]["signal_timestamp"], snapshot["points"][26]["timestamp"])
        self.assertIn("出现观察后的首根阳线", result["trade_events"][0]["reason"])
        self.assertIn("近10日红肥绿瘦", result["trade_events"][0]["reason"])
        self.assertIn("高于 MA200", result["trade_events"][0]["reason"])
        self.assertIn("阳线均量/阴线均量=2.60", result["trade_events"][0]["reason"])
        self.assertIn("涨幅=1.00%", result["trade_events"][0]["reason"])
        self.assertEqual(result["trade_events"][1]["curve_index"], 32)
        self.assertIn("5日退出", result["trade_events"][1]["reason"])
        self.assertIn("最大阳线涨幅仅 1.00%", result["trade_events"][1]["reason"])

    def test_kdj_rapid_drop_strategy_stops_after_three_pct_close_loss(self) -> None:
        snapshot = kdj_t2_snapshot(30, stop_index=27)
        with patch("backend.app.tools.backtest._kdj_j_values", return_value=rapid_drop_j_values(30)):
            result = run_backtest(snapshot, "KDJ急跌首阳T+1")
        self.assertEqual([event["curve_index"] for event in result["trade_events"]], [26, 28])
        self.assertIn("信号日收盘价 96.00", result["trade_events"][1]["reason"])
        self.assertIn("买入开盘价 100.00", result["trade_events"][1]["reason"])
        self.assertIn("-3% 止损线", result["trade_events"][1]["reason"])

    def test_kdj_rapid_drop_strategy_forces_exit_after_twenty_holding_days(self) -> None:
        snapshot = kdj_t2_snapshot(48, surge_index=28)
        with patch("backend.app.tools.backtest._kdj_j_values", return_value=rapid_drop_j_values(48)):
            result = run_backtest(snapshot, "KDJ急跌首阳T+1")
        self.assertEqual([event["curve_index"] for event in result["trade_events"]], [26, 46])
        self.assertIn("到期退出", result["trade_events"][1]["reason"])
        self.assertIn("已持仓 20 个交易日", result["trade_events"][1]["reason"])

    def test_kdj_rapid_drop_strategy_abandons_first_bullish_above_four_pct(self) -> None:
        snapshot = kdj_t2_snapshot(33, surge_index=25)
        with patch("backend.app.tools.backtest._kdj_j_values", return_value=rapid_drop_j_values(33)):
            result = run_backtest(snapshot, "KDJ急跌首阳T+1")
        self.assertEqual(result["trade_events"], [])

        exactly_four = kdj_t2_snapshot(33)
        exactly_four["points"][25]["close"] = 104.0
        with patch("backend.app.tools.backtest._kdj_j_values", return_value=rapid_drop_j_values(33)):
            boundary_result = run_backtest(exactly_four, "KDJ急跌首阳T+1")
        self.assertEqual(boundary_result["trade_events"][0]["curve_index"], 26)

    def test_kdj_rapid_drop_strategy_rejects_green_fat_red_thin_volume(self) -> None:
        snapshot = kdj_t2_snapshot(33)
        for index in range(15, 25):
            snapshot["points"][index]["volume"] = 400_000 if index % 2 == 0 else 1_400_000
        with patch("backend.app.tools.backtest._kdj_j_values", return_value=rapid_drop_j_values(33)):
            result = run_backtest(snapshot, "KDJ急跌首阳T+1")
        self.assertEqual(result["trade_events"], [])

    def test_kdj_rapid_drop_strategy_requires_observation_close_above_ma200(self) -> None:
        snapshot = kdj_t2_snapshot(33)
        for row in snapshot["indicator_warmup_points"]:
            row.update(open=120.0, high=121.0, low=119.0, close=120.0)
        with patch("backend.app.tools.backtest._kdj_j_values", return_value=rapid_drop_j_values(33)):
            result = run_backtest(snapshot, "KDJ急跌首阳T+1")
        self.assertEqual(result["trade_events"], [])

    def test_construction_wave_enters_on_first_j_below_zero_and_uses_trailing_profit(self) -> None:
        rows = []
        for index in range(260):
            close = open_price = 100.0
            volume = 1_000_000
            if index == 210:
                open_price, close, volume = 100.0, 105.0, 3_000_000
            elif index == 211:
                open_price, close = 105.0, 104.0
            elif index == 212:
                open_price, close = 104.0, 103.0
            elif 213 <= index <= 221:
                open_price = close = 103.0
            elif index == 222:
                open_price, close = 103.0, 112.0
            elif index == 223:
                open_price, close = 112.0, 106.0
            elif index >= 224:
                open_price = close = 106.0
            rows.append({
                "timestamp": 1_700_000_000 + index * 86_400,
                "open": open_price, "high": max(open_price, close) + 0.5,
                "low": min(open_price, close) - 0.5, "close": close, "volume": volume,
            })
        snapshot = {"symbol": "TEST", "name": "建仓波测试", "points": rows, "data_source": "测试", "adjustment": "前复权"}
        j_values = [10.0] * len(rows)
        j_values[220] = -1.0
        with (
            patch("backend.app.tools.backtest._snapshot_kdj_j_values", return_value=j_values),
            patch("backend.app.tools.backtest._snapshot_sma_values", return_value=[100.0] * len(rows)),
            patch("backend.app.tools.backtest._snapshot_atr_values", return_value=[2.0] * len(rows)),
        ):
            result = run_backtest(snapshot, "建仓波J<0动态止盈", fee_rate=0, slippage_bps=0)
        self.assertEqual([event["side"] for event in result["trade_events"][:2]], ["B", "S"])
        self.assertEqual(result["trade_events"][0]["timestamp"], rows[221]["timestamp"])
        self.assertIn("首次J=-1.00<0", result["trade_events"][0]["reason"])
        self.assertIn("动态止盈", result["trade_events"][1]["reason"])

    def test_construction_wave_rejects_first_j_signal_above_one_point_five_ma200(self) -> None:
        rows = [
            {
                "timestamp": 1_700_000_000 + index * 86_400,
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                "volume": 1_000_000,
            }
            for index in range(260)
        ]
        rows[210].update({"open": 100.0, "close": 105.0, "high": 105.5, "volume": 3_000_000})
        rows[211].update({"open": 105.0, "close": 104.0, "high": 105.5, "low": 103.5})
        rows[212].update({"open": 104.0, "close": 103.0, "high": 104.5, "low": 102.5})
        rows[220].update({"open": 151.0, "close": 151.0, "high": 152.0, "low": 150.0})
        snapshot = {"symbol": "TEST", "name": "价格上限测试", "points": rows, "data_source": "测试", "adjustment": "前复权"}
        j_values = [10.0] * len(rows)
        j_values[220] = j_values[221] = -1.0
        with (
            patch("backend.app.tools.backtest._snapshot_kdj_j_values", return_value=j_values),
            patch("backend.app.tools.backtest._snapshot_sma_values", return_value=[100.0] * len(rows)),
            patch("backend.app.tools.backtest._snapshot_atr_values", return_value=[2.0] * len(rows)),
        ):
            result = run_backtest(snapshot, "建仓波J<0动态止盈")
        self.assertEqual(result["trade_events"], [])

    def test_signal_executes_at_next_trading_day_open(self) -> None:
        snapshot = parse_chart_payload(chart_payload(), "600519.SS")
        baseline = run_backtest(snapshot, "均线交叉", 100_000, 0.0003)
        execution_index = baseline["trade_events"][0]["curve_index"]
        snapshot["points"][execution_index]["open"] = 123.4567
        result = run_backtest(snapshot, "均线交叉", 100_000, 0.0003)
        event = result["trade_events"][0]
        self.assertEqual(event["price"], 123.4567)
        self.assertEqual(event["timestamp"], snapshot["points"][execution_index]["timestamp"])
        self.assertEqual(event["signal_timestamp"], snapshot["points"][execution_index - 1]["timestamp"])
        self.assertEqual(event["signal_price"], snapshot["points"][execution_index - 1]["close"])

    def test_buy_and_hold_return_includes_entry_fee_and_has_buy_marker(self) -> None:
        snapshot = parse_chart_payload(chart_payload(), "600519.SS")
        result = run_backtest(snapshot, "买入持有", 100_000, 0.001)
        expected = ((139.5 / 100) * (1 - 0.001) - 1) * 100
        self.assertAlmostEqual(result["total_return_pct"], expected, places=2)
        self.assertEqual(result["total_return_pct"], result["benchmark_return_pct"])
        elapsed_days = (snapshot["points"][-1]["timestamp"] - snapshot["points"][0]["timestamp"]) / 86_400
        expected_annualized = ((1 + expected / 100) ** (365.2425 / elapsed_days) - 1) * 100
        self.assertAlmostEqual(result["annualized_return_pct"], expected_annualized, places=2)
        self.assertEqual(result["trade_events"][0]["curve_index"], 0)
        self.assertEqual(result["trade_events"][0]["side"], "B")
        self.assertEqual(result["holding_days"], len(snapshot["points"]))
        expected_daily = ((1 + expected / 100) ** (1 / len(snapshot["points"])) - 1) * 100
        self.assertAlmostEqual(result["holding_daily_return_pct"], expected_daily, places=4)

    def test_backtest_uses_external_hs300_benchmark_for_excess_return(self) -> None:
        snapshot = parse_chart_payload(chart_payload(), "600519.SS")
        benchmark = parse_chart_payload(chart_payload(), "000300.SS")
        benchmark["symbol"] = "000300.SS"
        benchmark["name"] = "沪深300"
        for row in benchmark["points"]:
            row.update(open=100.0, high=100.0, low=100.0, close=100.0)
        result = run_backtest(snapshot, "买入持有", 100_000, 0.001, benchmark)
        self.assertEqual(result["benchmark_symbol"], "000300.SS")
        self.assertEqual(result["benchmark_name"], "沪深300")
        self.assertAlmostEqual(result["benchmark_return_pct"], -0.1, places=2)
        self.assertGreater(result["total_return_pct"], result["benchmark_return_pct"])

    def test_summarizes_average_strategy_and_benchmark_returns(self) -> None:
        rows = [
            {"strategy": "均线交叉", "total_return_pct": 10, "annualized_return_pct": 8, "holding_days": 20, "holding_daily_return_pct": 0.48, "benchmark_return_pct": 6, "excess_return_pct": 4, "max_drawdown_pct": -5, "sharpe": 1.2, "trades": 4, "win_rate_pct": 50, "initial_capital": 100, "curve": [{"timestamp": 1, "equity": 100}, {"timestamp": 2, "equity": 110}, {"timestamp": 3, "equity": 121}]},
            {"strategy": "均线交叉", "total_return_pct": -2, "annualized_return_pct": -1, "holding_days": 10, "holding_daily_return_pct": -0.2, "benchmark_return_pct": 2, "excess_return_pct": -4, "max_drawdown_pct": -8, "sharpe": -0.2, "trades": 2, "win_rate_pct": None, "initial_capital": 100, "curve": [{"timestamp": 1, "equity": 100}, {"timestamp": 2, "equity": 90}, {"timestamp": 3, "equity": 99}]},
        ]
        summary = summarize_backtest_results(rows)[0]
        self.assertEqual(summary["average_return_pct"], 4)
        self.assertEqual(summary["average_annualized_return_pct"], 3.5)
        self.assertEqual(summary["average_benchmark_return_pct"], 4)
        self.assertEqual(summary["average_excess_return_pct"], 0)
        self.assertEqual(summary["average_holding_days"], 15)
        self.assertEqual(summary["average_holding_daily_return_pct"], 0.14)
        self.assertEqual(summary["profitable_rate_pct"], 50)
        self.assertEqual(summary["outperform_rate_pct"], 50)
        self.assertEqual(summary["portfolio_samples"], 2)
        self.assertEqual(summary["portfolio_return_pct"], 10)
        self.assertEqual(summary["portfolio_curve"][0]["equity"], 100)
        self.assertEqual(summary["portfolio_curve"][0]["benchmark"], 100)
        self.assertEqual(summary["portfolio_curve"][-1]["equity"], 110)
        self.assertEqual(summary["portfolio_max_drawdown_pct"], 0)
        self.assertGreater(summary["portfolio_sharpe"], 10)

    def test_batch_backtest_api_returns_strategy_comparison(self) -> None:
        def fake_snapshot(symbol: str, market: str, client: object, period: str) -> dict:
            snapshot = parse_chart_payload(chart_payload(), symbol)
            snapshot["symbol"] = symbol
            snapshot["name"] = f"测试-{symbol}"
            return snapshot

        with patch("backend.app.main.fetch_stock_snapshot", side_effect=fake_snapshot):
            response = TestClient(app).post("/api/backtest/batch", json={
                "symbols": ["600519", "000858"],
                "strategies": ["买入持有", "均线交叉"],
                "period": "2y",
            })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["succeeded_symbols"], 2)
        self.assertEqual(payload["failed_symbols"], 0)
        self.assertEqual(len(payload["results"]), 4)
        self.assertEqual(len(payload["summaries"]), 2)

    def test_ma20_ma60_first_pullback_buys_once_and_exits_below_ma60(self) -> None:
        closes = [100.0] * 60 + [float(value) for value in range(101, 116)] + [90.0] * 5
        rows = [
            {
                "timestamp": 1_700_000_000 + index * 86_400,
                "open": close,
                "high": close + 1,
                "low": 100.0 if index == 70 else close,
                "close": close,
                "volume": 1_000_000,
            }
            for index, close in enumerate(closes)
        ]
        snapshot = {"symbol": "TEST", "name": "测试标的", "points": rows, "data_source": "测试数据", "adjustment": "前复权"}
        result = run_backtest(snapshot, "MA20/60首次回踩", 100_000, 0.0003)
        self.assertEqual([event["side"] for event in result["trade_events"]], ["B", "S"])
        self.assertEqual([event["curve_index"] for event in result["trade_events"]], [71, 76])
        self.assertIn("首次回踩", result["trade_events"][0]["reason"])
        self.assertIn("跌破 MA60", result["trade_events"][1]["reason"])

    def test_ma20_ma55_cross_with_negative_j_buys_and_confirms_exit(self) -> None:
        closes = [100.0] * 60 + [101.0, 102.0, 103.0, 104.0, 105.0] + [90.0] * 30
        rows = [
            {
                "timestamp": 1_700_000_000 + index * 86_400,
                "open": close,
                "high": 200.0 if index >= 52 else 101.0,
                "low": 100.0 if index >= 52 and close >= 100 else close - 1,
                "close": close,
                "volume": 1_000_000,
            }
            for index, close in enumerate(closes)
        ]
        snapshot = {"symbol": "TEST", "name": "测试标的", "points": rows, "data_source": "测试数据", "adjustment": "前复权"}
        result = run_backtest(snapshot, "MA20/55金叉后J<13且涨幅<15%", 100_000, 0.0003)
        self.assertEqual([event["side"] for event in result["trade_events"]], ["B", "S"])
        self.assertEqual([event["curve_index"] for event in result["trade_events"]], [62, 66])
        self.assertIn("J=-3.04", result["trade_events"][0]["reason"])
        self.assertIn("MA55五日变化=0.05%", result["trade_events"][0]["reason"])
        self.assertIn("距金叉涨幅<15%", result["trade_events"][0]["reason"])
        self.assertIn("确认跌破 MA55", result["trade_events"][1]["reason"])

    def test_ma20_ma55_j_signal_rejects_more_than_fifteen_percent_rise(self) -> None:
        closes = [100.0] * 60 + [101.0] + [130.0] * 10
        rows = [
            {
                "timestamp": 1_700_000_000 + index * 86_400,
                "open": close,
                "high": 1000.0 if index >= 52 else 101.0,
                "low": 100.0 if index >= 52 else 99.0,
                "close": close,
                "volume": 1_000_000,
            }
            for index, close in enumerate(closes)
        ]
        snapshot = {"symbol": "TEST", "name": "测试标的", "points": rows, "data_source": "测试数据", "adjustment": "前复权"}
        result = run_backtest(snapshot, "MA20/55金叉后J<13且涨幅<15%")
        self.assertEqual(result["trade_events"], [])

    def test_ma20_ma55_j_signal_rejects_falling_ma55(self) -> None:
        closes = [200.0] * 30 + [100.0] * 30 + [110.0] * 60
        rows = [
            {
                "timestamp": 1_700_000_000 + index * 86_400,
                "open": close,
                "high": max(close, 200.0),
                "low": min(close, 100.0),
                "close": close,
                "volume": 1_000_000,
            }
            for index, close in enumerate(closes)
        ]
        snapshot = {"symbol": "TEST", "name": "测试标的", "points": rows, "data_source": "测试数据", "adjustment": "前复权"}
        result = run_backtest(snapshot, "MA20/55金叉后J<13且涨幅<15%")
        self.assertEqual(result["trade_events"], [])

    def test_ma120_and_ma200_pullback_hold_five_days_before_buying(self) -> None:
        for period in (120, 200):
            with self.subTest(period=period):
                touch_index = max(period + 10, 260)
                base = [100 + index * 0.1 for index in range(touch_index)]
                support_price = base[-1]
                closes = base + [support_price] * 7 + [50.0, 50.0]
                rows = [
                    {
                        "timestamp": 1_700_000_000 + index * 86_400,
                        "open": close,
                        "high": close + 1,
                        "low": 1.0 if index == touch_index else close,
                        "close": close,
                        "volume": 1_000_000,
                    }
                    for index, close in enumerate(closes)
                ]
                snapshot = {"symbol": "TEST", "name": "测试标的", "points": rows, "data_source": "测试数据", "adjustment": "前复权"}
                result = run_backtest(snapshot, f"MA{period}回踩5日不破")
                self.assertEqual([event["side"] for event in result["trade_events"]], ["B", "S"])
                self.assertEqual([event["curve_index"] for event in result["trade_events"]], [touch_index + 5, touch_index + 8])
                self.assertIn("第 2～5 日最高价均过线", result["trade_events"][0]["reason"])
                self.assertIn(f"跌破 MA{period}", result["trade_events"][1]["reason"])
                if period == 200:
                    self.assertIn("MA200>MA250", result["trade_events"][0]["reason"])

    def test_ma120_and_ma200_pullback_allows_closes_below_average_before_day_five(self) -> None:
        for period in (120, 200):
            with self.subTest(period=period):
                touch_index = max(period + 10, 260)
                base = [100 + index * 0.1 for index in range(touch_index)]
                previous_support = statistics.fmean(base[-period:])
                below_support = previous_support - 0.25
                closes = base + [below_support] * 4 + [previous_support + 1] * 2
                rows = [
                    {
                        "timestamp": 1_700_000_000 + index * 86_400,
                        "open": close,
                        "high": previous_support + 5 if index >= touch_index else close + 1,
                        "low": 1.0 if index == touch_index else close,
                        "close": close,
                        "volume": 1_000_000,
                    }
                    for index, close in enumerate(closes)
                ]
                snapshot = {"symbol": "TEST", "name": "测试标的", "points": rows, "data_source": "测试数据", "adjustment": "前复权"}
                result = run_backtest(snapshot, f"MA{period}回踩5日不破")
                self.assertEqual([event["side"] for event in result["trade_events"]], ["B"])
                self.assertEqual(result["trade_events"][0]["curve_index"], touch_index + 5)

    def test_ma120_and_ma200_pullback_reject_non_rising_average(self) -> None:
        for period in (120, 200):
            with self.subTest(period=period):
                touch_index = max(period + 10, 260)
                base = [200 - index * 0.1 for index in range(touch_index)]
                support_price = base[-1]
                closes = base + [support_price] * 7
                rows = [
                    {
                        "timestamp": 1_700_000_000 + index * 86_400,
                        "open": close,
                        "high": close + 1,
                        "low": 1.0 if index == touch_index else close,
                        "close": close,
                        "volume": 1_000_000,
                    }
                    for index, close in enumerate(closes)
                ]
                snapshot = {"symbol": "TEST", "name": "测试标的", "points": rows, "data_source": "测试数据", "adjustment": "前复权"}
                result = run_backtest(snapshot, f"MA{period}回踩5日不破")
                self.assertEqual(result["trade_events"], [])

    def test_ma200_pullback_requires_ma200_above_ma250(self) -> None:
        base = [200.0] * 100 + [100 + index * 0.1 for index in range(200)]
        touch_index = len(base)
        support = statistics.fmean(base[-200:])
        closes = base + [support] * 7
        rows = [
            {
                "timestamp": 1_700_000_000 + index * 86_400,
                "open": close,
                "high": support + 5 if index >= touch_index else close + 1,
                "low": 1.0 if index == touch_index else close,
                "close": close,
                "volume": 1_000_000,
            }
            for index, close in enumerate(closes)
        ]
        result = run_backtest(
            {"symbol": "TEST", "name": "MA200低于MA250", "points": rows, "data_source": "测试数据", "adjustment": "前复权"},
            "MA200回踩5日不破",
        )
        self.assertEqual(result["trade_events"], [])

    def test_calculates_all_requested_indicators_aligned_with_candles(self) -> None:
        snapshot = parse_chart_payload(chart_payload(), "600519.SS")
        indicators = calculate_indicators(snapshot["points"])
        self.assertTrue({"ma20", "macd_diff", "rsi14", "kdj_j", "atr14", "nine_turn", "boll_upper"}.issubset(indicators))
        self.assertTrue(all(len(values) == len(snapshot["points"]) for values in indicators.values()))

    def test_indicator_warmup_matches_continuous_kdj_history(self) -> None:
        rows = parse_chart_payload(chart_payload(), "600519.SS")["points"]
        continuous = attach_indicators(rows, ["KDJ"])
        warmed = attach_indicators(rows[50:], ["KDJ"], rows[:50])
        self.assertEqual(
            [row["kdj_j"] for row in warmed],
            [row["kdj_j"] for row in continuous[50:]],
        )

    def test_rsi14_matches_tonghuashun_sma_recurrence_and_warmup(self) -> None:
        values = [100.0, 102.0, 101.0, 104.0]
        series = rsi(values, 14)
        self.assertEqual(series[1], 100.0)
        self.assertAlmostEqual(series[2], 26 / 27 * 100, places=6)

        rows = [
            {"timestamp": 1_700_000_000 + index * 86_400, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1_000_000}
            for index, close in enumerate(100 + (index % 7) - (index % 3) * 1.5 for index in range(80))
        ]
        continuous = attach_indicators(rows, ["RSI"])
        warmed = attach_indicators(rows[50:], ["RSI"], rows[:50])
        self.assertEqual([row["rsi14"] for row in warmed], [row["rsi14"] for row in continuous[50:]])

    def test_safe_strategy_dsl_runs_and_rejects_python_imports(self) -> None:
        snapshot = parse_chart_payload(chart_payload(), "600519.SS")
        code = '\n'.join([
            'NAME="测试 DSL"',
            'ENTRY=ALL(GT(CLOSE,MA(5)),GT(MA(5),REF(MA(5),1)))',
            'EXIT=LT(CLOSE,MA(5))',
        ])
        result = run_dsl_backtest(snapshot, code)
        self.assertEqual(result["strategy"], "测试 DSL")
        self.assertEqual(result["trade_events"][0]["side"], "B")
        self.assertIn("下一交易日开盘买入", describe_strategy_code(code))
        with self.assertRaises(StrategyCodeError):
            run_dsl_backtest(snapshot, 'import os\nNAME="危险"\nENTRY=GT(CLOSE,1)\nEXIT=LT(CLOSE,1)')

    def test_capital_pool_respects_position_and_volume_limits(self) -> None:
        first, second = 1_700_000_000, 1_700_086_400
        candidates = [{
            "symbol": symbol, "name": symbol, "entry_timestamp": first, "entry_price": 10.0,
            "entry_volume": 100_000, "exit_timestamp": second, "exit_price": 11.0,
            "exit_volume": 100_000, "score": score, "average_turnover": 50_000_000,
            "entry_reason": "RSI信号", "exit_reason": "测试退出", "signal_timestamp": first - 86_400,
        } for symbol, score in (("000001.SZ", 10), ("000002.SZ", 9))]
        selected = select_capital_pool_trades(
            candidates, 1_000_000, 0.0003, 0.8, 0.1, 10, 0.03, 5, 30,
        )
        self.assertEqual(len(selected), 2)
        self.assertTrue(all(item["shares"] <= 3_000 for item in selected))
        self.assertTrue(all(item["buy_cost"] <= 100_000 for item in selected))
        snapshots = {
            item["symbol"]: {"points": [
                {"timestamp": first, "open": 10, "high": 10, "low": 10, "close": 10, "volume": 100_000},
                {"timestamp": second, "open": 11, "high": 11, "low": 11, "close": 11, "volume": 100_000},
            ]} for item in selected
        }
        benchmark = {"points": [
            {"timestamp": first, "close": 100}, {"timestamp": second, "close": 105},
        ]}
        result = build_capital_pool_result(selected, snapshots, benchmark, 1_000_000, 0.0003, 2)
        self.assertEqual(result["selected_trades"], 2)
        self.assertEqual(len(result["curve"]), 2)
        self.assertGreater(result["final_equity"], 1_000_000)

    def test_capital_pool_prefers_lower_rsi_rebound_score(self) -> None:
        first, second = 1_700_000_000, 1_700_086_400
        candidates = [{
            "symbol": symbol, "name": symbol, "entry_timestamp": first, "entry_price": 10.0,
            "entry_volume": 100_000, "exit_timestamp": second, "exit_price": 11.0,
            "exit_volume": 100_000, "score": score, "average_turnover": 50_000_000,
            "entry_reason": "RSI信号", "exit_reason": "测试退出", "signal_timestamp": first - 86_400,
        } for symbol, score in (("000001.SZ", 10), ("000002.SZ", 2))]
        selected = select_capital_pool_trades(
            candidates, 1_000_000, 0.0003, 0.1, 0.1, 1, 1, 0, 0,
        )
        self.assertEqual([item["symbol"] for item in selected], ["000002.SZ"])

    def test_capital_pool_can_select_ten_day_decline_rank_15_to_39(self) -> None:
        first, second = 1_700_000_000, 1_700_086_400
        candidates = [{
            "symbol": f"{index:06d}.SZ", "name": str(index),
            "entry_timestamp": first, "entry_price": 10.0, "entry_volume": 100_000,
            "exit_timestamp": second, "exit_price": 11.0, "exit_volume": 100_000,
            "score": 1, "ten_day_return_pct": float(index), "average_turnover": 50_000_000,
            "entry_reason": "RSI信号", "exit_reason": "测试退出", "signal_timestamp": first - 86_400,
        } for index in range(1, 46)]
        selected = select_capital_pool_trades(
            candidates, 10_000_000, 0, 1, 0.04, 25, 1, 0, 0,
            "ten_day_decline_rank_15_39",
        )
        self.assertEqual(len(selected), 25)
        self.assertEqual(selected[0]["symbol"], "000015.SZ")
        self.assertEqual(selected[-1]["symbol"], "000039.SZ")
        self.assertEqual([item["candidate_rank"] for item in selected], list(range(15, 40)))

    def test_market_signal_threshold_filters_entry(self) -> None:
        snapshot = parse_chart_payload(chart_payload(), "600519.SS")
        signal_timestamp = int(snapshot["points"][0]["timestamp"])
        with patch("backend.app.tools.backtest._target_position", return_value=1):
            rejected = run_backtest(
                snapshot, "均线交叉", market_signal_counts={signal_timestamp: 149},
                market_signal_threshold=150,
            )
            accepted = run_backtest(
                snapshot, "均线交叉", market_signal_counts={signal_timestamp: 150},
                market_signal_threshold=150,
            )
        self.assertEqual(rejected["trades"], 0)
        self.assertEqual(accepted["trade_events"][0]["side"], "B")
        self.assertIn("达到门槛150", accepted["trade_events"][0]["reason"])

    def test_volume_ratio_threshold_filters_entry_without_future_data(self) -> None:
        snapshot = parse_chart_payload(chart_payload(), "600519.SS")
        rows = snapshot["points"]
        for index, row in enumerate(rows):
            row["volume"] = 120 if 30 <= index <= 34 else 100

        def enter_on_signal_34(*args, **kwargs):
            return 1 if args[3] == 34 and args[4] == 0 else args[4]

        with patch("backend.app.tools.backtest._target_position", side_effect=enter_on_signal_34):
            accepted = run_backtest(snapshot, "均线交叉", volume_ratio_threshold=1.2)
        self.assertEqual(accepted["trade_events"][0]["side"], "B")
        self.assertIn("量能比=1.20", accepted["trade_events"][0]["reason"])

        rows[34]["volume"] = 119
        with patch("backend.app.tools.backtest._target_position", side_effect=enter_on_signal_34):
            rejected = run_backtest(snapshot, "均线交叉", volume_ratio_threshold=1.2)
        self.assertEqual(rejected["trades"], 0)
        self.assertGreater(rejected["volume_ratio_filter"]["filtered_entries"], 0)

    def test_backtest_applies_slippage_to_both_sides(self) -> None:
        snapshot = parse_chart_payload(chart_payload(), "600519.SS")

        def one_round_trip(*args, **kwargs):
            return 1 if args[3] == 0 else 0

        with patch("backend.app.tools.backtest._target_position", side_effect=one_round_trip):
            baseline = run_backtest(snapshot, "均线交叉", fee_rate=0, slippage_bps=0)
        with patch("backend.app.tools.backtest._target_position", side_effect=one_round_trip):
            slipped = run_backtest(snapshot, "均线交叉", fee_rate=0, slippage_bps=10)

        self.assertEqual([event["side"] for event in slipped["trade_events"]], ["B", "S"])
        self.assertAlmostEqual(
            slipped["trade_events"][0]["price"],
            float(snapshot["points"][1]["open"]) * 1.001,
            places=4,
        )
        self.assertAlmostEqual(
            slipped["trade_events"][1]["price"],
            float(snapshot["points"][2]["open"]) * 0.999,
            places=4,
        )
        self.assertLess(slipped["total_return_pct"], baseline["total_return_pct"])
        self.assertEqual(slipped["slippage_bps"], 10)

    def test_raw_rsi_market_signal_scan_ignores_position_state(self) -> None:
        timestamps = [1_700_000_000 + index * 86_400 for index in range(3)]
        snapshot = {"points": [
            {"timestamp": timestamps[0], "open": 100, "high": 101, "low": 95, "close": 100},
            {"timestamp": timestamps[1], "open": 100, "high": 104, "low": 99, "close": 103},
            {"timestamp": timestamps[2], "open": 102, "high": 106, "low": 101, "close": 105},
        ]}
        with patch("backend.app.tools.backtest._snapshot_rsi_values", return_value=[29.0, 31.0, 32.0]):
            signals = rsi_risk_raw_signal_timestamps(snapshot)
        self.assertEqual(signals, [timestamps[1]])

    def test_capital_pool_rejects_next_open_gap_above_three_percent(self) -> None:
        timestamps = [1_700_000_000 + index * 86_400 for index in range(3)]
        snapshot = {
            "symbol": "000001.SZ", "name": "测试股票", "points": [
                {"timestamp": timestamps[0], "open": 99, "high": 101, "low": 98, "close": 100, "volume": 100_000},
                {"timestamp": timestamps[1], "open": 104, "high": 105, "low": 103, "close": 104, "volume": 100_000},
                {"timestamp": timestamps[2], "open": 105, "high": 106, "low": 104, "close": 105, "volume": 100_000},
            ], "data_source": "测试", "adjustment": "动态前复权",
        }
        result = {"trade_events": [
            {"side": "B", "timestamp": timestamps[1], "signal_timestamp": timestamps[0], "price": 104,
             "reason": "已离开20日最低价2.50%（RSI=32.00）"},
            {"side": "S", "timestamp": timestamps[2], "price": 105, "reason": "测试退出"},
        ]}
        with patch("backend.app.tools.portfolio_backtest.run_backtest", return_value=result):
            self.assertEqual(build_rsi_pool_candidates(snapshot, {}, 0.0003), [])
            result["trade_events"][0]["price"] = 103
            accepted = build_rsi_pool_candidates(snapshot, {}, 0.0003)
            self.assertEqual(len(accepted), 1)
            self.assertAlmostEqual(accepted[0]["opening_gap_pct"], 3.0)

    def test_capital_pool_supports_strategy_and_entry_filters(self) -> None:
        timestamps = [1_700_000_000 + index * 86_400 for index in range(210)]
        points = [{
            "timestamp": timestamp, "open": 10 + index * 0.01,
            "high": 10.1 + index * 0.01, "low": 9.9 + index * 0.01,
            "close": 10 + index * 0.01, "volume": 100_000,
        } for index, timestamp in enumerate(timestamps)]
        snapshot = {
            "symbol": "000001.SZ", "name": "测试股票", "points": points,
            "data_source": "测试", "adjustment": "动态前复权",
        }
        result = {"trade_events": [
            {"side": "B", "timestamp": timestamps[206], "signal_timestamp": timestamps[205],
             "price": points[206]["open"], "reason": "均线交叉信号", "fundamental_score": 75},
            {"side": "S", "timestamp": timestamps[208], "price": points[208]["open"],
             "reason": "测试退出"},
        ]}
        with patch("backend.app.tools.portfolio_backtest.run_backtest", return_value=result) as mocked:
            accepted = build_pool_candidates(
                snapshot, {}, 0.0003, "均线交叉", max_opening_gap_pct=3,
                minimum_turnover=1_000_000, require_above_ma200=True,
                require_ma200_rising=True,
            )
            rejected = build_pool_candidates(
                snapshot, {}, 0.0003, "均线交叉", minimum_turnover=100_000_000,
            )
            build_pool_candidates(
                snapshot, {}, 0.0003, "RSI反转+止盈止损",
                market_signal_counts={timestamps[205]: 180}, market_signal_threshold=150,
                fundamental_reports=[{"score": 75}], fundamental_score_threshold=60,
            )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejected, [])
        self.assertEqual(mocked.call_args_list[0].args[1], "均线交叉")
        self.assertEqual(mocked.call_args.kwargs["market_signal_counts"], {timestamps[205]: 180})
        self.assertEqual(mocked.call_args.kwargs["market_signal_threshold"], 150)
        self.assertEqual(mocked.call_args.kwargs["fundamental_reports"], [{"score": 75}])
        self.assertEqual(mocked.call_args.kwargs["fundamental_score_threshold"], 60)

    def test_latest_strategy_signal_only_returns_signal_on_last_candle(self) -> None:
        period = 120
        touch_index = period + 10
        base = [100 + index * 0.1 for index in range(touch_index)]
        support = statistics.fmean(base[-period:])
        closes = base + [support - 0.25] * 4 + [support + 1]
        rows = [
            {"timestamp": 1_700_000_000 + index * 86_400, "open": close, "high": support + 5, "low": 1 if index == touch_index else close, "close": close, "volume": 1_000_000}
            for index, close in enumerate(closes)
        ]
        snapshot = {"symbol": "TEST", "name": "测试", "points": rows, "data_source": "测试", "adjustment": "前复权"}
        signal = latest_strategy_signal(snapshot, "MA120回踩5日不破")
        self.assertIsNotNone(signal)
        self.assertEqual(signal["side"], "B")


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_analysis_streams(self) -> None:
        snapshot = parse_chart_payload(chart_payload(), "600519.SS")
        chunks = [
            chunk
            async for chunk in stream_stock_analysis(snapshot, "波段", "综合分析", DEMO_SETTINGS)
        ]
        self.assertGreater(len(chunks), 3)
        self.assertIn("测试公司", "".join(chunks))
        self.assertIn("不构成", "".join(chunks))

    async def test_demo_news_analysis_streams_source_links(self) -> None:
        news = [{"title": "测试公司发布公告", "url": "https://example.com/a", "source": "测试媒体", "published_at": datetime.now(timezone.utc).isoformat(), "summary": "摘要"}]
        chunks = [chunk async for chunk in stream_news_analysis("测试公司", news, "事件影响", DEMO_SETTINGS)]
        text = "".join(chunks)
        self.assertIn("测试公司发布公告", text)
        self.assertIn("https://example.com/a", text)


class NewsFeedTests(unittest.TestCase):
    def rss(self) -> str:
        published = format_datetime(datetime.now(timezone.utc))
        return f'''<?xml version="1.0"?><rss><channel><item><title>测试公司重大进展</title><link>https://example.com/news</link><pubDate>{published}</pubDate><description><![CDATA[<p>新闻摘要</p>]]></description><source>测试媒体</source></item></channel></rss>'''

    def test_parses_rss_metadata(self) -> None:
        items = parse_news_rss(self.rss(), "测试源")
        self.assertEqual(items[0]["title"], "测试公司重大进展")
        self.assertEqual(items[0]["source"], "测试媒体")
        self.assertEqual(items[0]["summary"], "新闻摘要")

    def test_fetch_deduplicates_two_providers(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=self.rss())
        client = httpx.Client(transport=httpx.MockTransport(handler))
        items = fetch_stock_news("测试公司", "3d", 10, client)
        client.close()
        self.assertEqual(len(items), 1)


if __name__ == "__main__":
    unittest.main()
