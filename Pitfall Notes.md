

### 🗓️ 2026-06-12

#### ✨ 新增

| 功能 | 说明 |
|------|------|
| **Bing 搜索语法支持** | `site:` `inurl:` `intitle:` `allintitle:` `filetype:` `""`(精确) `-`(排除) `*`(模糊) 及日期范围 |
| **并发搜索/抓取** | `web_search` 和 `web_fetch` 均支持一次传入多个关键词/URL 并发执行 |
| **`skills/SKILL.md`** | 可选的 Skill 文件，供用户手动启用获取工具使用指南 |
| **`requirements.txt`** | 新增 `webdriver-manager` 自动管理浏览器驱动 |

#### 🚀 优化

| 项目 | 说明 |
|------|------|
| **搜索按钮点击** | 优先使用 JS 点击，提高无头模式兼容性 |
| **Token 优化** | 工具 description 精简、搜索结果默认返回 3 条、内容截断上限 1500 字符 |
| **项目结构** | 全部功能模块化到 `handlers/` 和 `tools/` 目录 |

#### 🐛 修复

| 问题 | 解决方案 |
|------|----------|
| `event.loop` 不存在 | 改用 `asyncio.to_thread()` |
| 搜索图标 `element not interactable` | JS 点击优先 + 三层后备 |

---

### 🗓️ 2026-06-12 (v2 完整重写)

#### 🔄 从 Selenium → DrissionPage

| 项目 | 变更 |
|------|------|
| **底层驱动** | 完全替换 Selenium 为 DrissionPage + ChromiumPage API |
| **依赖变更** | 移除 `selenium`、`webdriver-manager`，替换为 `DrissionPage>=4.1.0` |
| **代码简化** | API 更简洁，代码量减少约 50% |

#### 🔄 移除无头模式

从 headless 模式改为正常浏览器窗口，解决无头渲染额外开销导致的搜索缓慢问题。同时移除了所有冗余 `time.sleep`，改用 `ele_displayed()` 条件等待——页面内容渲染完成后立即提取。

| 变更项 | 之前 | 之后 |
|--------|------|------|
| **浏览器模式** | `co.headless(True)` 无头模式 | 正常显示浏览器窗口 |
| **等待机制** | `time.sleep(3)` + `time.sleep(2)` 硬编码等待 | `page.wait.ele_displayed('#b_results', timeout=15)` 条件等待 |
| **端口管理** | `_get_port()` + 临时目录 | 轻量场景无需隔离，重量场景保留 `set_local_port` |

#### 📈 搜索结果数量

| 变更项 | 之前 | 之后 |
|--------|------|------|
| **每搜索返回** | 最多 3 条结果 | 最多 20 条结果（LLM 自行判断） |
| **英文过滤** | 过滤纯字母/符号短标题（误杀 "Apple Inc." 等） | 完全移除，所有结果如实返回 |
| **地区偏好** | Bing 自动重定向到 cn.bing.com | `&mkt=en-US` 强制英文市场，结果质量与测试一致 |

#### 🐛 修复记录

| 问题 | 症状 | 根因 | 解决方案 |
|------|------|------|----------|
| **端口冲突 Handshake 404** | `Handshake status 404 Not Found`，浏览器启动失败 | 并发创建多个 `ChromiumPage` 实例时调试端口冲突 | `_get_port()` 用 PID+随机数生成独立端口 |
| **元素失效** | `元素对象已失效。可能是页面整体刷新` | Windows 上 `.click()` 触发整页刷新导致旧引用丢失 | 改用 `page.run_js("...click()")` JS 点击 |
| **搜索结果不相关** | 搜索"A股"返回"字母A的百科" | cn.bing.com 全局 `.b_algo` 匹配到非搜索结果容器元素 | 从 `#b_results` 容器内用 `children()` 逐层精确遍历 |
| **页面内容为空** | `web_fetch` 返回"页面内容为空" | 端口冲突导致页面未加载，`body.text` = "" | 端口隔离 + 检测到 CSS/JS 代码时自动降级 JS `innerText` |
| **等待 API 不兼容** | `'ChromiumPageWaiter' object has no attribute 'load_complete'` | DrissionPage 4.x 无 `load_complete()` 方法 | 改用 `page.wait.doc_loaded()` |

#### 🚀 稳定性优化

| 优化项 | 说明 |
|--------|------|
| **端口独立** | `_get_port()` 基于 PID + 随机数生成端口，支持并发 |
| **精确容器提取** | `page.ele('#b_results').children()` 逐层遍历，不依赖 CSS 类名 |
| **文本净化** | 检测到 `<style>`/`<script>` 内容时自动降级 JS `innerText` |
| **导入保护** | `try/except ImportError` 包裹 AstrBot API，支持独立测试 |
| **超时配置** | `_conf_schema.json` 注册 `search_timeout`，WebUI 可调（默认 60s） |

---

### 🗓️ 2026-06-12 (v2.1 速度优化)

#### 🔍 诊断发现的根因

| 症状 | 诊断方式 | 发现 |
|------|----------|------|
| 搜索 52 秒，但页面圈早就转完了 | 逐行计时脚本 `diagnose_bing.py` | `#b_results` DOM 骨架就绪仅 1.6s，但 Bing JS 渲染的 `.b_algo` 结果元素未出现 |
| 并发搜索报 `与页面的连接已断开` | 日志排查 | 两个 Edge 实例争用同一调试端口 |

#### ⚡ 速度提升

| 阶段 | 优化前 | 优化后 | 原因 |
|------|--------|--------|------|
| **搜索总耗时** | 52.40s | **2.56s** | 等对元素 + 端口隔离 |
| **page.get() 加载** | 等待完整 load（含广告/追踪器） | 等待 DOM 就绪 + `.b_algo` 渲染 | `ele_displayed('#b_results')` 空的 →
等待 `ele_displayed('.b_algo', timeout=3)` 真正的结果元素 |
| **并发端口冲突** | 共享端口导致连接断开 | `_get_port()` PID+随机数独立端口 | 各实例互不干扰 |

#### 📊 耗时分布（优化后单次搜索）

```
create page: 1.20s     ← Edge 浏览器启动
page.get:    2.12s     ← 页面加载
.b_algo ready: 2.34s  ← 等 JS 渲染搜索结果（渲染后立即返回）
extracted:   2.56s     ← 提取完成
```
