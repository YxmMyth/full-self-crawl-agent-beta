# 实施计划

> 创建日期：2026-04-13
> LLM：deepseek-chat (V3.2) via OpenAI-compatible gateway
> Vision LLM：kimi-k2.5 via 同一 gateway（browse visual 模式专用）
> 按依赖链排序，每步完整实现，不做简化版
>
> **已确认的设计决策：**
> - Location 创建：Recording Agent 在 create_observation 时自动 find-or-create Location（不由 browse 创建）
> - browser_eval save_as：路径相对于 `artifacts/{domain}/`（不限 samples/），agent 可 save_as="scripts/xxx.js" 或 "samples/xxx.json"
> - Vision 模型：kimi-k2.5（gateway 测试通过）
> - Microcompact：按实际 result 大小判断（阈值 ~2000 chars），不按工具名硬分类
> - Session outcome 枚举：natural_stop / context_exhausted / consecutive_errors / safety_net

---

## 文档索引：每个 Phase 该读什么

> **通读**：开始该 Phase 前必须完整读一遍的文档
> **参考**：实现过程中按需查阅的具体章节
> **硬约束**：CLAUDE.md §六 的 10 条硬约束贯穿所有 Phase，每个 Phase 开始前重读一遍

| Phase | 通读 | 参考 |
|-------|------|------|
| **0 骨架** | CLAUDE.md §二§三 | — |
| **1 LLM** | CLAUDE.md §二 LLM API 段 | .env 中的 gateway 配置；本文档头部 deepseek-chat 测试结论 |
| **2 WM 数据层** | docs/WorldModel设计.md 全文 | 架构共识文档.md §七（记忆与存储系统，理解三层架构的全景） |
| **3 浏览器** | 架构共识文档.md §六（浏览器环境与策略） | docs/AgentSession设计.md §7.3（基础设施层）、§7.6（browse 设计中的 page_repr/element_index/data signals/network capture 格式定义）；docs/browse工具深度设计报告.md（调研数据） |
| **4 工具集** | docs/工具重新设计共识.md §二（12 工具完整定义） | docs/AgentSession设计.md §7.5-§7.8（各工具能力边界 + 详细参数/返回值）；docs/browse工具深度设计报告.md、Extract工具设计调研报告.md、interact工具设计调研报告.md、bash工具设计调研报告.md（四份工具调研报告，实现细节和边界 case） |
| **5 Session** | docs/AgentSession设计.md 全文（§一-§六 Session 本质/认知循环/生命周期/进度意识/失败模式/停止条件，§八-§十 可观测性/prompt/context 管理） | docs/SystemPrompts设计.md §一（执行 Agent prompt 完整内容，直接复制到代码中） |
| **6 Recording** | 架构共识文档.md §7.4（录制 Agent 完整架构：单例/Producer-Consumer/工具集/对比表） | docs/工具重新设计共识.md §1.5（录制 Agent 设计理由 + 工具集）；docs/SystemPrompts设计.md §二（录制 Agent prompt）；docs/AgentSession设计.md §四（录制 Agent microcompact） |
| **7 Planner** | docs/Planner设计.md 全文 | docs/SystemPrompts设计.md §三-§六（Planner/Verification/Research/maintain_model 四个 prompt）；架构共识文档.md §7.4b-§7.4c（Research + Verification 子 agent 架构）；docs/系统架构与信息流.md §四（Planner 循环全景）；CLAUDE.md §一（系统概述，验证实现是否完整） |

### 各文档的核心价值

