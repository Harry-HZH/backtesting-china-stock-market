from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import math
import statistics
from typing import Any, Coroutine

from .database import (
    complete_backtest_run,
    create_backtest_run,
    adjustment_history_complete,
    get_job,
    get_bar_coverage,
    fundamental_cache_is_fresh,
    fundamental_coverage,
    dividend_coverage,
    latest_market_trade_date,
    market_data_status,
    list_backtest_instruments,
    list_instruments,
    list_strategy_positions,
    load_fundamental_reports,
    load_dividend_events,
    load_snapshot,
    price_mode_coverage,
    save_fundamental_reports,
    save_dividend_events,
    save_backtest_result,
    save_scan_signal,
    sync_strategy_position,
    save_snapshot,
    update_job,
    upsert_instruments,
)
from .tools.backtest import (
    BENCHMARK_SYMBOL, latest_strategy_signal, rsi_risk_raw_signal_timestamps,
    run_backtest, summarize_backtest_results,
)
from .tools.market_data import ETF_SYMBOLS, fetch_stock_history, fetch_stock_history_with_adjustments, normalize_symbol
from .tools.portfolio_backtest import (
    build_capital_pool_result,
    build_dividend_quality_candidates,
    build_multifactor_candidates,
    build_pure_a_etf_candidates,
    run_pure_a_v25_weight_backtest,
    build_trend_rotation_candidates,
    attach_market_exposure,
    build_pool_candidates,
    select_capital_pool_trades,
)
from .tools.market_universe import ETF_ROTATION_SYMBOLS, ETF_SIMPLE_ROTATION_SYMBOLS, ETF_V25_SYMBOLS, fetch_a_share_universe
from .tools.fundamentals import fetch_fundamental_reports
from .tools.dividends import fetch_dividend_events


_running_tasks: set[asyncio.Task[Any]] = set()


def front_adjustment_changed(
    stored_snapshot: dict[str, Any] | None,
    fresh_snapshot: dict[str, Any],
    tolerance: float = 0.0005,
) -> tuple[bool, float | None]:
    """根据重叠交易日的一致缩放，判断前复权因子是否发生变化。"""
    if not stored_snapshot:
        return False, None

    def closes_by_date(snapshot: dict[str, Any]) -> dict[str, float]:
        closes: dict[str, float] = {}
        for point in snapshot.get("points") or []:
            try:
                close = float(point["close"])
                trade_date = datetime.fromtimestamp(int(point["timestamp"]), timezone.utc).date().isoformat()
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(close) and close > 0:
                closes[trade_date] = close
        return closes

    stored_closes = closes_by_date(stored_snapshot)
    fresh_closes = closes_by_date(fresh_snapshot)
    ratios = [
        fresh_closes[trade_date] / old_close
        for trade_date, old_close in stored_closes.items()
        if trade_date in fresh_closes
    ]
    if len(ratios) < 2 or not all(math.isfinite(ratio) and ratio > 0 for ratio in ratios):
        return False, None

    median_ratio = statistics.median(ratios)
    if abs(median_ratio - 1.0) <= tolerance:
        return False, median_ratio

    # 行情接口价格通常保留有限小数，允许低价股因四舍五入产生少量比例偏差。
    consistency_tolerance = max(tolerance * 4, 0.003)
    consistent = sum(
        abs(ratio / median_ratio - 1.0) <= consistency_tolerance
        for ratio in ratios
    )
    required = max(2, math.ceil(len(ratios) * 0.8))
    return consistent >= required, median_ratio


def launch(coroutine: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coroutine)
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)


