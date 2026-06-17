"""网页抓取工具 — 通过 DrissionPage 驱动 Edge 获取页面文本（支持真正并发）"""
import asyncio
import logging
import time
import re
import threading
import platform
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


# ──────────────────────────────────────────────────────
# 1. 端口分配：全局计数器保证每个线程拿到唯一端口
# ──────────────────────────────────────────────────────
_next_port = 10000
_port_lock = threading.Lock()

def _alloc_port() -> int:
    """线程安全地分配一个唯一端口"""
    global _next_port
    with _port_lock:
        port = _next_port
        _next_port += 1
        # 防止溢出，循环使用
        if _next_port > 60000:
            _next_port = 10000
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


# ──────────────────────────────────────────────────────
# 2. 并发上限控制
# ──────────────────────────────────────────────────────
# 同时运行的 Chromium 实例上限，防止瞬间启动过多进程拖垮系统
_MAX_CONCURRENT_BROWSERS = 4
_browser_semaphore = threading.Semaphore(_MAX_CONCURRENT_BROWSERS)


import trafilatura

def _extract_text(page) -> str:
    """使用 trafilatura 提取干净的页面正文，并补充标题"""
    title = ""
    try:
        title_elem = page.ele('tag:title', timeout=3)
        if title_elem:
            title = title_elem.text.strip()
    except Exception:
        pass

    # 核心改动：获取页面HTML，用 trafilatura 提取正文
    try:
        # 获取完整的页面HTML
        html_content = page.html
        # trafilatura 自动识别并提取正文，返回纯文本
        extracted_text = trafilatura.extract(html_content, include_comments=False, include_tables=True)
        
        if extracted_text:
            # 如果成功提取，清理多余空行并组合标题
            cleaned_text = re.sub(r'\n\s*\n', '\n\n', extracted_text.strip())
            if title:
                return f"标题: {title}\n\n{cleaned_text}"
            return cleaned_text
        else:
            # 如果 trafilatura 提取失败（比如页面结构特殊），回退到原有逻辑
            logging.warning("trafilatura 提取为空，回退到原有提取方式")
            return _fallback_extract_text(page, title)
            
    except Exception as e:
        logging.error(f"trafilatura 提取出错: {e}，回退到原有方式")
        return _fallback_extract_text(page, title)

def _fallback_extract_text(page, title: str) -> str:
    """原有的提取逻辑作为备用方案"""
    text = ""
    try:
        body = page.ele('tag:body', timeout=5)
        if body:
            text = body.text.strip()
    except Exception:
        pass

    if not text or any(tag in text[:200] for tag in ['<style', '<script', 'function(']):
        try:
            raw = page.run_js("document.body?.innerText || document.body?.textContent || ''")
            if raw:
                text = raw.strip()
        except Exception:
            pass

    if not text:
        return "页面内容为空"

    text = re.sub(r'\n\s*\n', '\n\n', text)
    if title:
        text = f"标题: {title}\n\n{text}"
    return text


# ──────────────────────────────────────────────────────
# 3. 核心抓取函数（无全局锁，线程安全，受信号量控制）
# ──────────────────────────────────────────────────────
def fetch_page(url: str, timeout: int = 10, retries: int = 3) -> str:
    """
    获取指定 URL 的页面文本内容（每次独立浏览器，通过信号量控制并发数）
    """
    if not url:
        return "错误：未提供 URL"
    
    last_exception = None
    
    # 使用 with 自动获取和释放信号量
    with _browser_semaphore:
        for attempt in range(1, retries + 1):
            page = None
            try:
                # 分配唯一端口，避免冲突
                port = _alloc_port()
                co = ChromiumOptions()
                co.set_browser_path(_find_edge_path())
                co.set_argument("--disable-blink-features=AutomationControlled")
                co.set_local_port(port)
                page = ChromiumPage(addr_or_opts=co)
                logger.info(f"web_fetch: 获取页面 (url={url}, port={port})")
                
                page.get(url, timeout=timeout)
                page.wait.doc_loaded()
                # 等待 body 渲染完成
                try:
                    page.wait.ele_displayed('tag:body', timeout=10)
                except Exception:
                    pass

                # 滚动加载更多结果
                for _ in range(3):
                    page.scroll.down(2000)
                    time.sleep(0.5)

                text = _extract_text(page)
                return text
                
            except Exception as e:
                logger.warning(f"web_fetch 尝试 {attempt}/{retries} 失败 ({url}): {e}")
                last_exception = e
                if attempt < retries:
                    wait_time = 2 ** attempt  # 指数退避
                    time.sleep(wait_time)
                # 继续下一次
            finally:
                if page:
                    try:
                        page.quit()
                    except Exception:
                        pass
                    
    # 所有重试失败
    error_msg = f"获取页面失败，已重试{retries}次: {last_exception}"
    return error_msg


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
            "https://www.douban.com/group/topic/490787565/?_spm_id=MjE1OTQxMjA4&_i=1664378OkdmqjU",
            "https://www.nationalgeographic.com/travel/national-parks/article/lassen-volcanic-national-park",
        ]

    print(f"\n>>> 开始抓取 {len(urls)} 个页面: {urls}\n")

    async def run():
        tasks = [asyncio.to_thread(fetch_page, url, 10) for url in urls]
        results = await asyncio.gather(*tasks)
        for url, text in zip(urls, results):
            print(f"\n{'='*60}")
            print(f"URL: {url}")
            print(f"{'='*60}")
            print(text)

    asyncio.run(run())
    print(f"\n{'='*60}")
    print("✅ 测试完成")