from __future__ import annotations

import json
import math
import sqlite3
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .config import ROOT_DIR, get_settings
from .tools.market_data import normalize_symbol


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pack(value: Any) -> bytes:
    return zlib.compress(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), level=6)


def _unpack(value: str | bytes) -> Any:
    if isinstance(value, bytes):
        return json.loads(zlib.decompress(value).decode("utf-8"))
    return json.loads(value)


def database_path() -> Path:
    path = Path(get_settings().database_path).expanduser()
    return (path if path.is_absolute() else ROOT_DIR / path).resolve()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path, timeout=30)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("PRAGMA foreign_keys=ON")
    try:
        yield database
        database.commit()
    finally:
        database.close()


def init_database() -> None:
    with connection() as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS instruments (
                symbol TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                market TEXT NOT NULL,
                exchange TEXT,
                currency TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                universe_member INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_bars (
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL,
                adjustment TEXT NOT NULL DEFAULT '前复权',
                source TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, trade_date),
                FOREIGN KEY (symbol) REFERENCES instruments(symbol) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_daily_bars_date ON daily_bars(trade_date);
            CREATE TABLE IF NOT EXISTS fundamental_reports (
                symbol TEXT NOT NULL,
                report_date TEXT NOT NULL,
                effective_date TEXT NOT NULL,
                report_type TEXT,
                score REAL NOT NULL,
                roe REAL,
                revenue_growth REAL,
                profit_growth REAL,
                operating_cash REAL,
                net_profit REAL,
                debt_ratio REAL,
                components_json TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, report_date, effective_date),
                FOREIGN KEY (symbol) REFERENCES instruments(symbol) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_fundamental_effective ON fundamental_reports(symbol, effective_date);
            CREATE TABLE IF NOT EXISTS dividend_events (
                symbol TEXT NOT NULL,
                announcement_date TEXT NOT NULL,
                ex_date TEXT,
                payment_date TEXT,
                cash_per_10 REAL NOT NULL,
                raw_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(symbol, announcement_date, ex_date, cash_per_10),
                FOREIGN KEY(symbol) REFERENCES instruments(symbol) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_dividend_ex_date ON dividend_events(symbol, ex_date);
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                scope TEXT NOT NULL,
                status TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                strategies_json TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                summary_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                deleted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS backtest_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                curve_json TEXT NOT NULL,
                trades_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, symbol, strategy),
                FOREIGN KEY (run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_backtest_results_run ON backtest_results(run_id);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                payload_json TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scan_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL,
                reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS strategy_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                entry_price REAL,
                status TEXT NOT NULL DEFAULT 'open',
                exit_date TEXT,
                exit_price REAL,
                exit_reason TEXT,
                source_job_id TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(symbol, strategy, entry_date)
            );
            CREATE INDEX IF NOT EXISTS idx_strategy_positions_status ON strategy_positions(status, strategy, symbol);
            """
        )
        instrument_columns = {row["name"] for row in database.execute("PRAGMA table_info(instruments)").fetchall()}
        if "universe_member" not in instrument_columns:
            database.execute("ALTER TABLE instruments ADD COLUMN universe_member INTEGER NOT NULL DEFAULT 0")
        bar_columns = {row["name"] for row in database.execute("PRAGMA table_info(daily_bars)").fetchall()}
        for column in (
            "raw_open", "raw_high", "raw_low", "raw_close",
            "hfq_open", "hfq_high", "hfq_low", "hfq_close", "adjustment_factor",
        ):
            if column not in bar_columns:
                database.execute(f"ALTER TABLE daily_bars ADD COLUMN {column} REAL")
        backtest_columns = {row["name"] for row in database.execute("PRAGMA table_info(backtest_runs)").fetchall()}
        if "deleted_at" not in backtest_columns:
            database.execute("ALTER TABLE backtest_runs ADD COLUMN deleted_at TEXT")


def save_snapshot(snapshot: dict[str, Any], market: str = "自动") -> int:
    symbol = normalize_symbol(str(snapshot.get("requested_symbol") or snapshot["symbol"]), market)
    code = symbol.split(".")[0]
    market_name = "港股" if symbol.endswith(".HK") else "A股" if symbol.endswith((".SS", ".SZ", ".BJ")) else "美股"
    now = _now()
    valid_rows = [row for row in snapshot.get("points", []) if all(row.get(key) is not None for key in ("open", "high", "low", "close"))]
    source = str(snapshot.get("data_source") or "")
    with connection() as database:
        database.execute(
            """INSERT INTO instruments(symbol, code, name, market, exchange, currency, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET name=CASE
                   WHEN excluded.name=excluded.code OR excluded.name=excluded.symbol THEN instruments.name
                   ELSE excluded.name END, market=excluded.market,
               exchange=excluded.exchange, currency=excluded.currency, active=1, updated_at=excluded.updated_at""",
            (symbol, code, snapshot.get("name") or code, market_name, snapshot.get("exchange"), snapshot.get("currency"), now),
        )
        if valid_rows and "AKShare" in source:
            first_date = datetime.fromtimestamp(int(valid_rows[0]["timestamp"]), timezone.utc).date().isoformat()
            last_date = datetime.fromtimestamp(int(valid_rows[-1]["timestamp"]), timezone.utc).date().isoformat()
            database.execute(
                "DELETE FROM daily_bars WHERE symbol=? AND trade_date BETWEEN ? AND ? AND source NOT LIKE '%AKShare%'",
                (symbol, first_date, last_date),
            )
        database.executemany(
            """INSERT INTO daily_bars(
                   symbol, trade_date, timestamp, open, high, low, close, volume,
                   raw_open, raw_high, raw_low, raw_close, adjustment_factor,
                   hfq_open, hfq_high, hfq_low, hfq_close,
                   adjustment, source, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol, trade_date) DO UPDATE SET timestamp=excluded.timestamp, open=excluded.open,
               high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume,
               raw_open=COALESCE(excluded.raw_open, daily_bars.raw_open),
               raw_high=COALESCE(excluded.raw_high, daily_bars.raw_high),
               raw_low=COALESCE(excluded.raw_low, daily_bars.raw_low),
               raw_close=COALESCE(excluded.raw_close, daily_bars.raw_close),
               hfq_open=COALESCE(excluded.hfq_open, daily_bars.hfq_open),
               hfq_high=COALESCE(excluded.hfq_high, daily_bars.hfq_high),
               hfq_low=COALESCE(excluded.hfq_low, daily_bars.hfq_low),
               hfq_close=COALESCE(excluded.hfq_close, daily_bars.hfq_close),
               adjustment_factor=COALESCE(excluded.adjustment_factor, daily_bars.adjustment_factor),
               adjustment=excluded.adjustment, source=excluded.source, updated_at=excluded.updated_at""",
            [
                (
                    symbol,
                    datetime.fromtimestamp(int(row["timestamp"]), timezone.utc).date().isoformat(),
                    int(row["timestamp"]), float(row["open"]), float(row["high"]), float(row["low"]),
                    float(row["close"]), float(row["volume"]) if row.get("volume") is not None else None,
                    float(row["raw_open"]) if row.get("raw_open") is not None else None,
                    float(row["raw_high"]) if row.get("raw_high") is not None else None,
                    float(row["raw_low"]) if row.get("raw_low") is not None else None,
                    float(row["raw_close"]) if row.get("raw_close") is not None else None,
                    float(row["adjustment_factor"]) if row.get("adjustment_factor") is not None else None,
                    float(row["hfq_open"]) if row.get("hfq_open") is not None else None,
                    float(row["hfq_high"]) if row.get("hfq_high") is not None else None,
                    float(row["hfq_low"]) if row.get("hfq_low") is not None else None,
                    float(row["hfq_close"]) if row.get("hfq_close") is not None else None,
                    snapshot.get("adjustment", "前复权"), source, now,
                )
                for row in valid_rows
            ],
        )
    return len(valid_rows)


def load_snapshot(
    symbol: str, start_date: str, end_date: str, market: str = "自动", warmup_bars: int = 250,
    adjustment_mode: str = "前复权",
) -> dict[str, Any] | None:
    normalized = normalize_symbol(symbol, market)
    with connection() as database:
        instrument = database.execute("SELECT * FROM instruments WHERE symbol=?", (normalized,)).fetchone()
        is_a_share = normalized.endswith((".SS", ".SZ", ".BJ"))
        primary_source = "%AKShare%" if is_a_share else "%东方财富%"
        has_primary_source = database.execute(
            "SELECT 1 FROM daily_bars WHERE symbol=? AND source LIKE ? LIMIT 1",
            (normalized, primary_source),
        ).fetchone() is not None
        if instrument is None or not has_primary_source:
            return None
        source_filter = " AND source LIKE ?"
        valid_price_filter = " AND open>0 AND high>0 AND low>0 AND close>0"
        warmup_rows = database.execute(
            f"SELECT * FROM daily_bars WHERE symbol=? AND trade_date<?{source_filter}{valid_price_filter} ORDER BY trade_date DESC LIMIT ?",
            (normalized, start_date, primary_source, warmup_bars),
        ).fetchall()
        rows = database.execute(
            f"SELECT * FROM daily_bars WHERE symbol=? AND trade_date BETWEEN ? AND ?{source_filter}{valid_price_filter} ORDER BY trade_date",
            (normalized, start_date, end_date, primary_source),
        ).fetchall()
        factor_base_row = database.execute(
            f"SELECT adjustment_factor FROM daily_bars WHERE symbol=? AND adjustment_factor IS NOT NULL{source_filter} ORDER BY trade_date LIMIT 1",
            (normalized, primary_source),
        ).fetchone()
    if len(rows) < 2:
        return None
    snapshot = {
        "requested_symbol": symbol,
        "symbol": normalized,
        "name": instrument["name"],
        "exchange": instrument["exchange"],
        "currency": instrument["currency"],
        "points": [
            {key: row[key] for key in ("timestamp", "open", "high", "low", "close", "volume", "raw_open", "raw_high", "raw_low", "raw_close", "hfq_open", "hfq_high", "hfq_low", "hfq_close", "adjustment_factor")}
            for row in rows
        ],
        "indicator_warmup_points": [
            {key: row[key] for key in ("timestamp", "open", "high", "low", "close", "volume", "raw_open", "raw_high", "raw_low", "raw_close", "hfq_open", "hfq_high", "hfq_low", "hfq_close", "adjustment_factor")}
            for row in reversed(warmup_rows)
        ],
        "excluded_fallback_source": "非 AKShare 的历史来源（腾讯财经/东方财富/Yahoo）" if is_a_share else "Yahoo",
        "data_source": rows[-1]["source"] or "本地行情数据库",
        "adjustment": rows[-1]["adjustment"],
        "adjustment_factor_base": factor_base_row["adjustment_factor"] if factor_base_row else None,
    }
    from .tools.market_data import apply_price_adjustment
    return apply_price_adjustment(snapshot, adjustment_mode)


def save_fundamental_reports(symbol: str, reports: list[dict[str, Any]]) -> int:
    normalized = normalize_symbol(symbol, "A股")
    now = _now()
    with connection() as database:
        database.executemany(
            """INSERT OR REPLACE INTO fundamental_reports(
                   symbol, report_date, effective_date, report_type, score, roe, revenue_growth,
                   profit_growth, operating_cash, net_profit, debt_ratio, components_json, raw_json,
                   source, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(
                normalized, row["report_date"], row["effective_date"], row.get("report_type"), row["score"],
                row.get("roe"), row.get("revenue_growth"), row.get("profit_growth"), row.get("operating_cash"),
                row.get("net_profit"), row.get("debt_ratio"), json.dumps(row.get("components") or {}, ensure_ascii=False),
                json.dumps(row.get("raw") or {}, ensure_ascii=False), row.get("source") or "未知", now,
            ) for row in reports],
        )
    return len(reports)


def load_fundamental_reports(symbol: str, end_date: str | None = None) -> list[dict[str, Any]]:
    normalized = normalize_symbol(symbol, "A股")
    query = "SELECT * FROM fundamental_reports WHERE symbol=?"
    parameters: list[Any] = [normalized]
    if end_date:
        query += " AND effective_date<=?"
        parameters.append(end_date)
    query += " ORDER BY effective_date, report_date"
    with connection() as database:
        rows = database.execute(query, parameters).fetchall()
    return [{
        **dict(row),
        "components": json.loads(row["components_json"]),
        "raw": json.loads(row["raw_json"]),
    } for row in rows]


def save_dividend_events(symbol: str, events: list[dict[str, Any]]) -> int:
    normalized = normalize_symbol(symbol, "A股")
    with connection() as database:
        database.executemany(
            """INSERT OR REPLACE INTO dividend_events(
                   symbol, announcement_date, ex_date, payment_date, cash_per_10, raw_json, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(normalized, item["announcement_date"], item.get("ex_date"), item.get("payment_date"),
              item["cash_per_10"], json.dumps(item.get("raw") or {}, ensure_ascii=False), _now()) for item in events],
        )
    return len(events)


def load_dividend_events(symbol: str, end_date: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM dividend_events WHERE symbol=?"
    parameters: list[Any] = [normalize_symbol(symbol, "A股")]
    if end_date:
        query += " AND announcement_date<=?"
        parameters.append(end_date)
    query += " ORDER BY announcement_date, ex_date"
    with connection() as database:
        return [{**dict(row), "raw": json.loads(row["raw_json"])} for row in database.execute(query, parameters)]


def dividend_coverage(market: str = "A股") -> dict[str, int]:
    with connection() as database:
        total = int(database.execute(
            "SELECT COUNT(*) FROM instruments WHERE market=? AND active=1 AND universe_member=1", (market,),
        ).fetchone()[0])
        covered = int(database.execute(
            """SELECT COUNT(DISTINCT d.symbol) FROM dividend_events d JOIN instruments i ON i.symbol=d.symbol
               WHERE i.market=? AND i.active=1 AND i.universe_member=1""", (market,),
        ).fetchone()[0])
    return {"total": total, "covered": covered, "missing": max(0, total-covered)}


def fundamental_cache_is_fresh(symbol: str, max_age_hours: int = 12) -> bool:
    normalized = normalize_symbol(symbol, "A股")
    with connection() as database:
        row = database.execute(
            "SELECT MAX(updated_at) AS updated_at FROM fundamental_reports WHERE symbol=?", (normalized,),
        ).fetchone()
    if row is None or not row["updated_at"]:
        return False
    updated_at = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)).total_seconds() <= max_age_hours * 3600


def fundamental_coverage(market: str = "A股") -> dict[str, int]:
    with connection() as database:
        total = int(database.execute(
            "SELECT COUNT(*) FROM instruments WHERE market=? AND active=1 AND universe_member=1", (market,),
        ).fetchone()[0])
        covered = int(database.execute(
            """SELECT COUNT(DISTINCT f.symbol) FROM fundamental_reports f
               JOIN instruments i ON i.symbol=f.symbol
               WHERE i.market=? AND i.active=1 AND i.universe_member=1""", (market,),
        ).fetchone()[0])
    return {"total": total, "covered": covered, "missing": max(0, total - covered)}


def get_bar_coverage(symbol: str, market: str = "自动") -> tuple[str, str, int] | None:
    normalized = normalize_symbol(symbol, market)
    source_filter = " AND source LIKE '%AKShare%'" if normalized.endswith((".SS", ".SZ", ".BJ")) else ""
    with connection() as database:
        row = database.execute(
            f"SELECT MIN(trade_date) AS first_date, MAX(trade_date) AS last_date, COUNT(*) AS count FROM daily_bars WHERE symbol=?{source_filter}",
            (normalized,),
        ).fetchone()
    if row is None or not row["first_date"]:
        return None
    return str(row["first_date"]), str(row["last_date"]), int(row["count"])


def adjustment_history_complete(symbol: str, target_trade_date: str, market: str = "A股") -> bool:
    """判断复权历史是否完整且已到达本次同步探测出的真实交易日。"""
    normalized = normalize_symbol(symbol, market)
    with connection() as database:
        row = database.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(raw_close) AS raw_count,
                      COUNT(hfq_close) AS hfq_count,
                      MAX(trade_date) AS last_date
               FROM daily_bars WHERE symbol=? AND source LIKE '%AKShare%'""",
            (normalized,),
        ).fetchone()
    return bool(
        row and int(row["total"] or 0) >= 2
        and int(row["raw_count"] or 0) == int(row["total"])
        and int(row["hfq_count"] or 0) == int(row["total"])
        and str(row["last_date"] or "") >= target_trade_date
    )


def price_mode_coverage(
    mode: str,
    market: str = "A股",
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """统计指定回测区间内完整可用的复权行情，而不是仅检查是否存在任意一行。"""
    end_date = end_date or datetime.now(timezone.utc).date().isoformat()
    requested_start = datetime.fromisoformat(start_date or "2020-01-01").date()
    # 与全量同步一致，为回测开始日前的状态型指标保留预热区间。
    coverage_start = (requested_start - timedelta(days=400)).isoformat()
    if mode == "不复权":
        complete_condition = " AND ".join(f"b.raw_{field} IS NOT NULL" for field in ("open", "high", "low", "close"))
    elif mode in {"后复权", "动态前复权"}:
        complete_condition = " AND ".join([
            *(f"b.raw_{field} IS NOT NULL" for field in ("open", "high", "low", "close")),
            *(f"b.hfq_{field} IS NOT NULL" for field in ("open", "high", "low", "close")),
            "b.adjustment_factor IS NOT NULL",
            "b.adjustment_factor > 0",
        ])
    else:
        complete_condition = "b.close IS NOT NULL"
    eligible_condition = """(
        SELECT COUNT(*) FROM (
            SELECT 1 FROM daily_bars eligible_bars INDEXED BY sqlite_autoindex_daily_bars_1
            WHERE eligible_bars.symbol=i.symbol AND eligible_bars.source LIKE '%AKShare%'
              AND eligible_bars.trade_date BETWEEN ? AND ? LIMIT 2
        )
    ) >= 2"""
    with connection() as database:
        total = int(database.execute(
            f"""SELECT COUNT(*) FROM instruments i
                WHERE i.market=? AND i.active=1 AND i.universe_member=1
                  AND {eligible_condition}""",
            (market, start_date or "2020-01-01", end_date),
        ).fetchone()[0])
        covered_rows = database.execute(
            f"""SELECT i.symbol FROM instruments i
                WHERE i.market=? AND i.active=1 AND i.universe_member=1
                  AND {eligible_condition}
                  AND NOT EXISTS (
                      SELECT 1 FROM daily_bars b INDEXED BY sqlite_autoindex_daily_bars_1
                      WHERE b.symbol=i.symbol AND b.source LIKE '%AKShare%'
                        AND b.trade_date BETWEEN ? AND ? AND NOT COALESCE(({complete_condition}), 0)
                      LIMIT 1
                  )""",
            (market, start_date or "2020-01-01", end_date, coverage_start, end_date),
        ).fetchall()
        covered_symbols = [str(row["symbol"]) for row in covered_rows]
        covered = len(covered_symbols)
    return {
        "total": total,
        "covered": covered,
        "missing": max(0, total - covered),
        "coverage_start": coverage_start,
        "coverage_end": end_date,
        "covered_symbols": covered_symbols,
    }


def list_backtest_instruments(
    start_date: str,
    end_date: str,
    market: str = "A股",
    adjustment_mode: str = "前复权",
) -> list[dict[str, Any]]:
    """返回区间内有行情且所选复权字段完整的股票；保留期间交易过的退市股。"""
    coverage_start = (datetime.fromisoformat(start_date).date() - timedelta(days=400)).isoformat()
    if adjustment_mode == "不复权":
        complete_condition = " AND ".join(f"complete_bars.raw_{field} IS NOT NULL" for field in ("open", "high", "low", "close"))
    elif adjustment_mode in {"后复权", "动态前复权"}:
        complete_condition = " AND ".join([
            *(f"complete_bars.raw_{field} IS NOT NULL" for field in ("open", "high", "low", "close")),
            *(f"complete_bars.hfq_{field} IS NOT NULL" for field in ("open", "high", "low", "close")),
            "complete_bars.adjustment_factor IS NOT NULL",
            "complete_bars.adjustment_factor > 0",
        ])
    else:
        complete_condition = "complete_bars.close IS NOT NULL"
    with connection() as database:
        eligible_source = "b.source LIKE '%AKShare%'"
        complete_source = "complete_bars.source LIKE '%AKShare%'"
        rows = database.execute(
            f"""SELECT i.* FROM instruments i
               WHERE i.market=? AND i.active=1 AND i.universe_member=1
                 AND (
                     SELECT COUNT(*) FROM (
                         SELECT 1 FROM daily_bars b INDEXED BY sqlite_autoindex_daily_bars_1
                         WHERE b.symbol=i.symbol AND {eligible_source}
                           AND b.trade_date BETWEEN ? AND ? LIMIT 2
                     )
                 ) >= 2
                 AND NOT EXISTS (
                     SELECT 1 FROM daily_bars complete_bars INDEXED BY sqlite_autoindex_daily_bars_1
                     WHERE complete_bars.symbol=i.symbol AND {complete_source}
                       AND complete_bars.trade_date BETWEEN ? AND ?
                       AND NOT COALESCE(({complete_condition}), 0)
                     LIMIT 1
                 )
               ORDER BY i.symbol""",
            (market, start_date, end_date, coverage_start, end_date),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_instruments(items: list[dict[str, Any]]) -> int:
    now = _now()
    with connection() as database:
        markets = {item.get("market", "A股") for item in items}
        for market in markets:
            database.execute("UPDATE instruments SET universe_member=0 WHERE market=?", (market,))
        database.executemany(
            """INSERT INTO instruments(symbol, code, name, market, exchange, currency, active, universe_member, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)
               ON CONFLICT(symbol) DO UPDATE SET code=excluded.code, name=excluded.name, market=excluded.market,
               exchange=excluded.exchange, currency=excluded.currency, active=1, universe_member=1, updated_at=excluded.updated_at""",
            [(item["symbol"], item["code"], item["name"], item.get("market", "A股"), item.get("exchange"), item.get("currency", "CNY"), now) for item in items],
        )
    return len(items)


def list_instruments(market: str = "A股", active_only: bool = True, universe_only: bool = True) -> list[dict[str, Any]]:
    query = "SELECT * FROM instruments WHERE market=?"
    parameters: list[Any] = [market]
    if active_only:
        query += " AND active=1"
    if universe_only:
        query += " AND universe_member=1"
    query += " ORDER BY symbol"
    with connection() as database:
        return [dict(row) for row in database.execute(query, parameters).fetchall()]


def create_backtest_run(name: str, scope: str, start_date: str, end_date: str, strategies: list[str], parameters: dict[str, Any]) -> str:
    run_id = uuid4().hex
    with connection() as database:
        database.execute(
            """INSERT INTO backtest_runs(
                   id, name, scope, status, start_date, end_date, strategies_json, parameters_json,
                   summary_json, error, created_at, completed_at, deleted_at
               ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL)""",
            (run_id, name, scope, start_date, end_date, json.dumps(strategies, ensure_ascii=False), json.dumps(parameters, ensure_ascii=False), _now()),
        )
    return run_id


def save_backtest_result(run_id: str, result: dict[str, Any]) -> None:
    excluded = {"curve", "trade_events"}
    metrics = {key: value for key, value in result.items() if key not in excluded}
    with connection() as database:
        database.execute(
            """INSERT OR REPLACE INTO backtest_results(run_id, symbol, strategy, metrics_json, curve_json, trades_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, result["symbol"], result["strategy"], json.dumps(metrics, ensure_ascii=False), _pack(result["curve"]), _pack(result["trade_events"]), _now()),
        )


def complete_backtest_run(run_id: str, summary: dict[str, Any] | None = None, error: str | None = None) -> None:
    with connection() as database:
        database.execute(
            "UPDATE backtest_runs SET status=?, summary_json=?, error=?, completed_at=? WHERE id=?",
            ("failed" if error else "completed", json.dumps(summary, ensure_ascii=False) if summary is not None else None, error, _now(), run_id),
        )


def list_backtest_runs(limit: int = 50) -> list[dict[str, Any]]:
    with connection() as database:
        rows = database.execute(
            "SELECT *, (SELECT COUNT(*) FROM backtest_results r WHERE r.run_id=backtest_runs.id) AS result_count FROM backtest_runs WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            **dict(row),
            "strategies": json.loads(row["strategies_json"]),
            "parameters": json.loads(row["parameters_json"]),
            "summary": _summary_without_curves(json.loads(row["summary_json"])) if row["summary_json"] else None,
        }
        for row in rows
    ]


def _summary_without_curves(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        **summary,
        "strategies": [
            {key: value for key, value in strategy.items() if key != "portfolio_curve"}
            for strategy in summary.get("strategies") or []
        ],
    }


def get_backtest_run(run_id: str) -> dict[str, Any] | None:
    with connection() as database:
        run = database.execute("SELECT * FROM backtest_runs WHERE id=? AND deleted_at IS NULL", (run_id,)).fetchone()
        results = database.execute("SELECT * FROM backtest_results WHERE run_id=? ORDER BY symbol, strategy", (run_id,)).fetchall()
    if run is None:
        return None
    return {
        **dict(run),
        "strategies": json.loads(run["strategies_json"]),
        "parameters": json.loads(run["parameters_json"]),
        "summary": json.loads(run["summary_json"]) if run["summary_json"] else None,
        "results": [
            {
                **json.loads(row["metrics_json"]),
                "curve": _unpack(row["curve_json"]),
                "trade_events": _unpack(row["trades_json"]),
            }
            for row in results
        ],
    }


def list_backtest_result_summaries(
    run_id: str,
    limit: int = 100,
    offset: int = 0,
    query: str | None = None,
    strategy: str | None = None,
    sort_by: str = "symbol",
    sort_order: str = "asc",
) -> dict[str, Any]:
    with connection() as database:
        exists = database.execute("SELECT 1 FROM backtest_runs WHERE id=? AND deleted_at IS NULL", (run_id,)).fetchone()
    if exists is None:
        return {"total": 0, "limit": limit, "offset": offset, "items": [], "not_found": True}
    conditions = ["run_id=?"]
    parameters: list[Any] = [run_id]
    if query:
        keyword = f"%{query.strip()}%"
        conditions.append("(symbol LIKE ? OR json_extract(metrics_json, '$.name') LIKE ?)")
        parameters.extend([keyword, keyword])
    if strategy:
        conditions.append("strategy=?")
        parameters.append(strategy)
    where_clause = " AND ".join(conditions)
    sort_columns = {
        "symbol": "symbol COLLATE NOCASE",
        "return": "CAST(json_extract(metrics_json, '$.total_return_pct') AS REAL)",
        "drawdown": "CAST(json_extract(metrics_json, '$.max_drawdown_pct') AS REAL)",
        "sharpe": "CAST(json_extract(metrics_json, '$.sharpe') AS REAL)",
        "win_rate": "CAST(json_extract(metrics_json, '$.win_rate_pct') AS REAL)",
    }
    order_column = sort_columns.get(sort_by, sort_columns["symbol"])
    order_direction = "DESC" if sort_order.lower() == "desc" else "ASC"
    with connection() as database:
        total = int(database.execute(f"SELECT COUNT(*) FROM backtest_results WHERE {where_clause}", parameters).fetchone()[0])
        rows = database.execute(
            f"SELECT id, metrics_json FROM backtest_results WHERE {where_clause} ORDER BY {order_column} {order_direction}, strategy ASC LIMIT ? OFFSET ?",
            [*parameters, limit, offset],
        ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "query": query,
        "strategy": strategy,
        "sort_by": sort_by,
        "sort_order": order_direction.lower(),
        "items": [{"result_id": row["id"], **json.loads(row["metrics_json"])} for row in rows],
    }


def get_backtest_result(run_id: str, result_id: int) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            """SELECT r.* FROM backtest_results r JOIN backtest_runs b ON b.id=r.run_id
               WHERE r.run_id=? AND r.id=? AND b.deleted_at IS NULL""",
            (run_id, result_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "result_id": row["id"],
        **json.loads(row["metrics_json"]),
        "curve": _unpack(row["curve_json"]),
        "trade_events": _unpack(row["trades_json"]),
    }


def latest_market_trade_date(end_date: str, market: str = "A股") -> str | None:
    with connection() as database:
        row = database.execute(
            """SELECT MAX(b.trade_date) FROM daily_bars b
               JOIN instruments i ON i.symbol=b.symbol WHERE i.market=? AND b.trade_date<=?""",
            (market, end_date),
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def market_data_status(market: str = "A股") -> dict[str, Any]:
    """区分少数股票先更新的局部日期与覆盖80%股票池的全市场可用日期。"""
    with connection() as database:
        total = int(database.execute(
            "SELECT COUNT(*) FROM instruments WHERE market=? AND active=1 AND universe_member=1",
            (market,),
        ).fetchone()[0])
        latest_row = database.execute(
            """SELECT MAX(b.trade_date) AS trade_date FROM daily_bars b
               JOIN instruments i ON i.symbol=b.symbol
               WHERE i.market=? AND b.source LIKE '%AKShare%'""",
            (market,),
        ).fetchone()
        raw_latest_date = str(latest_row["trade_date"]) if latest_row and latest_row["trade_date"] else None
        if raw_latest_date is None:
            return {
                "market": market, "latest_trade_date": None, "raw_latest_trade_date": None,
                "symbols_on_latest_date": 0, "raw_latest_symbols": 0,
                "full_adjustment_symbols": 0, "universe_symbols": total,
                "coverage_pct": 0.0, "full_adjustment_coverage_pct": 0.0, "last_updated_at": None,
            }
        window_start = (datetime.fromisoformat(raw_latest_date).date() - timedelta(days=45)).isoformat()
        daily_rows = database.execute(
            """SELECT b.trade_date, COUNT(DISTINCT b.symbol) AS symbols,
                      COUNT(DISTINCT CASE WHEN b.raw_close IS NOT NULL AND b.hfq_close IS NOT NULL
                                               AND b.adjustment_factor IS NOT NULL AND b.adjustment_factor>0
                                          THEN b.symbol END) AS full_symbols,
                      MAX(b.updated_at) AS updated_at
               FROM daily_bars b JOIN instruments i ON i.symbol=b.symbol
               WHERE i.market=? AND i.active=1 AND i.universe_member=1
                 AND b.trade_date BETWEEN ? AND ? AND b.source LIKE '%AKShare%'
               GROUP BY b.trade_date ORDER BY b.trade_date DESC""",
            (market, window_start, raw_latest_date),
        ).fetchall()
    threshold = max(1, math.ceil(total * 0.8))
    safe_row = next((row for row in daily_rows if int(row["symbols"] or 0) >= threshold), None)
    latest_data_row = safe_row or (daily_rows[0] if daily_rows else None)
    raw_row = next((row for row in daily_rows if str(row["trade_date"]) == raw_latest_date), None)
    latest_date = str(latest_data_row["trade_date"]) if latest_data_row else raw_latest_date
    symbols = int(latest_data_row["symbols"] or 0) if latest_data_row else 0
    full_symbols = int(latest_data_row["full_symbols"] or 0) if latest_data_row else 0
    return {
        "market": market,
        "latest_trade_date": latest_date,
        "raw_latest_trade_date": raw_latest_date,
        "symbols_on_latest_date": symbols,
        "raw_latest_symbols": int(raw_row["symbols"] or 0) if raw_row else 0,
        "full_adjustment_symbols": full_symbols,
        "universe_symbols": total,
        "coverage_pct": round(symbols / total * 100, 2) if total else 0.0,
        "full_adjustment_coverage_pct": round(full_symbols / total * 100, 2) if total else 0.0,
        "last_updated_at": str(raw_row["updated_at"]) if raw_row and raw_row["updated_at"] else None,
        "coverage_threshold_pct": 80,
    }


def market_data_coverage_on_date(trade_date: str, market: str = "A股") -> dict[str, Any]:
    """统计指定交易日前复权与完整三口径行情的本地覆盖情况。"""
    with connection() as database:
        total = int(database.execute(
            "SELECT COUNT(*) FROM instruments WHERE market=? AND active=1 AND universe_member=1",
            (market,),
        ).fetchone()[0])
        row = database.execute(
            """SELECT COUNT(DISTINCT b.symbol) AS front_symbols,
                      COUNT(DISTINCT CASE WHEN b.raw_close IS NOT NULL AND b.hfq_close IS NOT NULL
                                               AND b.adjustment_factor IS NOT NULL AND b.adjustment_factor>0
                                          THEN b.symbol END) AS full_symbols,
                      MAX(b.updated_at) AS updated_at
               FROM daily_bars b JOIN instruments i ON i.symbol=b.symbol
               WHERE i.market=? AND i.active=1 AND i.universe_member=1
                 AND b.trade_date=? AND b.source LIKE '%AKShare%'""",
            (market, trade_date),
        ).fetchone()
    front = int(row["front_symbols"] or 0) if row else 0
    full = int(row["full_symbols"] or 0) if row else 0
    return {
        "trade_date": trade_date,
        "universe_symbols": total,
        "front_symbols": front,
        "full_symbols": full,
        "front_coverage_pct": round(front / total * 100, 2) if total else 0.0,
        "full_coverage_pct": round(full / total * 100, 2) if total else 0.0,
        "front_current": bool(total and front >= math.ceil(total * 0.8)),
        "full_current": bool(total and full >= math.ceil(total * 0.8)),
        "last_updated_at": str(row["updated_at"]) if row and row["updated_at"] else None,
        "coverage_threshold_pct": 80,
    }


def delete_backtest_run(run_id: str) -> bool:
    with connection() as database:
        cursor = database.execute(
            "UPDATE backtest_runs SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
            (_now(), run_id),
        )
    return cursor.rowcount > 0


def get_backtest_run_summary(run_id: str) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            """SELECT *, (SELECT COUNT(*) FROM backtest_results r WHERE r.run_id=backtest_runs.id) AS result_count
               FROM backtest_runs WHERE id=? AND deleted_at IS NULL""",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "scope": row["scope"],
        "status": row["status"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "result_count": row["result_count"],
        "strategies": json.loads(row["strategies_json"]),
        "summary": json.loads(row["summary_json"]) if row["summary_json"] else None,
        "created_at": row["created_at"],
    }


def create_job(kind: str, payload: dict[str, Any]) -> str:
    job_id = uuid4().hex
    now = _now()
    with connection() as database:
        database.execute(
            "INSERT INTO jobs VALUES (?, ?, 'pending', 0, 0, NULL, ?, NULL, NULL, ?, ?)",
            (job_id, kind, json.dumps(payload, ensure_ascii=False), now, now),
        )
    return job_id


def get_active_job(kind: str) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            "SELECT * FROM jobs WHERE kind=? AND status IN ('pending','running') ORDER BY created_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
    return dict(row) if row else None


def fail_interrupted_jobs() -> int:
    """进程启动时，关闭上一个进程遗留且不可能继续运行的异步任务。"""
    now = _now()
    with connection() as database:
        cursor = database.execute(
            """UPDATE jobs SET status='failed', message='任务因后端重启中断',
               error='后端进程已重启，原任务无法继续；请重新创建任务', updated_at=?
               WHERE status IN ('pending','running')""",
            (now,),
        )
    return int(cursor.rowcount)


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    current: int | None = None,
    total: int | None = None,
    message: str | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    assignments = ["updated_at=?"]
    values: list[Any] = [_now()]
    for column, value in (("status", status), ("progress_current", current), ("progress_total", total), ("message", message), ("error", error)):
        if value is not None:
            assignments.append(f"{column}=?")
            values.append(value)
    if result is not None:
        assignments.append("result_json=?")
        values.append(json.dumps(result, ensure_ascii=False))
    values.append(job_id)
    with connection() as database:
        database.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id=?", values)


def get_job(job_id: str) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    return {
        **dict(row),
        "payload": json.loads(row["payload_json"]),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
    }


def save_scan_signal(job_id: str, signal_date: str, symbol: str, strategy: str, side: str, price: float | None, reason: str) -> None:
    with connection() as database:
        database.execute(
            "INSERT INTO scan_signals(job_id, signal_date, symbol, strategy, side, price, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, signal_date, symbol, strategy, side, price, reason, _now()),
        )


def sync_strategy_position(signal: dict[str, Any], job_id: str) -> None:
    now = _now()
    with connection() as database:
        if signal["side"] == "B":
            database.execute(
                """INSERT OR IGNORE INTO strategy_positions(
                       symbol, strategy, entry_date, entry_price, status, source_job_id, updated_at
                   ) VALUES (?, ?, ?, ?, 'open', ?, ?)""",
                (signal["symbol"], signal["strategy"], signal["signal_date"], signal.get("price"), job_id, now),
            )
        elif signal["side"] == "S":
            database.execute(
                """UPDATE strategy_positions SET status='closed', exit_date=?, exit_price=?, exit_reason=?,
                       source_job_id=?, updated_at=?
                   WHERE symbol=? AND strategy=? AND status='open'""",
                (signal["signal_date"], signal.get("price"), signal.get("reason"), job_id, now,
                 signal["symbol"], signal["strategy"]),
            )


def list_strategy_positions(strategy: str | None = None, status: str = "open") -> list[dict[str, Any]]:
    query = "SELECT * FROM strategy_positions WHERE status=?"
    params: list[Any] = [status]
    if strategy:
        query += " AND strategy=?"
        params.append(strategy)
    query += " ORDER BY entry_date DESC, symbol"
    with connection() as database:
        return [dict(row) for row in database.execute(query, params).fetchall()]


def list_scan_signals(job_id: str) -> list[dict[str, Any]]:
    with connection() as database:
        return [dict(row) for row in database.execute("SELECT * FROM scan_signals WHERE job_id=? ORDER BY signal_date DESC, symbol", (job_id,)).fetchall()]
