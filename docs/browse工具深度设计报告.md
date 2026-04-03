# browse 工具深度设计报告

> 日期：2026-03-29
> 用途：为 browse 工具的实现提供完整设计方案
> 调研基础：Browser-Use, Agent-E, Stagehand, Playwright MCP, Vercel agent-browser, AgentOccam, D2Snap, Skyvern, Manus, Devin, OpenAI Operator, Anthropic Computer Use, Cloudflare Markdown for Agents, Beyond Browsing
> 前置：工具层调研报告.md, AgentSession设计.md

---

## 一、页面表示格式

### 1.1 各系统方案全景

| 系统 | 表示格式 | 核心机制 | 优势 | 劣势 |
|------|---------|---------|------|------|
| **Browser-Use** | 增强 DOM 树 | JS 注入遍历 DOM，可见性/可交互性过滤，编号索引 | 信息完整，索引精确 | Token 偏高 |
| **Agent-E** | 多模式 DOM（text_only/input_fields/all_fields）| DOM 蒸馏为 JSON，保留父子层级 | 按任务适配粒度 | 需要 LLM 自选模式 |
| **Stagehand** | AX tree + LLM 推理 | `observe()` 分析 AX tree，返回 XPath + 描述 | 语义化，自愈性强 | 每次交互需 LLM，慢 |
| **Playwright MCP** | YAML AX tree + ref | `ariaSnapshot()` 生成 YAML，交互元素分配 `[ref=eN]` | 标准化，语义清晰 | iframe/shadow DOM 缺失，token 偏高 |
| **Vercel agent-browser** | 精简 AX tree + @eN ref | AX tree 提取交互元素，200-400 tokens/页 | 极致 token 效率（省 93%） | 信息密度低，文本内容缺失 |
| **AgentOccam** | HTML/AX tree + Markdown 转换 | 合并冗余元素，表格/列表转 Markdown，保留层级 | 成功率 +161%（ICLR 2025） | 实现复杂 |
| **D2Snap** | Markdown + HTML 混合 | 内容转 Markdown，交互元素保留 HTML tag | 最佳准确率 73% | Token 中等偏高（7k-19k） |
| **Skyvern** | 截图 + Vision LLM | 截图送 Vision LLM，视觉识别元素 | 抗 DOM 变化 | 慢，昂贵，精度低（18.9%） |
| **Manus** | Markdown + 索引标签 | 页面自动转 Markdown，交互元素标注 `index[:]<tag>text</tag>` | 平衡信息量和效率 | 链接和图片在 Markdown 中被省略 |
| **Anthropic CU** | 截图 + 像素坐标 | 频繁截图，计算像素坐标 | 通用性极强 | 慢，成本高，GUI 定位 18.9% |
| **OpenAI CUA** | 截图 + 文本表示混合 | 截图 + 页面文本表示双通道 | 兼顾视觉和文本 | 已被 ChatGPT Agent 替代 |
| **Devin** | 截图 + 浏览器自动化 | 截图视觉感知 + WebDriver 协议 | 通用 | 细节不公开 |

### 1.2 量化对比数据

#### D2Snap 实验数据（arXiv:2508.04412，基于 GPT-4o + Online-Mind2Web）

| 表示方式 | 平均 Token | 成功率 | 信息保留 |
|---------|-----------|--------|---------|
| 原始截图 | 2,294 | 55% | 低（视觉模糊） |
| 标注截图（+ bounding box） | 3,754 | 65% | 中 |
| 原始 DOM（截断 8k） | 8,121 | 59% | 中（截断丢信息） |
| D2Snap 线性化 | 7,178 | 67% | 高 |
| D2Snap 下采样 DOM | 18,943 | **73%** | 最高 |

**D2Snap 核心结论：**
1. DOM 层级是最重要的特征——丢弃层级（扁平化）一致性地降低性能
2. 内容转 Markdown + 交互元素保留 HTML 是最佳混合格式
3. 视觉数据的边际收益很低——标注截图（65%）vs 纯文本标注截图（63%）几乎相同

#### AgentOccam 数据（ICLR 2025，WebArena）

| 优化步骤 | 平均 Token/步 | 成功率变化 |
|---------|-------------|-----------|
| 基线 Plain Agent | 3,376 | 基准 |
| + 观察空间优化 | 2,891 | **+161%** |
| + 选择性历史回放 | 3,051 | 额外 +15.8% |

#### Cloudflare Markdown for Agents 数据

| 格式 | Token 数 | 节省比例 |
|------|---------|---------|
| 原始 HTML | 16,180 | 基准 |
| 转换后 Markdown | 3,150 | **-80%** |

#### 其他关键数据

- Visual Confused Deputy：56.7% 的 CUA agent 点击打到错误目标
- GUI 定位准确率仅 18.9%（ScreenSpot-Pro benchmark）
- Beyond Browsing（ACL 2025）：混合 agent 38.9% vs 纯浏览 14.8% vs 纯 API 29.2%
- Vercel agent-browser：snapshot 200-400 tokens vs Playwright MCP 数千 tokens（省 93%）

### 1.3 关键发现汇总

1. **文本优于视觉**：所有量化证据一致表明，结构化文本表示优于截图。截图方案（Skyvern, Anthropic CU）的准确率系统性低于文本方案。
2. **层级必须保留**：D2Snap 和 AgentOccam 均证实，保留 DOM 层级是性能的关键因素。扁平化一致降低性能。
3. **Markdown + HTML 混合最佳**：D2Snap 73% 最高分来自"内容转 Markdown + 交互元素保留 HTML"。
4. **信息密度 > 信息完整**：Vercel agent-browser 证明极致压缩（200-400 tokens）能保持功能。AgentOccam 证明观察空间优化比架构改进效果高 161%。
5. **业界趋同**：Manus、Browser-Use、Playwright MCP、Vercel agent-browser 全部走向"结构化文本 + 编号索引"模式，无一使用纯截图。

