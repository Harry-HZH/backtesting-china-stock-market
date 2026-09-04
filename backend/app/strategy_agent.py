from __future__ import annotations

import json
import re
from typing import Any

from .agent_service import agno_enabled
from .config import Settings
from .tools.strategy_dsl import describe_strategy_code, parse_strategy_code


DSL_REFERENCE = """受限策略 DSL：
只允许三行赋值：NAME="策略名"、ENTRY=条件、EXIT=条件。
价格序列：OPEN、HIGH、LOW、CLOSE、VOLUME。
指标：MA(周期)、RSI(周期)、KDJ_K(周期)、KDJ_D(周期)、KDJ_J(周期)、ATR(周期)、
MACD_DIFF()、MACD_DEA()、MACD_HIST()、BOLL_MID(周期)、BOLL_UPPER(周期)、BOLL_LOWER(周期)。
逻辑：GT/GE/LT/LE/EQ、ALL、ANY、NOT、CROSS_UP、CROSS_DOWN、REF(序列,周期)。
示例：
NAME="MA20 趋势策略"
ENTRY=ALL(GT(CLOSE,MA(20)),CROSS_UP(MA(5),MA(20)))
EXIT=ANY(LT(CLOSE,MA(20)),GT(RSI(14),75))
禁止 Python import、属性访问、下标、循环、函数定义和任何未列出的函数。"""


def _clean_code(content: str) -> str:
    match = re.search(r"```(?:python)?\s*(.*?)```", content, re.DOTALL | re.IGNORECASE)
    return (match.group(1) if match else content).strip()


async def generate_strategy_code(description: str, settings: Settings) -> dict[str, Any]:
    if not agno_enabled(settings):
        code = '\n'.join([
            'NAME="AI Demo 趋势回踩策略"',
            'ENTRY=ALL(GT(CLOSE,MA(20)),GT(MA(20),REF(MA(20),1)),LT(RSI(14),45))',
            'EXIT=ANY(LT(CLOSE,MA(20)),GT(RSI(14),75))',
        ])
        parse_strategy_code(code)
        return {"code": code, "mode": "demo", "description": describe_strategy_code(code)}
    from agno.agent import Agent
    from agno.models.openai import OpenAIChat

    options: dict[str, Any] = {"id": settings.model_id, "api_key": settings.api_key}
    if settings.base_url:
        options["base_url"] = settings.base_url
    agent = Agent(
        model=OpenAIChat(**options),
        instructions=[
            "你是量化策略编译器。把用户中文策略严格转换为给定 DSL。",
            "只能输出三行 DSL 代码，不要 Markdown，不要解释，不要使用不存在的函数。",
            "无法精确表达的状态型条件应使用最接近的无未来函数条件，并在策略名末尾加“近似”。",
        ],
    )
    response = await agent.arun(f"{DSL_REFERENCE}\n\n用户策略：{description}")
    code = _clean_code(str(getattr(response, "content", response)))
    parse_strategy_code(code)
    return {"code": code, "mode": "agno", "description": describe_strategy_code(code)}


async def explain_strategy_code(code: str, settings: Settings) -> dict[str, Any]:
    deterministic = describe_strategy_code(code)
    if not agno_enabled(settings):
        return {"description": deterministic, "mode": "demo"}
    from agno.agent import Agent
    from agno.models.openai import OpenAIChat

    options: dict[str, Any] = {"id": settings.model_id, "api_key": settings.api_key}
    if settings.base_url:
        options["base_url"] = settings.base_url
    agent = Agent(
        model=OpenAIChat(**options),
        instructions=[
            "你是量化策略审阅员，用中文准确解释受限 DSL。",
            "说明开仓、平仓、所用指标、信号执行时点和风险，不得补充代码中没有的规则，不得承诺收益。",
        ],
        markdown=True,
    )
    response = await agent.arun(f"DSL 代码：\n{code}\n\n确定性解析：{deterministic}")
    return {"description": str(getattr(response, "content", response)), "mode": "agno"}
