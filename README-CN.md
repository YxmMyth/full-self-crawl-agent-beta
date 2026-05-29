# Full-Self-Crawl Agent

LLM 驱动的"调研 + 采集"agent。给定 domain + 自然语言数据需求,agent 自动浏览
站点、建立结构化 World Model、写可复用 crawler 脚本、产出数据集。

```bash
python -m src.main auto openslr.org \
  "采集 OpenSLR 上的小语种 (low-resource, 非英语/中文/印欧大语种) 语音数据集,
   含音频文件 + 转写 + 元数据 (SLR 编号、语种、时长、许可证、采样率)。" \
  --no-gate
```

不需要 URL 模板、XPath 选择器、schema 配置 — 自然语言描述**你要什么**,
agent 自己想清楚**怎么搞**,自己用 mission-specific 合约审核自己,
最后给你**样本数据 + 可执行脚本**。

---

## 这是什么 / 不是什么

**是:**

- 两阶段 agent 系统:**Recon** 阶段探站点 + 建 World Model + 写 catalog +
  收 sample;**Harvest** 阶段基于 recon handoff 真量产数据,LLM auditor
  对照 disk evidence 守门"做完了"的声明。
- **Domain-agnostic** 设计 — 无 hardcoded URL 模板、站点分类、数据 schema。
  所有运行时假设都是 agent 自己发现的。
- **Mission-specific 合约** — 每个 mission 启动时,基于 requirement + catalog
  即时编译 checklist,hash-pinned 防 agent 自己篡改,作为 audit 的 per-criterion
  接受合约。

**不是:**

- 一个 scraper 配置生成器 — 没有规则配置层让你写。
- 黑盒服务 — agent 写的所有东西(catalog、samples、transcripts、scripts、
  audit verdict)都在磁盘上,人可以直接看。
- 万能反爬突破工具 — 登录墙、CAPTCHA、强 WAF 时,agent 会知道**主动求救**
  (`request_human_assist` 弹桌面对话框),不会假装成功。

---

## Quick Start

### 前置依赖

- **Python 3.10+**
- **PostgreSQL 16** — 本地装或任何能访问的实例(Neon / Supabase / 云 PG 都行)。
  World Model + run 元数据存这。
- **Camoufox** — Firefox-based 反检测浏览器,通过 pip 装。首次运行会自动
  下载 ~250MB 二进制。
- **OpenAI 兼容的 LLM 端点**(DeepSeek / MiMo / Qwen / GLM / GPT / Claude
  — 任何说 `/v1/chat/completions` 协议的都行)。

### 安装

```bash
git clone <repo-url> full-self-crawl-agent
cd full-self-crawl-agent

# 1. Python 依赖
pip install -r requirements.txt

# 2. Camoufox 浏览器二进制(~250MB,一次性)
camoufox fetch

# 3. 数据库
psql -c "CREATE DATABASE recon_agent;"
psql recon_agent < src/world_model/schema.sql

# 4. 配置
cp .env.example .env
# 然后填 LLM_API_KEY、LLM_BASE_URL、LLM_MODEL、DATABASE_URL
```

### 运行

```bash
# Recon + Harvest 一气呵成,中间无人审 gate:
python -m src.main auto <domain> "<requirement>" --no-gate

# 或分阶段跑:
python -m src.main explore <domain> "<requirement>"            # 仅 recon
python -m src.main harvest <domain> --from-run <run_id>        # 仅 harvest
```

---

## 架构

