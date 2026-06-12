"""联网搜索服务（Web Search Service）。

为主脑提供网页搜索和内容抓取能力。

功能：
- 搜索网页内容（使用 Google 搜索或 DuckDuckGo）
- 抓取网页文本内容
- 解析搜索结果并返回摘要

设计要点：
- 异步 HTTP 调用，不阻塞主事件循环
- 超时控制，避免长时间挂起
- 返回结构化结果，便于主脑处理
- 所有网络调用都有错误处理和日志记录
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup

from utils.logger import setup_logging

logger = setup_logging()

# HTTP 请求配置
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


async def search_web(query: str, max_results: int = 5) -> dict:
    """执行网页搜索。
    
    Args:
        query: 搜索关键词
        max_results: 返回的最大结果数
        
    Returns:
        {
            "ok": bool,
            "query": str,
            "results": [
                {"title": str, "url": str, "snippet": str},
                ...
            ],
            "error": str (if ok=False)
        }
    """
    if not query or not isinstance(query, str):
        return {"ok": False, "query": "", "results": [], "error": "Invalid query"}
    
    query = query.strip()
    if len(query) > 200:
        query = query[:200]
    
    try:
        # 使用 DuckDuckGo 搜索（无需 API key）
        search_url = f"https://duckduckgo.com/html/?q={quote(query)}"
        
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(search_url, headers=_headers, ssl=False) as resp:
                if resp.status != 200:
                    logger.warning(
                        "web_search failed | query_len=%d | status=%d",
                        len(query), resp.status
                    )
                    return {
                        "ok": False,
                        "query": query,
                        "results": [],
                        "error": f"HTTP {resp.status}"
                    }
                
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                
                # 从 DuckDuckGo 结果页解析
                results = []
                for result_div in soup.find_all("div", class_="result"):
                    if len(results) >= max_results:
                        break
                    
                    try:
                        title_elem = result_div.find("a", class_="result__a")
                        snippet_elem = result_div.find("a", class_="result__snippet")
                        
                        if not title_elem:
                            continue
                        
                        title = title_elem.get_text(strip=True)
                        url = title_elem.get("href", "")
                        snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""
                        
                        if title and url:
                            results.append({
                                "title": title[:100],
                                "url": url[:200],
                                "snippet": snippet[:150]
                            })
                    except Exception as e:
                        logger.debug("parse_result_failed | err=%s", type(e).__name__)
                        continue
                
                logger.info(
                    "web_search ok | query_len=%d | results=%d",
                    len(query), len(results)
                )
                
                return {
                    "ok": True,
                    "query": query,
                    "results": results,
                    "error": ""
                }
    
    except asyncio.TimeoutError:
        logger.warning("web_search timeout | query_len=%d", len(query))
        return {
            "ok": False,
            "query": query,
            "results": [],
            "error": "Search timeout"
        }
    except Exception as e:
        logger.exception(
            "web_search failed | query_len=%d | err_type=%s",
            len(query), type(e).__name__
        )
        return {
            "ok": False,
            "query": query,
            "results": [],
            "error": f"Search error: {type(e).__name__}"
        }


async def fetch_webpage(url: str, max_chars: int = 3000) -> dict:
    """抓取网页内容。
    
    Args:
        url: 网页 URL
        max_chars: 返回的最大字符数
        
    Returns:
        {
            "ok": bool,
            "url": str,
            "title": str,
            "content": str (纯文本，截断到 max_chars),
            "error": str (if ok=False)
        }
    """
    if not url or not isinstance(url, str):
        return {"ok": False, "url": "", "title": "", "content": "", "error": "Invalid URL"}
    
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(url, headers=_headers, ssl=False, allow_redirects=True) as resp:
                if resp.status != 200:
                    logger.warning(
                        "fetch_webpage failed | url_len=%d | status=%d",
                        len(url), resp.status
                    )
                    return {
                        "ok": False,
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": f"HTTP {resp.status}"
                    }
                
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                
                # 提取标题
                title = ""
                if soup.title:
                    title = soup.title.string or ""
                
                # 移除脚本和样式
                for script in soup(["script", "style"]):
                    script.decompose()
                
                # 提取文本
                text = soup.get_text(separator="\n", strip=True)
                
                # 清理多余空白
                lines = [line.strip() for line in text.split("\n") if line.strip()]
                content = "\n".join(lines)
                
                # 截断到 max_chars
                if len(content) > max_chars:
                    content = content[:max_chars] + "..."
                
                logger.info(
                    "fetch_webpage ok | url_len=%d | content_len=%d",
                    len(url), len(content)
                )
                
                return {
                    "ok": True,
                    "url": url,
                    "title": title[:100],
                    "content": content,
                    "error": ""
                }
    
    except asyncio.TimeoutError:
        logger.warning("fetch_webpage timeout | url_len=%d", len(url))
        return {
            "ok": False,
            "url": url,
            "title": "",
            "content": "",
            "error": "Fetch timeout"
        }
    except Exception as e:
        logger.exception(
            "fetch_webpage failed | url_len=%d | err_type=%s",
            len(url), type(e).__name__
        )
        return {
            "ok": False,
            "url": url,
            "title": "",
            "content": "",
            "error": f"Fetch error: {type(e).__name__}"
        }


async def search_and_summarize(query: str) -> str:
    """搜索并生成摘要（供主脑直接调用）。
    
    返回格式化的搜索结果摘要，便于主脑进一步处理。
    """
    result = await search_web(query, max_results=3)
    
    if not result["ok"]:
        return f"搜索失败：{result['error']}"
    
    if not result["results"]:
        return f"未找到关于「{query}」的搜索结果"
    
    summary = f"关于「{query}」的搜索结果：\n\n"
    for i, item in enumerate(result["results"], 1):
        summary += f"{i}. {item['title']}\n"
        summary += f"   URL: {item['url']}\n"
        if item['snippet']:
            summary += f"   摘要: {item['snippet']}\n"
        summary += "\n"
    
    return summary.strip()
