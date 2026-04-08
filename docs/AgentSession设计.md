# Agent Session 设计

> 状态：共识稿 v4（2026-04-08 与工具重新设计共识对齐）
> 日期：2026-03-28（最后更新：2026-04-08）
> 前置：系统架构与信息流.md、WorldModel设计.md、工具重新设计共识.md
>
> ⚠️ 工具集已从 7 个更新为 12 个，interact 已拆分为 5 个独立工具，
> extract 更名为 browser_eval，录制 Agent 删除（录制 Agent 负责）。
> 详见 `docs/工具重新设计共识.md`。
> 调研基础：Browser-Use, Agent-E, Stagehand, AgentQL, Skyvern, Vercel agent-browser, rtrvr.ai, Playwright MCP, OpenHands, Manus, Devin, SWE-Agent, Claude Code, Firecrawl, Crawl4AI, CodeAct, D2Snap, AgentOccam, Anthropic PTC, Gorilla, ToolLLM, API-Bank, RAG-MCP, LATM

---

## 一、Session 的本质

Session 是系统的**执行层**——每个 session 是一个独立的 agent 实例，接收 briefing，自主探索，产出 observations。

类比：Session 是一次田野调查。调查员带着任务书出发，到实地观察、记录、采样，带着发现回来。

### Agent 的使命

**在 briefing 指定的方向上，最大化高质量 observations 的产出。**

"高质量"意味着经过验证的、有证据的发现，不是简单地浏览很多页面。

### 任务光谱

Planner 给出的 briefing 本质上对应不同类型的任务：

| 类型 | 典型 briefing | Agent 的行为模式 |
|------|-------------|-----------------|
| 发现型 | "探索这个站点的 tag 系统" | 广度优先，快速浏览多个页面，记录结构和模式 |
| 提取型 | "从 /pen/{id} 页面提取 pen 的完整字段" | 深度优先，尝试多种提取方式，验证字段完整性 |
| 验证型 | "验证 API 端点是否返回和页面相同的数据" | 交叉比对，严谨记录差异 |
| 补盲型 | "Semantic Model 中 /collections 标注'量级未知'，去确认" | 精确定向，回答具体问题 |

Agent 的**结构不随任务类型变化**——同一个 agent，同样的工具和认知框架，只是 briefing 不同。任务类型的差异完全通过 briefing 内容传达。

---

## 二、认知循环（共识）

Agent 在 session 内经历的认知循环：

```
感知（browse / read_network → 看到页面内容和网络请求）
  ↓
解读（think → 这意味着什么？有什么数据？）
  ↓
行动（browser_eval / click / input / bash → 做点什么）
  ↓
验证（结果符合预期吗？数据完整吗？）
  ↓
决策（继续当前方向？换方法？探索新发现？该停了？）

注：Agent 不负责记录——Session 结束后由录制 Agent 从完整 message array 中提取结构化 Observations。
  ↓
（循环）
```

**这个循环不是代码强制的，是从 LLM 推理中涌现的。** 设计的职责是**创造条件让它涌现**——通过正确的工具设计、system prompt、和信息呈现方式。

---

## 三、步数预算（共识）

**不固定步数。** 不同任务复杂度差异巨大——发现型任务 10 步够了，深度提取可能需要 80 步。

- **Planner 在 briefing 中给软预算**：Planner 比固定值更能判断任务需要的步数
- **硬上限兜底**：防止失控的安全网（如 100 步）
- Planner 的预算判断随经验改进——前几轮可能不准，后续通过 session 结果反馈调整

---

## 四、进度意识与工作记忆（共识）

### 核心问题

Agent 在长 session 中需要"进度意识"——知道自己做了什么、还差什么。这不需要额外结构，靠 **context 管理 + World Model 外部记忆** 解决。

### Context 分层衰减

Session 内的 context 管理采用分层衰减（与 Planner 的策略一致）：

```
最近 N 步 → 完整保留（tool call + 完整返回值）
更早的步骤 → 保留 tool name + 结果摘要（程序化截断，不是 LLM 摘要）
```

早期发现的核心信息仍然存在，只是细节被压缩。

### World Model 作为外部记忆

- Agent 的关键发现由录制 Agent（session 后）写入 World Model
- Context 内丢失的信息可以通过 `read_world_model` 找回
- Agent 不需要主动记录——它的 context 就是工作记忆，录制 Agent 负责结构化

### 不需要 Session 级 Model

Session 内不需要独立的 model 文档。原因：

1. Agent 写的 observations 已进入全局 World Model，可随时查询
2. Semantic Model 和 Procedural Model 由 Planner 维护，agent 可读
3. 再加一层 session-level model 是多余的复杂度——进度意识靠 context 管理解决
4. Session 是临时执行单元，它的"记忆"就是 context + World Model 的组合

---

## 五、失败模式与对策（共识）

| 失败模式 | 表现 | 需要的能力 | 靠什么提供 |
|---------|------|-----------|----------|
| 漫无目的 | 浏览很多页面没有清晰目的 | 目标清晰 | Briefing + 自我监控 |
| 浅尝辄止 | 发现数据但不验证不深入 | 验证纪律 | System prompt 认知原则 |
| 反复撞墙 | 同一种失败方式试了三遍 | 方法迭代 | Procedural Model（failed approaches） |
| 遗忘 | 早期发现被 context 删掉 | 工作记忆 | Context 分层衰减 + World Model |
| 不收敛 | 一直探索不提取 | 进度意识 | Context 保持连贯 + briefing 定义完成标准 |
| 过早停止 | 做了一点就觉得够了 | 完整性标准 | Briefing 定义什么算完成 |

大多数失败模式靠 prompt 和 briefing 引导涌现解决。需要结构支撑的是：
- **遗忘** → context 分层衰减 + World Model 外部记忆
- **反复撞墙** → Procedural Model 中的 Failed Approaches

---

## 六、停止条件（共识）

Session 内的停止判断：