### 1.4 推荐方案：Markdown + HTML 混合 + 索引标签

**设计原则：**
- 静态文本内容转 Markdown（标题、段落、列表、表格）——减 80% token
- 交互元素保留 HTML tag 并加编号——精确定位
- 保留层级结构——D2Snap 证实最重要特征
- 视口过滤 + 列表截断——控制 token 总量

**格式规范：**

```
=== Page: {title} ===
URL: {current_url}
Status: {http_status}

--- Content ---
# Main Heading

Some paragraph text describing the page content.

- List item one
- List item two

| Column A | Column B |
|----------|----------|
| Data 1   | Data 2   |

<nav>
  [1]<a href="/home">Home</a>
  [2]<a href="/explore">Explore</a>
  [3]<a href="/search">Search</a>
</nav>

<section class="results">
  ## Search Results

  <div class="card">
    ### Card Title
    Author: John Doe | Views: 1,234
    [4]<a href="/pen/abc123">View Pen</a>
    [5]<button>Like</button>
  </div>

  <div class="card">
    ### Another Card
    Author: Jane Smith | Views: 5,678
    [6]<a href="/pen/def456">View Pen</a>
    [7]<button>Like</button>
  </div>

  ... (18 more cards, 20 total)
</section>

[8]<input type="text" placeholder="Search pens..." />
[9]<button type="submit">Search</button>
[10]<select name="sort">
  <option>Trending</option>
  <option>Newest</option>
</select>

--- Scroll Position ---
2.1 pages above | 1.3 pages below

--- Data Signals ---
Framework: Next.js (detected: #__next, __NEXT_DATA__)
Embedded JSON: __NEXT_DATA__ found (127KB, props.pageProps contains 20 pen objects)
JSON-LD: 1 schema found (type: WebPage)

--- Network Requests ---
API Requests Captured:
  GET /api/v2/pens/popular?page=1&limit=20 → 200 (JSON, 20 items)
  GET /api/v2/tags/threejs → 200 (JSON, tag metadata)
  POST /graphql {operationName: "PensByTag"} → 200 (JSON)
Filtered: 12 tracking/analytics, 8 static assets
```

**格式规则：**
1. 标题/段落/列表/表格 → Markdown 语法
2. 交互元素 → `[N]<tag attr="val">visible text</tag>` 格式
3. 容器元素 → `<section>`/`<nav>`/`<div>` 保留层级，但不编号（不可交互）
4. 列表截断 → 超过 5 个同构项时，展示前 3 个 + `... (N more items, M total)`
5. 非可见区域 → 不返回内容，只显示滚动位置指示器
6. 数据信号 → 独立 section，结构化呈现
7. 网络请求 → 独立 section，只展示 JSON API，过滤噪声

---

## 二、元素索引系统

### 2.1 各系统方案对比

| 系统 | ID 类型 | 持久性 | 跨页面 | 大页面策略 |
|------|---------|--------|--------|-----------|
| **Browser-Use** | backendNodeId + SHA-256 哈希 | 5 级回退匹配 | 不跨页面 | 视口过滤 + 列表截断 |
| **Playwright MCP** | 短暂 ref `[ref=eN]` | 每次 action 后重建 | 不跨页面 | 无分页 |
| **Vercel agent-browser** | 顺序编号 `@eN` | 每次 snapshot 重建 | 不跨页面 | `-i` 交互过滤 |
| **Agent-E** | mmid（注入 DOM 属性） | 页面存活期间 | 不跨页面 | 多模式蒸馏 |
| **Stagehand** | XPath + LLM 推理 | 缓存 + LLM 自愈 | 不跨页面 | AX tree 减 80-90% |
| **Manus** | 顺序索引 `index[:]` | 每次 action 后重建 | 不跨页面 | 视口过滤 |

**业界共识：不跨页面维持元素引用。** 所有系统在任何可能修改 DOM 的 action 后都重建快照。

### 2.2 哪些元素需要编号

基于 Browser-Use 的 `ClickableElementDetector` 和 Playwright MCP 的实践：

**必须编号的元素（可交互）：**
- `<a>` 链接（含 href 的）
- `<button>` 按钮
- `<input>` 所有类型
- `<textarea>` 文本区
- `<select>` 下拉框
- `[role="button"]`, `[role="link"]`, `[role="tab"]`, `[role="checkbox"]`, `[role="radio"]`
- `[onclick]` 有点击事件的元素
- `[tabindex]` 可聚焦元素（tabindex >= 0）
- `[contenteditable]` 可编辑元素

**不编号的元素：**
- 纯文本节点（`<p>`, `<span>`, `<h1>`-`<h6>`）
- 容器/布局元素（`<div>`, `<section>`, `<nav>`, `<main>`）
- 装饰元素（`<img>` 非链接内的, `<svg>`, `<hr>`）
- 隐藏元素（`display:none`, `visibility:hidden`, 零尺寸）
- 被完全遮挡的元素（z-index 被覆盖）

### 2.3 编号格式设计

```
[N]<tag attr1="val1" attr2="val2">visible text</tag>
```

**具体规则：**
- N 从 1 开始，按 DOM 顺序递增（与视觉阅读顺序一致）
- 只保留有意义的属性：`type`, `placeholder`, `href`（截断到路径）, `name`, `value`, `aria-label`, `role`
- 不保留的属性：`class`, `id`, `style`, `data-*`（除非是唯一标识）
- visible text 截断到 80 字符，超出显示 `...`
- `<select>` 展示当前选中项和选项数：`[10]<select name="sort">[Trending] (3 options)</select>`