async def sync_market_data(job_id: str) -> None:
    job = get_job(job_id)
    if job is None:
        return
    payload = job["payload"]
    try:
        update_job(job_id, status="running", message="正在获取 A 股股票列表")
        requested = payload.get("symbols")
        saved_instruments = await asyncio.to_thread(list_instruments, "A股")
        instrument_names = {item["symbol"]: str(item.get("name") or "") for item in saved_instruments}
        if requested:
            symbols = [normalize_symbol(symbol, "A股") for symbol in requested]
            universe_source = "用户指定股票"
            universe_warning = None
        else:
            existing = saved_instruments
            try:
                universe = await asyncio.to_thread(fetch_a_share_universe)
                if existing and len(universe) < len(existing) * 0.8:
                    raise RuntimeError(f"远程列表仅返回 {len(universe)} 只，少于本地股票池 {len(existing)} 只的 80%")
                await asyncio.to_thread(upsert_instruments, universe)
                symbols = [item["symbol"] for item in universe]
                # ETF 不在 stock_info_a_code_name() 返回的股票列表中；加入同步任务，
                # save_snapshot 会以 universe_member=0 保存，不影响普通股票全市场回测。
                rotation_symbols = (*ETF_ROTATION_SYMBOLS, *ETF_SIMPLE_ROTATION_SYMBOLS, *ETF_V25_SYMBOLS)
                index_symbols = ("000985.SS",)
                symbols.extend(symbol for symbol in (*rotation_symbols, *index_symbols) if symbol not in symbols)
                instrument_names = {item["symbol"]: str(item.get("name") or "") for item in universe}
                instrument_names.update({symbol: symbol.split(".")[0] for symbol in (*rotation_symbols, *index_symbols)})
                universe_source = "AKShare沪深京股票列表"
                universe_warning = None
            except Exception as exc:
                if not existing:
                    raise
                symbols = [item["symbol"] for item in existing]
                rotation_symbols = (*ETF_ROTATION_SYMBOLS, *ETF_SIMPLE_ROTATION_SYMBOLS, *ETF_V25_SYMBOLS)
                index_symbols = ("000985.SS",)
                symbols.extend(symbol for symbol in (*rotation_symbols, *index_symbols) if symbol not in symbols)
                instrument_names.update({symbol: symbol.split(".")[0] for symbol in (*rotation_symbols, *index_symbols)})
                universe_source = "本地已保存股票池"
                universe_warning = f"AKShare股票列表暂时不可用，已继续使用本地 {len(symbols)} 只股票：{exc}"
        total = len(symbols)
        mode = payload.get("mode", "incremental")
        price_scope = payload.get("price_scope", "all")
        front_only = price_scope == "front_only"
        mode_label = "增量" if mode == "incremental" else "全量"
        scope_label = "仅前复权" if front_only else "完整复权"
        benchmark_start = (date.fromisoformat(payload["start_date"]) - timedelta(days=400)).isoformat()
        benchmark_snapshot = await asyncio.to_thread(
            fetch_stock_history, BENCHMARK_SYMBOL, "A股", benchmark_start, payload["end_date"],
        )
        await asyncio.to_thread(save_snapshot, benchmark_snapshot, "A股")
        if mode == "incremental":
            # 使用与本次同步相同的价格口径探测安全截止日，避免接口更新时间相差一天时
            # 让全市场股票逐只失败。该探针只读，不直接写库。
            probe_start = (date.fromisoformat(payload["end_date"]) - timedelta(days=45)).isoformat()
            probe_fetcher = fetch_stock_history if front_only else fetch_stock_history_with_adjustments
            cutoff_probe = await asyncio.to_thread(
                probe_fetcher, "000001.SZ", "A股", probe_start, payload["end_date"],
            )
            probe_trade_date = datetime.fromtimestamp(
                int(cutoff_probe["points"][-1]["timestamp"]), timezone.utc,
            ).date().isoformat()
            target_trade_date = min(probe_trade_date, payload["end_date"])
        else:
            benchmark_trade_date = datetime.fromtimestamp(
                int(benchmark_snapshot["points"][-1]["timestamp"]), timezone.utc,
            ).date().isoformat()
            target_trade_date = min(benchmark_trade_date, payload["end_date"])
        prepare_message = f"准备{mode_label}同步 {total} 只股票（{scope_label}；{universe_source}）"
        if universe_warning:
            prepare_message += "；远程列表获取失败但任务已降级继续"
        prepare_message += f"；完整行情截止 {target_trade_date}"
        update_job(job_id, total=total, message=prepare_message)
        requested_concurrency = int(payload.get("concurrency", 2))
        # 完整复权单票需要通过 AKShare 连续读取三种口径；仅前复权只读取一套。
        # 使用页面选择的 1～4 并发；批量失败时仍会自动降为串行。
        active_concurrency = max(1, min(requested_concurrency, 4))
        succeeded = 0
        cached = 0
        inactive_skipped = 0
        updated = 0
        pending_adjustment = 0
        final_failures: dict[str, str] = {}
        adjustment_repairs: list[dict[str, Any]] = []

        async def sync_one(symbol: str) -> tuple[str, str | None, float | None, str, int]:
            try:
                sync_start = payload["start_date"]
                coverage = await asyncio.to_thread(get_bar_coverage, symbol, "A股")
                stored_overlap: dict[str, Any] | None = None
                if mode == "full":
                    if symbol in ETF_SYMBOLS:
                        # ETF 可能存在未被上游复权接口平滑的份额拆分，完整同步时强制重拉并修复。
                        pass
                    elif front_only:
                        if coverage and coverage[0] <= payload["start_date"] and coverage[1] >= target_trade_date:
                            return symbol, None, None, "current", 0
                    elif await asyncio.to_thread(
                        adjustment_history_complete, symbol, target_trade_date, "A股",
                    ):
                        return symbol, None, None, "current", 0
                if mode == "incremental":
                    name = instrument_names.get(symbol, "").upper()
                    looks_inactive = "退" in name or name.startswith("PT")
                    if looks_inactive and (coverage is None or coverage[1] < target_trade_date):
                        return symbol, None, None, "inactive", 0
                    if coverage:
                        factors_complete = front_only or await asyncio.to_thread(
                            adjustment_history_complete, symbol, coverage[1], "A股",
                        )
                        if coverage[1] >= target_trade_date and factors_complete:
                            return symbol, None, None, "current", 0
                        if factors_complete:
                            # 用重叠区间识别除权后历史前复权价格的整体变化。
                            buffered = (date.fromisoformat(coverage[1]) - timedelta(days=20)).isoformat()
                            sync_start = max(sync_start, buffered)
                            stored_overlap = await asyncio.to_thread(
                                load_snapshot, symbol, sync_start, coverage[1], "A股",
                            )
                        else:
                            # 旧库中只保存过前复权数据时，一次性补齐整段不复权/后复权字段；
                            # 否则只更新末尾永远无法修好历史缺口。
                            sync_start = coverage[0]
                else:
                    # 指标预热区间只用于递推 KDJ/MACD 等状态型指标，不改变用户选择的回测开始日。
                    sync_start = (date.fromisoformat(sync_start) - timedelta(days=400)).isoformat()
                history_fetcher = fetch_stock_history if front_only else fetch_stock_history_with_adjustments
                snapshot = await asyncio.to_thread(
                    history_fetcher, symbol, "A股", sync_start, target_trade_date,
                )
                changed, ratio = front_adjustment_changed(stored_overlap, snapshot)
                if changed and coverage:
                    # 在完整结果成功返回前不写库，避免全量修复失败破坏已有可用历史。
                    snapshot = await asyncio.to_thread(
                        history_fetcher, symbol, "A股", coverage[0], target_trade_date,
                    )
                await asyncio.to_thread(save_snapshot, snapshot, "A股")
                pending_count = len(snapshot.get("pending_adjustment_dates") or [])
                return symbol, None, ratio if changed else None, "updated", pending_count
            except Exception as exc:
                return symbol, str(exc), None, "failed", 0

        async def run_batch(
            batch: list[str], concurrency: int, progress_base: int, report_progress: bool = True,
        ) -> list[tuple[str, str | None, float | None, str, int]]:
            semaphore = asyncio.Semaphore(concurrency)

            async def guarded(symbol: str) -> tuple[str, str | None, float | None, str, int]:
                async with semaphore:
                    return await sync_one(symbol)

            tasks = [asyncio.create_task(guarded(symbol)) for symbol in batch]
            results: list[tuple[str, str | None, float | None, str, int]] = []
            loop = asyncio.get_running_loop()
            last_reported_at = loop.time()
            for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
                results.append(await task)
                now = loop.time()
                if report_progress and (completed == len(tasks) or now - last_reported_at >= 0.5):
                    update_job(
                        job_id, current=progress_base + completed,
                        message=(
                            f"{mode_label}同步 {progress_base + completed}/{total}"
                            f"（{scope_label}，并发 {concurrency}）"
                        ),
                    )
                    last_reported_at = now
            return results

        processed = 0
        retried_succeeded = 0
        batch_size = 50
        for offset in range(0, total, batch_size):
            batch = symbols[offset:offset + batch_size]
            results = await run_batch(batch, active_concurrency, processed)
            network_attempted = sum(item[3] in {"updated", "failed"} for item in results)
            network_succeeded = sum(item[3] == "updated" for item in results)
            batch_failures = [item[0] for item in results if item[1] is not None]

            if network_attempted >= 20 and network_succeeded / network_attempted < 0.2:
                update_job(
                    job_id, current=processed,
                    message=(
                        f"检测到 AKShare 上游行情异常：本批 {network_attempted} 只成功 {network_succeeded} 只；"
                        f"正在对 {len(batch_failures)} 只失败股票串行退避重试"
                    ),
                )
                await asyncio.sleep(1.0)
                retry_results = await run_batch(batch_failures, 1, processed, report_progress=False)
                retry_by_symbol = {item[0]: item for item in retry_results}
                results = [retry_by_symbol.get(item[0], item) if item[1] is not None else item for item in results]
                retry_success = sum(item[1] is None for item in retry_results)
                retried_succeeded += retry_success
                effective_succeeded = sum(item[3] == "updated" for item in results)
                if effective_succeeded / network_attempted < 0.2:
                    sample = next((item[1] for item in results if item[1]), "未知网络错误")
                    raise RuntimeError(
                        f"AKShare 行情当前不可稳定访问：本批 {network_attempted} 只首次成功 {network_succeeded} 只，"
                        f"串行退避重试后共成功 {effective_succeeded} 只，已触发失败率熔断。"
                        f"已有行情未被覆盖；请稍后从断点重试。失败样例：{sample}"
                    )
                active_concurrency = 1

            for symbol, error, repair_ratio, sync_status, pending_count in results:
                if error:
                    final_failures[symbol] = error
                else:
                    final_failures.pop(symbol, None)
                    succeeded += 1
                    cached += int(sync_status == "current")
                    inactive_skipped += int(sync_status == "inactive")
                    updated += int(sync_status == "updated")
                    pending_adjustment += pending_count
                    if repair_ratio is not None:
                        adjustment_repairs.append({"symbol": symbol, "price_ratio": round(repair_ratio, 8)})
            processed += len(batch)
            repair_note = f"，复权修复 {len(adjustment_repairs)}" if adjustment_repairs else ""
            cache_note = f"，断点跳过 {cached}" if cached else ""
            inactive_note = f"，退市跳过 {inactive_skipped}" if inactive_skipped else ""
            pending_note = f"，末日待补齐 {pending_adjustment}" if pending_adjustment else ""
            retry_note = f"，重试恢复 {retried_succeeded}" if retried_succeeded else ""
            update_job(
                job_id, current=processed,
                message=(
                    f"{mode_label}同步 {processed}/{total}，更新 {updated}{cache_note}{inactive_note}"
                    f"{repair_note}{pending_note}{retry_note}，失败 {len(final_failures)}"
                ),
            )
        failures = [{"symbol": symbol, "message": message} for symbol, message in list(final_failures.items())[:100]]
        completed_message = f"行情{mode_label}同步完成（{scope_label}；股票池：{universe_source}）"
        if universe_warning:
            completed_message += "；远程列表不可用，本次沿用本地股票池"
        update_job(
            job_id,
            status="completed",
            current=total,
            message=completed_message,
            result={
                "mode": mode, "price_scope": price_scope, "requested": total, "succeeded": succeeded, "failed": total - succeeded,
                "updated": updated,
                "cached": cached,
                "inactive_skipped": inactive_skipped,
                "pending_adjustment_dates": pending_adjustment,
                "target_trade_date": target_trade_date,
                "requested_concurrency": requested_concurrency,
                "effective_concurrency": active_concurrency,
                "retried_succeeded": retried_succeeded,
                "failure_samples": failures, "universe_source": universe_source, "universe_warning": universe_warning,
                "adjustment_repairs": len(adjustment_repairs),
                "adjustment_repair_samples": adjustment_repairs[:100],
            },
        )
    except Exception as exc:
        update_job(job_id, status="failed", message="行情同步失败", error=str(exc))