1. **LLM 不再调工具** → 自然结束（agent 认为任务完成）
2. **步数达到硬上限** → 强制结束
3. **连续框架级失败 ≥ N** → 强制结束（框架错误，非正常工具返回的"没找到"类软错误）

停止条件由代码强制。LLM 的自然停止是最理想的出口——agent 自己判断任务完成。

---

## 七、工具集设计

### 能力来源分离（共识）

Agent 的能力来自五个来源，不能混为一谈：

| 来源 | 解决什么 | 对 agent 可见？ | 设计位置 |
|------|---------|---------------|---------|
| **工具接口** | agent 能做什么、看到什么 | ✓ agent 主动调用 | 本节 |
| **基础设施** | 做的时候怎么做得可靠 | ✗ 透明执行 | §七.3 |
| **System Prompt** | agent 怎么想 | ✓ 认知框架 | §八 |
| **Context 管理** | agent 记住什么 | ✗ 框架管理 | §九 |
| **模型能力** | 推理质量 | 给定约束 | 不可设计 |

工具接口 = input（agent 给工具什么）+ output（工具给 agent 什么）。其中 output 设计是价值最高的部分——AgentOccam（ICLR 2025）证明仅优化观察空间效果比改进架构高 161%。

### 7.1 工具集组成（共识 v4——已更新）

> 详细设计见 `docs/工具重新设计共识.md`

**12 个工具，5 个类别：**

```
浏览器感知（3 个，三个不可互替的能力层）：
  browse(url?, new_tab?, tab?)          — 页面内容快照 + 多标签页导航
  read_network(filter?)                 — 网络层信息（请求/响应/cookies）
  browser_eval(script, save_as?)        — 浏览器内 JS 执行（探测/提取/检查）

浏览器管理（1 个）：
  browser_reset(proxy?, browser_type?, headed?) — 重启浏览器到新配置

页面交互（5 个，按参数模式拆分）：
  click(target)                         — 点击元素
  input(target, value)                  — 输入/选择
  press_key(key, target?)               — 按键
  scroll(direction?, amount?, target?)  — 滚动
  go_back()                             — 浏览器后退

系统执行（1 个）：
  bash(command)                         — 浏览器外代码执行

认知辅助（2 个）：
  think(thought)                        — 推理
  read_world_model(section?)            — 查询 World Model
```

**与旧版（7 工具）的变化：**
- browse 拆出 read_network 和 browser_eval（三个不可互替的 Playwright 能力层）
- interact 拆为 5 个独立工具（一工具一能力，不做 enum 多路复用）
- extract 更名 browser_eval，从数据提取专用拓宽为通用 JS 执行
- 新增 browser_reset（agent 自主管理浏览器配置）
- **删除 录制 Agent**——录制 Agent 全权负责 Observation 写入
- 总数 7 → 12，仍在 8-15 的实证甜区

### 7.2 泛用性验证（共识 v4——已更新）

12 个工具覆盖所有非限制场景：

| 场景 | 工具 |
|------|------|
| 浏览页面看结构 | browse |
| 查看网络请求/API/cookies | read_network |
| 从 DOM/嵌入 JSON 提取数据 | browser_eval |
| 探测页面数据格局 | browser_eval |
| 点击元素 | click |
| 输入/选择 | input |
| 按键（Enter/Escape/Tab） | press_key |
| 滚动（含水平/容器内） | scroll |
| 浏览器后退 | go_back |
| 被检测/换代理/换浏览器 | browser_reset |
| 重放 API / 数据处理 / 文件操作 | bash（curl_cffi/python3） |
| 搜索站点相关信息 | bash（搜索 API） |
| 回顾进展 | read_world_model |
| 先想后做 | think |

### 7.3 基础设施层（共识 v4——对 agent 透明，不是工具）

以下能力由框架自动处理，agent 不需要感知也不需要操作：

**交互智能行为（参考 Browser-Use）：**

| 行为 | 触发时机 | 效果 |
|------|---------|------|
| click 元素类型识别 | click 目标是 `<select>` | 自动转为展示下拉选项 |
| input autocomplete 检测 | input 目标有 `combobox`/`aria-autocomplete` | 延迟 400ms 等待建议列表 |
| input 值验证 | input 执行后 | 检查实际值是否匹配，被格式化时警告 |
| 新元素标记 `*` | 任何交互后的页面快照 | 新出现的元素标 `*` 前缀 |
| 页面变化保护 | 交互导致 URL 变化 | 自动更新记录，返回新页面快照 |

**浏览器环境：**

| 能力 | 实现方案 | 优先级 |
|------|---------|--------|
| Dialog 自动处理 | `page.on('dialog')`: auto-accept | P0 |
| SPA 智能等待 | MutationObserver settle + 硬超时 | P0 |
| 导航错误处理 | 捕获异常，返回结构化错误 | P0 |
| 网络请求被动捕获 | `page.on('response')`（不用 `page.route()`——会触发反检测） | P0 |
| 新标签检测 | `context.on('page')` | P0 |
| 交互前遮挡检测 | `elementFromPoint()` 预检 + 回退 | P0 |
| 3 级点击回退链 | 正常 click → force click → JS click | P0 |
| 浏览器崩溃恢复 | 检测断开 + 自动重启 | P1 |
| 内存管理 | browser_reset 重启清理 | P1 |

### 7.4 文件系统设计（共识 v4——已更新）

**目录约定（per-domain）：**

```
artifacts/{domain}/
├── samples/          ← browser_eval save_as 存的数据样本
├── scripts/          ← 可复用脚本（精准记忆，Procedural Model 索引）
├── workspace/        ← 临时文件、bash 大输出落盘
└── transcripts/      ← session 完整记录（JSONL，录制 Agent 的输入）
```

- browser_eval 的 `save_as` 参数让 agent 控制文件命名和路径
- **scripts/ 是精准记忆的载体**——Procedural Model 存语义描述 + 文件路径引用，scripts/ 存完整可执行代码
- bash 大输出自动落盘到 workspace/，返回预览 + 文件路径
- transcripts/ 由基础设施层程序化保存（零 LLM 成本）