```
┌─────────────────────── Recon 阶段 ────────────────────────┐
│                                                          │
│   ReconPlanner (tool-use agent, 5 工具)                  │
│     ├─ spawn_execution ──► 执行 Agent Session            │
│     │                        ├─ 14 工具(browse, fetch,  │
│     │                        │   bash, click, ...)       │
│     │                        └─ push 增量 ──────────►   │
│     │                                       Recording   │
│     │                                       Agent ────► │
│     │                                       (单例)      │
│     │                                       维护         │
│     │                                       Observations│
│     ├─ spawn_research  ──► Research 子 agent             │
│     │                        (web_search, web_fetch,     │
│     │                         bash, think)               │
│     ├─ read_model      ──► 加载 Semantic + Procedural    │
│     ├─ think           ──► 无副作用                      │
│     └─ mark_done       ──► 触发 Recon Audit              │
│                            (6 criteria, LLM 单次调用)    │
│                                                          │
│   PASS 后 ──────────────────────────────────────────►    │
└──────────────────────────────────────────────────────────┘
                              │ catalog/, samples/, WM
                              ▼
              ┌─── 可选人审 gate ────────┐
              │  改 requirement.txt 或   │
              │  在 harvest 前 abort     │
              └────────────┬─────────────┘
                           ▼
┌────────────────── Harvest 阶段 ──────────────────────────┐
│                                                          │
│   Launcher 基于 requirement + catalog + samples +        │
│   procedural model 编译 checklist.md(hash-pinned)。     │
│                                                          │
│   Harvest Agent Session                                  │
│     ├─ 14 个 recon 工具 + apply_patch + mark_done        │
│     ├─ 开干前先读 checklist                              │
│     ├─ 写 data/ + crawler 脚本 + state.json              │
│     └─ 调 mark_done → Harvest Audit                      │
│                            (加载 checklist,逐 criterion │
│                             对照 disk 真状态评估)        │
│                                                          │
│   PASS ────────────────► mission 完成                    │
│   BLOCKED ──────────────► agent 迭代,重新 mark_done     │
└──────────────────────────────────────────────────────────┘
```

**数据架构(3 层,不可变性约束):**

| 层 | 来源 | 可变性 |
|---|---|---|
| **Transcripts** | 每个 session 完整 LLM 对话(JSONL) | append-only,从不编辑 |
| **Observations** | Recording Agent 维护的结构化 per-location 笔记 | 仅 Recording Agent 可 CRUD |
| **Models**(Semantic + Procedural) | LLM 蒸馏出的站点理解 | 每 session 由 maintain_model 全量重写 |

详见 `docs/WorldModel设计.md`。

---

## 实战案例

### 1. OpenSLR — 小语种语音数据

```bash
python -m src.main auto openslr.org \
  "采集 OpenSLR 站上小语种 (low-resource,非英语/中文/印欧大语种) 语音数据集,
   含音频文件 + 转写 + 元数据 (SLR 编号、语种、时长、许可证、采样率)。" \
  --no-gate
```

Recon 发现 `/resources.php` catalog + `info.txt` 元数据格式,识别 `dlcdn1.cgyouxi.com`
镜像比主站更稳,写出 `catalog/low_resource_candidates.json` 含 61 个筛选后的
数据集。Harvest 跑可断点续传 + retry + 流式下载 + 事后校验的脚本。

### 2. 66rpg.com(橙光) — 文字游戏资源包

```bash
python -m src.main auto 66rpg.com \
  "采集 66rpg.com 文字游戏 3 个,每个含完整剧本资源:
   剧情文本 + 立绘 + BGM + CG。" \
  --no-gate
```

Agent 通过反向工程 H5 player 的 JS 自主推出 CDN 协议 —
`/web/{guid}/{ver}/Map.bin` 取资源清单,`/shareres/{md5[:2]}/{md5}` 取单文件
— 完全绕开 WebGL 渲染的前端。

### 3. xmind.com Gallery — 公开思维导图

```bash
python -m src.main auto xmind.com \
  "采集 xmind.com gallery 公开思维导图 30 张,
   含图片本体 + 元数据 (标题、分类、作者、tag)。" \
  --no-gate
```

Recon 通过 API 摸到 SPA 数据层(`share.xmind.app/previews/{id}.png`),
在 `all_maps_dedup.jsonl` cataloged 990 张,按 requirement 数量选 30 张 harvest。

---

## 输出

每个 run 在独立目录:

```
artifacts/{domain}/runs/{run_id}/
├── samples/        ★ Primary data — 真文件(音频 / PDF / 源码 / 等)
├── catalog/        索引、列表、API 元数据 — recon 的 universe handoff
├── workspace/      Harvest agent 工作区:脚本、checklist、errors.log
│                   └── data/        Harvest 出的数据集(交付物)
├── transcripts/    Per-session LLM 对话(JSONL,append-only)
├── sessions/       Per-session traces + 截图
├── research/       Research 子 agent 报告
├── verification/   Audit 报告 + per-criterion 裁定
├── requirement.txt Mission 文本
└── strategy_report.md  Planner 最终策略总结
```

