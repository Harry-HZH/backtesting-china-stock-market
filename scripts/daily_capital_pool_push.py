"""每日资金池行情同步、交易计划和企业微信推送。"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.database import create_job, get_job, list_backtest_runs
from backend.app.task_service import build_capital_pool_plan, sync_market_data


WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=6bf5cd00-ec05-497e-bf3f-c9912be1bdd8"


def latest_pool_parameters() -> dict:
    for run in list_backtest_runs(100):
        if (
            run.get("scope") == "portfolio"
            and run.get("status") == "completed"
            and str((run.get("parameters") or {}).get("strategy") or "") == "RSI反转+止盈止损"
        ):
            params = dict(run.get("parameters") or {})
            return {
                "strategy": "RSI反转+止盈止损",
                "max_positions": int(params.get("max_positions") or 100),
                "market_signal_threshold": int(params.get("market_signal_threshold") or 0),
                "max_opening_gap_pct": float(params.get("max_opening_gap_pct") or 3),
            }
    return {"strategy": "RSI反转+止盈止损", "max_positions": 100, "market_signal_threshold": 0, "max_opening_gap_pct": 3}


async def run_job(kind: str, payload: dict, runner) -> dict:
    job_id = create_job(kind, payload)
    await runner(job_id)
    job = get_job(job_id) or {}
    if job.get("status") != "completed":
        raise RuntimeError(job.get("error") or job.get("message") or f"{kind} failed")
    return job


def send_wecom(content: str) -> dict:
    body = json.dumps({"msgtype": "text", "text": {"content": content}}, ensure_ascii=False).encode()
    request = urllib.request.Request(WEBHOOK, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("errcode") != 0:
        raise RuntimeError(f"企业微信推送失败：{result}")
    return result


def format_plan(plan: dict) -> str:
    result = plan.get("result") or {}
    trade_date = result.get("trade_date") or "未知"
    lines = [
        f"【资金池每日交易计划】\n信号日：{trade_date}",
        f"策略：{result.get('strategy', '未知')}",
        f"持仓：{len(result.get('holdings') or [])} 只；空位：{result.get('capacity', '—')}",
    ]
    holdings = result.get("holdings") or []
    lines.append("\n【当前持仓】")
    if holdings:
        for item in holdings:
            lines.append(
                f"持仓 {item.get('symbol')} {item.get('name') or ''}，"
                f"建仓日 {item.get('entry_date')}，持仓价 {item.get('entry_price')}"
            )
    else:
        lines.append("暂无已追踪持仓")
    lines.append(f"\n建议卖出：{len(result.get('sell_candidates') or [])} 只")
    for item in result.get("sell_candidates") or []:
        lines.append(f"卖出 {item.get('symbol')} {item.get('name')}：{item.get('reason')}")
    lines.append(f"建议开仓：{len(result.get('buy_candidates') or [])} 只")
    for index, item in enumerate(result.get("buy_candidates") or [], 1):
        lines.append(f"买入#{index} {item.get('symbol')} {item.get('name')}，信号价 {item.get('signal_price')}：{item.get('reason')}")
    lines.append("执行说明：下一交易日开盘前核对实际持仓；系统不自动下单。")
    return "\n".join(lines)


async def main() -> None:
    today = date.today().isoformat()
    # RSI 推送只依赖前复权日线；完整复权同步由单独的数据维护任务负责，
    # 避免每日推送被 5,599 只股票的多套行情请求拖住。
    sync_payload = {
        "symbols": None, "start_date": "2020-01-01", "end_date": today,
        "concurrency": 4, "mode": "incremental", "price_scope": "front_only",
    }
    # 临时补发可设置 SKIP_DAILY_SYNC=1，直接使用数据库当前最新行情生成计划；
    # 正常定时任务不设置该变量，仍会先执行增量同步。
    if __import__("os").environ.get("SKIP_DAILY_SYNC") != "1":
        await run_job("data_sync", sync_payload, sync_market_data)
    params = latest_pool_parameters()
    plan_payload = {"start_date": "2020-01-01", "end_date": today, "adjustment_mode": "前复权", **params}
    plan = await run_job("capital_pool_plan", plan_payload, build_capital_pool_plan)
    send_wecom(format_plan(plan))
    print(json.dumps({"status": "ok", "trade_date": (plan.get("result") or {}).get("trade_date"), "parameters": params}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
