# Full-Self-Crawl-Agent Beta — 实施指引

> 用途：给 Claude Code 的实施蓝图
> 日期：2026-03-18（最后更新：2026-04-05）
>
> **前置阅读（必读，按顺序）：**
> 1. 本文档
> 2. `架构共识文档.md` — 系统设计的完整共识
> 3. `docs/WorldModel设计.md` — 数据模型设计（取代旧 SiteWorldModel设计文档.md）
> 4. `docs/工具重新设计共识.md` — 工具层最新共识

---

## 一、系统概述

Full-Self-Crawl-Agent Beta 是一个 LLM 驱动的网站侦察系统。给定一个域名和自然语言需求，它自主探索网站结构、理解数据分布、采集样本。

**核心循环：**

```
ReconPlanner.run(domain, requirement)
  → 初始化 SiteWorldModel
  → [可选] spawn Research Subagent（初始背景调研）
  → loop:
      Agent Session（执行 Agent + 录制 Agent 并行）
        执行 Agent：自主探索和提取
        录制 Agent：实时写 Observations 到 DB（执行 Agent 可 read_world_model 查回)
      → evaluate_and_decide(wm, [research_reports])  # 更新 Model + DONE/CONTINUE
      → [可选] spawn Research Subagent（按需深入调研）
      → generate_briefing(wm, direction, [research_reports])  # 生成下一轮任务简报
  → 编译最终报告
```

**系统边界：** 到"理解 + 样本"为止。全量提取不在范围内。

**两层架构：**

```
ReconPlanner（战略层）
  管理 World Model 生命周期
  evaluate_and_decide / generate_briefing
  总预算管理
  不决定 agent 去哪个 URL，不微管理探索顺序
      │
      │ briefing（自然语言）
      ▼
Agent Session（执行层）
  接收 briefing + system prompt
  自主决定导航、观察、提取
  工具集详见 docs/工具重新设计共识.md
  发现即记录（note_insight）
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
| Camoufox 反检测浏览器 | Python 直接启动（pipe 通信） | 是（可降级本地 Chromium） |
| PostgreSQL 16 | asyncpg `postgresql://user:pass@localhost:5432/dbname` | 是（本地安装或云数据库） |
| LLM API | HTTPS（OpenAI-compatible gateway） | 是 |

### 运行环境

**MVP 完全本地运行，不使用 Docker。**

```
本地环境：
  Python 3.10+ (venv)
  Camoufox（通过 camoufox Python 包直接启动）
  PostgreSQL（本地安装或云数据库如 Supabase/Neon）
```

Docker Compose 作为**可选部署方案**保留，用于：生产服务器部署、团队环境一致性、CI/CD。
不作为 MVP 开发的必需项。

### 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `LLM_API_KEY` | 是 | gateway API 密钥 |
| `LLM_BASE_URL` | 是 | gateway 地址 |
| `LLM_MODEL` | 否 | 模型名，默认值待定 |
| `DATABASE_URL` | 是 | PostgreSQL 连接串 |
| `BROWSER_WS_URL` | 否 | 远程 Camoufox WS URL（本地直接启动时不需要） |
| `ARTIFACTS_DIR` | 否 | 样本输出目录，默认 `./artifacts` |

预留（MVP 不实现）：`AUTH_GITHUB_STATE`、`AUTH_GOOGLE_STATE` — 未来认证能力。

### 核心依赖

| 库 | 用途 |
|----|------|
| `camoufox` >=0.4.11 | 反检测浏览器（Python 直接启动） |
| `playwright` >=1.40.0 | 浏览器自动化 |
| `openai` >=1.0.0 | LLM API（通过 gateway） |
| `asyncpg` >=0.29.0 | PostgreSQL |
| `beautifulsoup4` >=4.12.0 | HTML 解析 |
| `httpx` >=0.25.0 | 异步 HTTP |
| `curl_cffi` >=0.7.0 | HTTP + 浏览器 TLS 指纹模拟（bash 中使用） |
| `duckduckgo-search` >=6.0.0 | Research Subagent 的 web_search 后端（可切换 Brave/Tavily） |
| `markdownify` >=0.11.0 | web_fetch 的 HTML → Markdown 转换 |
| `python-dotenv` >=1.0.0 | .env 加载 |