async def sync_fundamental_data(job_id: str) -> None:
    job = get_job(job_id)
    if job is None:
        return
    payload = job["payload"]
    try:
        requested = payload.get("symbols")
        if requested:
            symbols = [normalize_symbol(symbol, "A股") for symbol in requested]
        else:
            symbols = [item["symbol"] for item in list_instruments("A股")]
        if not symbols:
            raise RuntimeError("本地数据库还没有A股股票池，请先同步市场行情")
        mode = payload.get("mode", "incremental")
        total = len(symbols)
        update_job(job_id, status="running", total=total, message=f"开始{('增量' if mode == 'incremental' else '全量')}同步 {total} 只股票的基本面数据")
        semaphore = asyncio.Semaphore(int(payload.get("concurrency", 2)))
        succeeded = skipped = 0
        failures: list[dict[str, str]] = []

        async def sync_one(symbol: str) -> tuple[str, str, str | None]:
            async with semaphore:
                if mode == "incremental" and await asyncio.to_thread(fundamental_cache_is_fresh, symbol, 12):
                    return symbol, "skipped", None
                try:
                    reports = await asyncio.to_thread(fetch_fundamental_reports, symbol)
                    await asyncio.to_thread(save_fundamental_reports, symbol, reports)
                    return symbol, "saved", None
                except Exception as exc:
                    return symbol, "failed", str(exc)

        tasks = [asyncio.create_task(sync_one(symbol)) for symbol in symbols]
        for completed, future in enumerate(asyncio.as_completed(tasks), 1):
            symbol, status, error = await future
            if status == "saved":
                succeeded += 1
            elif status == "skipped":
                skipped += 1
            elif len(failures) < 100:
                failures.append({"symbol": symbol, "message": error or "未知错误"})
            if completed % 10 == 0 or completed == total:
                update_job(job_id, current=completed, message=f"基本面同步 {completed}/{total}，更新 {succeeded}，缓存命中 {skipped}")
        coverage = await asyncio.to_thread(fundamental_coverage, "A股")
        update_job(
            job_id, status="completed", current=total, message="基本面数据同步完成",
            result={
                "mode": mode, "requested": total, "succeeded": succeeded, "skipped": skipped,
                "failed": total - succeeded - skipped, "failure_samples": failures, "coverage": coverage,
            },
        )
    except Exception as exc:
        update_job(job_id, status="failed", message="基本面同步失败", error=str(exc))


async def sync_dividend_data(job_id: str) -> None:
    job = get_job(job_id)
    if job is None:
        return
    payload = job["payload"]
    try:
        requested = payload.get("symbols")
        symbols = ([normalize_symbol(symbol, "A股") for symbol in requested]
                   if requested else [item["symbol"] for item in list_instruments("A股")])
        total = len(symbols)
        update_job(job_id, status="running", total=total, message=f"开始同步 {total} 只股票的历史现金分红")
        semaphore = asyncio.Semaphore(int(payload.get("concurrency", 2)))
        succeeded = empty = failed = 0
        failures: list[dict[str, str]] = []

        async def sync_one(symbol: str) -> tuple[str, str, str | None]:
            async with semaphore:
                try:
                    events = await asyncio.to_thread(fetch_dividend_events, symbol)
                    if events:
                        await asyncio.to_thread(save_dividend_events, symbol, events)
                        return symbol, "saved", None
                    return symbol, "empty", None
                except Exception as exc:
                    return symbol, "failed", str(exc)

        tasks = [asyncio.create_task(sync_one(symbol)) for symbol in symbols]
        for completed, task in enumerate(asyncio.as_completed(tasks), 1):
            symbol, status, error = await task
            succeeded += int(status == "saved")
            empty += int(status == "empty")
            failed += int(status == "failed")
            if error and len(failures) < 100:
                failures.append({"symbol": symbol, "message": error})
            if completed % 10 == 0 or completed == total:
                update_job(job_id, current=completed, message=f"分红同步 {completed}/{total}，有分红 {succeeded}，无记录 {empty}，失败 {failed}")
        update_job(job_id, status="completed", current=total, message="历史分红同步完成",
                   result={"requested": total, "succeeded": succeeded, "empty": empty,
                           "failed": failed, "failure_samples": failures,
                           "coverage": await asyncio.to_thread(dividend_coverage, "A股")})
    except Exception as exc:
        update_job(job_id, status="failed", message="历史分红同步失败", error=str(exc))


