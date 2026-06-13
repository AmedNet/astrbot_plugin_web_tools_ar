"""Bing 搜索引擎工具 — 通过 DrissionPage 驱动 Edge 实现搜索"""
import asyncio
import logging
import os
import random
import re
import threading
import platform
import time
from pathlib import Path
from urllib.parse import quote

from DrissionPage import ChromiumPage, ChromiumOptions

# AstrBot 集成
try:
    from astrbot.api import FunctionTool, logger
    from astrbot.api.event import AstrMessageEvent
    from dataclasses import dataclass, field
    from mcp.types import CallToolResult, TextContent
    _ASTRBOT_AVAILABLE = True
except ImportError:
    FunctionTool = object
    logger = logging.getLogger("bing_search")
    _ASTRBOT_AVAILABLE = False

    def dataclass(cls):
        return cls

    def field(**kwargs):
        return None

_page_lock = threading.Lock()


def _find_edge_path() -> str:
    system = platform.system()
    if system == "Windows":
        candidates = [
            Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
            Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        ]
        for p in candidates:
            if p.exists():
                return str(p)
    elif system == "Darwin":
        p = Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
        if p.exists():
            return str(p)
    elif system == "Linux":
        candidates = [
            Path("/usr/bin/microsoft-edge"),
            Path("/usr/bin/microsoft-edge-stable"),
        ]
        for p in candidates:
            if p.exists():
                return str(p)
    raise FileNotFoundError("未找到 Edge 浏览器，请确认已安装 Microsoft Edge")


def _get_port():
    """用 PID + 随机数生成独立端口，避免并发冲突"""
    pid = os.getpid()
    rand = random.randint(100, 999)
    port = 10000 + (pid % 1000) * 10 + rand % 10
    return port


def _create_page():
    port = _get_port()
    co = ChromiumOptions()
    co.set_browser_path(_find_edge_path())
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_local_port(port)
    with _page_lock:
        page = ChromiumPage(addr_or_opts=co)
    logger.info(f"bing_search: DrissionPage(Edge) ready (port={port})")
    return page


def search_bing(keyword: str, max_results: int = 8) -> str:
    t_start = time.time()
    page = None
    try:
        page = _create_page()
        logger.info(f"bing_search: create page {time.time() - t_start:.2f}s")

        search_url = f"https://www.bing.com/search?q={quote(keyword)}&mkt=en-US"
        page.get(search_url)
        logger.info(f"bing_search: page.get {time.time() - t_start:.2f}s")

        # 等实际搜索结果元素渲染完成（最多等 3 秒，渲染后立即返回）
        page.wait.ele_displayed('.b_algo', timeout=3)
        logger.info(f"bing_search: .b_algo ready {time.time() - t_start:.2f}s")

        all_results = []
        # 从 #b_results 容器遍历 li 元素提取
        for li in page.eles('.b_algo', timeout=1):
            try:
                h2 = li.ele('tag:h2')
                a_tag = h2.ele('tag:a') if h2 else None
                if not h2 or not a_tag:
                    continue
                title = h2.text.strip()
                url = a_tag.attr('href') or ''
                if not title or len(title) < 3:
                    continue
                snippet = ''
                try:
                    ps = li.eles('tag:p')
                    if ps:
                        snippet = ps[0].text.strip()
                except Exception:
                    pass
                all_results.append({'title': title, 'url': url, 'snippet': snippet})
            except Exception:
                continue

        logger.info(f"bing_search: extracted {len(all_results)} results {time.time() - t_start:.2f}s")
        seen = set()
        unique_results = []
        for r in all_results:
            if r['title'] not in seen:
                seen.add(r['title'])
                unique_results.append(r)
        all_results = unique_results

        lines = [f'Bing 搜索结果: "{keyword}"', ""]
        found = 0
        for r in all_results:
            if found >= max_results:
                break
            found += 1
            lines.append(f"{found}. {r['title']}")
            lines.append(f"   {r['url']}")
            if r.get('snippet'):
                lines.append(f"   {r['snippet']}")
            lines.append("")

        if found == 0:
            return f'未找到关于 "{keyword}" 的搜索结果'
        return '\n'.join(lines)

    except Exception as e:
        logger.error(f"bing_search 出错 ({keyword}): {e}")
        return f'搜索 "{keyword}" 失败: {str(e)}'
    finally:
        if page:
            try:
                page.quit()
            except Exception:
                pass


# ============================================================
# AstrBot FunctionTool 集成
# ============================================================
if _ASTRBOT_AVAILABLE:

    @dataclass
    class BingSearchTool(FunctionTool):
        name: str = "web_search"
        description: str = "并发搜索 Bing，keywords 传多个关键词可同时搜。支持 site: inurl: intitle: filetype: 及引号精确匹配等搜索语法。"
        parameters: dict = field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "搜索关键词列表，可同时传入多个关键词并发搜索",
                    },
                },
                "required": ["keywords"],
            }
        )

        search_timeout: int = 60

        async def run(self, event: AstrMessageEvent, keywords: list[str]):
            if not keywords:
                return CallToolResult(
                    content=[TextContent(type="text", text="未提供搜索关键词")]
                )

            async def _search_with_timeout(kw: str):
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(search_bing, kw, 20),
                        timeout=self.search_timeout if self.search_timeout > 0 else None
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"bing_search: 搜索 '{kw}' 超时 ({self.search_timeout}s)")
                    return f'搜索 "{kw}" 超时（超过 {self.search_timeout} 秒）'

            tasks = [_search_with_timeout(kw) for kw in keywords]
            results = await asyncio.gather(*tasks)

            combined = "\n---\n".join(results)
            return CallToolResult(
                content=[TextContent(type="text", text=combined)]
            )

# ============================================================
# 命令行测试入口
# ============================================================
if __name__ == "__main__":
    import sys
    print("=" * 60)
    print("Bing 搜索测试")
    print("=" * 60)

    if len(sys.argv) > 1 and sys.argv[1] == "--concurrent":
        keywords = ["apple watch ultra 3 weight spec", "Python 入门教程", "今天天气", "比特币价格"]
        print(f"\n>>> 并发搜索 {len(keywords)} 个关键词: {keywords}\n")
        t0 = time.time()

        async def run_concurrent():
            tasks = [asyncio.to_thread(search_bing, kw, 3) for kw in keywords]
            results = await asyncio.gather(*tasks)
            for kw, result in zip(keywords, results):
                print(f"\n{'='*60}")
                print(f"关键词: {kw}")
                print(f"{'='*60}")
                print(result)

        asyncio.run(run_concurrent())
        print(f"\n⏱ 并发总耗时: {time.time() - t0:.2f}s")
    else:
        keyword = sys.argv[1] if len(sys.argv) > 1 else " Donald Trump president 2026 inauguration"
        print(f"\n>>> 开始搜索: {keyword}\n")
        t0 = time.time()
        result = search_bing(keyword, max_results=5)
        print(result)
        print(f"\n⏱ 搜索耗时: {time.time() - t0:.2f}s")

    print(f"\n{'='*60}")
    print("✅ 测试完成")