---

## 三、模块结构

```
src/
├── main.py                    # CLI 入口
├── planner/
│   └── recon_planner.py       # ReconPlanner：evaluate_and_decide / generate_briefing / 主循环
├── agent/
│   ├── session.py             # Agent Session 执行循环
│   └── tools/                 # 12 个工具（详见 docs/工具重新设计共识.md）
│       ├── browse.py          # 页面内容快照 + 多标签页导航
│       ├── read_network.py    # 网络层信息
│       ├── browser_eval.py    # 浏览器内 JS 执行
│       ├── browser_reset.py   # 浏览器重启/重配置
│       ├── click.py           # 点击元素
│       ├── input.py           # 输入/选择
│       ├── press_key.py       # 按键
│       ├── scroll.py          # 滚动
│       ├── go_back.py         # 浏览器后退
│       ├── bash_tool.py       # 系统代码执行
│       ├── think.py           # 推理
│       └── read_wm.py         # 查询 World Model
├── world_model/
│   ├── model.py               # SiteWorldModel dataclass
│   ├── db.py                  # PostgreSQL CRUD
│   └── schema.sql             # DDL（见 docs/WorldModel设计.md）
├── llm/
│   └── client.py              # OpenAI-compatible 统一客户端
├── browser/                       # 浏览器基础设施层（所有浏览器工具共享）
│   ├── manager.py             # 连接管理（直接启动 / WS / CDP / Chromium fallback）
│   ├── page_repr.py           # 页面表示（HTML → Markdown+HTML 混合格式）
│   ├── element_index.py       # 元素索引系统（编号 ↔ selector 映射）
│   ├── network_capture.py     # 网络请求捕获（被动 page.on('response')）
│   ├── dom_settle.py          # DOM 稳定等待 + SPA 空壳检测
│   └── context.py             # 工具共享的浏览器上下文
├── recording/
│   └── agent.py               # 录制 Agent：持久对话 + tool-use 循环，维护 Observations（CRUD）
├── research/
│   └── agent.py               # Research Subagent：调研互联网，产出报告文件（web_search/web_fetch/bash/think）
└── utils/
    ├── url.py                 # URL 规范化
    └── logging.py             # 结构化日志

# 运行时文件结构（per-domain）
artifacts/{domain}/
├── samples/                   # 提取的数据样本（browser_eval save_as）
├── scripts/                   # 可复用脚本（精准记忆）
├── workspace/                 # 临时文件、bash 大输出落盘
├── transcripts/               # session 完整记录（JSONL）
└── research/                  # Research Subagent 调研报告（markdown）
```

`planner/` 不拆多文件。evaluate_and_decide / generate_briefing 都是 LLM 调用，逻辑简单，放一个文件里直到复杂度要求拆分。

---

## 四、实施顺序

### Step 1：World Model 数据层

DB schema（4 张表：locations / observations / models / sessions，见 docs/WorldModel设计.md）+ Python dataclass + async CRUD。

**验证：** 能 connect → create tables → 写入 location + observation → 加载完整 World Model → 加载空 World Model。

### Step 2：浏览器 + LLM 客户端

**浏览器连接优先级：**
1. 默认 → `AsyncCamoufox(headless=True)` 直接启动（本地开发用 `headless=False` 可视调试）
2. `BROWSER_WS_URL` 环境变量 → `playwright.firefox.connect(ws_url)`（远程 Camoufox）
3. `BROWSER_CDP_URL` 环境变量 → `playwright.connect_over_cdp(url)`（远程 Chromium）
4. Camoufox 不可用 → `playwright.chromium.launch()` 本地 Chromium 兜底