### 2.4 索引生命周期

```
browse(url)
  → 导航完成 + SPA settle
  → 构建 DOM 树 → 过滤 → 编号 → 生成快照
  → 索引存入 ToolContext.selector_map: {1: DOMElement, 2: DOMElement, ...}
  → 返回给 LLM

interact(action, target)  ← target 可以是编号 "[3]" 或 CSS selector
  → 从 selector_map 查找元素
  → 执行交互
  → 交互后重建快照 → 新的 selector_map 替换旧的
  → 返回变化摘要 + 新快照

browse(new_url)
  → 旧 selector_map 完全丢弃
  → 新页面重新编号
```

**关键设计：**
- selector_map 是 ToolContext 的属性，所有工具共享
- interact 执行后自动重建（参考 Agent-E 的变化反馈模式）
- 过期索引产生明确错误："Element [N] no longer exists. The page has changed since the last snapshot."
- browse 和 interact 的返回值中都包含当前快照，LLM 始终看到最新状态

### 2.5 动态页面处理策略

**SPA 页面变化后索引失效：**
- interact 内部：执行 action → 等待 DOM settle → 重建快照
- 如果 action 触发了客户端路由（URL 变化），视为全新页面
- 如果 action 只是展开/折叠/显示 modal，保留现有元素编号 + 新增元素获得新编号

**AJAX 加载新内容：**
- 新出现的元素标记 `*`（参考 Browser-Use）：`*[15]<button>Load More</button>`
- 这帮助 LLM 注意到动态加载的内容

---

## 三、网络请求捕获

### 3.1 捕获范围

**捕获的请求类型：**
- Content-Type 含 `application/json` 的响应
- Content-Type 含 `application/graphql` 的响应
- URL 匹配 `/api/`, `/graphql`, `/v1/`, `/v2/` 等 API 路径模式
- POST 请求且 body 含 `query` 字段（GraphQL 检测）

**过滤的请求类型：**
- 静态资源：`image/*`, `font/*`, `text/css`, `application/javascript`
- 跟踪/分析：域名匹配 `google-analytics.com`, `segment.io`, `facebook.com/tr`, `doubleclick.net` 等
- 小型跟踪请求：响应 body < 100 bytes 且是 GET 请求
- Prefetch/Preload：`purpose: prefetch` header

### 3.2 捕获信息格式

```python
@dataclass
class CapturedRequest:
    method: str          # GET/POST
    url: str             # 完整 URL
    request_headers: dict  # 请求 headers（含 auth token）
    request_body: str | None  # POST body（JSON 字符串）
    status: int          # HTTP 状态码
    content_type: str    # 响应 Content-Type
    response_size: int   # 响应 body 大小（bytes）
    item_count: int | None  # 如果是 JSON 数组，元素数量
    response_preview: str  # 前 500 字符预览
```

### 3.3 呈现给 LLM 的格式

```
--- Network Requests ---
API Requests Captured:
  GET /api/v2/pens/popular?page=1&limit=20 → 200 (JSON, 20 items, 45KB)
  GET /api/v2/tags/threejs → 200 (JSON, tag metadata, 2KB)
  POST /graphql {operationName: "PensByTag"} → 200 (JSON, 15 items)
  GET /api/v2/search?q=webgl&page=1 → 200 (JSON, 25 items, 67KB)
Filtered: 12 tracking/analytics, 8 static assets
```

**设计原则：**
- 一行一个请求，高度压缩
- URL 省略域名（同域，已知）
- 显示 item_count 帮助 agent 理解数据量
- GraphQL 显示 operationName 而非完整 query
- 过滤的请求只显示总数，不逐条列出

### 3.4 价值论证

Beyond Browsing（ACL 2025）的量化数据：
- 混合 agent（浏览 + API）准确率 38.9%，纯浏览 14.8%，纯 API 29.2%
- 混合 agent 在 77.7% 的任务中同时使用了两种模态

**对我们系统的价值：**
1. **API 端点发现**：被动捕获是零成本的 API 发现机制，agent 不需要主动探测
2. **数据路径暴露**：SPA 的 DOM 是 API 数据的渲染视图，捕获 API 响应比抓 DOM 更干净
3. **提取策略指导**：看到 `GET /api/v2/pens?page=1` 返回 20 items，agent 知道可以直接 `curl` 重放并修改 page 参数
4. **GraphQL 发现**：GraphQL 端点通常无法通过页面 UI 发现，只能通过网络捕获

### 3.5 实现要点

```python
# 在导航前注册监听器
captured_requests: list[CapturedRequest] = []

async def on_response(response):
    content_type = response.headers.get("content-type", "")
    if not _is_data_api(response.url, content_type, response.request.method):
        return  # 过滤非数据请求

    body = await response.body()
    item_count = _count_items(body, content_type)  # 解析 JSON 数组长度

    captured_requests.append(CapturedRequest(
        method=response.request.method,
        url=response.url,
        request_headers=dict(response.request.headers),
        request_body=response.request.post_data,
        status=response.status,
        content_type=content_type,
        response_size=len(body),
        item_count=item_count,
        response_preview=body[:500].decode("utf-8", errors="replace"),
    ))

page.on("response", on_response)  # 必须在 page.goto() 之前
```

---

## 四、数据信号检测

### 4.1 嵌入数据检测

