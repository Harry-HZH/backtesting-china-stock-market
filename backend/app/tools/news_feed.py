from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import re
from typing import Any
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from agno.tools import tool
import httpx


GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
BING_NEWS_RSS = "https://www.bing.com/news/search"
LOOKBACK_DAYS = {"1d": 1, "3d": 3, "7d": 7, "30d": 30}


class NewsFeedError(RuntimeError):
    pass


def _text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return (child.text or "").strip() if child is not None else ""


def _clean_html(value: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", html.unescape(value))
    return re.sub(r"\s+", " ", plain).strip()


def parse_news_rss(xml_text: str, provider: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NewsFeedError(f"{provider} 返回的 RSS 无法解析") from exc
    items = []
    for node in root.findall("./channel/item"):
        title = _clean_html(_text(node, "title"))
        url = html.unescape(_text(node, "link"))
        published_raw = _text(node, "pubDate")
        if not title or not url or urlparse(url).scheme not in {"http", "https"}:
            continue
        try:
            published = parsedate_to_datetime(published_raw)
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published = published.astimezone(timezone.utc)
        except (TypeError, ValueError):
            published = datetime.now(timezone.utc)
        source_node = node.find("source")
        if source_node is None:
            source_node = node.find("{*}Source")
        source = (source_node.text or "").strip() if source_node is not None else provider
        items.append({
            "title": title,
            "url": url,
            "source": source or provider,
            "published_at": published.isoformat(),
            "summary": _clean_html(_text(node, "description"))[:500],
            "provider": provider,
        })
    return items


def _normalized_title(title: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", title).lower()


def fetch_stock_news(
    query: str,
    lookback: str = "3d",
    limit: int = 12,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    keyword = query.strip()
    if not keyword:
        raise NewsFeedError("新闻关键词不能为空")
    owns_client = client is None
    http = client or httpx.Client(timeout=20, follow_redirects=True, trust_env=True)
    feeds: list[dict[str, Any]] = []
    errors = []
    requests = [
        ("Google News", GOOGLE_NEWS_RSS, {"q": keyword, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}),
        ("Bing News", BING_NEWS_RSS, {"q": keyword, "format": "rss", "setlang": "zh-cn"}),
    ]
    try:
        for provider, url, params in requests:
            try:
                response = http.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"})
                response.raise_for_status()
                feeds.extend(parse_news_rss(response.text, provider))
            except (httpx.HTTPError, NewsFeedError) as exc:
                errors.append(f"{provider}: {exc}")
    finally:
        if owns_client:
            http.close()
    if not feeds:
        raise NewsFeedError("新闻源均不可用：" + "；".join(errors))
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS.get(lookback, 3))
    seen: set[str] = set()
    result = []
    for item in sorted(feeds, key=lambda value: value["published_at"], reverse=True):
        published = datetime.fromisoformat(item["published_at"])
        key = _normalized_title(item["title"])
        if published < cutoff or not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


@tool(name="get_latest_stock_news", description="搜索股票或公司关键词的最新公开新闻，返回来源、时间、标题和链接。")
def get_latest_stock_news(query: str, lookback: str = "3d", limit: int = 12) -> list[dict[str, Any]]:
    return fetch_stock_news(query, lookback, limit)