### 7.5 工具能力边界（共识）

#### 7.5.1 browse 的能力边界

**browse 的职责：导航 + 观察。只读不写，只看不动。**

与其他工具的分界：

| browse 做 | 不做（谁做） |
|----------|------------|
| 导航到 URL | 点击/填写/滚动（interact） |
| 返回页面结构化表示 | 执行 JS 提取数据（extract） |
| 被动捕获网络请求 | 主动发 HTTP 请求（bash curl） |
| 返回视口内容 | 查看视口外内容（interact scroll 后再 browse） |
| 检测数据信号（框架/嵌入 JSON） | 实际提取嵌入 JSON 数据（extract） |
| 截图 + vision 文字描述（visual 模式） | 视觉交互操作（不存在——视觉仅用于理解） |

浏览器硬限制（不可突破）：

| 限制 | 原因 | 应对 |
|------|------|------|
| 跨域 iframe 内容 | 浏览器同源策略 | 记录 iframe 存在，无法访问内容 |
| Closed Shadow DOM | Web 标准封装 | 记录存在，等标准演进 |
| Canvas/WebGL 渲染内容 | 非 DOM，无文本 | visual 模式部分覆盖（截图 + vision 描述） |
| PDF 嵌入内容 | 需专门处理 | 记录存在 |

系统设计限制（MVP 不做）：

| 限制 | 影响 | 应对 | 未来方向 |
|------|------|------|---------|
| 需要登录的内容 | 中 | 记录"需要认证"为侦察结论 | "调用人类"机制 |
| CAPTCHA | 中 | Camoufox 覆盖指纹；记录为障碍 | 专项调研 |
| 交互后才出现的内容 | 低 | 需要 interact 先触发 | 设计上已覆盖 |
| 实时内容（WebSocket） | 低 | 记录"有 WebSocket 连接" | WebSocket 监控 |
| 复杂交互（拖拽、多步表单） | 低 | 记录存在但无法操作 | 高级交互能力 |

**核心认知：准确描述限制本身就是高质量侦察结论。**

### 7.6 browse 工具设计（共识）

> 详细调研数据见 docs/browse工具深度设计报告.md

**参数：**
```
browse(
  url:    string?   — 导航目标，省略则刷新当前页面快照
  visual: boolean?  — 触发截图 + 独立 vision LLM 文字描述（默认 false）
)
```

**页面表示格式：Markdown + HTML 混合（D2Snap 73% 最高成功率）**

- 静态文本 → Markdown（标题/段落/列表/表格，减 80% token）
- 交互元素 → `[N]<tag attr="val">text</tag>` 编号 HTML
- 图片 → `![alt text](src)` Markdown 图片语法（保留 alt 和 src）
- data-* 属性 → 在编号元素上保留有业务含义的 data-*（过滤框架注入的）
- 容器元素 → `<nav>`/`<section>` 保留层级但不编号
- 列表截断 → 超过 5 个同构项展示前 3 个 + `... (N more, M total)`

格式示例：
```markdown
# ThreeJS Pens

<nav>
  [1]<a href="/home">Home</a>
  [2]<a href="/explore">Explore</a>
</nav>

### Cool 3D Scene
![3D rotating cube with dynamic lighting](/thumbs/abc123.png)
Author: John | Views: 1,234
[3]<a href="/pen/abc123" data-id="abc123" data-views="1234">View Pen</a>
[4]<button data-pen-id="abc123">Like</button>

... (18 more cards, 20 total)

[5]<input type="text" placeholder="Search pens..." />
[6]<button type="submit">Search</button>
```

**元素索引：**
- 只编号可交互元素（a/button/input/textarea/select + ARIA roles + onclick/tabindex）
- 顺序编号，按 DOM 顺序递增
- 每次 browse/interact 后重建，不跨页面持久
- 存入 ToolContext.selector_map，interact 通过编号查找

**数据信号（始终检测，独立 section）：**
```
--- Data Signals ---
Framework: Next.js (detected: #__next, __NEXT_DATA__)
Embedded JSON:
  __NEXT_DATA__: 127KB, props.pageProps has 20 pen objects
  JSON-LD: 1 schema (type: WebPage)
```

**网络请求捕获（始终开启，独立 section）：**
```
--- Network Requests ---
API Requests Captured (3):
  GET /api/v2/pens?tag=threejs&page=1 → 200 (JSON, 20 items, 45KB)
  POST /graphql {operationName: "PensByTag"} → 200 (JSON, 15 items)
Filtered: 12 tracking/analytics, 8 static assets
```

**大页面三层控制：**
1. 视口过滤（默认只返回可见区域）
2. 列表截断（>5 同构项）
3. Token 硬上限（~8000，可配置）

**视觉模式（visual=true）：**
- 视口截图（不做全页截图，业界共识）
- SoM 标注：在截图上叠加元素编号 `[N]`，与文本表示对齐
- 独立 vision LLM 处理：截图 → vision LLM → 文字描述进 agent context
- 文字描述 + 截图路径同时存入 observation

**自动 World Model 写入：**
- 自动创建/更新 Location（URL → pattern 启发式推断）
- 自动写入 page_summary 类型 Observation（URL/框架/API 端点/元素数）

**信号驱动的分层感知（共识）：**

browse 不试图一次返回一切。它返回**决策所需的信息 + 信号**，agent 根据信号决定是否深入：

| browse 返回的信号 | agent 的自然反应 |
|------------------|----------------|
| `__NEXT_DATA__ found (127KB, 20 objects)` | → 调 extract 提取嵌入 JSON |
| `API captured: GET /api/v2/pens → 200 (20 items)` | → 调 bash curl 重放 API |
| `SPA empty shell detected` | → 页面可能没加载好，等一下再 browse |
| 图片 `![3D scene](...)` + briefing 要求视觉评估 | → 调 browse(visual=true) |
| 内容被截断 `[showing 8000 of ~15000 tokens]` | → 调 interact(scroll) 看更多 |