async def run_full_market_backtest(job_id: str) -> None:
    job = get_job(job_id)
    if job is None:
        return
    payload = job["payload"]
    run_id: str | None = None
    try:
        adjustment_mode = payload.get("adjustment_mode", "前复权")
        universe_total: int | None = None
        excluded_adjustment = 0
        instruments: list[dict[str, Any]]
        if adjustment_mode != "前复权":
            price_coverage = price_mode_coverage(
                adjustment_mode, "A股", payload["start_date"], payload["end_date"],
            )
            universe_total = int(price_coverage["total"])
            coverage_rate = price_coverage["covered"] / price_coverage["total"] if price_coverage["total"] else 0
            if coverage_rate < 0.8:
                raise RuntimeError(
                    f"{adjustment_mode}行情覆盖不足：{price_coverage['covered']}/{price_coverage['total']}（{coverage_rate:.1%}）。"
                    "请先完成一次全量行情同步；同步期间不要重复创建任务。"
                )
            excluded_adjustment = int(price_coverage["missing"])
            covered_symbols = set(price_coverage["covered_symbols"])
            instruments = [item for item in list_instruments("A股") if item["symbol"] in covered_symbols]
        else:
            instruments = list_backtest_instruments(
                payload["start_date"], payload["end_date"], "A股", adjustment_mode,
            )
        total = len(instruments)
        universe_total = universe_total if universe_total is not None else total
        if not total:
            raise RuntimeError("所选日期范围内没有复权数据完整的 A 股行情，请先执行全市场行情同步")
        if payload.get("fundamental_score_enabled"):
            coverage = fundamental_coverage("A股")
            coverage_rate = coverage["covered"] / coverage["total"] if coverage["total"] else 0
            if coverage_rate < 0.8:
                raise RuntimeError(
                    f"基本面缓存覆盖不足：{coverage['covered']}/{coverage['total']}（{coverage_rate:.1%}）。"
                    "请先点击“同步基本面数据”，完成后再启动全市场回测。"
                )
        benchmark_snapshot = load_snapshot(BENCHMARK_SYMBOL, payload["start_date"], payload["end_date"], "A股")
        if benchmark_snapshot is None:
            raise RuntimeError("本地数据库缺少沪深300基准行情，请先重新同步市场数据")
        market_signal_threshold = int(payload.get("market_signal_threshold") or 0)
        market_signal_rate_threshold = float(payload.get("market_signal_rate_threshold") or 0)
        if market_signal_rate_threshold > 0:
            market_signal_threshold = max(1, math.ceil(total * market_signal_rate_threshold))
        breadth_strategy = "RSI反转+止盈止损"
        breadth_strategies = {"RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控"}
        breadth_period = 17 if "RSI17反转+止盈止损" in payload["strategies"] and "RSI反转+止盈止损" not in payload["strategies"] else 14
        market_signal_counts: dict[int, int] = {}
        qualified_signal_days = 0
        if market_signal_threshold > 0 and any(item in payload["strategies"] for item in breadth_strategies):
            counts: Counter[int] = Counter()
            update_job(
                job_id, status="running", total=total * 2,
                message=f"第一阶段：逐日独立扫描全市场 {breadth_strategy} 原始信号",
            )
            for index, instrument in enumerate(instruments, 1):
                try:
                    snapshot = load_snapshot(
                        instrument["symbol"], payload["start_date"], payload["end_date"], "A股",
                        adjustment_mode=adjustment_mode,
                    )
                    if snapshot is not None:
                        signal_timestamps = await asyncio.to_thread(
                            rsi_risk_raw_signal_timestamps, snapshot, breadth_period,
                        )
                        counts.update(signal_timestamps)
                except Exception:
                    pass
                if index % 10 == 0 or index == total:
                    update_job(
                        job_id, current=index, total=total * 2,
                        message=f"第一阶段：信号宽度扫描 {index}/{total}，已统计 {len(counts)} 个信号日",
                    )
                await asyncio.sleep(0)
            market_signal_counts = dict(counts)
            qualified_signal_days = sum(value >= market_signal_threshold for value in market_signal_counts.values())
        run_id = create_backtest_run(
            payload["name"], "market", payload["start_date"], payload["end_date"], payload["strategies"],
            {
                "initial_capital": payload["initial_capital"], "fee_rate": payload["fee_rate"], "job_id": job_id,
                "slippage_bps": payload.get("slippage_bps", 5),
                "fundamental_score_enabled": bool(payload.get("fundamental_score_enabled")),
                "fundamental_score_threshold": payload.get("fundamental_score_threshold", 60),
                "adjustment_mode": payload.get("adjustment_mode", "前复权"),
                "market_signal_threshold": market_signal_threshold,
                "market_signal_count_mode": "raw_daily_independent",
                "volume_ratio_threshold": float(payload.get("volume_ratio_threshold") or 0),
            },
        )
        excluded_note = f"；复权缺口排除 {excluded_adjustment} 只" if excluded_adjustment else ""
        progress_total = total * 2 if market_signal_counts else total
        update_job(
            job_id, status="running", total=progress_total,
            message=(
                f"第二阶段：{qualified_signal_days} 个交易日达到 {market_signal_threshold} 个信号，开始正式回测"
                if market_signal_counts else f"开始回测 {total} 只股票{excluded_note}"
            ),
        )
        summary_rows: list[dict[str, Any]] = []
        succeeded = 0
        failures = 0
        failure_samples: list[dict[str, str]] = []
        for index, instrument in enumerate(instruments, 1):
            try:
                snapshot = load_snapshot(
                    instrument["symbol"], payload["start_date"], payload["end_date"], "A股",
                    adjustment_mode=adjustment_mode,
                )
            except Exception as exc:
                snapshot = None
                if len(failure_samples) < 100:
                    failure_samples.append({"symbol": instrument["symbol"], "message": str(exc)})
            if snapshot is None:
                failures += 1
            else:
                symbol_succeeded = False
                fundamental_reports = None
                fundamental_threshold = None
                if payload.get("fundamental_score_enabled"):
                    fundamental_reports = load_fundamental_reports(instrument["symbol"], payload["end_date"])
                    fundamental_threshold = float(payload.get("fundamental_score_threshold", 60))
                for strategy in payload["strategies"]:
                    try:
                        if payload.get("fundamental_score_enabled") and not fundamental_reports:
                            raise RuntimeError("基本面数据不可用")
                        result = await asyncio.to_thread(
                            run_backtest, snapshot, strategy, payload["initial_capital"], payload["fee_rate"], benchmark_snapshot,
                            fundamental_reports, fundamental_threshold,
                            market_signal_counts if strategy in breadth_strategies else None,
                            market_signal_threshold if strategy in breadth_strategies else 0,
                            float(payload.get("slippage_bps", 5)),
                            float(payload.get("volume_ratio_threshold") or 0),
                            float(payload.get("benchmark_5d_drop_threshold") or 0),
                        )
                        result["excess_return_pct"] = round(result["total_return_pct"] - result["benchmark_return_pct"], 2)
                        await asyncio.to_thread(save_backtest_result, run_id, result)
                        summary_rows.append(result)
                        symbol_succeeded = True
                    except Exception as exc:
                        if len(failure_samples) < 100:
                            failure_samples.append({
                                "symbol": instrument["symbol"], "strategy": strategy, "message": str(exc),
                            })
                        continue
                succeeded += int(symbol_succeeded)
                failures += int(not symbol_succeeded)
            if index % 10 == 0 or index == total:
                update_job(
                    job_id,
                    current=index + (total if market_signal_counts else 0), total=progress_total,
                    message=f"已正式回测 {index}/{total}，有效 {succeeded}，失败 {failures}",
                )
            await asyncio.sleep(0)
        success_rate = succeeded / total if total else 0
        if success_rate < 0.8:
            sample = failure_samples[0]["message"] if failure_samples else "未知数据错误"
            raise RuntimeError(
                f"全市场回测有效覆盖不足：成功 {succeeded}/{total}（{success_rate:.1%}），"
                f"已拒绝生成不完整结果。请先完成全量行情同步；失败样例：{sample}"
            )
        summaries = summarize_backtest_results(summary_rows)
        summary = {
            "symbols": total, "succeeded": succeeded, "failed": failures, "strategies": summaries,
            "adjustment_mode": payload.get("adjustment_mode", "前复权"),
            "failure_samples": failure_samples,
            "universe_symbols": universe_total,
            "excluded_adjustment": excluded_adjustment,
            "market_signal_threshold": market_signal_threshold,
            "qualified_signal_days": qualified_signal_days,
            "market_signal_count_mode": "raw_daily_independent" if market_signal_threshold > 0 else None,
            "volume_ratio_threshold": float(payload.get("volume_ratio_threshold") or 0),
        }
        complete_backtest_run(run_id, summary)
        update_job(job_id, status="completed", current=progress_total, message="全市场回测完成", result={"run_id": run_id, **summary})
    except Exception as exc:
        if run_id:
            complete_backtest_run(run_id, None, str(exc))
        update_job(job_id, status="failed", message="全市场回测失败", error=str(exc))


