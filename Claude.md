# Full-Self-Crawl-Agent Beta — 实施指引

> 用途：给 Claude Code 的实施蓝图
> 日期：2026-03-18
>
> **前置阅读（必读，按顺序）：**
> 1. 本文档
> 2. `架构共识文档.md` — 系统设计的完整共识
> 3. `SiteWorldModel设计文档.md` — 数据模型设计

---

## 一、系统概述

Full-Self-Crawl-Agent Beta 是一个 LLM 驱动的网站侦察系统。给定一个域名和自然语言需求，它自主探索网站结构、理解数据分布、采集样本。

**核心循环：**

```
ReconPlanner.run(domain, requirement)
  → 初始化 SiteWorldModel
  → loop:
      Agent Session（自主探索、记录、提取）
      → reflect(wm)      # 从新observations提炼insight
      → decide_next(wm)  # 继续？停止？什么方向？
      → generate_briefing(wm, direction)  # 生成下一轮任务简报
  → 编译最终报告
```

**系统边界：** 到"理解 + 样本"为止。全量提取不在范围内。

**两层架构：**

```
ReconPlanner（战略层）
  管理 World Model 生命周期
  reflect / decide_next / generate_briefing
  总预算管理
  不决定 agent 去哪个 URL，不微管理探索顺序
      │
      │ briefing（自然语言）
      ▼
Agent Session（执行层）
  接收 briefing + system prompt
  自主决定导航、观察、提取
  10 个工具（browse/interact/bash/...）
  发现即记录（note_insight/note_relation）
```

---

## 二、技术约束

### 语言与框架

- Python 3.10+，全程 async
- Session 间串行执行，不做并发
- 异步设计留好扩展点，但 MVP 不需要并行 session

### LLM API

通过 **OpenAI-compatible gateway** 调用，兼容多模型（Claude / Gemini / 其他）。使用 `openai` Python SDK。

```
LLM_BASE_URL=http://<gateway>:3000/v1
LLM_API_KEY=<key>
LLM_MODEL=<model-name>
```

### 外部服务

| 服务 | 连接方式 | 必需 |
|------|----------|------|
| Camoufox 反检测浏览器 | WebSocket `ws://camoufox:1234/ws` | 是（可降级本地 Chromium） |
| PostgreSQL 16 | asyncpg `postgresql://user:pass@db:5432/dbname` | 是 |
| LLM API | HTTPS（OpenAI-compatible gateway） | 是 |

### Docker Compose

```yaml
services:
  camoufox:    # 反检测 Firefox，暴露 ws://camoufox:1234/ws
  db:          # PostgreSQL 16，端口 5432
  dev:         # 开发容器，源码 bind mount
```

### 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `LLM_API_KEY` | 是 | gateway API 密钥 |
| `LLM_BASE_URL` | 是 | gateway 地址 |
| `LLM_MODEL` | 否 | 模型名，默认值待定 |
| `DATABASE_URL` | 是 | PostgreSQL 连接串 |
| `BROWSER_WS_URL` | 否 | Camoufox WS URL，默认 `ws://camoufox:1234/ws` |
| `ARTIFACTS_DIR` | 否 | 样本输出目录，默认 `./artifacts` |

预留（MVP 不实现）：`AUTH_GITHUB_STATE`、`AUTH_GOOGLE_STATE` — 未来认证能力。

### 核心依赖

| 库 | 用途 |
|----|------|
| `playwright` >=1.40.0 | 浏览器自动化 |
| `openai` >=1.0.0 | LLM API（通过 gateway） |
| `asyncpg` >=0.29.0 | PostgreSQL |
| `beautifulsoup4` >=4.12.0 | HTML 解析 |
| `httpx` >=0.25.0 | 异步 HTTP |
| `duckduckgo-search` >=6.0.0 | search_site |
| `python-dotenv` >=1.0.0 | .env 加载 |

---

## 三、模块结构