| 文档 | 页数 | 核心价值 | 什么时候不需要读 |
|------|------|---------|----------------|
| **CLAUDE.md** | 长 | 系统全景 + 模块结构 + 硬约束 + 实施顺序 | Phase 1-6 中只需回查特定段落 |
| **架构共识文档.md** | 长 | 系统级设计共识：两层架构 / 探索方法论 / 记忆系统 / 浏览器策略 | Phase 1-2 不需要（与浏览器和 agent 无关） |
| **docs/Planner设计.md** | 中 | Planner 的 5 个工具 / 信息获取模型 / L1-L4 / maintain_model / 安全网 | Phase 0-6 不需要（Phase 7 专用） |
| **docs/WorldModel设计.md** | 中 | 三层数据架构 / 4 张表 DDL / Semantic+Procedural Model 结构和质量要求 | Phase 3-5 中只在 read_world_model 工具实现时回查 |
| **docs/工具重新设计共识.md** | 中 | 12 个工具的参数/返回值定义 + 录制 Agent 设计 + 工具粒度原则 | Phase 0-3 不需要；Phase 5 只在 ToolRegistry 集成时参考 |
| **docs/SystemPrompts设计.md** | 中 | 6 个 prompt 完整文本（执行/录制/Planner/Verification/Research/maintain_model） | Phase 0-4 不需要；Phase 5-7 中对应 prompt 直接复制到代码 |
| **docs/AgentSession设计.md** | 最长 | Session 的一切：认知循环 / 工具详细设计 / 基础设施层 / 可观测性 / context 管理 / 30 条设计决策 | Phase 0-2 不需要；Phase 3-4 只查工具相关段落 |
| **docs/系统架构与信息流.md** | 中 | 两层架构 / 信息流 / Planner 循环 / 停止条件 / 并发预留 | 大部分内容与架构共识重叠，Phase 7 查 Planner 循环全景时参考 |
| **4 份工具调研报告** | 各中 | browse/Extract/interact/bash 的调研数据和设计理由 | 实现对应工具时查阅，不需要通读 |
| **SiteWorldModel设计文档.md** | 中 | **已废弃**，被 docs/WorldModel设计.md 取代 | 不需要读 |

---

## Phase 0：项目骨架 + Config

- [ ] 创建完整目录结构（CLAUDE.md §三）
- [ ] `src/__init__.py` 及所有子包 `__init__.py`
- [ ] `src/config.py` — 读 .env，所有环境变量集中管理
  - LLM_API_KEY, LLM_BASE_URL, LLM_MODEL（deepseek-chat）
  - VISION_LLM_MODEL（默认 kimi-k2.5，browse visual 模式专用）
  - DATABASE_URL
  - BROWSER_WS_URL, BROWSER_CDP_URL（可选）
  - ARTIFACTS_DIR（默认 `./artifacts`）
  - VERIFICATION_SUBAGENT_ENABLED（默认 `true`）
  - 安全网参数：MAX_PLANNER_TOOL_CALLS=200, MAX_SESSIONS=15, MAX_CONSECUTIVE_SAME_TOOL=5
- [ ] `requirements.txt` — 按 CLAUDE.md §二 完整依赖列表（补充 camoufox, curl_cffi, markdownify）
- [ ] `src/utils/url.py` — URL 规范化
- [ ] `src/utils/logging.py` — 结构化日志

**验证：** `python -c "from src.config import Config; print(Config.LLM_MODEL)"`

**无依赖。**

---

## Phase 1：LLM 客户端

- [ ] `src/llm/__init__.py`
- [ ] `src/llm/client.py` — OpenAI-compatible 统一客户端
  - [ ] `chat_with_tools(messages, tools, model?)` — Agent Session 用
    - 调用 openai SDK chat.completions.create
    - 解析 tool_calls 返回结构化结果
    - 处理 deepseek-chat 响应格式（reasoning_content 字段，有则保留供日志，不回传）
    - 处理 finish_reason: tool_calls / stop / length
    - model 参数可选，默认用 Config.LLM_MODEL，browse visual 时用 Config.VISION_LLM_MODEL
  - [ ] `generate(prompt, system?, model?)` — maintain_model / summarize_session 等 LLM 函数用
    - 单次调用，返回文本
  - [ ] `describe_image(image_base64, prompt?)` — browse visual 模式专用
    - 调用 VISION_LLM_MODEL (kimi-k2.5)
    - 发送截图 base64 + 文字 prompt → 返回图像文字描述
  - [ ] 重试策略
    - content_filter 错误：重试 3 次，每次 sleep 2s
    - 网络超时：SDK 层 2 次指数退避
    - LLM 返回空 → 返回 None，调用方决定退出
  - [ ] Token 用量记录（prompt_tokens, completion_tokens, total_tokens）

**验证：** 能调 deepseek-chat 拿到 tool_calls 响应；generate 能返回文本。

**依赖：Phase 0**

---

## Phase 2：World Model 数据层

- [ ] `src/world_model/schema.sql` — 完整 DDL（4 张表）
  - locations（id TEXT PK, run_id, domain, pattern, how_to_reach, created_at, updated_at）
  - observations（id SERIAL PK, location_id FK, agent_step, raw JSONB, created_at）
  - sessions（id TEXT PK, run_id, direction, started_at, ended_at, outcome, steps_taken, trajectory_summary）
  - models（domain+model_type PK, content TEXT, updated_at）