async def run_capital_pool_backtest(job_id: str) -> None:
    job = get_job(job_id)
    if job is None:
        return
    payload = job["payload"]
    run_id: str | None = None
    try:
        adjustment_mode = payload.get("adjustment_mode") or "动态前复权"
        update_job(job_id, status="running", message=f"正在检查{adjustment_mode}完整性")
        coverage = await asyncio.to_thread(
            price_mode_coverage, adjustment_mode, "A股", payload["start_date"], payload["end_date"],
        )
        coverage_rate = coverage["covered"] / coverage["total"] if coverage["total"] else 0
        rotation_strategy = payload["strategy"] in {"自适应趋势轮动", "纯A股ETF-V25", "纯A股ETF-14基线"}
        if coverage_rate < 0.8 and not rotation_strategy:
            raise RuntimeError(
                f"{adjustment_mode}覆盖不足：{coverage['covered']}/{coverage['total']}（{coverage_rate:.1%}），"
                "请先完成全量行情同步"
            )
        covered_symbols = set(coverage["covered_symbols"])
        if rotation_strategy:
            if payload["strategy"] == "纯A股ETF-V25":
                from .tools.market_universe import ETF_V25_SYMBOLS
                rotation_codes = set(ETF_V25_SYMBOLS)
            elif payload["strategy"] == "纯A股ETF-14基线":
                from .tools.market_universe import ETF_SIMPLE_ROTATION_SYMBOLS
                rotation_codes = set(ETF_SIMPLE_ROTATION_SYMBOLS)
            else:
                rotation_codes = set(ETF_ROTATION_SYMBOLS)
            instruments = [item for item in await asyncio.to_thread(list_instruments, "A股", True, False) if item["symbol"] in rotation_codes]
        else:
            instruments = [item for item in await asyncio.to_thread(list_instruments, "A股") if item["symbol"] in covered_symbols]
        total = len(instruments)
        fundamental_status: dict[str, Any] | None = None
        dividend_strategy = payload["strategy"] == "红利质量动量"
        multifactor_strategy = payload["strategy"] == "多因子月度轮动"
        factor_ranking_enabled = payload.get("candidate_ranking") == "multifactor_score"
        factor_weights = {
            key: float(value) for key, value in (payload.get("factor_weights") or {}).items()
            if float(value) > 0
        }
        factor_needs_fundamentals = bool({"fundamental_score", "roe", "growth"}.intersection(factor_weights))
        if dividend_strategy:
            dividend_status = await asyncio.to_thread(dividend_coverage, "A股")
            if dividend_status["covered"] == 0:
                raise RuntimeError("本地没有历史现金分红缓存，请先点击“同步全市场历史分红”")
        if payload.get("fundamental_score_enabled") or dividend_strategy or ((multifactor_strategy or factor_ranking_enabled) and factor_needs_fundamentals):
            fundamental_status = await asyncio.to_thread(fundamental_coverage, "A股")
            fundamental_rate = (
                fundamental_status["covered"] / fundamental_status["total"]
                if fundamental_status["total"] else 0
            )
            if fundamental_rate < 0.8:
                raise RuntimeError(
                    f"基本面缓存覆盖不足：{fundamental_status['covered']}/{fundamental_status['total']}"
                    f"（{fundamental_rate:.1%}）。请先完成全量历史财报同步"
                )
        benchmark_snapshot = await asyncio.to_thread(
            load_snapshot, BENCHMARK_SYMBOL, payload["start_date"], payload["end_date"], "A股",
        )
        if benchmark_snapshot is None:
            raise RuntimeError("本地缺少同期沪深300行情")
        regime_snapshot = benchmark_snapshot
        if payload["strategy"] == "纯A股ETF-V25":
            regime_snapshot = await asyncio.to_thread(
                load_snapshot, "000985.SS", payload["start_date"], payload["end_date"], "A股", 300, adjustment_mode,
            )
            if regime_snapshot is None:
                raise RuntimeError("本地缺少中证全指000985行情，请先同步指数数据")
        market_signal_threshold = int(payload.get("market_signal_threshold") or 0)
        market_signal_rate_threshold = float(payload.get("market_signal_rate_threshold") or 0)
        if market_signal_rate_threshold > 0:
            market_signal_threshold = max(1, math.ceil(total * market_signal_rate_threshold))
        breadth_enabled = (
            payload["strategy"] in {"RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控"} and market_signal_threshold > 0
        )
        market_signal_counts: dict[int, int] = {}
        qualified_signal_days = 0
        if breadth_enabled:
            counts: Counter[int] = Counter()
            update_job(
                job_id, total=total * 2,
                message="第一阶段：逐日独立统计全市场 RSI 原始开仓信号",
            )
            for index, instrument in enumerate(instruments, 1):
                try:
                    snapshot = await asyncio.to_thread(
                        load_snapshot, instrument["symbol"], payload["start_date"], payload["end_date"],
                        "A股", 250, adjustment_mode,
                    )
                    if snapshot is not None:
                        period = 17 if payload["strategy"] == "RSI17反转+止盈止损" else 14
                        counts.update(await asyncio.to_thread(rsi_risk_raw_signal_timestamps, snapshot, period))
                except Exception:
                    pass
                if index % 10 == 0 or index == total:
                    update_job(
                        job_id, current=index, total=total * 2,
                        message=f"第一阶段：信号宽度扫描 {index}/{total}，已统计 {len(counts)} 个信号日",
                    )
                await asyncio.sleep(0)
            market_signal_counts = dict(counts)
            qualified_signal_days = sum(
                count >= market_signal_threshold for count in market_signal_counts.values()
            )
        candidates: list[dict[str, Any]] = []
        rotation_snapshots: dict[str, dict[str, Any]] = {}
        data_failures = 0
        progress_total = total * 2 if breadth_enabled else total
        update_job(
            job_id, total=progress_total,
            message=(
                f"第二阶段：{qualified_signal_days} 个交易日达到 {market_signal_threshold} 个信号，开始资金池回测"
                if breadth_enabled else f"正在扫描 {total} 只股票的{payload['strategy']}信号"
            ),
        )
        for index, instrument in enumerate(instruments, 1):
            try:
                snapshot = await asyncio.to_thread(
                    load_snapshot, instrument["symbol"], payload["start_date"], payload["end_date"],
                    "A股", 250, adjustment_mode,
                )
                if snapshot is None:
                    data_failures += 1
                else:
                    if payload["strategy"] == "纯A股ETF-V25":
                        rotation_snapshots[instrument["symbol"]] = snapshot
                    fundamental_reports = None
                    fundamental_threshold = None
                    if payload.get("fundamental_score_enabled") or dividend_strategy or ((multifactor_strategy or factor_ranking_enabled) and factor_needs_fundamentals):
                        fundamental_reports = await asyncio.to_thread(
                            load_fundamental_reports, instrument["symbol"], payload["end_date"],
                        )
                        if not fundamental_reports:
                            raise RuntimeError("基本面数据不可用")
                        fundamental_threshold = float(payload["fundamental_score_threshold"]) if payload.get("fundamental_score_enabled") else None
                    if dividend_strategy:
                        dividend_events = await asyncio.to_thread(
                            load_dividend_events, instrument["symbol"], payload["end_date"],
                        )
                        candidates.extend(build_dividend_quality_candidates(
                            snapshot, fundamental_reports or [], dividend_events,
                        ))
                    elif rotation_strategy:
                        if payload["strategy"] == "纯A股ETF-V25":
                            candidates.extend(build_pure_a_etf_candidates(snapshot, "v25", regime_snapshot))
                        elif payload["strategy"] == "纯A股ETF-14基线":
                            candidates.extend(build_pure_a_etf_candidates(snapshot, "simple14", benchmark_snapshot))
                        else:
                            candidates.extend(build_trend_rotation_candidates(snapshot, benchmark_snapshot))
                    elif multifactor_strategy:
                        candidates.extend(build_multifactor_candidates(
                            snapshot, fundamental_reports or [], list(factor_weights),
                            float(payload["max_opening_gap_pct"]), float(payload["minimum_turnover"]),
                            bool(payload["require_above_ma200"]), bool(payload["require_ma200_rising"]),
                        ))
                    else:
                        candidates.extend(build_pool_candidates(
                            snapshot, benchmark_snapshot, float(payload["fee_rate"]), payload["strategy"],
                            float(payload["max_opening_gap_pct"]), float(payload["minimum_turnover"]),
                            bool(payload["require_above_ma200"]), bool(payload["require_ma200_rising"]),
                            market_signal_counts=market_signal_counts if breadth_enabled else None,
                            market_signal_threshold=market_signal_threshold if breadth_enabled else 0,
                            fundamental_reports=fundamental_reports,
                            fundamental_score_threshold=fundamental_threshold,
                            volume_ratio_threshold=float(payload.get("volume_ratio_threshold") or 0),
                            benchmark_5d_drop_threshold=float(payload.get("benchmark_5d_drop_threshold") or 0),
                            factor_weights=factor_weights if factor_ranking_enabled else None,
                        ))
            except Exception:
                data_failures += 1
            if index % 10 == 0 or index == total:
                update_job(
                    job_id, current=index + (total if breadth_enabled else 0), total=progress_total,
                    message=f"资金池信号扫描 {index}/{total}，候选交易 {len(candidates)}，数据失败 {data_failures}",
                )
            await asyncio.sleep(0)

        if payload["strategy"] == "纯A股ETF-V25":
            update_job(job_id, message=f"已加载 {len(rotation_snapshots)} 只 ETF，正在运行 V25 目标权重引擎")
            result = await asyncio.to_thread(
                run_pure_a_v25_weight_backtest,
                rotation_snapshots, benchmark_snapshot, regime_snapshot,
                float(payload["initial_capital"]), float(payload["fee_rate"]), float(payload["base_slippage_bps"]),
            )
            result.update({
                "universe_symbols": int(coverage["total"]), "eligible_symbols": len(rotation_snapshots),
                "excluded_adjustment": int(coverage["missing"]), "data_failures": data_failures,
                "parameters": {key: payload.get(key) for key in ("fee_rate", "base_slippage_bps", "max_positions", "single_position_limit")},
            })
            run_id = create_backtest_run(
                payload["name"], "portfolio", payload["start_date"], payload["end_date"],
                ["资金池·纯A股ETF-V25（目标权重引擎）"], {**payload, "job_id": job_id, "engine": "v25_target_weight"},
            )
            await asyncio.to_thread(save_backtest_result, run_id, result)
            summary = {"strategies": summarize_backtest_results([result]), "portfolio": {key: value for key, value in result.items() if key not in {"curve", "trade_events", "regime_diagnostics"}}}
            await asyncio.to_thread(complete_backtest_run, run_id, summary)
            update_job(job_id, status="completed", current=progress_total, message="纯A股ETF-V25目标权重回测完成", result={"run_id": run_id, **result})
            return

        if dividend_strategy:
            attach_market_exposure(candidates, benchmark_snapshot)
        selected = select_capital_pool_trades(
            candidates,
            float(payload["initial_capital"]), float(payload["fee_rate"]),
            1.0 if (dividend_strategy or rotation_strategy) else float(payload["exposure_limit"]),
            (0.25 if payload["strategy"] in {"自适应趋势轮动", "纯A股ETF-V25"} else 1 / 3 if payload["strategy"] == "纯A股ETF-14基线" else (0.10 if dividend_strategy else float(payload["single_position_limit"]))),
            (8 if payload["strategy"] == "纯A股ETF-V25" else 5 if payload["strategy"] == "自适应趋势轮动" else 3 if payload["strategy"] == "纯A股ETF-14基线" else (10 if dividend_strategy else int(payload["max_positions"]))), float(payload["volume_participation_limit"]),
            float(payload["base_slippage_bps"]), float(payload["impact_bps"]),
            (
                "pure_a_v25" if payload["strategy"] == "纯A股ETF-V25" else "pure_a_simple14" if payload["strategy"] == "纯A股ETF-14基线" else "trend_rotation" if rotation_strategy
                else "dividend_quality_momentum" if dividend_strategy
                else "multifactor" if multifactor_strategy
                else str(payload.get("candidate_ranking") or "rsi_rebound_score")
            ),
            factor_weights=factor_weights if (multifactor_strategy or factor_ranking_enabled) else None,
        )
        selected_symbols = sorted({item["symbol"] for item in selected})
        selected_snapshots: dict[str, dict[str, Any]] = {}
        update_job(job_id, message=f"已选择 {len(selected)} 笔交易，正在生成共享资金净值曲线")
        for symbol in selected_symbols:
            snapshot = await asyncio.to_thread(
                load_snapshot, symbol, payload["start_date"], payload["end_date"],
                "A股", 250, adjustment_mode,
            )
            if snapshot:
                selected_snapshots[symbol] = snapshot
        cash_proxy_snapshot = None
        if dividend_strategy:
            proxy_symbol = "511880.SS"
            cash_proxy_snapshot = await asyncio.to_thread(
                load_snapshot, proxy_symbol, payload["start_date"], payload["end_date"], "A股", 20, adjustment_mode,
            )
            if cash_proxy_snapshot is None:
                update_job(job_id, message="正在补充银华日利511880行情")
                proxy_start = (date.fromisoformat(payload["start_date"]) - timedelta(days=30)).isoformat()
                proxy = await asyncio.to_thread(
                    fetch_stock_history_with_adjustments, proxy_symbol, "A股", proxy_start, payload["end_date"],
                )
                await asyncio.to_thread(save_snapshot, proxy, "A股")
                cash_proxy_snapshot = await asyncio.to_thread(
                    load_snapshot, proxy_symbol, payload["start_date"], payload["end_date"], "A股", 20, adjustment_mode,
                )
            if cash_proxy_snapshot is None:
                raise RuntimeError("红利质量策略无法获得银华日利511880行情")
        result = build_capital_pool_result(
            selected, selected_snapshots, benchmark_snapshot,
            float(payload["initial_capital"]), float(payload["fee_rate"]), len(candidates),
            payload["strategy"], cash_proxy_snapshot,
        )
        result.update({
            "universe_symbols": int(coverage["total"]),
            "eligible_symbols": total,
            "excluded_adjustment": int(coverage["missing"]),
            "data_failures": data_failures,
            "parameters": {key: payload.get(key) for key in (
                "exposure_limit", "single_position_limit", "max_positions",
                "volume_participation_limit", "minimum_turnover", "max_opening_gap_pct",
                "market_signal_threshold", "require_above_ma200", "require_ma200_rising",
                "volume_ratio_threshold", "benchmark_5d_drop_threshold",
                "fundamental_score_enabled", "fundamental_score_threshold",
                "base_slippage_bps", "impact_bps",
                "candidate_ranking", "factor_weights",
            )},
            "fundamental_coverage": fundamental_status,
            "qualified_signal_days": qualified_signal_days,
            "market_signal_count_mode": "raw_daily_independent" if breadth_enabled else None,
        })
        run_id = create_backtest_run(
            payload["name"], "portfolio", payload["start_date"], payload["end_date"],
            [f"资金池·{payload['strategy']}"], {**payload, "job_id": job_id, "adjustment_mode": adjustment_mode},
        )
        await asyncio.to_thread(save_backtest_result, run_id, result)
        summary = {
            "strategies": summarize_backtest_results([result]),
            "portfolio": {key: value for key, value in result.items() if key not in {"curve", "trade_events"}},
        }
        await asyncio.to_thread(complete_backtest_run, run_id, summary)
        update_job(
            job_id, status="completed", current=progress_total,
            message=f"资金池{payload['strategy']}组合回测完成",
            result={"run_id": run_id, **result},
        )
    except Exception as exc:
        if run_id:
            complete_backtest_run(run_id, None, str(exc))
        update_job(job_id, status="failed", message="资金池回测失败", error=str(exc))