```
src/
├── main.py                    # CLI 入口
├── planner/
│   └── recon_planner.py       # ReconPlanner：reflect / decide_next / generate_briefing / 主循环
├── agent/
│   ├── session.py             # Agent Session 执行循环
│   └── tools/                 # 10 个工具实现
│       ├── browse.py
│       ├── interact.py
│       ├── search_site.py
│       ├── bash_tool.py
│       ├── execute_code.py
│       ├── think.py
│       ├── note_insight.py
│       ├── note_relation.py
│       ├── read_wm.py
│       └── extract.py
├── world_model/
│   ├── model.py               # SiteWorldModel dataclass
│   ├── db.py                  # PostgreSQL CRUD
│   └── schema.sql             # DDL（见 SiteWorldModel设计文档.md）
├── llm/
│   └── client.py              # OpenAI-compatible 统一客户端
├── browser/
│   └── manager.py             # Playwright / Camoufox 连接管理
└── utils/
    ├── url.py                 # URL 规范化
    └── logging.py             # 结构化日志
```

`planner/` 不拆多文件。reflect / decide_next / generate_briefing 都是 LLM 调用，逻辑简单，放一个文件里直到复杂度要求拆分。

---

## 四、实施顺序

### Step 1：World Model 数据层

DB schema（4 张表，见 SiteWorldModel设计文档.md）+ Python dataclass + async CRUD。

**验证：** 能 connect → create tables → 写入 location + observation + relation → 加载完整 World Model → 加载空 World Model。

### Step 2：浏览器 + LLM 客户端

**浏览器连接优先级：**
1. `BROWSER_WS_URL` 环境变量 → `playwright.connect(ws_url)`（Camoufox）
2. `BROWSER_CDP_URL` 环境变量 → `playwright.connect_over_cdp(url)`
3. 都没有 → `playwright.chromium.launch()` 本地启动

**LLM 客户端两种调用模式：**
- `chat_with_tools(messages, tools)` — Agent Session 用，解析 tool_calls
- `generate(prompt, system?)` — ReconPlanner 用（reflect / decide_next / generate_briefing）

**重试策略：**
- content_filter 错误：重试 3 次，每次 sleep 2s
- 网络超时：SDK 层 2 次指数退避
- LLM 返回空 → Session 循环退出

**验证：** 能连 Camoufox 导航页面；能调 LLM 拿到 tool_calls 响应。

### Step 3：工具集（10 个）

每个工具的设计说明见架构共识文档§六。这里补充实施要点：

**browse(url)：** 核心工具。导航 + 自动写 World Model（创建/更新 Location，追加 Observation）+ 返回结构化摘要给 LLM。摘要至少应包含标题、可交互元素概况、链接模式。需要做 SPA 空壳检测：如果元素很多但可见文本很少，提示 agent 页面可能需要交互/等待才能加载内容。

**interact(action, target)：** 统一 click / fill / scroll_down / scroll_up / select / press_key。`action` 是动作类型字符串，`target` 是 CSS selector 或描述。

**extract(script, key)：** 在浏览器执行 JS 提取数据。自动追加 Observation（提取方法 + 样本索引）。样本数据存文件（`artifacts/samples/`），Observation 中存引用路径。返回摘要给 LLM（记录数、字段名、前几条预览），不把完整数据塞进 context。

**bash(command) / execute_code(code, lang)：** subprocess 执行，30 秒超时，Docker 容器隔离。

**note_insight(content, location?) / note_relation(from, to, relation)：** 写 World Model。location 参数接受 pattern 字符串，自动解析到 location_id。

**read_world_model(section?)：** 返回格式化的 World Model 快照。section 可选过滤（locations / observations / relations / all）。

**search_site(query)：** 域名锁定搜索（`site:domain query`），使用 DuckDuckGo。

**think(thought)：** 无副作用，返回 ok。

**ToolRegistry：** 注册 + JSON Schema 生成 + 分发执行。所有 session 使用同一套工具集。

**验证：** 每个工具独立调用成功；browse 自动写 World Model；extract 保存样本到文件。

### Step 4：Agent Session

**核心循环：** system prompt + briefing → LLM chat with tools → 执行 tool calls → 追加消息 → 循环直到停止。

