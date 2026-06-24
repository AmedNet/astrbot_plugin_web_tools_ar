"""Bing 搜索引擎工具 — 通过 DrissionPage 驱动 Edge 实现搜索（支持真正并发）"""
import asyncio
import logging
import re
import threading
import platform
import time
import json
import tempfile
import shutil
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse, quote

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

# ──────────────────────────────────────────────────────
# 2. DrissionPage 默认 userData 目录
# ──────────────────────────────────────────────────────
# DrissionPage 会在系统临时目录下创建 DrissionPage/userData 存放浏览器数据
# 每次运行后都要清理这个目录，防止磁盘被撑爆
_DRISSIONPAGE_USERDATA = os.path.join(tempfile.gettempdir(), "DrissionPage", "userData")


@lru_cache(maxsize=1)
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
# 3. 并发上限控制 — 全局信号量
# ──────────────────────────────────────────────────────
_MAX_CONCURRENT_BROWSERS = 4
_browser_semaphore = threading.Semaphore(_MAX_CONCURRENT_BROWSERS)


def search_bing(keywords: list, max_results: int = 8, timeout: int = 45) -> str:
    """
    执行Bing搜索（每次独立浏览器，通过信号量控制并发数）
    支持空结果自动重试（最多3次）
    """
    if not keywords:
        return json.dumps({"error": "未提供关键词"}, ensure_ascii=False)

    keyword = keywords[0] if isinstance(keywords, list) else keywords

    # 重试次数配置
    max_retries = 3
    retry_count = 0

    # 使用全局信号量 — 这样才真正限流
    with _browser_semaphore:
        # 创建临时用户数据目录，用完后删除，避免 /tmp 下 userdata 堆积
        user_data_dir = None
        port = _alloc_port()
        co = ChromiumOptions()
        co.set_browser_path(_find_edge_path())
        co.set_argument("--disable-blink-features=AutomationControlled")
        co.set_local_port(port)

        # 用 --user-data-dir 替代 set_user_data_path，更底层更可靠
        try:
            user_data_dir = tempfile.mkdtemp(prefix="bing_search_")
            co.set_argument(f"--user-data-dir={user_data_dir}")
        except Exception:
            pass

        page = ChromiumPage(addr_or_opts=co)
        try:
            # 外层循环：处理空结果重试
            while retry_count < max_retries:
                # 直接访问 Bing 首页，然后在搜索框中输入关键词
                page.get("https://www.bing.com", timeout=timeout)

                # 等待搜索框出现并输入关键词
                try:
                    search_box = page.ele('#sb_form_q', timeout=5)
                    if search_box:
                        search_box.clear()
                        search_box.input(keyword)
                        # 点击搜索按钮
                        search_btn = page.ele('#search_icon', timeout=2)
                        if search_btn:
                            search_btn.click()
                        else:
                            # 如果没有搜索按钮，直接回车
                            search_box.send_keys('\n')
                        page.wait.doc_loaded()
                    else:
                        # 如果找不到搜索框，回退到 URL 方式
                        encoded_kw = quote(keyword, safe='')
                        search_url = f"https://www.bing.com/search?q={encoded_kw}&count={max_results}"
                        page.get(search_url, timeout=timeout)
                except Exception:
                    # 如果搜索框方式失败，回退到 URL 方式
                    encoded_kw = quote(keyword, safe='')
                    search_url = f"https://www.bing.com/search?q={encoded_kw}&count={max_results}"
                    page.get(search_url, timeout=timeout)

                # 空白页重试：检测 #b_results 容器是否出现，最多重试 3 次
                for attempt in range(4):
                    try:
                        page.wait.ele_displayed('#b_results', timeout=3)
                        break  # 容器出现，跳出
                    except Exception:
                        if attempt < 3:
                            logging.warning(f"bing_search: 第 {attempt+1} 次空白页，重新加载...")
                            page.refresh()
                        else:
                            raise  # 3 次都空白页，抛异常走到外层 except

                # 滚动加载更多结果 — 等待 .b_algo 数量确实增加了才继续
                for _ in range(3):
                    # 滚动前记录当前结果数
                    try:
                        before = len(page.eles('#b_results .b_algo'))
                    except Exception:
                        before = 0

                    page.scroll.down(500)

                    # 轮询等待新结果出现（最多 1.5 秒）
                    for _ in range(3):
                        try:
                            after = len(page.eles('#b_results .b_algo'))
                        except Exception:
                            after = 0
                        if after > before:
                            break  # 新结果到了
                        time.sleep(0.5)

                # 精准定位搜索结果容器
                b_results = page.ele('#b_results', timeout=1)
                if b_results:
                    result_items = b_results.children('.b_algo', timeout=0.5) or b_results.children('li', timeout=0.5)
                else:
                    result_items = page.eles('.b_algo', timeout=1.5)

                results = []
                for li in result_items[:max_results]:
                    # 每条的 fallback 信息
                    fallback_text = ""
                    try:
                        fallback_text = li.text.strip()[:200]
                    except Exception:
                        pass

                    try:
                        h2 = li.ele('tag:h2', timeout=0.3)
                        if not h2:
                            # 没有 h2 → 用 fallback
                            if fallback_text and fallback_text not in ("", " "):
                                results.append({
                                    "title": fallback_text[:80],
                                    "url": "",
                                    "snippet": fallback_text,
                                    "date": "",
                                    "source": "",
                                    "rank": len(results) + 1
                                })
                            continue

                        a_tag = h2.ele('tag:a', timeout=0.3)
                        if not (a_tag and h2.text and a_tag.attr('href')):
                            # 没有链接 → 用 fallback
                            if fallback_text and fallback_text not in ("", " "):
                                results.append({
                                    "title": fallback_text[:80],
                                    "url": "",
                                    "snippet": fallback_text,
                                    "date": "",
                                    "source": "",
                                    "rank": len(results) + 1
                                })
                            continue

                        title = h2.text.strip()
                        url = a_tag.attr('href')

                        # 摘要和日期 - 合并两个 DOM 查询为一次
                        snippet = ''
                        # 先查 p 标签（Bing 标准结构）
                        p_tag = li.ele('tag:p', timeout=0.3)
                        if p_tag:
                            snippet = p_tag.text.strip()
                        else:
                            # 降级到 div
                            div_tag = li.ele('tag:div', timeout=0.3)
                            if div_tag:
                                snippet = re.sub(r'\s+', ' ', div_tag.text.strip())

                        # 日期：优先从摘要中正则提取（零 DOM 开销）
                        date = ''
                        if snippet:
                            dm = re.search(r'(\d{4}[-年]\d{1,2}[-月]\d{1,2}日?)', snippet)
                            if dm:
                                date = dm.group(1).replace('年', '-').replace('月', '-').replace('日', '')
                        if not date:
                            dt = li.ele('.news_dt', timeout=0.2)
                            if dt:
                                dm = re.search(r'(\d{4}-\d{2}-\d{2}|\d+年\d+月\d+日)', dt.text)
                                if dm:
                                    date = dm.group(1)

                        source = urlparse(url).netloc.replace('www.', '') if url else ''

                        results.append({
                            "title": title,
                            "url": url,
                            "snippet": snippet or fallback_text,
                            "date": date,
                            "source": source,
                            "rank": len(results) + 1
                        })
                    except Exception:
                        # 解析失败 → fallback 兜底
                        if fallback_text and fallback_text not in ("", " "):
                            results.append({
                                "title": fallback_text[:80],
                                "url": "",
                                "snippet": fallback_text,
                                "date": "",
                                "source": "",
                                "rank": len(results) + 1
                            })
                        else:
                            logging.error("bing_search: 解析单条结果失败且无 fallback", exc_info=True)
                        continue


                # 检查结果是否为空
                if results:
                    # 成功获取到结果，返回
                    response_data = {
                        "query": keyword,
                        "total_results": len(results),
                        "results": results
                    }
                    return json.dumps(response_data, ensure_ascii=False, indent=2)
                else:
                    # 结果为空，增加重试计数，继续循环
                    retry_count += 1
                    logging.warning(f"bing_search: 搜索 '{keyword}' 返回空结果，第 {retry_count} 次重试...")
                    # 等待片刻再重试，降低被反爬的风险
                    time.sleep(1)

            # 所有重试均失败，返回空结果并附加提示
            return json.dumps({
                "query": keyword,
                "total_results": 0,
                "results": [],
                "message": f"多次重试（{max_retries}次）后仍未获得搜索结果"
            }, ensure_ascii=False)

        except Exception as e:
            logging.error(f"bing_search: 搜索 '{keyword}' 失败", exc_info=True)
            return json.dumps({
                "error": f"搜索失败: {type(e).__name__}: {str(e)}",
                "query": keyword
            }, ensure_ascii=False)

        finally:
            # 无论成功或失败，都关闭浏览器
            try:
                page.quit()
            except Exception:
                pass
            # 清理我们自己创建的临时目录
            if user_data_dir:
                try:
                    shutil.rmtree(user_data_dir, ignore_errors=True)
                except Exception:
                    pass
            # ⚠️ 额外清理 DrissionPage 默认 userData 目录（双重保险）
            try:
                if os.path.isdir(_DRISSIONPAGE_USERDATA):
                    shutil.rmtree(_DRISSIONPAGE_USERDATA, ignore_errors=True)
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
    print("Bing 搜索测试（支持并发）")
    print("=" * 60)

    if len(sys.argv) > 1 and sys.argv[1] == "--concurrent":
        keywords = ["apple watch ultra 3 weight spec", "入门教程", "墙云", "原彩显示"]
        print(f"\n>>> 并发搜索 {len(keywords)} 个关键词: {keywords}\n")
        t0 = time.time()

        async def run_concurrent():
            # 每个关键词在一个独立线程中执行，asyncio.to_thread 会分配线程
            tasks = [asyncio.to_thread(search_bing, kw, 10) for kw in keywords]
            results = await asyncio.gather(*tasks)
            for kw, result in zip(keywords, results):
                print(f"\n{'='*60}")
                print(f"关键词: {kw}")
                print(f"{'='*60}")
                print(result)

        asyncio.run(run_concurrent())
        print(f"\n⏱ 并发总耗时: {time.time() - t0:.2f}s")
    else:
        keyword = sys.argv[1] if len(sys.argv) > 1 else "git使用教程"
        print(f"\n>>> 开始搜索: {keyword}\n")
        t0 = time.time()
        result = search_bing(keyword, max_results=20)
        print(result)
        print(f"\n⏱ 搜索耗时: {time.time() - t0:.2f}s")

    print(f"\n{'='*60}")
    print("✅ 测试完成")