| 信号 | 框架 | 检测方式 | 报告格式 |
|------|------|---------|---------|
| `#__NEXT_DATA__` | Next.js Pages Router | `document.querySelector('#__NEXT_DATA__')` | `__NEXT_DATA__ found (127KB, props.pageProps contains 20 pen objects)` |
| `self.__next_f.push()` | Next.js App Router (RSC) | 检测 inline script 含 `__next_f.push` | `Next.js Flight Data found (RSC, ~85KB across 12 chunks)` |
| `window.__NUXT__` | Nuxt.js 2/3 | `window.__NUXT__` 存在性 | `__NUXT__ found (45KB, devalue-serialized state)` |
| `window.__INITIAL_STATE__` | Redux/Vuex | `window.__INITIAL_STATE__` | `Redux/Vuex initial state found (23KB)` |
| `window.__APOLLO_STATE__` | Apollo GraphQL | `window.__APOLLO_STATE__` | `Apollo GraphQL cache found (67KB)` |
| `script[type="application/ld+json"]` | Schema.org | `querySelectorAll` | `JSON-LD: 2 schemas (Product, BreadcrumbList)` |
| `script[type="application/json"]` | 通用 | `querySelectorAll`（排除已知框架） | `Generic JSON scripts: 3 found` |

### 4.2 框架检测

```javascript
const detection = {
    react: !!document.querySelector('[data-reactroot]') || !!window.__REACT_DEVTOOLS_GLOBAL_HOOK__,
    nextjs: !!document.querySelector('#__NEXT_DATA__') || !!document.querySelector('#__next'),
    vue: !!window.__VUE__ || !!document.querySelector('[data-v-]'),
    nuxt: !!window.__NUXT__ || !!document.querySelector('#__nuxt'),
    angular: !!window.ng || !!document.querySelector('[ng-version]'),
    svelte: !!document.querySelector('[data-svelte-h]'),
};
```

### 4.3 呈现格式

```
--- Data Signals ---
Framework: Next.js (detected: #__next, __NEXT_DATA__)
Embedded JSON:
  __NEXT_DATA__: 127KB, props.pageProps has 20 pen objects with fields [id, title, author, views, likes]
  JSON-LD: 1 schema (type: WebPage, name: "ThreeJS Pens")
Meta:
  og:title: "ThreeJS Pens | CodePen"
  og:description: "Explore ThreeJS pens on CodePen"
```

### 4.4 检测的价值

**对 agent 策略选择的直接影响：**
- 检测到 `__NEXT_DATA__` → agent 知道可以直接 `extract` 嵌入 JSON，无需 DOM 解析
- 检测到 `__APOLLO_STATE__` → agent 知道有 GraphQL cache，可以直接访问
- 检测到 `JSON-LD` → agent 知道有标准化结构数据，适合作为补充源
- 检测到 Next.js → agent 知道可能有 `_next/data/` API 路径

**成本层级（来自调研报告）：**
```
嵌入 JSON 提取：~0 cost，毫秒级，95%+ 可靠
API 重放：~0/call，毫秒级，90%+ 可靠
DOM CSS/XPath：~0，快，但脆弱
LLM 驱动提取：5k-10k tokens/page，秒级
```

这些信号帮助 agent 从最低成本方法开始尝试。

---

## 五、大页面处理

### 5.1 各系统策略对比

| 系统 | 策略 | token 范围 |
|------|------|-----------|
| **Browser-Use** | 视口过滤 + bounding box 合并 + 列表截断 | ~2k-5k |
| **Vercel agent-browser** | 只返回交互元素的精简 AX tree | 200-400 |
| **Playwright MCP** | 完整 AX tree，无分页 | 3k-15k+ |
| **Manus** | 视口内容 + Markdown 转换 | ~1k-3k |
| **AgentOccam** | 元素合并 + Markdown 转换 + 选择性回放 | ~2.9k/步 |
| **D2Snap** | DOM 下采样算法 | 7k-19k |

### 5.2 推荐策略：三层控制

**Layer 1：视口过滤（默认行为）**
- 默认只返回视口内可见的内容
- scroll_down/scroll_up 后刷新视口快照
- 滚动位置指示器：`2.1 pages above | 1.3 pages below`
- 理由：Manus 的做法——"Browser tools only return elements in visible viewport by default"

**Layer 2：列表截断**
- 同构项列表（如搜索结果卡片、商品列表、导航项）
- 超过 5 个同构项：展示前 3 个完整 + 计数信息
- 格式：`... (17 more cards, 20 total)`
- 理由：20 个相同结构的卡片对 LLM 没有额外信息价值

**Layer 3：token 硬上限**
- 页面快照总量不超过 8,000 tokens（可配置）
- 超限时按优先级截断：
  1. 保留所有交互元素（索引标签）
  2. 保留数据信号和网络请求 section
  3. 截断文本内容（保留前 N 段 + 省略标记）
- 截断时显示明确信息：`[Content truncated: page has ~15,000 tokens, showing first 8,000. Use scroll to see more.]`

### 5.3 SPA 空壳检测

浏览器渲染后，检测页面是否实际加载了内容：

```python
async def detect_spa_empty_shell(page) -> str | None:
    """检测 SPA 空壳——元素多但可见文本少"""
    metrics = await page.evaluate("""() => {
        const all_elements = document.querySelectorAll('*').length;
        const visible_text = document.body.innerText.trim();
        const text_length = visible_text.length;
        const script_count = document.querySelectorAll('script').length;
        return { all_elements, text_length, script_count };
    }""")

    # 启发式判断：大量元素但几乎没有文本
    if metrics['all_elements'] > 50 and metrics['text_length'] < 200:
        return "SPA empty shell detected: page has many elements but minimal visible text. The page may need interaction or additional loading time."
    return None
```