tool description 中写清楚这些对应关系，agent 自然知道怎么用。不需要教"层次"概念。

### 7.7 交互工具设计（共识 v4——interact 已拆分为 5 个独立工具）

> 详细调研数据见 docs/interact工具设计调研报告.md
> 拆分决策见 docs/工具重新设计共识.md §2.2

**5 个交互工具（按参数模式拆分）：**

| 工具 | 参数 | 示例 |
|------|------|------|
| `click(target)` | 元素编号 | `click("3")` |
| `input(target, value)` | 元素编号 + 文本 | `input("8", "threejs")` |
| `press_key(key, target?)` | 按键名 + 可选元素 | `press_key("Enter")` |
| `scroll(direction?, amount?, target?)` | 方向/屏数/容器 | `scroll("down", 2)` |
| `go_back()` | 无参数 | `go_back()` |

**拆分理由：** SWE-Agent 消融实验证明细粒度工具优于粗粒度 enum。不同参数签名的操作不应合并——click 只需 target，input 需 target+value，scroll 需 direction。

**元素定位：只接受编号。** 编号来自 browse 返回的页面快照，编号失效时返回明确错误 + 提示 browse 刷新。

**错误信息设计（可操作的诊断信息）：**

| 错误 | 返回信息 |
|------|---------|
| 编号不存在 | "Element [N] does not exist. Use browse() to refresh." |
| 元素被遮挡 | "Element [N] is behind an overlay." |
| 元素不可交互 | "Element [N] is not interactable (disabled/hidden)." |

**内置智能行为（对 agent 透明）：** 见 §7.3

### 7.8 其他工具调研结论

**browser_eval（原 extract，已拓宽为通用 JS 执行器）：**
- 纯 JS page.evaluate，`save_as` 可选参数
- 通用能力：探测数据格局、提取数据、检查状态、调用页面内 API
- 错误分类 + hint 帮助 agent 自调试（CodeAct 自调试模式 +2-12%）

**bash：**
- 无状态 subprocess.run，command + timeout 参数
- 大输出自动落盘 workspace/ + 返回预览和文件路径
- 安全边界：受限用户 + 专用服务器（不使用 Docker）
- curl_cffi 可用于 TLS 指纹模拟

**read_world_model：**
- section 过滤 + location 模糊匹配
- 默认返回 Semantic Model + Procedural Model + Location 索引

**think：**
- 1 参数（thought），返回 "ok"，no-op
- tau-bench +54.1%，BrowseComp +40.1%

**录制 Agent：已删除。** 录制 Agent 在 session 结束后从完整 message array 中提取结构化 Observations，全权负责 WM 写入。

### 7.7 设计原则（调研共识）

| 原则 | 证据 | 来源 |
|------|------|------|
| 感知质量 > 推理能力 | 仅优化观察空间效果比改进架构高 161% | AgentOccam (ICLR 2025) |
| 每个工具必须证明存在价值 | 去掉 80% 工具后 3.5x 快 | Vercel d0 |
| 混合（浏览+代码）> 纯模态 | 38.9% vs 14.8% vs 29.2% | Beyond Browsing (ACL 2025) |
| 交互后变化反馈 | 省掉 interact→browse 双步 | Agent-E |
| 不跨页面维持元素引用 | 所有系统每次 action 后重建快照 | 业界共识 |
| 嵌入 JSON > API > DOM | 成本递增、脆弱性递增 | Zyte, Scrapfly 等 |
| 基础设施消除沉默故障 | dialog/popup/SPA 等问题不应到达 agent | 多方验证 |
| 工具描述质量是最高杠杆 | 精炼描述 → SWE-bench SOTA；API 文档 → +20.43% 准确率 | Anthropic 工程文档, Gorilla (NeurIPS 2024) |
| 返回值设计 ≥ 能力设计 | agent 看到什么比能做什么更影响质量 | AgentOccam + 全系统趋同 |
| 结构化防错 > 错误处理 | 强制绝对路径 → "model used flawlessly"；Edit 要求先 Read | Claude Code poka-yoke |
| 工具集 session 内静态 | 动态增删破坏 KV-cache（10x 成本） | Manus, 全系统共识 |
| 工具间零语义重叠 | 选择准确率退化根因是语义混淆，非数量 | arXiv:2601.04748 |

*各工具的具体参数 schema 和返回值格式待讨论后补充。*

---

## 八、可观测性设计（共识）

### 问题

之前跑完后无法理解 agent 做了什么、为什么做、哪里出了问题。没有可观测性就没法有效迭代。

### 核心原则

可观测性和知识系统是**并行的两套系统**，不是替代关系：

| | 可观测性（trace） | 知识系统（World Model） |
|---|---|---|
| 给谁看 | 开发者/运营者 | Agent + Planner |
| 记什么 | 过程——每步做了什么、想了什么 | 产出——发现了什么知识 |
| 生命周期 | 单次 session 调试产物 | 跨 session 持久化 |
| 运行时可读 | 不可——agent 不读自己的 trace | 可——read_world_model |

### Session 产出物（共识）

每次 session 结束后，`artifacts/sessions/{run_id}/{session_id}/` 下生成：

```
trace.jsonl          ← 每步一行，完整记录
screenshots/
  step_001.jpg       ← 每步截图（JPEG 压缩，50 步约 5-10MB）
  step_002.jpg
  ...
summary.txt          ← 人类可读的一行一步摘要
wm_snapshot.json     ← session 结束时的 World Model 快照
```

### trace.jsonl Schema（共识）

每行一个 JSON 对象，记录 TAO triple（Thought-Action-Observation）：

```json
{
  "step": 1,
  "ts": "2026-03-26T10:23:45Z",
  "reasoning": "LLM 在调工具前的文字输出（最重要的调试数据）",
  "tool": "browse",
  "input": {"url": "https://codepen.io/tag/threejs"},
  "output_summary": "42 elements, 3 API captured, __NEXT_DATA__ detected",
  "output_full_path": "outputs/step_001_browse.txt",
  "tokens_in": 1234,
  "tokens_out": 567,
  "latency_ms": 2300,
  "url": "https://codepen.io/tag/threejs",
  "wm_writes": ["loc:xxx", "obs:yyy"],
  "error": null
}
```