- [ ] `src/world_model/model.py` — 完整 dataclass
  - Location（id, run_id, domain, pattern, how_to_reach, observations, created_at, updated_at）
  - Observation（id, location_id, agent_step, raw: dict, created_at）
  - Session（id, run_id, started_at, ended_at, outcome, steps_taken, trajectory_summary, direction）
  - SiteWorldModel（domain, locations, semantic_model, procedural_model）— 聚合根
- [ ] `src/world_model/db.py` — asyncpg 完整 async CRUD
  - [ ] connect(database_url) + ensure_tables()
  - [ ] Locations: create / get_by_id / list_by_domain / update
  - [ ] Observations: create / update / delete / list_by_location / list_by_session
  - [ ] Sessions: create / update（ended_at, outcome, steps_taken, trajectory_summary）
  - [ ] Models: upsert(domain, model_type, content) / load(domain, model_type) / load_both(domain)
  - [ ] load_world_model(domain) — 加载完整 WM（locations + observations + models）
  - [ ] 空 WM 处理（首次运行，返回空结构）

**验证：** connect → create tables → 写入 location + observation → 加载完整 WM → 加载空 WM。

**依赖：Phase 0（可与 Phase 1 并行）**

---

## Phase 3：浏览器基础设施层

- [ ] `src/browser/manager.py` — 连接管理
  - [ ] 四级优先链：
    1. 默认 → `AsyncCamoufox(headless=True)` 直接启动（本地 `headless=False` 可视调试）
    2. BROWSER_WS_URL → `playwright.firefox.connect(ws_url)`
    3. BROWSER_CDP_URL → `playwright.connect_over_cdp(url)`
    4. Camoufox 不可用 → `playwright.chromium.launch()` 兜底
  - [ ] 关闭 + 按新配置重启（供 browser_reset 调用）
  - [ ] 崩溃检测 + 自动恢复（P1）
- [ ] `src/browser/context.py` — 工具共享的浏览器上下文
  - [ ] BrowserContext dataclass：page, selector_map, network_captures, tabs, active_tab_index
  - [ ] 标签页管理（创建/切换/关闭/列表）
  - [ ] context.on('page') 新标签检测
- [ ] `src/browser/page_repr.py` — 页面表示（Markdown+HTML 混合格式）
  - [ ] 静态文本 → Markdown（标题/段落/列表/表格）
  - [ ] 交互元素 → `[N]<tag attr="val">text</tag>` 编号 HTML
  - [ ] 图片 → `![alt text](src)` Markdown 图片语法
  - [ ] data-* 属性保留（过滤框架注入的 data-reactid/data-v-xxx/data-testid）
  - [ ] 容器元素（nav/section）保留层级但不编号
  - [ ] 列表截断：同父 + 同 tag + 连续 >5 → 前 3 + `... (N more, M total)`
  - [ ] Token 硬上限 ~8000，超出末尾标注 `[truncated at ~8000 tokens]`
- [ ] `src/browser/element_index.py` — 元素索引系统
  - [ ] 只编号可交互元素（a/button/input/textarea/select + ARIA roles + onclick/tabindex）
  - [ ] 顺序编号，DOM 顺序递增
  - [ ] 每次 browse/交互后重建，不跨页面持久
  - [ ] 编号 ↔ selector 映射存入 context.selector_map
- [ ] `src/browser/network_capture.py` — 网络请求捕获
  - [ ] 被动监听 `page.on('response')`（不用 page.route()，会触发反检测）
  - [ ] 存 buffer：method, url, status, content_type, size, response_body_preview
  - [ ] POST 请求额外存 request body
  - [ ] 过滤 analytics/tracking + static assets
  - [ ] clear 操作清空 buffer
- [ ] `src/browser/dom_settle.py` — DOM 稳定等待
  - [ ] MutationObserver 监听 DOM 变化，无变化持续 N ms 视为稳定
  - [ ] SPA 空壳检测（body 内容极少 → 可能还在加载）
  - [ ] 硬超时兜底

**验证：** 能连 Camoufox 导航 codepen.io，返回 Markdown+HTML 混合页面表示，元素有编号，网络请求被捕获。