**三层停止：**
1. LLM 不再调工具 → 自然结束（while 循环的自然出口）
2. 步数预算耗尽（初始值 30 步/session）→ 强制结束
3. 连续失败 ≥ 5 → 强制结束

**System Prompt：** 短、稳定。教 agent 怎么想（5 条思维原则，见架构共识文档§五）。不含任何站点特定信息。

**Briefing：** 动态生成，作为 user message 注入。首次 session 直接用 domain + requirement。后续由 generate_briefing 生成。

**Context 管理：** 保留最近 N 步完整 tool call 历史，旧步骤压缩/截断，总 context 超限从最旧删。browse 和 extract 的返回值可能很大，非最近步骤需要截断。MVP 用简单截断即可。

**验证：** 给一个手写 briefing，agent 能跑完一个 session，World Model 中出现 locations + observations。

### Step 5：ReconPlanner

**主循环：** 见本文档§一的核心循环描述。

**reflect / decide_next / generate_briefing：** 都是 LLM 调用。prompt 措辞实施时调试，框架见架构共识文档§三。reflect 和 decide_next 可以合并为一次调用。

**停止条件：**
1. decide_next 返回 DONE
2. 总 session 数达到上限（初始值 10 轮）
3. 连续 N 轮 reflect 无新 insight（初始值 3 轮）

**验证：** 给 codepen.io + 一个 requirement，能跑完多轮 session，World Model 有 locations / observations / relations / reflections，最终停止。

---

## 五、MVP 定义

### 目标站点：codepen.io

选择理由：SPA + 公开 API + 多种数据路径 + 公开数据充足，能真正验证架构价值。

codepen.io 的特征（agent 应该自己发现这些，不要硬编码，但可以用来验证 World Model 质量）：
- 前端是 SPA，需要 JS 渲染
- 公开 API 端点存在
- 数据有多条访问路径：tag 页 → 列表页 → 详情页、搜索、API
- 相同实体（pen）在不同位置有不同字段

### MVP 成功标准

给 codepen.io 和一个简单 requirement（如"找出 threejs 相关的 pen 数据"），系统能：

1. 启动并连接 DB + Camoufox + LLM
2. ReconPlanner 发起 Session，Agent 自主导航探索
3. World Model 中出现多个 Location（tag 页、详情页、API 端点等）
4. Observations 包含：页面摘要、agent 洞察、提取方法
5. Relations 记录了位置间关系（如列表→详情、子集关系）
6. reflect 产出了高层 insight（如"三条数据路径的覆盖关系"）
7. `artifacts/samples/` 中有提取的数据样本
8. decide_next 最终返回 DONE

### MVP 不需要

- 跨 Run 复用（加载旧 World Model）
- 认证/登录
- 精细 context 管理（简单截断即可）
- 精细预算控制（固定步数即可）
- CLI 参数解析（先硬编码 domain 和 requirement）
- 最终报告编译（返回原始 World Model）

---

## 六、硬约束（不可违反）

1. **不预设网站类型。** 没有"电商站""文档站"分类。
2. **不预设数据 schema。** 没有 target_fields。
3. **不预设关系类型枚举。** Agent 自由描述。
4. **Observation 只追加不修改。**
5. **所有知识都是 Observation。** 不拆独立表。
6. **智能优先。** 信息的组织、筛选、呈现交给 LLM，不硬编码。
7. **不硬编码控制流。** Agent 自己决定去哪、看什么、提什么。
8. **System prompt 教怎么想，Briefing 告诉想什么。**
9. **Observation.raw 是自由格式 JSONB。**
10. **浏览器默认 Camoufox，不预设访问障碍。**

---

## 七、安全模型

**安全边界：Docker 容器。** 容器内 agent 有完整权限（浏览器、bash、文件系统），这是设计决定。

**信任边界：**
- LLM 输出不可信：工具调用需参数验证（防格式错误导致崩溃，不是防恶意）
- 目标网站不可信：agent 在容器内操作，不影响宿主机
- 操作员完全可信

**密钥管理：** 环境变量注入，不写入代码。

---

*本文档是实施起点。实施过程中细节会调整，但§六的硬约束不可改变。*