**关键：LLM 的 reasoning text 必须保存。** 这是理解 agent 决策的唯一窗口。当前 session loop 可能丢弃了 `response.content`（LLM 在 tool_calls 之外的文字输出）。

### summary.txt 格式（共识）

```
Session abc123 | 15 steps | outcome: natural_stop | 2m34s
───────────────────────────────────────────────
 1. browse  codepen.io/tag/threejs     → 42 elements, 3 API, __NEXT_DATA__
 2. think   "page has embedded JSON, try extract first"
 3. extract __NEXT_DATA__              → 15 pens → samples/pens/threejs_p1.jsonl
 4. browse  codepen.io/pen/abcdef      → detail page, 28 elements
 5. extract pen detail                 → 12 fields, 2 null → samples/pens/detail_abcdef.jsonl
 ...
15. (stop) "briefing goals met"

WM: 3 locations, 8 observations, 1 insight, 45 records extracted
```

### 截图策略（共识）

每步截图，JPEG 压缩。50 步 session 约 5-10MB，存储成本可忽略，调试价值极高。

理由：TRAIL benchmark 发现最好的 LLM 自动分析 trace 的准确率仅 11%。人类检查 trace 在可预见的未来不可替代——截图让人类能立即看到 agent "看到"了什么。

### 简单异常检测（共识）

Session loop 中内建：
- 同一 tool + input 连续出现 3+ 次 → 日志警告 "LOOP_DETECTED"
- 连续 3+ 步 tool 返回错误 → 日志警告 "CONSECUTIVE_ERRORS"

### 未来扩展（不在 MVP）

- OpenTelemetry span 输出（接入 Langfuse/Phoenix）
- CLI session replay 命令
- 测试套件（domain + requirement → 期望 WM 结果）
- Prompt A/B 对比

---

## 九、System Prompt 设计（共识框架，具体措辞实现时调试）

### 设计前提

**System prompt 在整个系统中的位置：**

Agent 的信息来源有三个：
1. **System prompt**（稳定）— 所有 session 相同，教怎么想和做
2. **Briefing**（动态）— 每 session 不同，Planner 编译，含方向 + 相关原始 observations
3. **read_world_model**（按需拉取）— Agent 主动查询 Semantic Model、Procedural Model、observation 索引

System prompt 只负责教 Agent 的认知纪律和方法论。具体"这次做什么"由 briefing 告诉。站点的结构化理解（Semantic Model）和方法论（Procedural Model）由 Agent 通过 read_world_model 按需获取，不在 system prompt 中。

**什么 session 都可能需要广度和深度。** 网站认知的每一层（站点结构 → 数据分布 → 需求映射 → 样本）都可能需要广度探索。不是"先广后深"的线性推进——一种数据可能在多个位置以不同形式存在，任何时候都不应该找到一个来源就停。Agent 在 briefing 给定的 scope 内自主平衡广度和深度。

**Agent 是任务驱动的。** 每个 session 有明确的 briefing 意图。Agent 的 job 是以高质量的 observations 回应这个意图，不是自由探索。

### Prompt 结构（共识）

参考生产系统共同模式（Manus、Browser-Use、OpenHands、Devin），采用模块化结构：

```
1. 身份与使命        — 你是谁、你的核心产出是什么
2. 认知纪律          — 怎么观察、怎么判断、怎么记录
3. 工具使用原则      — 跨工具的方法论（不是单工具使用说明，那在 tool description 里）
4. 完成与自检        — 怎么判断做完了、怎么发现自己卡住了
```

保持简洁。工具特定的使用说明（参数含义、返回值格式）放在 tool description 里，不放 system prompt。两者互补不重复。

### 9.1 身份与使命

Agent 执行 Planner 通过 briefing 下达的方向。核心产出是写入 World Model 的高质量 observations。

**关键：observation 质量决定整个系统的质量上限。** Agent 的 observations 是 Planner 合成 Semantic Model 和 Procedural Model 的唯一素材。如果 observations 模糊（"提取了一些数据"），Planner 的 Model 就是垃圾，后续 session 的 briefing 也是垃圾。整个知识循环的质量 = Agent observation 的质量。

### 9.2 认知纪律

#### 感知

- browse 返回多维信号：DOM 结构、交互元素索引、网络请求捕获、嵌入数据检测（__NEXT_DATA__ 等）、框架检测。全部注意，不只是看页面文字
- 这些信号直接指导下一步行动——检测到嵌入 JSON 说明有低成本提取路径，捕获到 API 说明可以直接重放

#### 广度意识

- 同一种数据可能在多个位置以不同形式存在（列表页、详情页、API、嵌入 JSON），字段和完整度不同
- 不要找到一个数据来源就停——横向扫描不同来源、比较差异，本身就是高价值的发现
- 发现新的位置或数据路径时，即使不是 briefing 重点，也记录下来

#### 记录纪律

这是最重要的认知纪律。每条 observation 应该具体到可复用：

| 好的 observation | 差的 observation |
|-----------------|-----------------|
| "extract via page.evaluate('#__NEXT_DATA__'), 15 records, fields: [id, title, author, views], author 字段 2/15 为 null" | "提取了一些 pen 数据" |
| "/api/v1/pens?tag=threejs 返回 JSON, 25 条, 比页面多 10 条, 额外字段: [forks, likes, created_at]" | "发现了一个 API" |
| "DOM .pen-card 只有 title 和 author, 缺少 views/likes, 不如 __NEXT_DATA__ 完整" | "DOM 提取不太好" |

方法论也要记录：不只记"发现了什么"，也记"怎么发现的"和"什么没用"。这些信息被 Planner 合成为 Procedural Model，帮助后续 session 避免重复工作。