**依赖：Phase 0（可与 Phase 1、2 并行）**

---

## Phase 4：完整工具集（12 个）+ ToolRegistry

- [ ] `src/agent/tools/registry.py` — ToolRegistry
  - [ ] 工具注册（name, description, parameters schema, handler function）
  - [ ] JSON Schema 生成（OpenAI tools 格式）
  - [ ] 分发执行（name → handler，参数验证）
  - [ ] 列出所有已注册工具

### 浏览器感知（3 个）

- [ ] `src/agent/tools/browse.py` — 页面内容快照 + 多标签页导航
  - [ ] 参数：url?, new_tab?, tab?, visual?
  - [ ] 导航（如有 url）+ SPA settle 等待
  - [ ] 调用 page_repr 生成 Markdown+HTML 混合格式
  - [ ] Data Signals section（通用检测，不命名特定框架）：
    - script tags with type="application/json" / type="application/ld+json" / inline — 报告 id + size
  - [ ] Network Requests section（摘要）：
    - 来自 network_capture buffer
    - API 请求列表（method/URL/status/size/item count）
    - 过滤计数（tracking/analytics, static assets）
  - [ ] 多标签页：new_tab=true 开新标签页；tab=N 切换标签页
  - [ ] visual=true：视口截图 + SoM 标注 + kimi-k2.5 文字描述（通过 LLM client.describe_image）
  - [ ] 新元素标记 `*`（交互后快照中标注新出现的元素）

- [ ] `src/agent/tools/read_network.py` — 网络层信息
  - [ ] 参数：filter?（string contains 过滤）, clear?（读完清空 buffer，默认 false）
  - [ ] 返回：请求列表 + response body preview（每条 1000 chars）
  - [ ] POST 额外显示 request body
  - [ ] cookies（含 HttpOnly，只显示 name + flags + domain）
  - [ ] 末尾 tip 引导 bash curl 重放

- [ ] `src/agent/tools/browser_eval.py` — 浏览器内 JS 执行
  - [ ] 参数：script（支持 async/await，不需要 return）, save_as?
  - [ ] 在当前活跃标签页执行 page.evaluate()
  - [ ] save_as → artifacts/{domain}/{save_as}（相对于 domain 目录，agent 可存 samples/ 或 scripts/）
  - [ ] 过滤 `..` 防止目录遍历
  - [ ] 返回标注 type + size
  - [ ] 大结果 >50KB 自动落盘 workspace/ + 返回 preview + 文件路径
  - [ ] 错误返回 raw error + 程序化 hints（null property access / element not found / timeout / not defined）
  - [ ] 执行超时 30s

### 浏览器管理（1 个）

- [ ] `src/agent/tools/browser_reset.py` — 重启浏览器到新配置
  - [ ] 参数：proxy?, browser_type?（camoufox/chromium）, headed?
  - [ ] 所有参数可选，裸调用 = 干净重启同配置
  - [ ] 关闭当前浏览器 → 按新配置重启 → 返回确认
  - [ ] 裸调用场景：清理 cookies/cache、崩溃恢复、内存清理

### 页面交互（5 个）

- [ ] `src/agent/tools/click.py` — 点击元素
  - [ ] 参数：target（元素编号）
  - [ ] 编号 → selector_map 查找 → 定位元素
  - [ ] 自动识别 `<select>` → 展示下拉选项，提示用 input 选择
  - [ ] 3 级点击回退链：normal click → force click → JS click
  - [ ] 遮挡检测：elementFromPoint() 预检
  - [ ] 导致 URL 变化 → 自动附带新页面 browse 快照
  - [ ] 未导航 → 返回确认 + 提示 browse()
  - [ ] 编号不存在 → "Element [N] not found. Use browse() to refresh."

- [ ] `src/agent/tools/input.py` — 输入/选择
  - [ ] 参数：target（元素编号）, value（文本）
  - [ ] 自动识别元素类型：text input → fill，select → 选择选项
  - [ ] autocomplete 检测（combobox/aria-autocomplete）→ 延迟 400ms 等待建议列表
  - [ ] 执行后验证实际值，不匹配时 ⚠ 警告
  - [ ] 导致 URL 变化 → 附带快照

- [ ] `src/agent/tools/press_key.py` — 按键
  - [ ] 参数：key（支持组合键 "Ctrl+A", "Enter", "Escape"）, target?（可选元素编号）
  - [ ] target 省略 → 全局按键
  - [ ] 导致 URL 变化 → 附带快照

