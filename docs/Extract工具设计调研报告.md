# Extract 工具设计深度调研报告

> 日期：2026-03-29
> 用途：为 extract 工具的最终设计方案提供全面调研依据
> 调研范围：Stagehand, Firecrawl, Crawl4AI, AgentQL, Skyvern, Manus, Devin, Browser-Use, OpenHands; 学术论文; 工程实践

---

## 一、提取方式设计（最关键）

### 1.1 各系统提取方案深度对比

#### Stagehand extract()

**设计哲学：** Schema 驱动 + LLM 理解。

**参数设计：**
| 参数 | 类型 | 说明 |
|------|------|------|
| `instruction` | string | 自然语言描述提取目标 |
| `schema` | ZodTypeAny | Zod schema 定义数据结构，提供运行时验证 + TypeScript 类型推断 |
| `selector` | string | XPath/CSS selector 限定提取范围（可选） |
| `model` | ModelConfiguration | 指定 AI 模型（可选） |
| `timeout` | number | 超时毫秒数 |

**返回值设计：**
- 有 schema：`Promise<z.infer<T> & { cacheStatus?: "HIT" | "MISS" }>`（类型安全）
- 仅 instruction：`Promise<{ extraction: string }>`
- 无参数：`Promise<{ pageText: string }>`（返回 AX tree 原文）

**核心机制：** 使用 AX tree + LLM 进行语义理解，自动穿透 iframe 和 Shadow DOM。缓存成功的 selector 路径，后续运行先尝试重放再回退 LLM。

**适用场景：** 已知要提取什么结构的数据（schema 已定义），需要跨站点鲁棒性。

**局限：** 每次调用消耗 5k-10k tokens。不适合探索性提取（不知道页面有什么数据时）。