#### 方法选择

提取数据时，优先使用成本最低的方法：
1. 嵌入 JSON（__NEXT_DATA__ 等）— browse 返回的数据信号会提示是否存在
2. 网络捕获的 API — browse 返回的网络请求摘要会显示 JSON API
3. 直接 API 调用 — 用 bash curl 调用已发现的端点
4. DOM 提取 — 最常用但最脆弱，改版即失效

不是说不能用 DOM 提取——是先检查有没有更好的方法。

#### 意外处理

发现 briefing 之外的有价值信息时：
- 快速探测可以（bash curl 看看 API 返回什么、browse 扫一眼新发现的页面）
- 深入跟进不可以——记录发现，留给 Planner 在后续 session 安排
- 判断标准：这个探测会花几步还是几十步？几步可以，几十步不行

### 9.3 工具使用原则

跨工具的方法论放在 system prompt，单工具的具体说明放在 tool description：

| 内容 | 放哪里 |
|------|--------|
| "提取优先嵌入 JSON > API > DOM" | System prompt（跨工具策略） |
| "browse 返回的网络请求摘要格式是..." | Tool description（单工具说明） |
| "interact 后不需要再调 browse，变化反馈已包含在返回值中" | Tool description（单工具说明） |
| "每个重要发现用 录制 Agent 记录" | System prompt（跨工具纪律） |
| "需要刷新记忆时调 read_world_model" | System prompt（跨工具纪律） |
| "bash 可以做 HTTP 请求、数据处理、代码执行" | Tool description（单工具说明） |

### 9.4 完成与自检

#### 完成判断

对照 briefing 的意图检查：
- Briefing 要求理解的东西，是否有 observations 支撑？
- Briefing 提到的未知，是否已经回答？
- 如果 briefing 给了多个方面，是否每个方面都有覆盖？

不是"做了一些事就停"——是"briefing 意图已充分回应"才停。

#### 自检

- 同一方法连续失败 2 次 → 换方法，不要第三次
- 长时间在同一个 URL 没有新发现 → 换方向或换方法
- 收到大量新信息后 → think 消化再继续，不要急着行动
- 不确定接下来该做什么 → read_world_model 检查全局进度和缺口

### 9.5 Prompt 长度与格式（共识）

- **简洁**：system prompt 控制在数百 token 以内。动态内容（briefing）才是 token 预算的主要去处
- **确定性**：不包含时间戳或每次变化的内容（影响 KV-cache 命中率）
- **格式**：使用结构化标签（XML 或 Markdown headers），不用自然语言段落堆砌
- **不用隐喻**：不说"你是田野研究员"，直接说具体行为要求

### 调研依据

| 发现 | 来源 |
|------|------|
| 工具描述精炼比 system prompt 修改影响更大 | Anthropic "Writing Tools for Agents" |
| System prompt 超 ~16K token 后性能不稳定 | Chroma 2025 study |
| System prompt 前缀需完全确定性（KV-cache） | Manus context engineering blog |
| Prompt engineering 到 ~75% 后收益递减，需转向架构优化 | Softcery evidence |
| "give heuristics and principles, not rigid examples" | Anthropic agent guidance |
| 结构化标签（XML/Markdown）显著提升工具使用准确率 | Speakeasy study |
| 所有生产系统都有 pre-done 验证步骤 | Browser-Use, Devin, OpenHands |
| "卡住时反思 5-7 种可能原因" | OpenHands prompt |

---

## 十、Context 管理细节（待讨论）

*分层衰减的具体实现策略。待讨论后补充。*

---

## 十一、设计决策记录

### D1: Agent 的核心产出是高质量 observations

Agent 执行 briefing 给定的方向，以高质量的 observations 回应 briefing 的意图。Agent 有自主权决定怎么做（工具选择、步骤顺序、广度/深度平衡），但方向由 Planner 通过 briefing 决定。

理由：Agent 是任务驱动的执行者，不是自由探索者。它的自主性在战术层面（怎么做），不在战略层面（做什么）。Briefing 给方向，不给步骤。

### D2: 步数不固定，Planner 给软预算

固定步数无法适应任务复杂度差异。Planner 更能判断任务需要多少步。硬上限兜底防失控。

理由：发现型任务可能 10 步，深度提取可能 80 步。一刀切的 30 步既浪费简单任务的时间又限制复杂任务的能力。

### D3: 进度意识靠 context 管理 + World Model，不需要 session-level model

Session 的"记忆"= context（短期工作记忆）+ World Model（长期外部记忆）。不引入 session 级别的 model 文档。

理由：Agent 的关键发现已通过工具写入 World Model，可随时查回。再加一层 model 是多余复杂度。

### D4: 认知循环涌现，不硬编码

感知→解读→行动→验证→记录→决策的循环从 LLM 推理中涌现，不在代码中强制步骤顺序。

理由：硬编码步骤会限制 agent 的灵活性。不同任务类型需要不同的认知节奏。设计的职责是通过工具、prompt、信息呈现创造涌现条件。

### D5: Agent 结构不随任务类型变化

同一个 agent，同样的工具集和认知框架，适用于所有任务类型。任务差异完全通过 briefing 传达。

理由：任务类型是一个连续光谱，不是离散分类。一个 session 中可能同时涉及发现和提取。为每种类型设计不同的 agent 是过度工程。

### D6: 感知质量优先于推理能力

投资在"agent 能看到什么"上的回报远大于"agent 怎么想"。browse 一次应获取尽可能丰富的多维信息。

理由：AgentOccam（ICLR 2025）证明优化观察空间的效果比改进 agent 架构高 161%。Agent 看不到的东西，再聪明也无法推理出来。

### D7: 提取方法按层级尝试，DOM 抓取不是首选

嵌入式 JSON > 网络捕获 API > 直接 API 调用 > DOM 抓取。现代站点的数据在 JSON/API 中通常比 DOM 更完整。