- [ ] `src/agent/tools/scroll.py` — 滚动
  - [ ] 参数：direction?（up/down/left/right，默认 down）, amount?（屏数，默认 1）, target?（容器元素编号）
  - [ ] 支持水平滚动 + 容器内滚动
  - [ ] 返回位置百分比 `Position: ~33% (720px of ~2160px)`

- [ ] `src/agent/tools/go_back.py` — 浏览器后退
  - [ ] 无参数
  - [ ] 后退 → 附带新页面 browse 快照
  - [ ] 栈空 → "Cannot go back — no history."

### 系统执行（1 个）

- [ ] `src/agent/tools/bash_tool.py` — 浏览器外代码执行
  - [ ] 参数：command, timeout?（默认 120000ms，最大 600000ms）
  - [ ] 每次 spawn 新进程（无状态）
  - [ ] 工作目录固定 artifacts/{domain}/workspace/
  - [ ] 输出上限 30,000 chars，尾部截断 + `[output truncated — NKB removed]`
  - [ ] 大输出同时自动落盘 workspace/（bash_NNN.txt）+ 返回 preview + 文件路径
  - [ ] 返回始终包含 exit code
  - [ ] curl_cffi 可用于 TLS 指纹模拟（tool description 中提示：API 重放推荐 curl_cffi）

### 认知辅助（2 个）

- [ ] `src/agent/tools/think.py` — 无副作用推理
  - [ ] 参数：thought
  - [ ] 返回 `{"thought": "(回显)"}` ，no-op

- [ ] `src/agent/tools/read_wm.py` — 查询 World Model
  - [ ] 参数：location?
  - [ ] 无参数 → 返回完整 Semantic + Procedural Model
  - [ ] 有 location → 返回该 location 的 Observations
  - [ ] 两层检索：Model 是索引，Observations 是证据

### 基础设施内置行为（不是工具，对 agent 透明）

- [ ] 对话框自动处理：`page.on('dialog')` auto-accept
- [ ] 新元素标记 `*`：任何交互后的页面快照中标注新出现的元素
- [ ] 页面变化保护：交互导致 URL 变化 → 自动附带新页面快照
- [ ] 交互前遮挡检测：elementFromPoint() 预检

**验证：** 每个工具独立调用成功；browse 返回完整页面快照含 Data Signals 和 Network 摘要；browser_eval 能探测和提取数据；bash 能执行命令并正确截断大输出。

**依赖：Phase 1 + 2 + 3**

---

## Phase 5：Agent Session

- [ ] `src/agent/session.py` — Agent Session 完整执行循环
  - [ ] System prompt（docs/SystemPrompts设计.md §一 完整内容）
    - 身份：web reconnaissance agent
    - How to Think（7 条）
    - Data Discovery Priority（嵌入 JSON > API > DOM）
    - Boundaries
  - [ ] Briefing 作为 user message
  - [ ] 主循环：
    1. Microcompact 处理 message array
    2. LLM chat_with_tools 调用
    3. 解析 tool_calls → ToolRegistry 分发执行
    4. 追加 assistant message + tool results 到 message array
    5. 检查停止条件 → 循环或退出
  - [ ] 三个停止条件：
    1. LLM 不再调工具 → 自然结束（while 循环自然出口）
    2. Context window 满 → 强制结束
    3. 连续框架级失败 ≥ 5 → 强制结束
  - [ ] Microcompact 实现：
    - 按 API round 分组（一次 LLM 调用 = 一个 round）
    - 最近 5 个 round 的 tool results 完整保留
    - 更早 round 的 tool results：按实际大小判断（>2000 chars → 替换为 `[已清除，调 read_world_model 查回]`）
    - 所有 tool_use blocks（工具名+参数）永远保留
    - 注：交互工具导致导航时返回 browse 快照（大输出），按实际大小判断而非工具名
  - [ ] Session 记录写入 DB sessions 表（create → update outcome/steps/ended_at）
  - [ ] 可观测性：
    - trace.jsonl — 每步一行（step, ts, reasoning, tool, input, output_summary, tokens, url, error）
    - 每步截图（JPEG 压缩，存 screenshots/）
    - summary.txt — 人类可读一行一步摘要
    - wm_snapshot.json — session 结束时 WM 快照
  - [ ] Transcript 保存：完整 message array → artifacts/{domain}/transcripts/{session-id}.jsonl
  - [ ] 简单异常检测：
    - 同 tool + input 连续 3+ 次 → 日志警告 LOOP_DETECTED
    - 连续 3+ 步 tool 返回错误 → 日志警告 CONSECUTIVE_ERRORS

