"""AstrBot Web Tools 插件

为 AstrBot LLM 提供 Bing 搜索和网页抓取能力。
注册两个 FunctionTool：
  - web_search: Bing 联网搜索（并发）
  - web_fetch:  网页内容抓取（并发）
"""

try:
    from astrbot.api.star import Context, Star, register
    from astrbot.api import AstrBotConfig, logger
    _ASTRBOT_AVAILABLE = True
except ImportError:
    _ASTRBOT_AVAILABLE = False
    logger = None

from .tools.bing_search import BingSearchTool
from .tools.web_fetch import WebFetchTool


if _ASTRBOT_AVAILABLE:

    @register("BingWebSearch", "APOLI", "LLM 联网搜索与网页抓取工具", "1.0.0")
    class WebTools(Star):
        def __init__(self, context: Context, config: AstrBotConfig = None):
            super().__init__(context)
            # 从配置读取超时时间（默认 60 秒）
            search_timeout = 60
            if config:
                search_timeout = config.get("search_timeout", 60)

            # 注册 LLM 函数工具
            self.context.add_llm_tools(
                BingSearchTool(search_timeout=search_timeout),
                WebFetchTool(search_timeout=search_timeout),
            )
            self.config = config

        async def initialize(self):
            pass

        async def terminate(self):
            pass