理由：DOM 是渲染层面的表达，经常只包含数据的子集。API 和嵌入 JSON 是数据层面的表达，通常更完整。且 DOM 抓取最脆弱——站点改版就失效。

### D8: 元素索引系统是精确交互的基础

browse 给交互元素编号，interact 通过编号定位。消除 LLM 猜 CSS selector 的失败模式。

理由：Browser-Use、Agent-E、Vercel agent-browser 全部采用此模式。这是业界第一共识。LLM 不可能可靠地猜出正确的 CSS selector。

### D9: 已知能力边界作为侦察结论记录

agent 遇到无法处理的场景（登录墙、CAPTCHA、复杂交互），正确识别并记录为 observation，而非尝试强行解决。

理由：准确描述限制本身就是高质量侦察结论。"核心数据在认证墙后面"对需求方的决策价值很高。强行解决反而浪费预算且可能产出错误结论。

### D10: 12 个工具分 5 类（已更新为 v4）

浏览器感知（browse, read_network, browser_eval）、浏览器管理（browser_reset）、页面交互（click, input, press_key, scroll, go_back）、系统执行（bash）、认知辅助（think, read_world_model）。WM 写入由录制 Agent 负责，不是执行 Agent 的工具。

理由：一工具一能力（SWE-Agent 证据），按不可互替的 Playwright 能力层拆分浏览器感知，按参数模式拆分交互工具。

### D11: 能力来源分离——工具/基础设施/prompt/context/模型

工具解决"agent 能做什么和看到什么"。基础设施解决"做的时候怎么可靠"（dialog、cookie banner、SPA 等待、遮挡检测等对 agent 透明）。System prompt 解决"agent 怎么想"。Context 管理解决"agent 记住什么"。模型能力是给定约束。

理由：混淆来源会导致设计错位——把该由基础设施解决的问题做成工具（增加 agent 决策负担），或把该由工具解决的问题藏进基础设施（剥夺 agent 控制权）。

### D12: bash 是通用计算工具，合并 execute_code

bash 覆盖所有非浏览器能力：HTTP 请求、数据处理、文件操作、代码执行、网络搜索、API 探索。不需要独立的 execute_code、search_site、HTTP client、Read/Write 工具。

理由：`execute_code("print('hello')", "python")` ≡ `bash("python3 -c \"print('hello')\"")`。Manus 只有 `execute_command`，Claude Code 只有 Bash，SWE-Agent 只有 bash。减少工具 = 减少决策负担 = 更好表现（Vercel d0 证据）。

### D13: 文件系统是 agent 的工作台，extract 提供 save_as 参数

extract 工具的 `save_as` 参数让 agent 控制文件命名和路径（而非自动生成不可读名字）。目录约定 `artifacts/samples/`（数据样本）+ `artifacts/workspace/`（中间结果）。

理由：Manus 核心发现——agent 写中间结果到文件、后续步骤读取，效果显著优于纯 context。之前 artifacts/samples/ 堆满不可读 JSON 的问题来自 agent 缺少文件组织控制。

### D14: 可观测性和知识系统并行，不替代

Trace（JSONL + 截图 + 摘要）记录 agent 的过程，给开发者调试。World Model 记录 agent 的产出，给 Planner 和后续 session 使用。两者独立运行，服务不同对象。

理由：trace 是按时间排列的事件流，World Model 是按语义组织的知识库。trace 让我们理解 WHY（agent 为什么选了这个 action），World Model 让 Planner 理解 WHAT（发现了什么）。缺任何一个系统就无法有效运作或迭代。

### D15: Agent 是任务驱动的，不是自由探索者

每个 session 有明确的 briefing 意图。Agent 的 job 是以高质量 observations 回应这个意图。Agent 在 briefing 给定的 scope 内自主平衡广度和深度，但不自己决定战略方向。

理由：战略方向是 Planner 的职责（它有全局视野）。Agent 是高度自主的执行者，但它的自主性在"怎么做"而非"做什么"。

### D16: 任何层级任何时候都可能需要广度

网站认知每一层（结构 → 数据分布 → 需求映射 → 样本）都可能需要广度探索。同一种数据可能在多个位置以不同形式存在。不是"先广后深"的线性推进——一个 session 内可能同时需要广度和深度。

理由：之前把 Session 1 = 广度、Session 2+ = 深度是认知偏差。实际上发现一个数据来源不代表找全了，每一层的广度和深度由 briefing scope 和 agent 的实时判断共同决定。

### D17: Observation 质量是整个系统的质量上限

Agent 的 observations 是 Planner 合成 Semantic Model 和 Procedural Model 的唯一素材。模糊的 observations 导致模糊的 Models，导致模糊的 briefings，导致下一个 session 效果更差。System prompt 中记录纪律的优先级高于探索策略。

理由：整个知识循环——Agent observations → Planner Models → Briefing → Agent——的质量瓶颈在第一步。投资在"Agent 怎么记录"上的回报最高。

### D18: System prompt 教怎么想，tool description 教怎么用

跨工具的策略和认知纪律放 system prompt（如提取优先级、记录要求、完成判断）。单工具的参数说明和使用场景放 tool description。两者互补不重复。

理由：Anthropic 证实工具描述精炼比 system prompt 修改影响更大。工具描述是 agent 理解工具的直接来源。把工具特定信息放 system prompt 增加噪声、分散注意力。

### D19: System prompt 简洁确定，不含动态内容

System prompt 保持数百 token，不包含时间戳或每次变化的内容。动态信息走 briefing（每 session 不同）和 read_world_model（按需拉取）。

理由：Manus 发现 KV-cache 命中率是最重要的性能指标。system prompt 前缀任何变化都会使缓存失效（10x 成本增加）。Chroma 测试 18 个模型，超 ~16K token 后性能不稳定。

### D20: 工具描述是"新人入职手册"，不是 API doc

每个工具的描述应包含：用途、参数含义、返回值格式、使用约束、典型用法、什么场景**不该**用这个工具。描述质量直接决定工具选择准确率。