**来源：** [Stagehand extract() 文档](https://docs.stagehand.dev/references/extract)

---

#### Firecrawl /extract

**设计哲学：** 自然语言 + Schema 双模式，面向"不写代码"的提取。

**参数设计：**
| 参数 | 类型 | 说明 |
|------|------|------|
| `urls` | string[] | 支持单页和通配符（`example.com/*`） |
| `prompt` | string | 自然语言指令（可选，与 schema 二选一或并用） |
| `schema` | JSON Schema | 定义预期结构（可选） |
| `enableWebSearch` | boolean | 是否扩展到域外 |
| `agent` | object | 配置 FIRE-1 agent 进行复杂导航 |

**返回值设计：**
```json
{
  "success": boolean,
  "data": { /* 结构化数据 */ },
  "status": "completed|processing|failed|cancelled",
  "expiresAt": "ISO-8601"
}
```

**核心特点：**
- 支持异步作业：`start_extract()` 返回 Job ID，通过 `/extract/{jobId}` 轮询
- 可跨多页提取并聚合为一个结构化结果
- 每 credit = 15 tokens 的统一计费

**适用场景：** 已知目标结构、批量页面提取。

**局限：** 大规模站点覆盖不完全。动态站点结果可能不一致。Beta 阶段。

**来源：** [Firecrawl extract 文档](https://docs.firecrawl.dev/features/extract)

---

#### Crawl4AI 多策略体系

**设计哲学：** Strategy 模式分离 LLM 和非 LLM 提取路径。

**LLM-Free 策略：**

| 策略 | 核心机制 | 适用场景 |
|------|---------|---------|
| `JsonCssExtractionStrategy` | CSS selector + schema 定义 | 表格/列表等规则结构 |
| `JsonXPathExtractionStrategy` | XPath + 同样 schema | 复杂嵌套、基于文本选择 |
| `RegexExtractionStrategy` | 预编译 + 自定义正则 | 邮箱/电话/价格等特定格式 |
| `CosineStrategy` | 嵌入向量余弦相似度 | 语义相似内容聚类 |

**Schema 设计（JsonCss/XPath）：**
```python
schema = {
    "name": "Products",
    "baseSelector": "div.product-row",      # 容器选择器
    "fields": [
        {"name": "title", "selector": "h2.name", "type": "text"},
        {"name": "price", "selector": "span.price", "type": "text"},
        {"name": "specs", "selector": "ul.specs", "type": "nested_list",
         "fields": [
            {"name": "label", "selector": "dt", "type": "text"},
            {"name": "value", "selector": "dd", "type": "text"}
         ]}
    ]
}
```

**LLM 策略：** `LLMExtractionStrategy` 将页面内容发送给模型 + 可选 schema。

**混合能力：** `generate_schema()` 让 LLM 一次性生成可复用的 CSS/XPath schema，结合 AI 灵活性和传统速度。

**来源：** [Crawl4AI LLM-Free 策略文档](https://docs.crawl4ai.com/extraction/no-llm-strategies/), [LLM 策略文档](https://docs.crawl4ai.com/extraction/llm-strategies/)

---

#### AgentQL 语义查询语言

**设计哲学：** 用自然语言 selector 替代 CSS/XPath。

**查询语法：**
```
{
    products[] {
        name
        price(integer)
        rating(number out of 5 stars)
    }
}
```

**核心特点：**
- 元素按**语义**而非 DOM 位置匹配
- 自愈性：UI 变化后查询仍能命中
- 查询定义 = 输出结构，无需后处理
- Python 和 JavaScript SDK + REST API

**适用场景：** 跨站点通用提取、UI 频繁变化的站点。

**局限：** 依赖 AgentQL 云服务。对嵌入 JSON / API 数据无直接支持。

**来源：** [AgentQL 查询语言文档](https://docs.agentql.com/agentql-query)

---

#### Skyvern Data Extraction Agent

**设计哲学：** 多 agent 协作，视觉优先。

**核心机制：**
- Interactable Element Agent：解析 HTML 提取可交互元素
- Navigation Agent：规划导航路径
- Data Extraction Agent：读取表格和文本，按用户定义 schema 输出

**参数设计：** 在主 prompt 中嵌入 `data_extraction_schema`（JSONC 格式）。

**输出：** 按 schema 结构化为 JSON 或 CSV。

**适用场景：** 复杂多步骤工作流（需要导航后提取）。

**局限：** 重量级，面向工作流自动化而非轻量提取。

**来源：** [Skyvern GitHub](https://github.com/Skyvern-AI/skyvern)

---

#### Manus 文件系统方法

**设计哲学：** 代码即提取，文件系统即外部记忆。

**核心做法：** Manus 不设独立"提取"工具。Agent 通过 `shell_exec` 写 Python 脚本执行提取，通过 `file_write` 保存结果。浏览器内容通过 `browser_view` 获取后，用代码处理。

**关键洞察：** "Save intermediate results and notes to files rather than trying to hold everything in the chat context."

**来源：** [Manus leaked prompt analysis](https://gist.github.com/jlia0/db0a9695b3ca7609c9b1a08dcbf872c9)

---

#### Devin

**方法：** 生成完整爬虫脚本（client-side 和 server-side），通过代码执行提取。能自主构建 data collection pipeline。本质是代码生成 + 执行，没有专用提取工具。

**来源：** [Devin Web Scraping 文档](https://docs.devin.ai/use-cases/web-scraping)

---

#### Browser-Use

**方法：** LLM 驱动的全自主浏览器控制。通过结构化输出（非 tool calling）让 LLM 描述要提取的数据。使用 DOM 索引系统标注交互元素，LLM 根据索引决定操作。

**提取方式：** 不设专用 extract 工具。Agent 通过 `get_state` 获取页面状态，LLM 从返回的 DOM 中"读取"数据。

---

#### OpenHands

**方法：** CodeAct 范式。`execute_ipython_cell` 在持久 Jupyter kernel 中执行 Python，变量跨调用存活。浏览器交互委托给 BrowsingAgent。数据处理回到 CodeAct。

### 1.2 侦察系统 vs 爬虫系统：对提取设计的影响

这是本调研最关键的区分。

| 维度 | 爬虫系统（已知结构） | 侦察系统（探索未知） |
|------|-------------------|-------------------|
| 提取时机 | 明确——页面就绪后立即提取 | 不确定——先观察再决定是否提取 |
| Schema | 预定义——提前知道字段 | 未知——提取时才发现 schema |
| 提取方法 | 固定——预编写 selector/脚本 | 动态——agent 根据观察写 JS |
| 提取目的 | 收集数据 | 验证理解 + 采集样本 |
| 批量需求 | 高——数千页相同结构 | 低——几条样本即可 |
| 错误处理 | 自动重试 + 备选 selector | 失败本身是信息（记录为 observation） |

**核心结论：** Schema 驱动提取（Stagehand、Firecrawl、Crawl4AI JsonCss）需要**预知目标结构**，与侦察系统的"探索未知"本质冲突。纯 LLM 提取（Firecrawl prompt-only、Crawl4AI LLMStrategy）灵活但昂贵。

**对我们的影响：** 侦察系统的 extract 工具必须是**通用的 JS 执行**——因为 agent 需要根据观察到的实际页面结构即时编写提取逻辑。预定义 schema 或 CSS selector 模板在未知站点上没有意义。

### 1.3 方案对比总结

| 方案 | 灵活性 | 成本 | 适合侦察？ | 原因 |
|------|-------|------|-----------|------|
| page.evaluate(JS) | 最高 | 0（无 LLM 成本） | **最适合** | Agent 根据观察即时写 JS |
| Stagehand schema | 中 | 高（LLM 调用） | 不适合 | 需要预知 schema |
| Firecrawl prompt | 高 | 高 | 部分适合 | 灵活但外部服务 |
| Crawl4AI JsonCss | 低 | 0 | 不适合 | 需要预知 selector |
| AgentQL query | 中 | 高 | 部分适合 | 语义灵活但需外部服务 |
| Manus 代码生成 | 最高 | 0 | 适合但过重 | 写完整脚本太重 |

---

## 二、JS 执行 vs 高层抽象

### 2.1 page.evaluate 的灵活性与复杂性

**灵活性优势：**
- 完全继承浏览器 session 的 cookie、localStorage、认证状态
- 能访问任何 DOM 内容、全局 JS 变量、框架状态
- 无 LLM 成本，毫秒级执行
- 支持任意复杂的提取逻辑（循环、条件、正则）

**复杂性风险：**
- Playwright 文档明确：`page.evaluate()` 不等待元素就绪，可能导致 flaky 结果
- LLM 写的 JS 可能有语法错误或运行时异常
- 复杂 DOM 遍历代码难以一次写对

**关键缓解：** 在 extract 工具内部做 `await page.wait_for_load_state('domcontentloaded')` + SPA settle 检测，确保 DOM 稳定后再执行 JS。

**来源：** [Playwright evaluating 文档](https://playwright.dev/docs/evaluating)

### 2.2 LLM 写 JS 的可靠性

**量化证据：**
- CodeAct 论文（ICML 2024）：GPT-4 在 M3ToolEval 上 CodeAct 74.4% vs Text 53.7%（+20.7pp）
- NEXT-EVAL（2025）基准：LLM 在结构化 web 提取上 F1 > 0.95，前提是输入正确格式化
- llm-scraper 库证明 LLM 能为任意页面生成 `page.evaluate()` 代码并通过 schema 验证

**关键洞察：** LLM 写 JS 的可靠性取决于：
1. **输入质量**：给 LLM 看到的页面表示要精准（browse 返回值的质量）
2. **模式匹配**：常见 pattern（`querySelectorAll + map`、`JSON.parse`、`__NEXT_DATA__`）LLM 非常熟练
3. **错误反馈**：JS 执行报错时返回完整 error stack，LLM 能自我调试

### 2.3 常见 JS 提取 pattern

LLM 最擅长的提取 pattern（训练数据中大量存在）：

```javascript
// Pattern 1：嵌入 JSON（最高效）
JSON.parse(document.querySelector('#__NEXT_DATA__').textContent).props.pageProps

// Pattern 2：JSON-LD
Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
  .map(s => JSON.parse(s.textContent))

// Pattern 3：列表提取
Array.from(document.querySelectorAll('.item')).map(el => ({
  title: el.querySelector('.title')?.textContent?.trim(),
  link: el.querySelector('a')?.href,
  meta: el.querySelector('.meta')?.textContent?.trim()
}))

// Pattern 4：表格提取
(() => {
  const rows = document.querySelectorAll('table tbody tr');
  const headers = Array.from(document.querySelectorAll('table thead th'))
    .map(th => th.textContent.trim());
  return Array.from(rows).map(row => {
    const cells = row.querySelectorAll('td');
    return Object.fromEntries(headers.map((h, i) => [h, cells[i]?.textContent?.trim()]));
  });
})()

// Pattern 5：框架状态变量
window.__INITIAL_STATE__ || window.__NUXT__ || window.__APOLLO_STATE__

// Pattern 6：API 响应体中的数据（拦截后重放）
// 这个通常用 bash curl 而非 extract
```

### 2.4 是否需要预制 helper？

**结论：不需要。**

理由：
1. **侦察系统的特殊性**——每个站点结构不同，预制 helper 覆盖率低
2. **LLM 能力足够**——上述 pattern 在训练数据中极其丰富
3. **增加工具复杂度**——Helper 会增加参数设计负担，与"每个工具必须证明存在价值"原则冲突
4. **Vercel d0 教训**——"我们在替 model 思考"，预制 helper 是同样的错误

**替代方案：** 在 system prompt 中植入提取方法优先级知识（嵌入 JSON > API > DOM），以及常见 pattern 的提示。Agent 知道"怎么想"比"怎么做"更重要。

### 2.5 错误处理

**JS 执行失败时的诊断信息设计：**

```python
try:
    result = await page.evaluate(script)
except PlaywrightError as e:
    return {
        "success": False,
        "error_type": classify_error(e),  # syntax_error / runtime_error / timeout
        "error_message": str(e),
        "hint": generate_hint(e),  # "Check if the selector exists" / "Element may not be loaded yet"
        "page_url": page.url
    }
```

错误分类：
| 错误类型 | 含义 | 提示 |
|---------|------|------|
| `syntax_error` | JS 语法错误 | 提示修正语法 |
| `runtime_error` | 执行时异常（null reference 等） | 提示检查 selector 是否存在 |
| `timeout` | 执行超时 | 提示简化脚本或分步执行 |
| `empty_result` | 执行成功但返回 null/[]/{} | 提示页面可能未加载或 selector 不匹配 |

---

## 三、数据保存设计

### 3.1 文件格式选择

| 格式 | 优势 | 劣势 | 最佳场景 |
|------|------|------|---------|
| **JSONL** | 追加友好、流式处理、行级容错、内存效率 | 不便人类阅读嵌套 | **列表/批量数据（主选）** |
| **JSON** | 人类可读、支持深层嵌套、工具兼容好 | 追加需重写整个文件 | **单个对象/API 响应** |
| CSV | Excel 兼容、体积小 | 不支持嵌套、类型信息丢失 | 表格数据（不推荐默认） |

**结论：JSONL 作为默认格式。**

理由：
1. **追加友好**——extract 可能多次追加到同一文件（如分页提取），JSONL 只需 append
2. **容错**——如果提取中断，已写入的行不受影响
3. **内存友好**——不需要加载整个文件到内存
4. **行业共识**——Scrapy 默认输出 JSONL；Scrapfly、Crawl4AI 等推荐 JSONL 做批量数据

**例外：** 当提取结果是单个 JSON 对象（如 `__NEXT_DATA__`、单条 API 响应）时，存为 .json。

**来源：** [Scrapfly: JSONL vs JSON](https://scrapfly.io/blog/posts/jsonl-vs-json)

### 3.2 文件命名设计

**方案：`save_as` 参数，agent 控制路径和文件名。**

理由：
1. **Agent 有最佳上下文**——知道提取的是什么数据、来自哪里
2. **Manus 验证**——文件系统作为外部记忆，命名由 agent 决定
3. **自动命名不可靠**——URL 不一定反映内容、时间戳不可读

**设计细节：**
- `save_as` 是相对于 `artifacts/samples/` 的路径
- 自动创建中间目录
- 如果文件已存在，追加而非覆盖（JSONL 格式天然支持）
- 如果 agent 未提供 `save_as`，自动生成：`{url_slug}_{timestamp}.jsonl`

**目录约定：**
```
artifacts/
├── samples/          ← extract 的数据样本
│   ├── pens/
│   │   ├── threejs_tag_page1.jsonl
│   │   └── pen_detail_abc123.json
│   └── api/
│       └── search_response.json
├── workspace/        ← agent 的工作文件
└── manifest.json     ← 索引
```

### 3.3 大数据集处理

**侦察系统的特殊性：** 提取目的是"样本"而非"全量"。大数据集在侦察中不常见。

**防护措施：**
- 单次 extract 返回值硬限：前 100 条记录写文件，超过的丢弃并在返回中提示
- 文件大小软限：>1MB 时警告 agent "数据量较大，考虑是否需要全部"
- 不做分片——如果 agent 需要更多数据，显式多次调用

---

## 四、返回值设计（信息密度控制）

### 4.1 核心原则

**来自调研的两个关键发现：**

1. **AgentOccam（ICLR 2025）：** 仅优化观察空间效果比改进架构高 161%。返回值设计是最高杠杆。
2. **Pointer 论文（arXiv:2511.22729）：** 传统方法平均 6,411 tokens vs pointer 方法 841 tokens（7.6x 削减），且无信息损失。

**设计原则：** 返回**摘要 + 外部引用**，不返回完整数据。完整数据存文件，摘要中包含足够信息让 agent 决定下一步。

### 4.2 返回值 Schema

```python
# 成功时
{
    "success": True,
    "file": "samples/pens/threejs_tag.jsonl",     # 文件路径
    "record_count": 24,                            # 记录数
    "fields": ["title", "author", "views", "likes"], # 字段名列表
    "preview": [                                    # 前 3 条预览
        {"title": "3D Globe", "author": "user1", "views": 1200, "likes": 45},
        {"title": "Particle System", "author": "user2", "views": 890, "likes": 32},
        {"title": "Shader Art", "author": "user3", "views": 2100, "likes": 78}
    ],
    "quality": {                                    # 数据质量指标
        "null_rate": {"views": 0.0, "likes": 0.04}, # 字段级 null 率（仅报告有 null 的字段）
        "empty_fields": [],                          # 全 null 字段
        "type_consistency": True                     # 各字段类型是否一致
    }
}

# 失败时
{
    "success": False,
    "error_type": "runtime_error",
    "error_message": "Cannot read properties of null (reading 'textContent')",
    "hint": "The selector '.pen-title' did not match any elements. The page may use different class names.",
    "page_url": "https://codepen.io/tag/threejs"
}
```

### 4.3 各字段的设计理由

| 字段 | 理由 | 信息量 |
|------|------|--------|
| `record_count` | Agent 判断"提取是否完整"的核心指标 | 1 token |
| `fields` | Agent 判断"字段覆盖是否充分"，对比不同提取方式 | ~10 tokens |
| `preview` | Agent 验证"数据是否正确"，发现类型/格式问题 | ~100-200 tokens |
| `quality.null_rate` | Agent 判断"提取质量"，决定是否换方法 | ~20 tokens |
| `file` | Agent 引用样本路径，写入 note_insight | 1 token |

**总 token 预算：** ~200-400 tokens/次提取，远低于完整数据。

### 4.4 各系统的返回值对比

| 系统 | 返回什么 | 信息密度 |
|------|---------|---------|
| Stagehand | 完整数据（类型安全） | 高但占 context |
| Firecrawl | 完整结构化数据 + status | 高但占 context |
| Manus | 写入文件，通过 shell 输出确认 | 低——需要额外读文件 |
| Claude Code | 返回 ≤25,000 tokens | 截断 |
| **我们** | **摘要 + 文件引用** | **~300 tokens，信息完整** |

我们的方案结合了 Manus 的"数据存文件"和 Stagehand 的"结构化反馈"，取两者之长。

---

## 五、嵌入数据检测与提取

### 5.1 常见嵌入 JSON 模式

| 模式 | 框架 | 检测方式 | 数据位置 | 数据量 |
|------|------|---------|---------|--------|
| `<script id="__NEXT_DATA__">` | Next.js Pages Router | 固定 ID script 标签 | `props.pageProps` | 30KB-500KB |
| `self.__next_f.push()` | Next.js App Router (RSC) | 内联 script 中 flight data | 行分隔协议（chunk_id:type:payload） | 变化大 |
| `window.__NUXT__` | Nuxt.js 2/3 | 内联 script | devalue 序列化 | 中等 |
| `window.__INITIAL_STATE__` | Redux/Vuex | 内联 script | JSON 状态树 | 变化大 |
| `window.__APOLLO_STATE__` | Apollo GraphQL | 内联 script | GraphQL cache | 变化大 |
| `<script type="application/ld+json">` | Schema.org | 标准化标签 | JSON-LD 结构化数据 | 通常 <10KB |
| `<script type="application/json">` | 各种 | 通用 JSON 标签 | 组件数据 | 变化大 |

### 5.2 检测与提取的分工

**结论：browse 检测，extract 提取。**

理由：
1. **检测是感知层工作**——属于"页面上有什么"的观察，是 browse 的职责
2. **提取需要 agent 决策**——选择提取 `__NEXT_DATA__` 的哪部分、是否需要过滤
3. **Agent-E 模式验证**——交互后的变化反馈（browse 的附加信息）引导 agent 做出更好的行动决策

**browse 的检测内容（作为返回值的一部分）：**
```
Data Signals Detected:
- __NEXT_DATA__ found (187KB, pageProps contains 'pens' array with 15 items)
- JSON-LD found (2 blocks: Product, BreadcrumbList)
- API requests captured: GET /api/v1/pens?tag=threejs → 200 (JSON, 15 items)
```

**Agent 看到信号后的决策示例：**
```
Agent: "browse 检测到 __NEXT_DATA__ 有 187KB 的 pageProps。
       让我用 extract 提取 pageProps.pens 数组。"
→ extract(script="JSON.parse(document.getElementById('__NEXT_DATA__').textContent).props.pageProps.pens")
```

**框架检测脚本（在 browse 内部自动执行）：**
```javascript
const signals = {};
// Next.js Pages Router
const nd = document.getElementById('__NEXT_DATA__');
if (nd) signals.next_data = { size: nd.textContent.length, keys: Object.keys(JSON.parse(nd.textContent).props?.pageProps || {}) };
// Next.js App Router RSC
if (typeof self !== 'undefined' && self.__next_f) signals.next_rsc = true;
// Nuxt
if (window.__NUXT__) signals.nuxt = { keys: Object.keys(window.__NUXT__) };
// Vue/Vuex
if (window.__INITIAL_STATE__) signals.initial_state = { keys: Object.keys(window.__INITIAL_STATE__) };
// Apollo
if (window.__APOLLO_STATE__) signals.apollo = true;
// JSON-LD
const jsonld = document.querySelectorAll('script[type="application/ld+json"]');
if (jsonld.length) signals.json_ld = { count: jsonld.length, types: Array.from(jsonld).map(s => { try { return JSON.parse(s.textContent)['@type'] } catch { return 'parse_error' }}) };
```

### 5.3 为什么不自动提取

有些系统（如 Firecrawl）在爬取时自动提取嵌入 JSON。我们不这样做：

1. **侦察系统的目标是理解**——自动提取所有嵌入数据会产生大量噪音
2. **Agent 需要选择性**——`__NEXT_DATA__` 可能有 500KB，只需要其中一小部分
3. **硬约束"不预设数据 schema"**——自动提取预设了"所有嵌入数据都有价值"

---

## 六、与 browse 的关系

### 6.1 当前流程评估

```
browse(url) → 页面摘要 + 数据信号检测
    ↓
Agent 推理（think）
    ↓
extract(script) → 执行 JS 提取 + 保存 + 返回摘要
```

**这个流程是否最优？**

**结论：是最优的。**

理由：
1. **感知-决策-行动分离**——符合认知循环设计（AgentSession设计.md §二）
2. **Agent 自主性**——Agent 看到数据信号后自己决定是否提取、提取什么，符合"不硬编码控制流"硬约束
3. **信息效率**——browse 的信号检测消耗极少（~50ms JS 执行），但为 agent 的下一步提供高价值信息

### 6.2 是否有系统合并检测和提取？

| 系统 | 合并程度 | 结果 |
|------|---------|------|
| Firecrawl | 完全合并（scrape = 获取 + 转换 + 提取） | 适合批量爬取，不适合探索 |
| Stagehand | 部分合并（extract 直接在页面上语义提取） | 需要预知 schema |
| Browser-Use | 无显式 extract——LLM 从页面状态中"读取" | 数据不持久化、占 context |
| OpenHands | 分离（browse → CodeAct 处理） | 灵活但两步 |
| **我们** | **分离但紧耦合**（browse 提供信号，extract 执行） | **灵活且信息高效** |

**关键：** 合并检测和提取适合"知道要什么"的爬虫场景。侦察系统需要先看再决定，分离是正确选择。

---

## 七、与 bash 的边界

### 7.1 核心区分

| 维度 | extract（浏览器内） | bash curl（浏览器外） |
|------|-------------------|---------------------|
| 执行环境 | 浏览器 page context | 系统 shell |
| Cookie/Session | 自动继承浏览器状态 | 需手动传递（从 browse 的网络捕获中获取） |
| JS 渲染 | 能访问渲染后 DOM | 只能获取原始 HTML |
| 框架状态 | 能访问 `window.*` 变量 | 无法访问 |
| 速度 | 快（毫秒级 JS 执行） | 快（网络请求级） |
| 适用场景 | DOM 数据、嵌入 JSON、需要 session 的操作 | API 重放、无需渲染的请求、数据处理 |

### 7.2 使用场景决策树

```
需要提取数据？
├─ 数据在渲染后的 DOM 中？ → extract
├─ 数据在嵌入 JSON 中（__NEXT_DATA__ 等）？ → extract
├─ 数据在浏览器 JS 变量中？ → extract
├─ 数据在 API 中（browse 已捕获）？
│   ├─ API 需要 session cookie？ → extract（通过 fetch()）或 bash（手动带 cookie）
│   └─ API 无需认证？ → bash curl（更灵活，支持翻页参数化）
├─ 需要处理/分析已提取的数据？ → bash（python3）
└─ 需要下载文件？ → bash（wget/curl）
```

### 7.3 边界清晰度

**结论：边界清晰，核心判断标准是"是否需要浏览器 session 状态"。**

- 需要浏览器状态（cookie、DOM、JS 变量）→ extract
- 不需要浏览器状态（独立 HTTP 请求、数据处理）→ bash
- 灰色地带：API 需要 cookie 但不需要 JS 渲染 → 两者都可，extract 更简单（`await fetch()`），bash 更灵活（循环翻页）

**关键洞察：** 从 browse 的网络捕获中获取 cookie/header 后，bash curl 可以重放任何 API。这是从"浏览器内"到"浏览器外"的桥梁，由 agent 自主决定何时跨越。

---

## 八、参数设计

### 8.1 最终参数 Schema

```json
{
    "name": "extract",
    "description": "Execute JavaScript in the browser context to extract data from the current page. The script runs in the same session as browse/interact, inheriting all cookies and authentication state.\n\nUse this for:\n- Extracting data from the rendered DOM (querySelectorAll + map)\n- Reading embedded JSON (__NEXT_DATA__, JSON-LD, window.__INITIAL_STATE__)\n- Accessing JavaScript variables and framework state\n- Any extraction that needs the browser's session context\n\nDo NOT use this for:\n- API calls that don't need browser cookies (use bash curl instead)\n- Data processing/analysis (use bash python3 instead)\n- Downloading files (use bash wget/curl instead)\n\nExtraction priority: Embedded JSON > Captured API replay > DOM selectors\n\nThe script should return the extracted data (array or object). Data is automatically saved to a file and a summary is returned (record count, field names, preview, quality metrics). Full data is NOT returned to save context space.",
    "parameters": {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "JavaScript code to execute via page.evaluate(). Must return the extracted data (array of objects for multiple records, or a single object). Use arrow function or IIFE for complex logic. Example: `Array.from(document.querySelectorAll('.item')).map(el => ({title: el.querySelector('h2')?.textContent?.trim(), url: el.querySelector('a')?.href}))`"
            },
            "save_as": {
                "type": "string",
                "description": "File path relative to artifacts/samples/ to save extracted data. Use descriptive names that reflect the content. Examples: 'pens/threejs_tag_page1.jsonl', 'api/search_results.json'. If the result is an array, saved as JSONL (one JSON object per line). If a single object, saved as JSON. Intermediate directories are created automatically. If file exists, new records are appended (JSONL) or file is overwritten (JSON)."
            }
        },
        "required": ["script"]
    }
}
```

### 8.2 参数设计决策

**`script` (必需)**

- **类型：** string（JS 代码）
- **为什么是 string 而非结构化：** 侦察系统需要灵活性。结构化参数（如 selector + schema）限制了提取逻辑的表达能力。LLM 写 JS 的可靠性已通过 CodeAct 论文和 llm-scraper 验证。
- **防错：** 工具描述中给出常见 pattern 示例，降低 LLM 犯错概率。

**`save_as` (可选)**

- **为什么可选：** 有时 agent 只想"看看页面上有什么数据"（探索性提取），不需要保存。此时返回摘要但不写文件。
- **为什么不自动生成文件名：** Agent 控制命名让文件系统成为有意义的外部记忆（Manus 模式验证）。
- **兜底：** 如果未提供 `save_as` 但有数据需要保存的场景，不保存文件，仅返回摘要（含 preview）。

**曾考虑但放弃的参数：**

| 参数 | 放弃理由 |
|------|---------|
| `selector`（CSS/XPath 定位范围） | `script` 本身可以包含 selector 逻辑，独立参数增加语义重叠 |
| `schema`（Zod/JSON Schema 验证） | 侦察系统不预知 schema，强制验证会阻碍探索 |
| `format`（json/jsonl/csv） | 自动推断：数组→JSONL，单对象→JSON。减少 agent 决策负担 |
| `timeout` | 统一使用工具级默认超时（10s），极端场景 agent 用 bash 处理 |
| `wait_for`（等待元素出现） | 基础设施层（SPA settle）已处理，不暴露给 agent |
| `key`（原 CLAUDE.md 设计） | 被 `save_as` 替代，`key` 语义模糊 |

### 8.3 防错设计

| 风险 | 防错措施 |
|------|---------|
| JS 语法错误 | 捕获异常，返回 error_type + error_message + hint |
| 空结果 | 检测 null/[]/{}，返回 `empty_result` 错误类型 + 提示 |
| 超大结果 | 截断前 100 条写入文件，返回中提示 `truncated: true, total_available: N` |
| 危险操作（如 `document.write`） | 不做限制（Docker 容器安全边界），但工具描述中不鼓励 |
| `save_as` 路径注入 | 限制在 `artifacts/samples/` 下，过滤 `..` |
| 非序列化返回值（DOM 元素、函数） | `JSON.stringify` 失败时返回错误提示 |

---

## 九、质量保证

### 9.1 自动质量指标

extract 工具在每次成功提取后自动计算：

```python
def compute_quality(data: list[dict]) -> dict:
    if not data:
        return {"empty": True}

    all_fields = set()
    for record in data:
        all_fields.update(record.keys())

    null_rates = {}
    for field in all_fields:
        null_count = sum(1 for r in data if r.get(field) is None or r.get(field) == "")
        rate = null_count / len(data)
        if rate > 0:
            null_rates[field] = round(rate, 2)

    empty_fields = [f for f, r in null_rates.items() if r == 1.0]

    # 类型一致性检查
    type_consistent = True
    for field in all_fields:
        types = set(type(r.get(field)).__name__ for r in data if r.get(field) is not None)
        if len(types) > 1:
            type_consistent = False
            break

    return {
        "null_rate": null_rates,
        "empty_fields": empty_fields,
        "type_consistency": type_consistent
    }
```

### 9.2 质量指标的意义

| 指标 | Agent 行为触发 |
|------|--------------|
| `null_rate` 高（>50%） | Selector 可能失效，需要换方法 |
| `empty_fields` 存在 | 某些字段可能需要不同提取路径 |
| `type_consistency` = False | 数据清洗可能不完整 |
| `record_count` 异常低 | 分页未处理，或 selector 太窄 |
| `record_count` = 0 | 提取逻辑完全不匹配 |

### 9.3 不做自动重试

**结论：不做。**

理由：
1. **失败是信息**——在侦察系统中，"这个方法不行"是高价值 observation
2. **Agent 应该决定重试策略**——换 selector、换方法、还是放弃，取决于上下文
3. **自动重试掩盖根因**——Agent 看不到中间失败，无法学习

### 9.4 自动写入 World Model

每次 extract（无论成功失败）自动写入 Observation：

```python
# 成功时
observation = {
    "extraction_method": "js_extract",
    "script_summary": script[:200],  # 脚本前 200 字符（不存完整脚本）
    "record_count": len(data),
    "fields": list(all_fields),
    "sample_ref": file_path,         # 文件路径引用
    "quality": quality_metrics
}

# 失败时
observation = {
    "extraction_failed": True,
    "script_summary": script[:200],
    "error_type": error_type,
    "error_message": error_message
}
```

---

## 十、完整设计方案

### 10.1 参数 Schema（最终版）

```json
{
    "name": "extract",
    "description": "Execute JavaScript in the browser to extract data from the current page. Inherits browser session (cookies, auth state).\n\nBest for: DOM data, embedded JSON (__NEXT_DATA__, JSON-LD), JS variables.\nNot for: standalone API calls (use bash curl), data processing (use bash python3).\n\nPriority: embedded JSON > captured API > DOM selectors.\n\nReturns a summary (count, fields, preview, quality). Full data saved to file, not returned.",
    "parameters": {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "JavaScript to execute via page.evaluate(). Must return extracted data (array or object)."
            },
            "save_as": {
                "type": "string",
                "description": "Path relative to artifacts/samples/. Array → JSONL, object → JSON. Dirs auto-created. Omit to only preview without saving."
            }
        },
        "required": ["script"]
    }
}
```

### 10.2 返回值 Schema（最终版）

```python
# 成功 + 保存
{
    "success": True,
    "file": "samples/pens/threejs_tag.jsonl",
    "record_count": 24,
    "fields": ["title", "author", "views", "likes"],
    "preview": [
        {"title": "3D Globe", "author": "user1", "views": 1200, "likes": 45},
        # ... 前 3 条
    ],
    "quality": {
        "null_rate": {"likes": 0.04},
        "empty_fields": [],
        "type_consistency": True
    }
}

# 成功 + 不保存（未提供 save_as）
{
    "success": True,
    "file": None,
    "record_count": 24,
    "fields": ["title", "author", "views", "likes"],
    "preview": [
        {"title": "3D Globe", "author": "user1", "views": 1200, "likes": 45},
        # ... 前 3 条
    ],
    "quality": {
        "null_rate": {"likes": 0.04},
        "empty_fields": [],
        "type_consistency": True
    }
}

# 失败
{
    "success": False,
    "error_type": "runtime_error",  # syntax_error | runtime_error | timeout | empty_result
    "error_message": "Cannot read properties of null (reading 'textContent')",
    "hint": "Selector '.pen-title' matched 0 elements. Check if the page uses different class names or if content needs interaction to load.",
    "page_url": "https://codepen.io/tag/threejs"
}
```

### 10.3 实现要点

```python
async def execute_extract(script: str, save_as: str | None, page: Page, wm: SiteWorldModel, step: int) -> dict:
    """extract 工具的核心实现"""

    # 1. 确保 DOM 稳定（基础设施层）
    await wait_for_dom_settle(page)

    # 2. 执行 JS
    try:
        raw_result = await asyncio.wait_for(
            page.evaluate(script),
            timeout=10.0
        )
    except PlaywrightError as e:
        error_info = classify_and_format_error(e, script, page.url)
        # 写入失败 observation
        await wm.add_observation(
            location_pattern=url_to_pattern(page.url),
            raw={"extraction_failed": True, "script_summary": script[:200],
                 "error_type": error_info["error_type"], "error_message": str(e)},
            step=step
        )
        return error_info

    # 3. 结果验证
    if raw_result is None or raw_result == [] or raw_result == {}:
        error_info = {
            "success": False, "error_type": "empty_result",
            "error_message": "Script returned empty result (null, [], or {}).",
            "hint": "The extraction logic may not match the page structure. Try inspecting the page with browse first.",
            "page_url": page.url
        }
        await wm.add_observation(
            location_pattern=url_to_pattern(page.url),
            raw={"extraction_failed": True, "script_summary": script[:200],
                 "error_type": "empty_result"},
            step=step
        )
        return error_info

    # 4. 标准化为列表
    if isinstance(raw_result, dict):
        data_list = [raw_result]
        is_single = True
    elif isinstance(raw_result, list):
        data_list = raw_result[:100]  # 硬限 100 条
        is_single = False
    else:
        # 原始值（string, number）包装为 dict
        data_list = [{"value": raw_result}]
        is_single = True

    truncated = isinstance(raw_result, list) and len(raw_result) > 100

    # 5. 计算质量指标
    quality = compute_quality(data_list)

    # 6. 提取字段名
    all_fields = list(set().union(*(r.keys() for r in data_list if isinstance(r, dict))))

    # 7. 保存文件
    file_path = None
    if save_as:
        full_path = Path(ARTIFACTS_DIR) / "samples" / save_as
        full_path.parent.mkdir(parents=True, exist_ok=True)

        if is_single or save_as.endswith('.json'):
            # 单对象或显式 .json → JSON 格式
            with open(full_path, 'w', encoding='utf-8') as f:
                json.dump(raw_result if is_single else data_list, f, ensure_ascii=False, indent=2)
        else:
            # 列表 → JSONL 格式，追加模式
            with open(full_path, 'a', encoding='utf-8') as f:
                for record in data_list:
                    f.write(json.dumps(record, ensure_ascii=False) + '\n')

        file_path = f"samples/{save_as}"

    # 8. 构造预览
    preview = data_list[:3]

    # 9. 写入成功 observation
    obs_raw = {
        "extraction_method": "js_extract",
        "script_summary": script[:200],
        "record_count": len(data_list),
        "fields": all_fields,
        "quality": quality
    }
    if file_path:
        obs_raw["sample_ref"] = file_path

    await wm.add_observation(
        location_pattern=url_to_pattern(page.url),
        raw=obs_raw,
        step=step
    )

    # 10. 返回摘要
    result = {
        "success": True,
        "file": file_path,
        "record_count": len(data_list),
        "fields": all_fields,
        "preview": preview,
        "quality": quality
    }
    if truncated:
        result["truncated"] = True
        result["total_available"] = len(raw_result)

    return result
```

### 10.4 关键设计决策汇总

| 决策 | 选择 | 理由 | 对比方案 |
|------|------|------|---------|
| 提取方式 | 纯 JS（page.evaluate） | 侦察系统需要灵活性；LLM 写 JS 可靠性已验证 | Schema 驱动（Stagehand）需预知结构 |
| 无预制 helper | Agent 写 JS | 每个站点不同，helper 覆盖率低；减少工具复杂度 | 预制 extractTable/extractList |
| 默认文件格式 | JSONL（数组）/ JSON（单对象） | 追加友好、容错、行业共识 | 强制 JSON / 让 agent 选 |
| save_as 可选 | Agent 可不保存 | 支持探索性提取（只想看看有什么） | 强制保存 |
| 文件命名 | Agent 控制 | Agent 有最佳上下文；文件系统作为外部记忆 | 自动生成（不可读） |
| 返回值 | 摘要 + 文件引用 | 控制 context token（~300）；完整数据存文件 | 返回完整数据（占 context） |
| 质量指标 | 自动计算 null_rate + type_consistency | 指导 agent 判断提取质量 | 无质量反馈 |
| 不自动重试 | 失败是信息 | 侦察系统中"方法不行"是高价值发现 | 自动重试 3 次 |
| 不自动提取嵌入 JSON | 分离检测和提取 | Agent 需要选择性；不预设什么数据有价值 | browse 时自动提取 |
| 自动写 Observation | 成功和失败都写入 | 保证 World Model 完整性；Procedural Memory 积累 | 只写成功 |
| 结果截断 | 100 条硬限 | 侦察目的是样本不是全量 | 无限制（占磁盘/context） |
| browse→extract 分离 | 感知-决策-行动分离 | 符合认知循环设计；Agent 自主决定 | 合并检测+提取 |

---

## 附录：数据流全景

```
browse(url)
  ├─ 页面渲染 + SPA settle
  ├─ 页面摘要（标题、元素、链接模式）
  ├─ 数据信号检测（__NEXT_DATA__, JSON-LD, API 捕获）
  └─ 返回给 Agent（~500 tokens）
      │
Agent 推理（think）："有 __NEXT_DATA__，187KB，含 pens 数组"
      │
extract(script="JSON.parse(...).props.pageProps.pens", save_as="pens/threejs.jsonl")
  ├─ 等待 DOM 稳定
  ├─ page.evaluate(script)
  ├─ 验证结果非空
  ├─ 计算质量指标（null_rate, type_consistency）
  ├─ 保存到 artifacts/samples/pens/threejs.jsonl（JSONL 格式）
  ├─ 写入 Observation（extraction_method + fields + sample_ref + quality）
  └─ 返回摘要给 Agent（~300 tokens）
      │
Agent 推理："24 条记录，4 个字段，likes 有 4% null。
            让我 note_insight 记录发现。"
      │
note_insight("Tag 页 /tag/threejs 的 __NEXT_DATA__ 包含 24 条 pen，字段: title, author, views, likes。数据质量好，likes 有少量 null。")
```

---

## Sources

- [Stagehand extract() API](https://docs.stagehand.dev/references/extract)
- [Firecrawl Extract Endpoint](https://docs.firecrawl.dev/features/extract)
- [Mastering Firecrawl Extract](https://www.firecrawl.dev/blog/mastering-firecrawl-extract-endpoint)
- [Crawl4AI LLM-Free Strategies](https://docs.crawl4ai.com/extraction/no-llm-strategies/)
- [Crawl4AI LLM Strategies](https://docs.crawl4ai.com/extraction/llm-strategies/)
- [AgentQL Query Language](https://docs.agentql.com/agentql-query)
- [Skyvern GitHub](https://github.com/Skyvern-AI/skyvern)
- [Manus Leaked Prompt Analysis](https://gist.github.com/jlia0/db0a9695b3ca7609c9b1a08dcbf872c9)
- [Manus Technical Investigation](https://gist.github.com/renschni/4fbc70b31bad8dd57f3370239dccd58f)
- [Devin Web Scraping](https://docs.devin.ai/use-cases/web-scraping)
- [Morph: AI Web Scraping Benchmarks](https://www.morphllm.com/ai-web-scraping)
- [Scraping Next.js in 2025 (Trickster Dev)](https://www.trickster.dev/post/scraping-nextjs-web-sites-in-2025/)
- [Scrapfly: JSONL vs JSON](https://scrapfly.io/blog/posts/jsonl-vs-json)
- [Zyte Data Quality Validation](https://www.zyte.com/blog/guide-to-web-data-extraction-qa-validation-techniques/)
- [Context Window Overflow (arXiv:2511.22729)](https://arxiv.org/html/2511.22729v1)
- [Playwright Evaluating JS](https://playwright.dev/docs/evaluating)
- [CodeAct (ICML 2024)](https://arxiv.org/abs/2402.01030)
- [AgentOccam (ICLR 2025)](https://arxiv.org/abs/2508.04412)
- [Beyond Browsing (ACL 2025)](https://arxiv.org/abs/2410.16464)
- [Firecrawl Scraper vs Crawler](https://www.firecrawl.dev/blog/scraper-vs-crawler)
- [Browser-Use Source Code (deepwiki)](https://deepwiki.com/browser-use/browser-use/2.4-dom-processing-engine)