---

## 六、返回值结构设计

### 6.1 完整返回值结构

```python
@dataclass
class BrowseResult:
    # === 导航状态 ===
    url: str                    # 最终 URL（可能与请求不同，如重定向）
    title: str                  # 页面标题
    status: int | None          # HTTP 状态码（SPA 内部导航可能为 None）

    # === 页面快照（给 LLM 的核心内容） ===
    snapshot: str               # Markdown + HTML 混合格式的页面表示

    # === 元素索引 ===
    element_count: int          # 索引元素总数

    # === 数据信号 ===
    framework: str | None       # 检测到的框架（"Next.js", "Vue", "Angular", None）
    embedded_data: list[dict]   # 嵌入数据信号列表
    # 每项: {"type": "__NEXT_DATA__", "size_kb": 127, "summary": "20 pen objects with fields [id, title, ...]"}

    # === 网络请求 ===
    api_requests: list[dict]    # 捕获的 API 请求列表
    # 每项: {"method": "GET", "path": "/api/v2/pens?page=1", "status": 200, "items": 20, "size_kb": 45}
    filtered_count: int         # 被过滤的非数据请求数

    # === 滚动状态 ===
    scroll_position: dict       # {"pages_above": 2.1, "pages_below": 1.3}

    # === 警告/提示 ===
    warnings: list[str]         # SPA 空壳、token 截断、加载超时等

    # === 错误（如果导航失败） ===
    error: str | None           # 导航错误信息
```

### 6.2 给 LLM 的格式化输出

BrowseResult 序列化为一个结构化文本字符串返回给 LLM：

```
=== Page: ThreeJS Pens | CodePen ===
URL: https://codepen.io/tag/threejs
Status: 200

--- Content ---
{snapshot 内容，见 1.4 节格式规范}

--- Scroll Position ---
2.1 pages above | 1.3 pages below

--- Data Signals ---
Framework: Next.js (detected: #__next, __NEXT_DATA__)
Embedded JSON:
  __NEXT_DATA__: 127KB, props.pageProps has 20 pen objects with fields [id, title, author, views, likes]
  JSON-LD: 1 schema (type: WebPage)

--- Network Requests ---
API Requests Captured (3):
  GET /api/v2/pens/popular?page=1&limit=20 → 200 (JSON, 20 items, 45KB)
  GET /api/v2/tags/threejs → 200 (JSON, tag metadata, 2KB)
  POST /graphql {operationName: "PensByTag"} → 200 (JSON, 15 items)
Filtered: 12 tracking/analytics, 8 static assets

--- Warnings ---
(none)
```

### 6.3 信息密度控制

| Section | 典型 token 数 | 必需性 | 截断策略 |
|---------|-------------|--------|---------|
| 头部（URL/title/status） | ~20 | 必需 | 不截断 |
| Content（snapshot） | 2,000-6,000 | 核心 | Layer 1-3 控制 |
| Scroll Position | ~15 | 必需 | 不截断 |
| Data Signals | 50-200 | 高价值 | 不截断 |
| Network Requests | 50-300 | 高价值 | 超过 10 条时截断 |
| Warnings | 0-50 | 按需 | 不截断 |
| **总计** | **2,500-7,000** | | 硬上限 8,000 |

---

## 七、参数设计

### 7.1 推荐参数 Schema

```json
{
    "name": "browse",
    "description": "Navigate to a URL and observe the page. Returns structured page content including: rendered text (as Markdown), interactive elements (indexed for use with interact), detected data signals (__NEXT_DATA__, JSON-LD, etc.), and captured API requests. Also automatically records the visit as an observation in the World Model.\n\nThe page snapshot only includes content visible in the current viewport. Use interact(action='scroll_down') to see more content.\n\nInteractive elements are numbered [1], [2], etc. Use these numbers with interact() to click, fill, or otherwise operate on elements. Numbers are invalidated after any page change.\n\nData Signals section reveals embedded JSON data and framework info — check this before attempting DOM extraction, as embedded JSON is often more complete and easier to extract.\n\nNetwork Requests section shows API endpoints discovered during page load — these can be replayed directly with bash(curl ...) for cleaner data access.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to navigate to. Must include protocol (https://). For the current page, omit this parameter to refresh the snapshot."
            },
            "wait_for": {
                "type": "string",
                "description": "Optional CSS selector to wait for before capturing the snapshot. Use when you know a specific element should appear (e.g., after SPA navigation). Default: automatic SPA settle detection (waits for DOM to stabilize)."
            }
        },
        "required": []
    }
}
```

### 7.2 参数设计决策

**url（可选）：**
- 有 url → 导航到新页面
- 无 url → 刷新当前页面快照（用于 interact 后想重新获取完整快照，或 scroll 后想看新内容）
- 理由：Manus 的 `browser_view` 不接受 URL，只看当前页面。我们合并为一个工具，url 可选实现两种用途。

**wait_for（可选）：**
- 默认行为：MutationObserver settle（1.5s 静默阈值）+ 硬超时 15s
- 有值时：额外等待指定 selector 出现
- 理由：某些 SPA 页面 settle 后还需要等待特定元素（如搜索结果加载）
- 来源：Stagehand 的 DOM settle 模式 + Playwright 的 `waitForSelector`

**不需要的参数：**

