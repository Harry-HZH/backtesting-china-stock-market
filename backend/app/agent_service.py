from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from typing import Any

from .config import Settings


CHUNK_SIZE = 10
CHUNK_DELAY = 0.015


def agno_enabled(settings: Settings) -> bool:
    return bool(settings.api_key)


async def stream_stock_analysis(
    snapshot: dict[str, Any],
    horizon: str,
    focus: str,
    settings: Settings,
) -> AsyncIterator[str]:
    if not agno_enabled(settings):
        async for chunk in _pace(_demo_analysis(snapshot, horizon, focus)):
            yield chunk
        return

    from agno.agent import Agent
    from agno.models.openai import OpenAIChat

    model_options: dict[str, Any] = {"id": settings.model_id, "api_key": settings.api_key}
    if settings.base_url:
        model_options["base_url"] = settings.base_url
    agent = Agent(
        model=OpenAIChat(**model_options),
        instructions=[
            "你是严谨的股票研究助手，用中文回答。",
            "只使用输入中的行情事实，不得编造公司财务、估值、新闻、公告或实时价格。",
            "先注明数据范围和局限，再依次分析趋势、动量、波动与回撤、关键价位、上涨/中性/下跌三种情景。",
            "明确区分事实、推断和仍需核实的信息。不要输出确定性涨跌预测或承诺收益。",
            "结尾提供观察清单、触发条件、失效条件和风险提示；这不是个性化投资建议。",
        ],
        markdown=True,
    )
    compact = {key: value for key, value in snapshot.items() if key != "points"}
    prompt = (
        f"投资期限：{horizon}\n关注重点：{focus}\n\n"
        f"行情快照：\n{json.dumps(compact, ensure_ascii=False, indent=2)}"
    )
    response_stream = agent.arun(prompt, stream=True, stream_events=False)
    async for event in response_stream:  # type: ignore[union-attr]
        content = getattr(event, "content", None)
        if content:
            async for chunk in _pace(str(content)):
                yield chunk


async def stream_news_analysis(
    query: str,
    news: list[dict[str, Any]],
    focus: str,
    settings: Settings,
) -> AsyncIterator[str]:
    if not agno_enabled(settings):
        lines = [f"## {query} 新闻速览", "", f"共找到 {len(news)} 条近期公开新闻，关注重点：**{focus}**。", ""]
        for index, item in enumerate(news[:8], start=1):
            lines.append(f"{index}. [{item['title']}]({item['url']}) — {item['source']}")
        lines.extend(["", "### 阅读提示", "新闻标题不能替代公告和财报。请优先核对交易所公告、公司原文与事件发生时间，不要仅凭单条新闻交易。"])
        async for chunk in _pace("\n".join(lines)):
            yield chunk
        return

    from agno.agent import Agent
    from agno.models.openai import OpenAIChat

    model_options: dict[str, Any] = {"id": settings.model_id, "api_key": settings.api_key}
    if settings.base_url:
        model_options["base_url"] = settings.base_url
    agent = Agent(
        model=OpenAIChat(**model_options),
        instructions=[
            "你是股票新闻研究助手，用中文输出。",
            "只能依据给定新闻条目分析，不得补写未提供的事实、数字、公告或市场传闻。",
            "先按事件主题聚类，再区分事实、潜在影响和待核实事项。",
            "标注影响方向（潜在利好、潜在利空、中性）与期限（短期、中期、长期），但不得作确定性涨跌预测。",
            "引用新闻时必须使用输入提供的标题和 URL；结尾列出最值得继续跟踪的触发条件与风险。",
        ],
        markdown=True,
    )
    compact = [{key: item[key] for key in ("title", "url", "source", "published_at", "summary")} for item in news]
    prompt = f"关键词：{query}\n关注重点：{focus}\n\n新闻：\n{json.dumps(compact, ensure_ascii=False, indent=2)}"
    response_stream = agent.arun(prompt, stream=True, stream_events=False)
    async for event in response_stream:  # type: ignore[union-attr]
        content = getattr(event, "content", None)
        if content:
            async for chunk in _pace(str(content)):
                yield chunk


def _demo_analysis(snapshot: dict[str, Any], horizon: str, focus: str) -> str:
    price = snapshot["price"]
    ma20 = snapshot.get("ma20")
    trend = "高于" if ma20 is not None and price >= ma20 else "低于"
    return "\n".join([
        f"## {snapshot['name']}（{snapshot['symbol']}）",
        "",
        f"当前为本地 Demo 分析，使用约 6 个月日线数据；投资期限为 **{horizon}**，关注 **{focus}**。",
        "",
        "### 行情事实",
        f"- 最新价格：{price} {snapshot.get('currency') or ''}，单日变化 {snapshot['change_pct']}%。",
        f"- 区间收益：{snapshot['period_return_pct']}%；年化波动率：{snapshot['annualized_volatility_pct']}%。",
        f"- MA5 / MA20 / MA60：{snapshot['ma5']} / {snapshot['ma20']} / {snapshot['ma60']}，现价{trend} MA20。",
        f"- RSI(14)：{snapshot['rsi14']}；区间最大回撤：{snapshot['max_drawdown_pct']}%。",
        "",
        "### 情景框架",
        f"- 上行情景：价格站稳 {snapshot['period_high']} 附近并伴随成交量改善，趋势可能延续。",
        f"- 中性情景：价格在 MA20 与区间高点之间反复，等待方向选择。",
        f"- 下行情景：跌破 MA60（{snapshot['ma60']}）后无法收复，需要警惕回撤扩大。",
        "",
        "### 下一步核实",
        "补充最新财报、估值、行业比较、公司公告和事件催化后，再形成完整研究结论。",
        "",
        "> 以上仅为研究演示，不构成个性化投资建议；行情可能延迟。",
    ])


async def _pace(text: str) -> AsyncIterator[str]:
    for index in range(0, len(text), CHUNK_SIZE):
        yield text[index:index + CHUNK_SIZE]
        await asyncio.sleep(CHUNK_DELAY)