浏览器持久化 profile 单独在 `artifacts/_profiles/{domain}/`,跨 run 共享 —
一次登录,后续同 domain 的 run 复用。

---

## 配置(`.env`)

| 变量 | 必需 | 说明 |
|---|---|---|
| `LLM_API_KEY` | 是 | OpenAI 兼容 API key |
| `LLM_BASE_URL` | 是 | 端点,如 `https://api.deepseek.com/v1` |
| `LLM_MODEL` | 是 | 模型名,如 `deepseek-v4-pro` |
| `DATABASE_URL` | 是 | PostgreSQL 连接串 |
| `VISION_LLM_MODEL` | 否 | `browse(visual=True)` 用的多模态模型;默认 `Doubao-Seed-2.0-pro` |
| `BROWSER_WS_URL` | 否 | 远程 Camoufox WebSocket URL |
| `BROWSER_CDP_URL` | 否 | 远程 Chromium CDP URL |
| `RECON_HEADLESS` | 否 | 设为 `1` 强制 recon headless(默认 headed) |
| `ARTIFACTS_DIR` | 否 | 覆盖 artifacts 根路径(默认 `./artifacts`) |
| `MAX_PLANNER_TOOL_CALLS` | 否 | Recon planner 安全网(默认 200) |
| `MAX_SESSIONS` | 否 | Recon planner session 上限(默认 15) |

Agent 已在以下端点验证过:

- DeepSeek 官方 API
- DeepSeek 公司 gateway(代理)
- 小米 MiMo Token Plan(`mimo-v2.5-pro`)
- 豆包(`Doubao-Seed-2.0-pro`)— vision
- Claude(`claude-opus-4-7`)— vision

---

## 测过的站点

| 站点 | 测试阶段 | 结果 | 备注 |
|---|---|---|---|
| `openslr.org` | recon + harvest | recon PASS 6/6,harvest 进行中 | 61 数据集 cataloged,~2.7GB samples |
| `66rpg.com`(橙光) | recon | PASS 6/6 | Agent 独立反推出 CDN 协议 |
| `xmind.com` | recon + harvest | recon PASS 6/6,harvest blocked(audit 抓 satisficing) | 30 张图 + 预览交付 |
| `chemrxiv.org` | recon | 未完成 | 撞 Cloudflare Turnstile,agent 调 `request_human_assist` 求救 |
| `codepen.io` | recon | PASS | 早期 MVP 测试,SPA + 公开代码 |
| `douyin.com` | recon | PASS | 早期 MVP 测试,需登录内容 |

详细 run-by-run 发现见 `docs/three_missions_observations.md`。

---

## 文档

设计文档在 `docs/`,按领域组织:

| 文档 | 领域 |
|---|---|
| `CLAUDE.md` | 实施蓝图 — 硬架构约束 |
| `docs/Planner设计.md` | Planner tool-use 循环、5 工具、决策策略 |
| `docs/AgentSession设计.md` | Execution agent 循环、停止条件、microcompact |
| `docs/WorldModel设计.md` | 3 层数据架构的设计动机 |
| `docs/工具重新设计共识.md` | 14 个执行工具的逐个设计说明 |
| `docs/抽象边界原则.md` | Agent vs Infrastructure 边界 — 哪些暴露 / 哪些屏蔽 |
| `docs/SystemPrompts设计.md` | 每层 agent 的 system prompt 结构 |
| `docs/three_missions_observations.md` | 最新 E2E 跑日志 — 3 个 mission(chemrxiv / 66rpg / xmind) |
| `docs/部门部署架构_local-recon_server-harvest.md` | 部署方向讨论 |
| `docs/agent_stress_test_candidates.md` | 来自内部需求的压测候选清单 |

英文 README 在 `README.md`。

---

## 状态

MVP,持续迭代。核心 pipeline(recon → handoff → harvest → audit)已 E2E 验证。
已知限制 + follow-up bug 在 `docs/three_missions_observations.md` 跟踪。

主要在 Windows 11 + Python 3.14 + Camoufox 135 / 150 上构建和测试。
之前归咎于 Camoufox 二进制的 headed 浏览器 launch hang,实际是 psutil 强杀
session 后残留的 `parent.lock` 文件 — fix 在 commit `ed8bcdb`。

License: 暂未指定。