| 考虑过的参数 | 不采用的原因 |
|-------------|-------------|
| `viewport_only` | 默认就是视口过滤，不需要切换。需要完整页面时通过 extract JS 获取 |
| `max_elements` | token 预算已通过 Layer 3 硬上限控制，不需要暴露给 agent |
| `include_network` | 网络请求信息 token 开销小（50-300），始终包含比按需包含更好 |
| `include_signals` | 同上，数据信号是高价值低成本信息 |
| `screenshot` | 不需要——文本表示优于截图已有量化证据。截图自动保存到 trace |
| `timeout` | 基础设施层处理，agent 不需要关心 |

---

## 八、自动 World Model 写入

### 8.1 browse 自动创建的 Observation

每次 browse 调用成功后，自动写入一条 Observation：

```python
observation = {
    "page_summary": {
        "url": "https://codepen.io/tag/threejs",
        "title": "ThreeJS Pens | CodePen",
        "status": 200,
        "element_count": 42,
        "framework": "Next.js",
        "has_embedded_data": True,
        "embedded_data_types": ["__NEXT_DATA__", "JSON-LD"],
        "api_endpoints_found": [
            "/api/v2/pens/popular?page=1&limit=20",
            "/api/v2/tags/threejs",
        ],
        "content_summary": "Tag page showing 20 pen cards with title, author, views. Pagination available.",
    }
}
```

**自动写入的内容：**
- URL、标题、状态码
- 页面元素数量
- 检测到的框架
- 嵌入数据类型
- 捕获到的 API 端点
- 页面内容的一句话摘要（由程序化规则生成，非 LLM）

### 8.2 自动写入 vs 手动记录（note_insight）的边界

| 内容 | 谁写 | 写到哪里 |
|------|------|---------|
| 页面有什么元素、什么框架、什么 API | browse 自动 | Observation（`page_summary` key） |
| 这些数据有什么含义、覆盖关系如何 | agent 用 note_insight | Observation（`insight` key） |
| 提取结果（样本数、字段名、null 率） | extract 自动 | Observation（`extraction_method` key） |
| 不同数据来源的比较和判断 | agent 用 note_insight | Observation（`insight` key） |

**原则：**
- browse 自动记录**客观事实**：页面有什么、检测到什么
- agent 手动记录**主观判断**：这意味着什么、应该怎么利用
- 自动写入保证"做了就有记录"——即使 agent 忘了 note_insight，基本的导航历史也不会丢失

### 8.3 Location 自动创建/更新

browse 自动管理 Location：

```python
async def _auto_update_location(url: str, wm: SiteWorldModel):
    """从 URL 推断 pattern，创建或更新 Location"""
    pattern = url_to_pattern(url)  # e.g., "codepen.io/tag/{tag}"
    location_id = f"{domain}::{pattern}"

    existing = wm.get_location(location_id)
    if existing is None:
        # 创建新 Location
        await wm.add_location(Location(
            id=location_id,
            domain=domain,
            pattern=pattern,
            how_to_reach=url,  # 首次访问的具体 URL
        ))
    else:
        # 更新 how_to_reach（如果有更好的路径）
        pass  # Location 的 pattern 由 agent 通过 note_insight 修正

    return location_id
```

**URL 到 Pattern 的规则（启发式）：**
1. 移除 query parameters → 保留路径
2. 检测路径中的变量段（UUID, 数字 ID, slug）→ 替换为 `{param}`
3. 例子：
   - `https://codepen.io/tag/threejs` → `codepen.io/tag/{tag}`
   - `https://codepen.io/pen/abc123` → `codepen.io/pen/{id}`
   - `https://codepen.io/api/v2/pens?page=1` → `codepen.io/api/v2/pens`

**注意：** 这是启发式的初始 pattern。agent 可以通过 note_insight 修正。符合硬约束"Location 粒度由 agent 决定"。

---

## 九、完整实现流程

### 9.1 browse 执行流程

```
browse(url, wait_for=None)
│
├─ 1. 导航前准备
│   ├─ 注册网络请求监听器 page.on('response', on_response)
│   ├─ 清空 captured_requests 列表
│   └─ 记录起始时间
│
├─ 2. 导航
│   ├─ 如果有 url → page.goto(url, wait_until='domcontentloaded', timeout=30000)
│   ├─ 如果无 url → 使用当前页面（刷新快照）
│   ├─ 捕获导航异常 → 返回结构化错误
│   └─ 处理重定向 → 记录最终 URL
│
├─ 3. 等待内容稳定
│   ├─ MutationObserver settle（1.5s 无 DOM 变化）
│   ├─ 如果有 wait_for → 额外等待 page.waitForSelector(wait_for, timeout=10000)
│   └─ 硬超时兜底（15s）
│
├─ 4. 构建页面快照
│   ├─ 4a. 注入 JS 构建 DOM 树
│   │   ├─ 递归遍历 DOM
│   │   ├─ 可见性检测（viewport 内）
│   │   ├─ 可交互性检测
│   │   ├─ 遮挡检测（z-index/elementFromPoint）
│   │   └─ 返回增强 DOM 树结构
│   │
│   ├─ 4b. DOM 树 → Markdown + HTML 混合格式
│   │   ├─ 文本节点 → Markdown
│   │   ├─ 交互元素 → 编号 HTML tag
│   │   ├─ 容器元素 → 保留层级
│   │   ├─ 列表截断（>5 同构项）
│   │   └─ token 硬上限检查
│   │
│   ├─ 4c. 元素索引构建
│   │   └─ selector_map = {1: element_ref, 2: element_ref, ...}
│   │
│   ├─ 4d. 数据信号检测
│   │   ├─ page.evaluate() 执行框架检测脚本
│   │   ├─ 检测嵌入 JSON（__NEXT_DATA__ 等）
│   │   └─ 检测 JSON-LD / meta tags
│   │
│   └─ 4e. SPA 空壳检测
│       └─ 元素多但文本少 → 生成 warning
│
├─ 5. 自动写 World Model
│   ├─ URL → Pattern → Location（创建或更新）
│   └─ 写入 Observation（page_summary 类型）
│
├─ 6. 截图（trace 用）
│   └─ page.screenshot() → artifacts/sessions/{run_id}/{session_id}/screenshots/step_{N}.jpg
│
└─ 7. 组装返回值
    ├─ BrowseResult 数据结构
    └─ 格式化为 LLM 可读的文本
```