**验证：** 给一个手写 briefing "Explore codepen.io/tag/threejs, discover data sources, extract a sample"，agent 能跑完一个 session，trace.jsonl 和 transcript 正确保存，截图目录有文件。

**依赖：Phase 4**

---

## Phase 6：Recording Agent

- [ ] `src/recording/agent.py` — 单例 Recording Agent
  - [ ] 持久对话 + tool-use 循环（与执行 Agent 同构）
  - [ ] System prompt（docs/SystemPrompts设计.md §二 完整内容）
    - 身份：observation specialist
    - What to Record（findings not actions / relationships / methods）
    - How to Work（read before write / merge don't duplicate / one obs = one fact / use agent's words / skip noise）
    - Output Quality
  - [ ] 4 个工具：
    - read_observations(location?) — 查看当前 observations
    - create_observation(location, raw) — 创建新 observation（location 传 pattern 字符串，自动 find-or-create Location）
    - edit_observation(id, raw) — 更新已有 observation
    - delete_observation(id) — 删除冗余/已合并的 observation
  - [ ] 单例设计：所有 session 共享同一个实例
  - [ ] Producer-Consumer 集成：
    - asyncio.Queue 作为推送通道
    - Python 代码把各 session 的 transcript 增量推送（标记 session_id）
    - 增量按 API round 分批，作为 user message 注入录制 Agent 对话
    - 只处理产生新知识的 tool call（browse/bash/browser_eval 的 results + agent reasoning text）
    - Session 结束时 flush queue + 超时保护（防死锁）
  - [ ] 录制 Agent 自身 Microcompact：
    - 最近 3 批 transcript 增量完整保留
    - 更早的已处理增量 → `[transcript batch N — 已处理，产出 M 条 observations]`
    - 所有 tool_use blocks 永远保留
  - [ ] 先 read 再改（避免 ID 错位，参考 Claude Code Read/Edit 模式）
- [ ] Session 中集成 Recording Agent：
  - [ ] 每次执行 Agent tool call 后，异步推送 transcript 增量给 Recording Agent
  - [ ] Session 结束时确认 Recording Agent 已处理完本 session 所有增量

**验证：** 跑一个完整 session，DB 中出现 locations + observations。录制 Agent 能 read → create/edit/delete observations。执行 Agent 能通过 read_world_model 查回录制 Agent 写入的 observations。

**依赖：Phase 5 + Phase 2（observations CRUD）**

---

## Phase 7：Planner + maintain_model + 子 Agent + CLI

### maintain_model

- [ ] `src/llm/maintain_model.py` — LLM 函数（非 agent，单次调用）
  - [ ] Prompt（docs/SystemPrompts设计.md §六 完整内容）
  - [ ] 输入：current Semantic Model + current Procedural Model + new observations + transcript brief（程序化提取工具名序列，不需要 LLM 摘要）
  - [ ] 输出解析：new Semantic Model + new Procedural Model + session summary + model diff
  - [ ] 写回 DB models 表（upsert）
  - [ ] Semantic ~8000 chars 上限，Procedural ~6000 chars 上限
  - [ ] 首次生成（无旧 model）：从零生成两个文档

### Research Subagent

- [ ] `src/research/agent.py` — tool-use 循环
  - [ ] System prompt（docs/SystemPrompts设计.md §五 完整内容）
  - [ ] 输入：topic + questions（spawn_research 参数作为 user message）
  - [ ] 4 个工具：
    - web_search(query, domain?, save_as?) — DuckDuckGo 后端（MVP）
    - web_fetch(url, save_as?) — httpx 轻量 HTTP 获取（不走浏览器），HTML 用 markdownify 转 Markdown
    - bash(command) — 兜底能力
    - think(thought) — 无副作用推理
  - [ ] 输出：调研报告写入 artifacts/{domain}/research/{topic}.md
  - [ ] 返回 { key_findings, report_path }

### Verification Subagent

