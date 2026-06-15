# Web Skill
你拥有联网搜索功能,遇到不知道的问题请合理使用以下工具:
工具：`web_search`（搜索）、`web_fetch`（抓取）。

## 原则

1. **并发优先** — 一次传入多个关键词/URL
2. **search 搜索，fetch 看详情** — 先搜结果，再抓链接

## web_search

- **用途**：Bing 并发搜索
- **参数**：`keywords`（字符串数组）

**搜索语法：**

| 语法 | 说明 | 示例 |
|------|------|------|
| 直接输入 | 普通搜索 | `台风` |
| `-` | 排除 | `苹果 -手机` |
| `""` | 精确匹配 | `"气候变化"` |
| `*` | 模糊 | `中国*发展` |
| `filetype:` | 文件类型 | `报告 filetype:pdf` |
| `日期范围` | 限定时间 | `AI 2024..2026` |
| `site:` | 限定网站 | `台风 site:news.cn` |
| `intitle:` | 标题包含 | `intitle:地震` |
| `allintitle:` | 标题全含 | `allintitle:人工智能` |
| `inurl:` | URL含 | `inurl:login` |

**示例：**
```json
{"keywords": ["台风 \"最新动态\" site:news.cn", "allintitle:人工智能 2025..2026", "比特币 -区块链 filetype:pdf"]}
```

## web_fetch

- **用途**：并发抓取页面文本
- **参数**：`urls`（字符串数组）

**示例：**
```json
{"urls": ["https://example.com/1", "https://example.com/2"]}
```

## 流程

1. 想好关键词 → 一次性 `web_search`
2. 看结果 → 提取有价值的链接
3. 一次性 `web_fetch` 抓详情
4. 综合回答