agent 可通过 `browser_reset(browser_type?, proxy?, headed?)` 动态切换浏览器配置。
详细策略见架构共识文档§六"浏览器环境与策略"。

**LLM 客户端两种调用模式：**
- `chat_with_tools(messages, tools)` — Agent Session 用，解析 tool_calls
- `generate(prompt, system?)` — ReconPlanner 用（evaluate_and_decide / generate_briefing）

**重试策略：**
- content_filter 错误：重试 3 次，每次 sleep 2s
- 网络超时：SDK 层 2 次指数退避
- LLM 返回空 → Session 循环退出

**验证：** 能连 Camoufox 导航页面；能调 LLM 拿到 tool_calls 响应。

### Step 3：工具集

工具设计详见 `docs/工具重新设计共识.md`。核心工具按能力层分：

**浏览器感知（3 个）：**
- `browse(url?, new_tab?, tab?)` — 页面内容快照 + 多标签页导航
- `read_network(filter?)` — 网络层信息（请求/响应/cookies，JS 无法获取的数据）
- `browser_eval(script, save_as?)` — 浏览器内 JS 执行（探测 + 提取 + 检查）

**浏览器管理（1 个）：**
- `browser_reset(proxy?, browser_type?, headed?)` — 重启浏览器到新配置

**页面交互（5 个，原 interact 按参数模式拆分）：**
- `click(target)` — 点击元素（自动识别 `<select>` 转下拉处理）
- `input(target, value)` — 输入/选择（自动检测 autocomplete，执行后验证值）
- `press_key(key, target?)` — 按键（支持组合键 "Ctrl+A"）
- `scroll(direction?, amount?, target?)` — 滚动（支持水平 + 容器内滚动）
- `go_back()` — 浏览器后退

**系统执行（1 个）：**
- `bash(command)` — 浏览器外代码执行（API 重放、数据处理、搜索）

**认知辅助（2 个）：**
- `think(thought)` — 无副作用推理
- `read_world_model(location?)` — 查询 World Model（无参数返回完整 Models，有 location 返回该 location 的 Observations）

注：note_insight 不再需要——录制 Agent 与执行 Agent 实时并行，全权负责 Observation 写入。
交互工具内置智能行为（元素类型识别、autocomplete 检测、值验证、新元素标记 `*`、对话框自动处理），详见工具重新设计共识 §2.3。

**ToolRegistry：** 注册 + JSON Schema 生成 + 分发执行。

**验证：** 每个工具独立调用成功；browse 返回页面快照；browser_eval 能探测和提取数据。

### Step 4：Agent Session

**核心循环：** system prompt + briefing → LLM chat with tools → 执行 tool calls → 追加消息 → 循环直到停止。

**停止条件：**
1. LLM 不再调工具 → 自然结束（while 循环的自然出口）
2. Context window 满了 → 强制结束
3. 连续失败 ≥ 5 → 强制结束

**System Prompt：** 短、稳定。教 agent 怎么想（5 条思维原则，见架构共识文档§五）。不含任何站点特定信息。

**Briefing：** 动态生成，作为 user message 注入。首次 session 直接用 domain + requirement。后续由 generate_briefing 生成。

**Context 管理（microcompact，参考 Claude Code）：** 每次 LLM 调用前程序化处理——最近 5 个 API round 的 tool results 完整保留，更早的大输出工具（browse/browser_eval/bash/read_network）results 替换为 `[已清除，调 read_world_model 查回]`。小输出工具（click/input/scroll/press_key/go_back/think/read_world_model/browser_reset）不清除。所有 tool_use blocks（工具名+参数）永远保留。

**验证：** 给一个手写 briefing，agent 能跑完一个 session，World Model 中出现 locations + observations。

### Step 5：ReconPlanner + Research Subagent

**主循环：** 见本文档§一的核心循环描述。