async def scan_market(job_id: str) -> None:
    job = get_job(job_id)
    if job is None:
        return
    payload = job["payload"]
    try:
        instruments = list_instruments("A股")
        total = len(instruments)
        if not total:
            raise RuntimeError("本地数据库还没有 A 股行情，请先执行全市场行情同步")
        update_job(job_id, status="running", total=total, message=f"开始扫描 {total} 只股票")
        buy_signals = 0
        sell_signals = 0
        failures = 0
        failure_samples: list[dict[str, str]] = []
        scan_date = latest_market_trade_date(payload["end_date"], "A股")
        if scan_date is None:
            raise RuntimeError("指定日期前没有可扫描的本地行情")
        benchmark_snapshot = None
        if "多周期趋势跟随" in payload["strategies"]:
            benchmark_snapshot = load_snapshot(BENCHMARK_SYMBOL, payload["start_date"], payload["end_date"], "A股")
            if benchmark_snapshot is None:
                raise RuntimeError("趋势扫描缺少沪深300行情，请先同步市场数据")
        for index, instrument in enumerate(instruments, 1):
            try:
                snapshot = load_snapshot(instrument["symbol"], payload["start_date"], payload["end_date"], "A股")
                if snapshot:
                    for strategy in payload["strategies"]:
                        signal = latest_strategy_signal(snapshot, strategy, benchmark_snapshot)
                        if signal and signal["side"] in {"B", "S"}:
                            signal_date = datetime.fromtimestamp(int(signal["timestamp"]), timezone.utc).date().isoformat()
                            if signal_date == scan_date:
                                saved_signal = {"signal_date": signal_date, "symbol": instrument["symbol"], "strategy": strategy,
                                                "side": signal["side"], "price": signal["price"], "reason": signal["reason"]}
                                save_scan_signal(job_id, signal_date, instrument["symbol"], strategy, signal["side"], signal["price"], signal["reason"])
                                sync_strategy_position(saved_signal, job_id)
                                if signal["side"] == "B":
                                    buy_signals += 1
                                else:
                                    sell_signals += 1
            except Exception as exc:
                failures += 1
                if len(failure_samples) < 100:
                    failure_samples.append({"symbol": instrument["symbol"], "message": str(exc)})
            if index % 25 == 0 or index == total:
                update_job(
                    job_id, current=index,
                    message=(
                        f"已扫描 {index}/{total}，建议买入 {buy_signals} 个，"
                        f"卖出提醒 {sell_signals} 个，失败 {failures}"
                    ),
                )
            await asyncio.sleep(0)
        update_job(
            job_id, status="completed", current=total, message="策略雷达扫描完成",
            result={"symbols": total, "signals": buy_signals, "buy_signals": buy_signals,
                    "sell_signals": sell_signals, "scan_date": scan_date,
                    "failed": failures, "failure_samples": failure_samples},
        )
    except Exception as exc:
        update_job(job_id, status="failed", message="策略雷达扫描失败", error=str(exc))