- [ ] `src/verification/agent.py` — tool-use 循环（feature-gated）
  - [ ] `src/verification/prompt.py` — 反 satisficing system prompt（docs/SystemPrompts设计.md §四 完整内容）
    - 四要素：命名 rationalization / 读 WM 不是验证 / 强制 adversarial probe / 严格输出格式
  - [ ] 输入：WM 快照 + requirement 原文 + mark_done 理由
  - [ ] 3 个工具：
    - read_world_model(location?) — 严格只读
    - bash(command) — 执行验证（curl API / 跑脚本 / 读 samples）
    - think(thought)
  - [ ] 输出：验证报告 → artifacts/{domain}/verification/round_N.md（N = mark_done 调用次数，全局自增）
  - [ ] 最后一行 VERDICT: PASS / FAIL / PARTIAL（程序解析）
  - [ ] VERIFICATION_SUBAGENT_ENABLED=false 时完全跳过

### ReconPlanner

- [ ] `src/planner/recon_planner.py` — Planner tool-use agent 主循环
  - [ ] System prompt（docs/SystemPrompts设计.md §三 完整内容）
    - 角色定义 + L1-L4 层级认知 + Briefing 原则 + 工作节奏
  - [ ] Initial user message："Domain: {domain}\nRequirement: {requirement}"
  - [ ] 5 个工具完整实现：
    - [ ] spawn_execution(briefing) — 完整 pipeline：
      1. 创建 Session 记录
      2. 启动 Execution Agent（tool-use 循环，12 工具）
      3. 单例 Recording Agent 持续维护 Observations
      4. Session 停止
      5. 确认 Recording Agent 已处理完本 session 增量
      6. 调 maintain_model（更新 Models + 生成 summary/diff）
      7. 返回 { summary, model_diff, new_obs_count, session_id }
    - [ ] spawn_research(topic, questions) — 启动 Research Subagent → 返回 { key_findings, report_path }
    - [ ] read_model() — 幂等，返回 DB 最新 { semantic_model, procedural_model, version }
    - [ ] think(thought) — 回显推理
    - [ ] mark_done(reason) — 内部自动触发 Verification：
      - VERIFICATION_SUBAGENT_ENABLED=true → 运行 Verification Subagent
        - PASS → { status: "DONE" }
        - FAIL/PARTIAL → { status: "blocked", verdict, gaps }
      - VERIFICATION_SUBAGENT_ENABLED=false → 直接 { status: "DONE" }
  - [ ] Planner Microcompact：
    - 最近 5 轮 tool_results 完整保留
    - 更早 round 的 tool results：按实际大小判断（>2000 chars → 替换为 `[已清除，调 read_model 查看当前 Model]`）
    - 注：read_model 返回 ~14K 也属于大输出，5 轮外清除
    - 所有 tool_use blocks 永远保留
  - [ ] 安全网：
    - MAX_PLANNER_TOOL_CALLS=200
    - MAX_SESSIONS=15
    - MAX_CONSECUTIVE_SAME_TOOL=5
    - 触发时注入终止信号
  - [ ] 停止条件：
    1. mark_done → DONE
    2. 安全网触发
    3. Context window 满

### CLI 入口

- [ ] `src/main.py` — CLI 入口
  - [ ] 输入：domain + requirement（先硬编码，MVP 不需要 CLI 参数解析）
  - [ ] 初始化：DB 连接 + 浏览器启动 + 创建 Recording Agent 单例
  - [ ] 调用 ReconPlanner.run(domain, requirement)
  - [ ] 结束：关闭浏览器 + 关闭 DB 连接
  - [ ] artifacts/{domain}/ 目录结构自动创建：
    - samples/, scripts/, workspace/, transcripts/, research/, verification/

**验证：** `python src/main.py` 跑 codepen.io + "找出 threejs 相关的 pen 数据"，能多轮 session，World Model 有 locations/observations/Semantic Model/Procedural Model，最终 mark_done 返回 DONE。

**依赖：Phase 6**

---

## 依赖关系总览

```
Phase 0（骨架）
  ├── Phase 1（LLM 客户端）──────┐
  ├── Phase 2（World Model 数据层）├── Phase 4（12 工具）── Phase 5（Session）── Phase 6（Recording）── Phase 7（Planner + 子 Agent + CLI）
  └── Phase 3（浏览器基础设施）───┘
```

Phase 0-3 可并行，Phase 4 是汇合点。