**Session 内并行 + Session 间流程：**
- **录制 Agent**（独立持久对话 + tool-use 循环）：与执行 Agent **同构但独立**。程序定期注入执行 transcript 增量作为 user message，录制 Agent 用 4 个工具（read/create/edit/delete observations）维护 Observations。执行 Agent 可随时通过 read_world_model 查回。可用更轻模型。
- Session 结束后：
  1. **evaluate_and_decide**（Planner LLM 调用）：读 Observations（已实时写入）+ 完整 WM + 现有 research 报告 → 更新 Semantic/Procedural Model → DONE/CONTINUE → 可选输出"需要调研 X"信号。
  2. **[可选] spawn Research Subagent**：根据 evaluate_and_decide 的调研信号 spawn 一个调研任务。
  3. **generate_briefing**（Planner LLM 调用）：从 WM + research 报告生成下一轮 briefing。

**Research Subagent（tool-use 循环，与执行 Agent 同构）：**
- 工具：`web_search(query, domain?)`, `web_fetch(url)`, `bash(command)`, `think(thought)`
- 输入：调研任务描述（domain + 要查的方向）
- 输出：调研报告写入 `artifacts/{domain}/research/{topic}.md`，路径返回给 Planner
- 触发时机：
  - Session 0 之前：初始背景调研（目标站点 tech stack、API 文档、社区讨论）
  - evaluate_and_decide 输出"需要调研 X"时：按需深入调研
- 跟执行 Agent 的区别：感知对象是互联网（不是目标站点），工具集更小（4 个）

**停止条件：**
1. evaluate_and_decide 返回 DONE
2. 总 session 数达到上限（初始值 10 轮）
3. 连续 N 轮无新发现（初始值 3 轮）

**验证：** 给 codepen.io + 一个 requirement，能跑完多轮 session，World Model 有 locations / observations / Semantic Model / Procedural Model，最终停止。

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
5. Semantic Model 记录了结构理解和位置间关系
6. evaluate_and_decide 产出了高层 insight（如"三条数据路径的覆盖关系"）
7. `artifacts/samples/` 中有提取的数据样本
8. decide_next 最终返回 DONE

### MVP 不需要

- 跨 Run 复用（加载旧 World Model）
- 认证/登录
- 精细 context 管理（microcompact 即可，不需要 LLM 摘要）
- CLI 参数解析（先硬编码 domain 和 requirement）
- 最终报告编译（返回原始 World Model）

---

## 六、硬约束（不可违反）

1. **不预设网站类型。** 没有"电商站""文档站"分类。
2. **不预设数据 schema。** 没有 target_fields。
3. **不预设关系类型枚举。** Agent 自由描述。
4. **Transcript 只追加不修改。** Observations 由录制 Agent 维护（可创建/更新/合并/删除），Transcript 是不可变的审计追踪。
5. **三层数据架构。** Transcript（不可变）→ Observations（录制 Agent 维护）→ Models（Planner 全量重写）。详见 docs/WorldModel设计.md。
6. **智能优先。** 信息的组织、筛选、呈现交给 LLM，不硬编码。
7. **不硬编码控制流。** Agent 自己决定去哪、看什么、提什么。
8. **System prompt 教怎么想，Briefing 告诉想什么。**
9. **Observation.raw 是自由格式 JSONB。**
10. **浏览器默认 Camoufox，不预设访问障碍。**

---

## 七、安全模型

**安全边界：专用运行环境（本地开发机或专用服务器）。** Agent 有完整权限（浏览器、bash、文件系统），这是设计决定。

**bash 安全策略（无 Docker 时）：**
- MVP：受限用户 + 工作目录权限限制，专用服务器（炸了重部署）
- 后续可选：命令模式验证器（参考 Claude Code 的 22+ bash validator）
- Docker 沙箱作为可选加强方案保留

**信任边界：**
- LLM 输出不可信：工具调用需参数验证（防格式错误导致崩溃，不是防恶意）
- 目标网站不可信：agent 在受限环境内操作
- 操作员完全可信

**密钥管理：** 环境变量注入（.env），不写入代码。

---

*本文档是实施起点。实施过程中细节会调整，但§六的硬约束不可改变。*
