import asyncio
import json
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .agent_service import agno_enabled, stream_news_analysis, stream_stock_analysis
from .config import get_settings
from .database import (
    complete_backtest_run,
    create_job,
    create_backtest_run,
    delete_backtest_run,
    fail_interrupted_jobs,
    get_backtest_run,
    get_backtest_run_summary,
    get_backtest_result,
    init_database,
    get_job,
    get_active_job,
    list_instruments,
    list_scan_signals,
    list_strategy_positions,
    list_backtest_runs,
    list_backtest_result_summaries,
    market_data_status,
    market_data_coverage_on_date,
    load_fundamental_reports,
    save_fundamental_reports,
    save_backtest_result,
)
from .history_service import get_stock_history
from .schemas import (
    AnalyzeRequest,
    BacktestRequest,
    BacktestCompareRequest,
    BatchBacktestRequest,
    CapitalPoolBacktestRequest,
    CapitalPoolPlanRequest,
    DividendDataSyncRequest,
    FactorAnalysisRequest,
    FundamentalDataSyncRequest,
    FullMarketBacktestRequest,
    HistoricalBacktestRequest,
    MarketDataSyncRequest,
    MarketScanRequest,
    NewsRequest,
    StrategyExplainRequest,
    StrategyGenerateRequest,
)
from .strategy_agent import explain_strategy_code, generate_strategy_code
from .task_service import (
    launch, run_capital_pool_backtest, run_full_market_backtest,
    build_capital_pool_plan, scan_market, sync_dividend_data, sync_fundamental_data, sync_market_data,
)
from .tools.backtest import BENCHMARK_SYMBOL, run_backtest, summarize_backtest_results
from .tools.indicators import attach_indicators
from .tools.strategy_dsl import StrategyCodeError, run_dsl_backtest
from .tools.market_data import (
    MarketDataError, fetch_stock_history, fetch_stock_history_with_adjustments, fetch_stock_snapshot,
)
from .tools.fundamentals import fetch_fundamental_reports
from .tools.factor_analysis import analyze_factors, factor_catalog
from .tools.news_feed import NewsFeedError, fetch_stock_news


settings = get_settings()
init_database()
fail_interrupted_jobs()
app = FastAPI(title="Stock Research Agent", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "mode": "agno" if agno_enabled(settings) else "demo",
        "model": settings.model_id if agno_enabled(settings) else None,
    }




@app.get("/api/database/market-status")
async def database_market_status(market: str = Query(default="A股", pattern="^(A股|港股|美股)$")) -> dict[str, object]:
    return await run_in_threadpool(market_data_status, market)


@app.get("/api/database/market-status/check")
async def check_database_market_status() -> dict[str, object]:
    """主动探测行情源最新交易日，并核对本地前复权/完整行情覆盖。"""
    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=45)).isoformat()
    probes: dict[str, dict[str, object]] = {}
    errors: dict[str, str] = {}
    for scope, fetcher in (
        ("front", fetch_stock_history),
        ("full", fetch_stock_history_with_adjustments),
    ):
        try:
            snapshot = await run_in_threadpool(fetcher, "000001.SZ", "A股", start_date, end_date)
            points = snapshot.get("points") or []
            if not points:
                raise RuntimeError("行情源没有返回有效日线")
            target_date = datetime.fromtimestamp(int(points[-1]["timestamp"]), timezone.utc).date().isoformat()
            coverage = await run_in_threadpool(market_data_coverage_on_date, target_date, "A股")
            probes[scope] = {"source_latest_trade_date": target_date, **coverage}
        except Exception as exc:
            errors[scope] = str(exc)
    if not probes:
        raise HTTPException(status_code=502, detail=f"无法探测 AKShare 最新行情：{errors}")
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "front": probes.get("front"), "full": probes.get("full"), "errors": errors,
    }


@app.get("/api/factors/catalog")
def factors_catalog() -> list[dict[str, object]]:
    return factor_catalog()