### 9.2 DOM 树构建 JS 核心逻辑

```javascript
// 注入浏览器执行的 buildDomTree 函数核心逻辑
function buildDomTree(node, options) {
    const result = {
        tag: node.tagName?.toLowerCase(),
        role: node.getAttribute?.('role'),
        text: '',
        children: [],
        isInteractive: false,
        isVisible: false,
        rect: null,
    };

    // 1. 可见性检测
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    result.isVisible = (
        rect.width > 0 && rect.height > 0 &&
        style.display !== 'none' &&
        style.visibility !== 'hidden' &&
        style.opacity !== '0' &&
        // 视口内检测
        rect.bottom > 0 && rect.top < window.innerHeight &&
        rect.right > 0 && rect.left < window.innerWidth
    );

    // 2. 可交互性检测
    const interactiveTags = ['a', 'button', 'input', 'textarea', 'select'];
    const interactiveRoles = ['button', 'link', 'tab', 'checkbox', 'radio', 'menuitem'];
    result.isInteractive = (
        interactiveTags.includes(result.tag) ||
        interactiveRoles.includes(result.role) ||
        node.hasAttribute('onclick') ||
        node.hasAttribute('tabindex') ||
        node.hasAttribute('contenteditable')
    );

    // 3. 递归子节点
    for (const child of node.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
            const text = child.textContent.trim();
            if (text) result.text += text + ' ';
        } else if (child.nodeType === Node.ELEMENT_NODE) {
            // 跳过 script, style, meta 等
            if (['script', 'style', 'meta', 'link', 'noscript'].includes(child.tagName.toLowerCase())) continue;
            const childResult = buildDomTree(child, options);
            if (childResult) result.children.push(childResult);
        }
    }

    // 4. 过滤不可见且无可见子节点的元素
    if (!result.isVisible && !result.children.some(c => c.isVisible)) {
        return null;
    }

    return result;
}
```

### 9.3 DOM 树 → 格式化输出

```python
def dom_tree_to_snapshot(tree: dict, max_tokens: int = 8000) -> tuple[str, dict]:
    """将 DOM 树转换为 Markdown + HTML 混合格式"""
    lines = []
    selector_map = {}
    index_counter = [0]  # mutable for closure

    def process_node(node, depth=0):
        if not node:
            return

        tag = node.get('tag', '')
        text = node.get('text', '').strip()
        children = node.get('children', [])
        is_interactive = node.get('isInteractive', False)

        # 交互元素 → 编号 HTML tag
        if is_interactive:
            index_counter[0] += 1
            idx = index_counter[0]
            attrs = _extract_meaningful_attrs(node)
            label = text[:80] + ('...' if len(text) > 80 else '')
            lines.append(f"{'  ' * depth}[{idx}]<{tag}{attrs}>{label}</{tag}>")
            selector_map[idx] = node  # 存储元素引用
            return

        # 标题 → Markdown
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            level = int(tag[1])
            lines.append(f"{'#' * level} {text}")
            return

        # 段落 → Markdown
        if tag == 'p' and text:
            lines.append(f"\n{text}\n")
            return

        # 列表 → Markdown（检测同构项截断）
        if tag in ('ul', 'ol'):
            _process_list(node, depth, lines, selector_map, index_counter)
            return

        # 表格 → Markdown 表格
        if tag == 'table':
            _process_table(node, lines)
            return

        # 容器元素 → 保留层级
        if tag in ('nav', 'section', 'main', 'article', 'div', 'form'):
            if _has_semantic_value(node):
                lines.append(f"{'  ' * depth}<{tag}>")
                for child in children:
                    process_node(child, depth + 1)
                lines.append(f"{'  ' * depth}</{tag}>")
                return

        # 其他：直接处理子节点
        for child in children:
            process_node(child, depth)

    process_node(tree)
    snapshot = '\n'.join(lines)

    # Token 硬上限检查
    estimated_tokens = len(snapshot) // 4  # 粗略估算
    if estimated_tokens > max_tokens:
        snapshot = _truncate_to_budget(snapshot, max_tokens)
        snapshot += f"\n[Content truncated: showing ~{max_tokens} tokens of ~{estimated_tokens}. Scroll or use extract for full content.]"

    return snapshot, selector_map
```

---

## 十、设计决策记录

### D1: Markdown + HTML 混合格式

**决策：** 静态文本转 Markdown，交互元素保留 HTML tag + 编号索引。

**证据：**
- D2Snap（arXiv:2508.04412）：混合格式 73% 成功率，最高
- Cloudflare Markdown for Agents：HTML → Markdown 减 80% token
- AgentOccam（ICLR 2025）：表格/列表转 Markdown 显著降低 token 同时保持语义
- Manus 实践：页面内容自动转 Markdown，交互元素标注索引

**替代方案被否：**
- 纯 AX tree：iframe 不包含，ARIA 标注差的站点信息丢失严重（WebAIM：有 ARIA 的页面多 41% 错误）
- 纯截图：GUI 定位准确率仅 18.9%，成本高
- 纯 Markdown：交互元素丢失精确定位能力
- 纯 HTML：token 开销过大（比 Markdown 多 5x+）

