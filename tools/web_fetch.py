"""网页抓取工具 — 通过 DrissionPage 驱动 Edge 获取页面文本"""
import asyncio
import logging
import os
import random
import re
import threading
import platform
import tempfile
import time
from pathlib import Path

from DrissionPage import ChromiumPage, ChromiumOptions

# AstrBot 集成（仅在插件环境中可用）
try:
    from astrbot.api import FunctionTool, logger
    from astrbot.api.event import AstrMessageEvent
    from dataclasses import dataclass, field
    from mcp.types import CallToolResult, TextContent
    _ASTRBOT_AVAILABLE = True
except ImportError:
    FunctionTool = object
    logger = logging.getLogger("web_fetch")
    _ASTRBOT_AVAILABLE = False

    def dataclass(cls):
        return cls

    def field(**kwargs):
        return None

_page_lock = threading.Lock()


def _get_port():
    """用 PID + 随机数生成独立端口，避免并发冲突"""
    pid = os.getpid()
    rand = random.randint(100, 999)
    port = 10000 + (pid % 1000) * 10 + rand % 10
    return port


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


def _create_page():
    port = _get_port()
    co = ChromiumOptions()
    co.set_browser_path(_find_edge_path())
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_local_port(port)
    with _page_lock:
        page = ChromiumPage(addr_or_opts=co)
    logger.info(f"web_fetch: DrissionPage(Edge) ready (port={port})")
    return page


def _extract_text(page) -> str:
    """提取页面文本（与 bing_search.py 一致的方式）"""
    # 获取标题
    title = ""
    try:
        title_elem = page.ele('tag:title', timeout=3)
        if title_elem:
            title = title_elem.text.strip()
    except Exception:
        pass

    # 获取 body 文本
    text = ""
    try:
        body = page.ele('tag:body', timeout=5)
        if body:
            text = body.text.strip()
    except Exception:
        pass

    if not text or any(tag in text[:200] for tag in ['<style', '<script', 'function(']):
        # 尝试 document.innerText（自动过滤 script/style 内容）
        try:
            raw = page.run_js("document.body?.innerText || document.body?.textContent || ''")
            if raw:
                text = raw.strip()
        except Exception:
            pass

    if not text:
        return "页面内容为空"

    # 清理多余空行
    text = re.sub(r'\n\s*\n', '\n\n', text)

    if title:
        text = f"标题: {title}\n\n{text}"

    return text


def fetch_page(url: str, timeout: int = 10) -> str:
    """获取指定 URL 的页面文本内容"""
    page = None
    try:
        page = _create_page()
        page.get(url, timeout=timeout)
        page.wait.doc_loaded()
        # 等待 body 渲染完成
        try:
            page.wait.ele_displayed('tag:body', timeout=10)
        except Exception:
            pass

        text = _extract_text(page)

        return text

    except Exception as e:
        logger.error(f"web_fetch 出错 ({url}): {e}")
        return f"获取页面失败: {str(e)}"
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
    class WebFetchTool(FunctionTool):
        name: str = "web_fetch"
        description: str = "并发抓取多个 URL 的页面文本内容。"
        parameters: dict = field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要访问的页面 URL 列表（完整链接，含 https://），可同时传入多个 URL 并发抓取",
                    },
                },
                "required": ["urls"],
            }
        )

        search_timeout: int = 60

        async def run(self, event: AstrMessageEvent, urls: list[str]):
            if not urls:
                return CallToolResult(
                    content=[TextContent(type="text", text="未提供 URL")]
                )

            async def _fetch_with_timeout(url: str):
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(fetch_page, url, 10),
                        timeout=self.search_timeout if self.search_timeout > 0 else None
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"web_fetch: 抓取 '{url}' 超时 ({self.search_timeout}s)")
                    return f"抓取超时（超过 {self.search_timeout} 秒）"

            tasks = [_fetch_with_timeout(url) for url in urls]
            results = await asyncio.gather(*tasks)

            parts = []
            for url, text in zip(urls, results):
                parts.append(f"=== {url} ===\n{text}")

            combined = "\n\n".join(parts)
            return CallToolResult(
                content=[TextContent(type="text", text=combined)]
            )

# ============================================================
# 命令行测试入口
# ============================================================
if __name__ == "__main__":
    import sys
    print("=" * 60)
    print("网页抓取测试")
    print("=" * 60)

    if len(sys.argv) > 1:
        urls = sys.argv[1:]
    else:
        urls = [
            "https://www.sina.com.cn",
            "https://news.qq.com",
            "https://www.baidu.com",
        ]

    print(f"\n>>> 开始抓取 {len(urls)} 个页面: {urls}\n")

    async def run():
        tasks = [asyncio.to_thread(fetch_page, url, 10) for url in urls]
        results = await asyncio.gather(*tasks)
        for url, text in zip(urls, results):
            print(f"\n{'='*60}")
            print(f"URL: {url}")
            print(f"{'='*60}")
            print(text[:500])
            print("...")

    asyncio.run(run())
    print(f"\n{'='*60}")
    print("✅ 测试完成")