@app.post("/api/factors/analyze")
async def factors_analyze(request: FactorAnalysisRequest) -> dict[str, object]:
    try:
        return await run_in_threadpool(
            analyze_factors,
            request.factors,
            request.start_date.isoformat(),
            request.end_date.isoformat(),
            request.forward_days,
            request.universe_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/market/candles")
async def market_candles(
    symbol: str,
    market: str = "自动",
    start_date: str = "2020-01-01",
    end_date: str | None = None,
    indicators: list[str] = Query(default=["MA", "MACD", "RSI", "KDJ", "ATR", "九转", "BOLL"]),
    refresh: bool = False,
    adjustment_mode: str = Query(default="前复权", pattern="^(前复权|后复权|不复权|动态前复权)$"),
) -> dict[str, object]:
    try:
        snapshot = await run_in_threadpool(get_stock_history, symbol, market, start_date, end_date, refresh, adjustment_mode)
        public_snapshot = {key: value for key, value in snapshot.items() if key != "indicator_warmup_points"}
        return {
            **public_snapshot,
            "points": attach_indicators(snapshot["points"], indicators, snapshot.get("indicator_warmup_points")),
            "indicators": indicators,
            "indicator_note": f"KDJ、RSI、MA 等指标使用{adjustment_mode}日线，并使用开始日前最多 250 根日线预热。",
        }
    except (MarketDataError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/backtests/run")
async def run_historical_backtest(request: HistoricalBacktestRequest) -> dict[str, object]:
    start_date, end_date = request.start_date.isoformat(), request.end_date.isoformat()
    if request.start_date > request.end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    if not request.strategies and not request.strategy_code:
        raise HTTPException(status_code=422, detail="至少选择一个内置策略或提供自定义策略代码")
    strategy_labels = list(request.strategies) + (["AI 自定义策略"] if request.strategy_code else [])
    run_id = create_backtest_run(
        request.name,
        "single" if len(request.symbols) == 1 else "compare",
        start_date,
        end_date,
        strategy_labels,
        {
            "symbols": request.symbols, "initial_capital": request.initial_capital,
            "fee_rate": request.fee_rate, "slippage_bps": request.slippage_bps,
            "strategy_code": request.strategy_code,
            "fundamental_score_enabled": request.fundamental_score_enabled,
            "fundamental_score_threshold": request.fundamental_score_threshold,
            "adjustment_mode": request.adjustment_mode,
        },
    )
    results: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    try:
        benchmark_snapshot = await run_in_threadpool(
            get_stock_history, BENCHMARK_SYMBOL, "A股", start_date, end_date, request.refresh_data,
        )
        for symbol in request.symbols:
            try:
                snapshot = await run_in_threadpool(
                    get_stock_history, symbol, request.market, start_date, end_date, request.refresh_data, request.adjustment_mode,
                )
                fundamental_reports = None
                fundamental_threshold = None
                if request.fundamental_score_enabled:
                    fundamental_reports = await run_in_threadpool(load_fundamental_reports, symbol, end_date)
                    if not fundamental_reports or request.refresh_data:
                        fetched_reports = await run_in_threadpool(fetch_fundamental_reports, symbol)
                        await run_in_threadpool(save_fundamental_reports, symbol, fetched_reports)
                        fundamental_reports = await run_in_threadpool(load_fundamental_reports, symbol, end_date)
                    if not fundamental_reports:
                        raise MarketDataError(f"{symbol} 在回测截止日前没有可用基本面评分")
                    fundamental_threshold = request.fundamental_score_threshold
                for strategy in request.strategies:
                    result = await run_in_threadpool(
                        run_backtest, snapshot, strategy, request.initial_capital, request.fee_rate, benchmark_snapshot,
                        fundamental_reports, fundamental_threshold, slippage_bps=request.slippage_bps,
                    )
                    result["excess_return_pct"] = round(result["total_return_pct"] - result["benchmark_return_pct"], 2)
                    await run_in_threadpool(save_backtest_result, run_id, result)
                    results.append(result)
                if request.strategy_code:
                    result = await run_in_threadpool(
                        run_dsl_backtest, snapshot, request.strategy_code, request.initial_capital, request.fee_rate, benchmark_snapshot,
                        fundamental_reports, fundamental_threshold, slippage_bps=request.slippage_bps,
                    )
                    result["excess_return_pct"] = round(result["total_return_pct"] - result["benchmark_return_pct"], 2)
                    await run_in_threadpool(save_backtest_result, run_id, result)
                    results.append(result)
            except Exception as exc:
                errors.append({"symbol": symbol, "message": str(exc)})
        summary = {
            "requested_symbols": len(request.symbols),
            "successful_results": len(results),
            "failed_symbols": len(errors),
            "strategies": summarize_backtest_results(results),
        }
        await run_in_threadpool(complete_backtest_run, run_id, summary)
        return {"run_id": run_id, "status": "completed", "summary": summary, "results": results, "errors": errors}
    except Exception as exc:
        await run_in_threadpool(complete_backtest_run, run_id, None, str(exc))
        raise HTTPException(status_code=500, detail=f"回测任务失败：{exc}") from exc


@app.get("/api/backtests/records")
async def backtest_records(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, object]]:
    return await run_in_threadpool(list_backtest_runs, limit)


@app.get("/api/backtests/records/{run_id}")
async def backtest_record(run_id: str) -> dict[str, object]:
    record = await run_in_threadpool(get_backtest_run, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    return record


@app.get("/api/backtests/records/{run_id}/summary")
async def backtest_record_summary(run_id: str) -> dict[str, object]:
    record = await run_in_threadpool(get_backtest_run_summary, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    return record


@app.get("/api/backtests/records/{run_id}/results")
async def backtest_record_results(
    run_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    query: str | None = Query(default=None, max_length=50),
    strategy: str | None = Query(default=None, max_length=100),
    sort_by: str = Query(default="symbol", pattern="^(symbol|return|drawdown|sharpe|win_rate)$"),
    sort_order: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> dict[str, object]:
    response = await run_in_threadpool(
        list_backtest_result_summaries, run_id, limit, offset, query, strategy, sort_by, sort_order,
    )
    if response.get("not_found"):
        raise HTTPException(status_code=404, detail="回测记录不存在或已删除")
    return response


@app.get("/api/backtests/records/{run_id}/results/{result_id}")
async def backtest_record_result(run_id: str, result_id: int) -> dict[str, object]:
    result = await run_in_threadpool(get_backtest_result, run_id, result_id)
    if result is None:
        raise HTTPException(status_code=404, detail="回测明细不存在")
    return result


@app.delete("/api/backtests/records/{run_id}")
async def remove_backtest_record(run_id: str) -> dict[str, object]:
    deleted = await run_in_threadpool(delete_backtest_run, run_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="回测记录不存在或已删除")
    return {"deleted": True, "run_id": run_id, "recoverable": True}


@app.post("/api/backtests/compare")
async def compare_backtest_records(request: BacktestCompareRequest) -> dict[str, object]:
    records = []
    missing = []
    for run_id in request.run_ids:
        record = await run_in_threadpool(get_backtest_run_summary, run_id)
        if record is None:
            missing.append(run_id)
        else:
            records.append(record)
    rows = []
    for record in records:
        strategy_rows = ((record.get("summary") or {}).get("strategies") or [])
        for summary in strategy_rows:
            rows.append({
                "run_id": record["id"],
                "record_name": record["name"],
                "scope": record["scope"],
                "start_date": record["start_date"],
                "end_date": record["end_date"],
                "result_count": record["result_count"],
                **summary,
            })
    rows.sort(
        key=lambda row: float(row["average_return_pct"]) if row.get("average_return_pct") is not None else float("-inf"),
        reverse=True,
    )
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return {"selected": len(request.run_ids), "available": len(records), "missing": missing, "rows": rows}


@app.get("/api/market/instruments")
async def market_instruments() -> list[dict[str, object]]:
    return await run_in_threadpool(list_instruments, "A股")


@app.post("/api/jobs/data-sync", status_code=202)
async def create_data_sync_job(request: MarketDataSyncRequest) -> dict[str, str]:
    if request.start_date > request.end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    active = await run_in_threadpool(get_active_job, "data_sync")
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"已有行情同步任务正在运行（{active['progress_current']}/{active['progress_total']}，任务ID：{active['id']}），请勿重复启动",
        )
    payload = request.model_dump(mode="json")
    job_id = await run_in_threadpool(create_job, "data_sync", payload)
    launch(sync_market_data(job_id))
    return {"job_id": job_id, "status": "pending"}


@app.post("/api/jobs/full-market-backtest", status_code=202)
async def create_full_market_backtest_job(request: FullMarketBacktestRequest) -> dict[str, str]:
    if request.start_date > request.end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    payload = request.model_dump(mode="json")
    job_id = await run_in_threadpool(create_job, "full_market_backtest", payload)
    launch(run_full_market_backtest(job_id))
    return {"job_id": job_id, "status": "pending"}


@app.post("/api/jobs/capital-pool-backtest", status_code=202)
async def create_capital_pool_backtest_job(request: CapitalPoolBacktestRequest) -> dict[str, str]:
    if request.start_date > request.end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    if request.single_position_limit > request.exposure_limit:
        raise HTTPException(status_code=422, detail="单票仓位上限不能高于总仓位上限")
    active = await run_in_threadpool(get_active_job, "capital_pool_backtest")
    if active:
        raise HTTPException(status_code=409, detail="已有资金池回测正在运行，请勿重复启动")
    job_id = await run_in_threadpool(create_job, "capital_pool_backtest", request.model_dump(mode="json"))
    launch(run_capital_pool_backtest(job_id))
    return {"job_id": job_id, "status": "pending"}


@app.post("/api/jobs/capital-pool-plan", status_code=202)
async def create_capital_pool_plan_job(request: CapitalPoolPlanRequest) -> dict[str, str]:
    if request.start_date > request.end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    active = await run_in_threadpool(get_active_job, "capital_pool_plan")
    if active:
        raise HTTPException(status_code=409, detail="已有资金池交易计划正在生成，请勿重复启动")
    job_id = await run_in_threadpool(create_job, "capital_pool_plan", request.model_dump(mode="json"))
    launch(build_capital_pool_plan(job_id))
    return {"job_id": job_id, "status": "pending"}


@app.post("/api/jobs/fundamental-sync", status_code=202)
async def create_fundamental_sync_job(request: FundamentalDataSyncRequest) -> dict[str, str]:
    payload = request.model_dump(mode="json")
    job_id = await run_in_threadpool(create_job, "fundamental_sync", payload)
    launch(sync_fundamental_data(job_id))
    return {"job_id": job_id, "status": "pending"}


@app.post("/api/jobs/dividend-sync", status_code=202)
async def create_dividend_sync_job(request: DividendDataSyncRequest) -> dict[str, str]:
    job_id = await run_in_threadpool(create_job, "dividend_sync", request.model_dump(mode="json"))
    launch(sync_dividend_data(job_id))
    return {"job_id": job_id, "status": "pending"}


@app.post("/api/jobs/market-scan", status_code=202)
async def create_market_scan_job(request: MarketScanRequest) -> dict[str, str]:
    if request.start_date > request.end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    payload = request.model_dump(mode="json")
    job_id = await run_in_threadpool(create_job, "market_scan", payload)
    launch(scan_market(job_id))
    return {"job_id": job_id, "status": "pending"}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, object]:
    job = await run_in_threadpool(get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.get("/api/jobs/{job_id}/signals")
async def job_signals(job_id: str) -> list[dict[str, object]]:
    job = await run_in_threadpool(get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return await run_in_threadpool(list_scan_signals, job_id)


@app.get("/api/strategy-positions")
async def strategy_positions(strategy: str | None = None, status: str = Query(default="open", pattern="^(open|closed|all)$")) -> list[dict[str, object]]:
    if status == "all":
        return await run_in_threadpool(list_strategy_positions, strategy, "open") + await run_in_threadpool(list_strategy_positions, strategy, "closed")
    return await run_in_threadpool(list_strategy_positions, strategy, status)


@app.post("/api/strategies/generate")
async def strategy_generate(request: StrategyGenerateRequest) -> dict[str, object]:
    try:
        return await generate_strategy_code(request.description, settings)
    except StrategyCodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI 策略生成失败：{exc}") from exc


@app.post("/api/strategies/explain")
async def strategy_explain(request: StrategyExplainRequest) -> dict[str, object]:
    try:
        return await explain_strategy_code(request.code, settings)
    except StrategyCodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI 策略解释失败：{exc}") from exc


@app.post("/api/analyze/stream")
async def analyze_stream(request: AnalyzeRequest) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        yield sse("meta", {"mode": "agno" if agno_enabled(settings) else "demo"})
        try:
            snapshot = await run_in_threadpool(fetch_stock_snapshot, request.symbol, request.market, None, request.period)
            yield sse("snapshot", snapshot)
            async for token in stream_stock_analysis(snapshot, request.horizon, request.focus, settings):
                yield sse("token", {"content": token})
            yield sse("done", {"ok": True})
        except MarketDataError as exc:
            yield sse("error", {"message": str(exc)})
        except Exception as exc:
            yield sse("error", {"message": f"股票分析失败：{exc}"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/backtest")
async def backtest(request: BacktestRequest) -> dict[str, object]:
    try:
        snapshot = await run_in_threadpool(fetch_stock_snapshot, request.symbol, request.market, None, request.period)
        benchmark_snapshot = await run_in_threadpool(fetch_stock_snapshot, BENCHMARK_SYMBOL, "A股", None, request.period)
        return await run_in_threadpool(
            run_backtest, snapshot, request.strategy, request.initial_capital, request.fee_rate, benchmark_snapshot,
            slippage_bps=request.slippage_bps,
        )
    except MarketDataError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/backtest/batch")
async def batch_backtest(request: BatchBacktestRequest) -> dict[str, object]:
    semaphore = asyncio.Semaphore(4)
    benchmark_snapshot = await run_in_threadpool(fetch_stock_snapshot, BENCHMARK_SYMBOL, "A股", None, request.period)

    async def run_symbol(symbol: str) -> tuple[list[dict[str, object]], dict[str, str] | None]:
        async with semaphore:
            try:
                snapshot = await run_in_threadpool(fetch_stock_snapshot, symbol, request.market, None, request.period)
                rows = []
                for strategy in request.strategies:
                    result = await run_in_threadpool(
                        run_backtest, snapshot, strategy, request.initial_capital, request.fee_rate, benchmark_snapshot,
                        slippage_bps=request.slippage_bps,
                    )
                    rows.append({
                        "requested_symbol": symbol,
                        "symbol": result["symbol"],
                        "name": result["name"],
                        "strategy": strategy,
                        "total_return_pct": result["total_return_pct"],
                        "annualized_return_pct": result["annualized_return_pct"],
                        "holding_days": result["holding_days"],
                        "holding_daily_return_pct": result["holding_daily_return_pct"],
                        "benchmark_return_pct": result["benchmark_return_pct"],
                        "excess_return_pct": round(result["total_return_pct"] - result["benchmark_return_pct"], 2),
                        "max_drawdown_pct": result["max_drawdown_pct"],
                        "sharpe": result["sharpe"],
                        "trades": result["trades"],
                        "win_rate_pct": result["win_rate_pct"],
                    })
                return rows, None
            except Exception as exc:
                return [], {"symbol": symbol, "message": str(exc)}

    completed = await asyncio.gather(*(run_symbol(symbol) for symbol in request.symbols))
    results = [row for rows, _ in completed for row in rows]
    errors = [error for _, error in completed if error is not None]
    return {
        "requested_symbols": len(request.symbols),
        "succeeded_symbols": len(request.symbols) - len(errors),
        "failed_symbols": len(errors),
        "period": request.period,
        "summaries": summarize_backtest_results(results),
        "results": results,
        "errors": errors,
    }


@app.post("/api/news/stream")
async def news_stream(request: NewsRequest) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        yield sse("meta", {"mode": "agno" if agno_enabled(settings) else "demo"})
        try:
            news = await run_in_threadpool(fetch_stock_news, request.query, request.lookback, request.limit)
            yield sse("news", news)
            async for token in stream_news_analysis(request.query, news, request.focus, settings):
                yield sse("token", {"content": token})
            yield sse("done", {"ok": True, "count": len(news)})
        except NewsFeedError as exc:
            yield sse("error", {"message": str(exc)})
        except Exception as exc:
            yield sse("error", {"message": f"新闻 Agent 调用失败：{exc}"})

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