理由：Anthropic 证实精炼工具描述是影响最大的单一优化——Claude Sonnet 3.5 靠此拿到 SWE-bench SOTA。Gorilla 证明 API 文档质量 → 准确率 +20.43%。API-Bank 证明模型间最大差距在选择而非执行。

### D21: 返回值控制信息密度，大数据走文件指针

工具返回给 LLM 的应是高信号摘要（语义标识优先于技术 ID），不是原始大数据。大数据（提取结果、完整页面等）保存到文件，返回路径 + 统计摘要。

理由：AgentOccam 证明"agent 看到什么"的影响 > "agent 能做什么"（161%）。Claude Code 限制工具返回 25K tokens。Anthropic：detailed/concise 模式切换可在功能无损下减少 2/3 token。

### D22: 参数设计优先防错，而非依赖错误处理

用 enum 约束有界值域（如 interact 的 action 类型），必填参数明确化，可推断参数自动填充，扁平参数优于深层嵌套。目标是让 agent 结构性地不可能犯某些错误。

理由：Anthropic poka-yoke 实践——强制绝对路径后 "model used this method flawlessly"。错误处理是第二道防线，参数设计是第一道。

### D23: 工具集 session 内静态，不动态增删

12 个工具在整个 session 生命周期内始终完整存在。不根据任务类型或阶段动态添加/移除工具。如果某工具在某些场景不适用，通过描述中的使用条件指导 agent。

理由：Manus、Devin、Browser-Use、OpenHands 全部采用静态工具集。动态增删破坏 KV-cache prefix，成本增加 10x（Manus 数据）。

### D24: 指导分工——系统 prompt 管选择策略，工具描述管单工具用法

涉及多个工具之间的选择/优先级/协作的指导放系统 prompt（如提取优先级层次、browse vs bash 选择时机）。涉及单个工具的参数/场景/约束放工具描述。两者互补不重复。

理由：Claude Code / Manus / Devin 趋同模式。系统 prompt 中有跨工具策略区块（Manus: `<browser_rules>`），工具描述放操作细节。此条细化 D18 的"怎么想 vs 怎么用"分工，明确分界线在"跨工具 vs 单工具"。

### D25: browse 返回值包含图片和 data-* 属性

browse 的 Markdown+HTML 混合格式中，图片用 `![alt](src)` 保留 alt 文本和 src 路径，编号元素上保留有业务含义的 data-* 属性（过滤框架注入的 data-reactid/data-v-xxx/data-testid 等）。

理由：data-* 可能包含页面不可见的结构化数据（data-id、data-price、data-views）。图片的 alt 和 src 帮助 agent 理解页面内容和 URL 模式。这些信息对 agent 做决策有直接价值，遗漏会导致低质量侦察。

### D26: 分层感知——browse 返回信号，agent 按需深入

browse 不试图一次返回页面的全部信息。它返回决策所需的摘要 + 数据信号，agent 根据信号选择是否用其他工具深入。不在 agent 中教"层次"概念——信号驱动的自然反应足够。

理由：任何单一表示都无法完整捕获页面的全部信息（D2Snap 最高 73%，无方案达 100%）。分层感知让 browse 高效覆盖 90% 场景，剩下的通过 extract（JS 深入探查）、visual 模式（视觉理解）、bash（外部信息）补盲。

### D27: 视觉能力通过独立 vision LLM 实现，仅 browse 具备

browse(visual=true) 时：视口截图 + SoM 编号标注 + 独立 vision LLM → 文字描述进 agent context。不使用主模型多模态（OpenAI-compatible gateway 兼容性不确定）。其他工具不具备视觉能力——需要视觉评估时 agent 调 browse(visual=true)。

理由：独立 vision LLM 兼容性最好（主模型不需要多模态），可用更便宜的模型（Haiku 级别），文字描述进 context 比原始图片更可控。视口截图是业界共识（Anthropic CU、OpenAI CUA、WebVoyager 全部如此），不做全页截图。

### D28: interact 只接受编号定位，不支持 CSS selector

interact 的 target 参数只接受 browse 返回的元素编号。不支持 CSS selector、XPath、自然语言等其他定位方式。编号失效时返回明确错误并提示 agent 调 browse() 刷新。

理由：CSS selector 依赖具体站点 DOM 结构，不通用。编号索引把定位质量问题集中到 browse 的检测逻辑一处，而非分散到 browse 检测 + interact 定位两处。agent 需要适用所有网页，编号是唯一的通用定位方式。

### D29: interact 返回轻量信号，不返回完整页面快照

interact 返回操作结果 + 变化信号（URL 是否变化、DOM 是否变化、提示信息），不返回完整页面快照。深度感知（数据信号检测、网络捕获、WM 写入）是 browse 的职责，interact 不重复。

理由：源码级调研 8 个系统发现三种模式——无系统在交互后做"深度感知"。Manus（click 只返回 URL/title）和 Agent-E（只返回变化描述）验证了分离模式。interact 返回信号让 agent 自己决定是否 browse——URL 变化时通常需要 browse 做完整分析，DOM 变化时可继续 interact。职责清晰：interact 负责操作+检测变化，browse 负责深度感知+记录。

### D30: interact 和 browse 的职责分离

interact = 手（操作页面）+ 触觉（检测变化信号）。browse = 眼（观察页面）+ 笔记（写入 World Model）。两者不重叠：interact 不做 browse 的分析工作，browse 不做 interact 的操作工作。agent 根据 interact 的变化信号决定是否需要 browse。

理由：调研发现业界存在三种模式（自动快照/分离/循环级），非一种共识。选择分离模式因为：(1) browse 承担重职责（Location 创建、Observation 追加、框架检测、嵌入数据检测、网络捕获），interact 复制这些会导致代码重复；(2) 侦察场景中大量 click 是导航——agent 导航后通常需要 browse 的完整分析；(3) 职责单一更容易维护和调试。

---

*下一步讨论：各工具详细参数/返回值 schema，Context 管理（§十），System prompt 具体内容。*