### D2: 默认视口过滤

**决策：** 默认只返回视口内容，通过 scroll 查看更多。

**证据：**
- Manus："Browser tools only return elements in visible viewport by default"
- Browser-Use：视口过滤 + bounding box 合并是其核心策略
- 控制 token 的最有效手段——用户已经在屏幕上的内容才最可能是当前需要的

### D3: 编号不跨页面持久化

**决策：** 每次 browse 和 interact-with-DOM-change 后重建编号。

**证据：**
- 业界共识：Browser-Use, Playwright MCP, Vercel agent-browser, Agent-E 无一跨页面持久化
- CDP BackendNodeId 在导航后全部失效
- 跨页面维持引用的复杂度远大于每次重建

### D4: 网络请求始终捕获并展示

**决策：** 不设开关，browse 始终捕获 JSON API 请求并在返回值中展示。

**证据：**
- Beyond Browsing（ACL 2025）：混合 agent 38.9% vs 纯浏览 14.8%
- 捕获是零成本的（`page.on('response')` 已注册）
- 展示 token 开销小（50-300 tokens）
- 对 agent 发现 API 端点的价值极高——这是唯一的被动 API 发现机制

### D5: 数据信号检测作为 browse 的内置功能

**决策：** 每次 browse 自动检测框架、嵌入 JSON、JSON-LD。

**证据：**
- 嵌入 JSON 提取成本 ~0，可靠性 95%+（Zyte 分析）
- 不检测 = agent 不知道有低成本提取路径，可能直接跳到高成本 DOM 提取
- 检测 token 开销极小（50-200 tokens）

### D6: browse 自动写 World Model

**决策：** browse 自动创建 Location 和 page_summary 类型的 Observation。

**证据：**
- CLAUDE.md 明确要求："browse(url)：核心工具。导航 + 自动写 World Model"
- 自动写入保证"做了就有记录"——即使 agent 认知纪律不好
- 写入的是客观事实（URL、框架、API 端点），不是主观判断

### D7: url 参数可选

**决策：** url 省略时刷新当前页面快照。

**证据：**
- Manus 有独立的 `browser_view`（无 URL）和 `browser_navigate`（有 URL）
- 合并为一个工具减少工具数量（已有 7 个工具的约束）
- 刷新快照的场景：interact 后想看完整页面、scroll 后想看新内容

### D8: wait_for 可选参数

**决策：** 提供 wait_for 参数允许等待特定 selector。

**证据：**
- 默认的 DOM settle 检测覆盖 90%+ 场景
- 但某些 SPA 页面（如 AJAX 搜索结果）需要等待特定元素
- Agent-E 和 Playwright MCP 都支持类似功能
- 作为可选参数，不增加默认使用的复杂度

---

## 十一、工具描述（完整版）

browse 工具的描述需要作为"给新人的入职手册"来写（Anthropic ACI 指导）：

```
Navigate to a URL and observe the page content.

Returns a structured page snapshot containing:
1. **Content**: Page text in Markdown format with interactive elements as indexed HTML tags ([1]<button>...).
2. **Element Index**: Numbered interactive elements. Use these numbers with interact() — e.g., interact(action="click", target="[3]").
3. **Data Signals**: Detected frameworks (Next.js, Vue, etc.) and embedded JSON data (__NEXT_DATA__, JSON-LD). If embedded data is detected, consider using extract() to get it directly — it's often more complete than DOM scraping.
4. **Network Requests**: API endpoints discovered during page load. These can be replayed with bash("curl ...") for clean, structured data.

Key behaviors:
- Only shows content within the current viewport. Use interact(action="scroll_down") to see more.
- Element numbers [N] become invalid after any page change. Always use the latest numbers.
- Automatically records the visit in the World Model.
- Waits for SPA content to stabilize before capturing (1.5s DOM settle timeout).

Omit the url parameter to refresh the snapshot of the current page (useful after scrolling or interaction).

When to use browse vs other tools:
- browse: See what's on a page, discover structure and data signals
- interact: Click, fill, scroll elements you see in browse output
- extract: Run JS in the browser to extract data (especially embedded JSON found by browse)
- bash: Replay API endpoints found by browse, process data, make HTTP requests
```

---

## 参考文献

### 学术论文
- D2Snap (arXiv:2508.04412) — DOM 下采样 + Markdown/HTML 混合格式最佳
- AgentOccam (ICLR 2025, Amazon) — 观察空间优化 +161% vs 架构改进
- Beyond Browsing (ACL 2025, arXiv:2410.16464) — 混合 agent 38.9%
- Visual Confused Deputy (arXiv:2603.14707) — 56.7% CUA 点击错误
- Agent-E (NeurIPS 2024, arXiv:2407.13032) — DOM 蒸馏 + 变化观察
- CodeAct (ICML 2024) — 代码 action +20% 成功率

### 生产系统
- Browser-Use — buildDomTree.js, DOMService, 5 级回退匹配
- Playwright MCP — YAML AX tree + ref 系统
- Vercel agent-browser — 精简 AX tree + @eN ref, 93% token 节省
- Stagehand v3 — observe/act/extract 原子操作, LLM 自愈
- Manus — Markdown 转换 + 索引标签, KV-cache 优化
- Skyvern 2.0 — Vision LLM + planner-actor-validator
- Anthropic Computer Use — 截图 + 像素坐标
- OpenAI CUA/Operator — 截图 + 文本表示混合

### 基础设施
- Cloudflare Markdown for Agents — HTML → Markdown 减 80% token
- Playwright aria snapshot — YAML 格式 AX tree
- njsparser — Next.js Pages Router + App Router 解析