async def build_capital_pool_plan(job_id: str) -> None:
    """按最新交易日生成资金池下一交易日执行清单。"""
    job = get_job(job_id)
    if job is None:
        return
    payload = job["payload"]
    try:
        strategy = str(payload.get("strategy") or "RSI反转+止盈止损")
        instruments = list_instruments("A股")
        total = len(instruments)
        if not total:
            raise RuntimeError("本地数据库还没有 A 股行情，请先执行全市场行情同步")
        # 采用覆盖率达标的全市场可用日，避免少数股票先写入的局部日期形成错误计划。
        trade_date = (await asyncio.to_thread(market_data_status, "A股")).get("latest_trade_date")
        if not trade_date:
            raise RuntimeError("指定日期前没有可用的本地行情")
        update_job(job_id, status="running", total=total, message=f"正在扫描 {trade_date} 的资金池信号")
        threshold = int(payload.get("market_signal_threshold") or 0)
        breadth: Counter[int] = Counter()
        snapshots: dict[str, dict[str, Any]] = {}
        failures = 0
        for index, instrument in enumerate(instruments, 1):
            try:
                snapshot = await asyncio.to_thread(
                    load_snapshot, instrument["symbol"], payload["start_date"], trade_date,
                    "A股", 250, payload.get("adjustment_mode") or "动态前复权",
                )
                if snapshot is not None:
                    snapshots[instrument["symbol"]] = snapshot
                    if strategy in {"RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控"} and threshold > 0:
                        breadth.update(await asyncio.to_thread(rsi_risk_raw_signal_timestamps, snapshot))
            except Exception:
                failures += 1
            if index % 25 == 0 or index == total:
                update_job(job_id, current=index, message=f"已扫描 {index}/{total}，行情失败 {failures}")
            await asyncio.sleep(0)
        holdings = list_strategy_positions(strategy, "open")
        holding_symbols = {str(item["symbol"]) for item in holdings}
        buys: list[dict[str, Any]] = []
        sells: list[dict[str, Any]] = []
        for symbol, snapshot in snapshots.items():
            signal = latest_strategy_signal(snapshot, strategy)
            if not signal:
                continue
            signal_date = datetime.fromtimestamp(int(signal["timestamp"]), timezone.utc).date().isoformat()
            if signal_date != trade_date:
                continue
            side = signal.get("side")
            item = {
                "symbol": symbol, "name": next((str(x.get("name") or symbol) for x in instruments if x["symbol"] == symbol), symbol),
                "signal_date": signal_date, "signal_price": signal.get("price"),
                "reason": signal.get("reason") or "策略信号", "strategy": strategy,
            }
            if side == "S":
                if symbol in holding_symbols:
                    position = next(x for x in holdings if str(x["symbol"]) == symbol)
                    sells.append({**item, "entry_date": position.get("entry_date"), "entry_price": position.get("entry_price"), "action": "卖出"})
            elif side == "B" and symbol not in holding_symbols:
                item["action"] = "候选买入"
                item["signal_count"] = int(breadth.get(int(signal["timestamp"]), 0)) if threshold > 0 else None
                item["threshold_passed"] = not threshold or item["signal_count"] >= threshold
                if item["threshold_passed"]:
                    buys.append(item)
        # RSI 反转候选优先按 RSI 数值低、反弹幅度小排序；其他策略保持代码扫描顺序。
        import re
        def buy_score(item: dict[str, Any]) -> tuple[float, str]:
            match = re.search(r"RSI=([\d.]+)", str(item.get("reason") or ""))
            return (float(match.group(1)) if match else 999.0, str(item["symbol"]))
        if strategy in {"RSI反转+止盈止损", "RSI17反转+止盈止损", "RSI反转+暴跌强化风控"}:
            buys.sort(key=buy_score)
        capacity = max(0, int(payload.get("max_positions", 100)) - len(holding_symbols))
        selected_buys = buys[:capacity]
        update_job(job_id, status="completed", current=total, message=f"交易计划完成：卖出 {len(sells)} 只，买入候选 {len(selected_buys)} 只", result={
            "trade_date": trade_date, "next_trade_day_note": "清单对应下一交易日开盘执行；实际买入价以开盘及滑点为准",
            "strategy": strategy, "holdings": holdings, "sell_candidates": sells,
            "buy_candidates": selected_buys, "buy_signal_count": len(buys), "selected_buy_count": len(selected_buys),
            "capacity": capacity, "market_signal_threshold": threshold,
            "market_signal_count": int(breadth.get(int(datetime.fromisoformat(trade_date).replace(tzinfo=timezone.utc).timestamp()), 0)) if threshold > 0 else None,
            "scanned": total, "loaded": len(snapshots), "failures": failures,
        })
    except Exception as exc:
        update_job(job_id, status="failed", message="资金池交易计划失败", error=str(exc))
