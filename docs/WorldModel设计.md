# World Model 设计

> 状态：共识稿 v2
> 日期：2026-03-25
> 前置：系统架构与信息流.md
> 调研基础：AriGraph, MemGPT/Letta, MAGMA, CoALA, Generative Agents, Manus, HypoGeniC

---

## 一、World Model 的本质

World Model 不是数据库，不是 observation 的集合，是**从数据中提炼出来的结构化理解**。

类比：100 条田野观察记录是 data。一份"该地区的植被分布图 + 采样方法手册"是 model。

### 三层记忆架构（共识）

来自 CoALA 框架（Cognitive Architectures for Language Agents）和 AriGraph 等系统的共识模式：

```
Transcript（原始记录）— 发生了什么
  = Session 完整 message array，不可变 append-only
  人类类比：录音带

Observations（结构化知识）— 从记录中提炼的观察
  = 录制 Agent 维护的知识层，可创建/更新/合并/删除
  人类类比：田野笔记本（可修订）

Semantic Memory（语义记忆）— 世界是什么样
  = Semantic Model，持久化，不断更新的站点理解
  人类类比：研究报告

Procedural Memory（程序性记忆）— 怎么做才有效
  = Procedural Model，持久化，不断更新的方法论
  人类类比：操作手册
```

各层服务不同的认知需求：
- Planner 决定方向 → 主要读 Semantic Model
- Agent 执行任务 → 主要读 Procedural Model + Briefing 中的 observations
- Agent mid-session 回忆 → 读 Observations（通过 read_world_model）
- 人类评估结果 → 读 Semantic Model + Procedural Model
- 调试/审计 → Transcript（不可变原始记录）

### 三层数据架构

| 层 | 性质 | 可变性 | 维护者 |
|---|------|--------|--------|
| Transcript | 不可变原始记录 | **append-only** | 基础设施程序化保存 |
| Observations | 结构化知识 | **可维护**（CRUD） | 录制 Agent |
| Semantic / Procedural Model | 精炼理解 | **全量重写** | Planner LLM |

Transcript 是不可变的事实记录。Observations 是录制 Agent 从 transcript 中提炼并持续维护的结构化知识。Model 是 Planner 从 observations 中精炼的高层理解。

---

## 二、Episodic Memory：Observations

### 数据模型

```python
@dataclass
class Observation:
    id: int | None       # DB 自增
    location_id: str     # 属于哪个 location
    agent_step: int | None
    raw: dict            # 自由格式 JSONB
    created_at: datetime | None
```

### raw 的内容类型

不用 `type` 字段区分，用 key 存在性：

| Key 存在性 | 来源 | 含义 |
|-----------|------|------|
| `"page_summary" in raw` | browse 工具 | 页面快照 |
| `"extraction_method" in raw` | extract 工具 | 提取结果 |
| `"insight" in raw` | note_insight 工具 | Agent 的理解 |

### Location

```python
@dataclass
class Location:
    id: str              # domain::pattern
    run_id: str | None
    domain: str
    pattern: str         # 由 agent 决定粒度
    how_to_reach: str | None
    observations: list[Observation]
```

Location 的粒度由 agent 自行决定（硬约束："不预设网站类型"）。系统不强制 URL 归纳规则。

### 设计约束

- **录制 Agent 维护**：observations 由录制 Agent 全权管理（创建/更新/合并/删除），执行 Agent 不直接写入
- **Transcript 是不可变备份**：原始 message array 程序化保存到 transcripts/（JSONL），任何 observation 的修改都可追溯
- **自由格式**：raw 是 JSONB，不预设 schema

---

## 三、Semantic Model：站点理解

一个有界的、LLM 维护的文档，表达 agent 对站点的当前结构化理解。

### 文档结构

```markdown
## Site Overview
（一段话：站点是什么、核心实体/内容类型是什么、整体架构特征）

## Locations
（每个已知 URL pattern 的认知卡片）

  ### {pattern}
  - 内容类型：...
  - 数据/字段：...
  - 量级：...
  - 访问条件：...
  - 与需求的关系：...

## Data Relationships
（跨 location 的数据关联——包含/子集/超集/替代关系，实体在不同位置的表现差异）

## Requirement Mapping
（需求到数据路径的映射：已验证路径、备选路径、受阻路径）

## Open Questions
（当前理解中明确的缺口，每个都是具体的、可通过探索回答的问题）
```

### 各部分质量要求

**Site Overview**
- 必须具体到"这个站点是做什么的、核心内容/实体是什么"
- 不可模糊（✗ "这是一个网站"）
- 不可编造未观察到的技术细节
- 观察到的影响探索的架构特征要写明（如 SPA、需要 JS 渲染、有反爬机制）

**Locations**
- 每个 location 必须说清楚：**有什么内容/数据**、**什么字段/格式**、**大概多少量**
- 标明未知（"字段未确认"、"量级未知"比猜测更有价值）
- 存 pattern 不存具体 URL（具体 URL 是 observation 的事）
- 必须可指导下一步行动——读完一个 location 的卡片，应该能决定要不要去探索它

**Data Relationships**
- 说清楚实体在不同位置的字段差异和数据量差异
- 说清楚数据的包含/子集/替代关系
- 不可只说"有关系"——说清楚什么关系

**Requirement Mapping**
- 明确指出已验证 vs 未验证 vs 受阻的路径
- 受阻原因要具体（"需要认证" 而非 "不可用"）
- 如果有多条路径，说明各自的优劣

**Open Questions**
- 每个问题必须是具体的、可通过一次或几次探索回答的
- 不可模糊（✗ "还有很多不知道的"）
- 这些问题直接驱动 Planner 的下一步方向决策

### 约束

- **有界**：~8000 chars 上限。超限时 LLM 压缩旧内容，不截断
- **可验证**：每个断言应可追溯到 observations（不需显式标注，但不可编造）
- **未知 > 编造**：宁可写"未知"也不猜测
- **Pattern > Instance**：存 URL pattern，具体 URL 留在 observations 里
- **全量重写**：每次更新整篇重写，被新发现推翻的旧内容直接替换

---

## 四、Procedural Model：方法论

一个有界的、LLM 维护的文档，表达 agent 积累的操作方法论。

### 文档结构

```markdown
## Site Characteristics
（影响所有操作的站点级特征——渲染方式、认证要求、反爬机制、响应特征等。
  只记录已观察到的、会影响 agent 行为的特征。）

## Working Methods
（已验证有效的方法，按 location 或数据类型组织。
  每条方法包含：具体做法、产出什么、验证结果。）

## Failed Approaches
（已验证无效的方式。
  每条包含：尝试了什么、失败原因。防止重复犯错。）

## Navigation Patterns
（站点内的导航方式——分页机制、入口路径、跳转规律等。）
```

### 各部分质量要求

**Site Characteristics**
- 只记录已观察到的特征，不猜测
- 必须是会影响 agent 操作行为的特征（如果不影响操作就不需要记录）

**Working Methods**
- 必须具体到可直接复用的程度——读完一条方法，agent 应该能直接执行
- 包含产出描述（产出了什么数据、什么字段、多少条）
- 不限定特定技术手段——提取可以是任何方式（DOM 操作、API 调用、文件解析、脚本执行等），文档不预设

**Failed Approaches**
- 失败原因必须明确——不是"试过了不行"而是"为什么不行"
- 具体到可避免的程度——读完后 agent 知道不要重复这个错误

**Navigation Patterns**
- 说清楚怎么从一个位置到另一个位置
- 分页、筛选、排序等机制如果已发现就记录

### 约束

- **有界**：~6000 chars 上限
- **方法中立**：不预设提取技术——任何在目标站点上有效的方法都应记录
- **全量重写**：同 Semantic Model
- **经验驱动**：只记录实际尝试过的方法和结果，不记录未验证的推测

---

## 五、Model 的更新机制

### 更新时机

Semantic Model 和 Procedural Model 在 Planner 的 evaluate_and_decide 调用中更新——理解、方法更新、决策在同一个 LLM context 中完成（无序列化边界的信息损失）。

```
LLM 调用 evaluate_and_decide:
  输入：
    - 新 observations（分层格式化）
    - 现有 Semantic Model 全文
    - 现有 Procedural Model 全文
    - Session 历史
    - Requirement
  输出：
    - 更新后的 Semantic Model 全文
    - 更新后的 Procedural Model 全文
    - DONE 或 CONTINUE + 方向 + relevant locations
```

Model 更新是决策的**副产物**，不是独立步骤。

### 为什么是全量重写而非增量 patch

调研发现（Aider benchmark, Manus 实践, MemGPT）：
- 文档在 ~400 行以下时，LLM 全量重写的可靠性 > 增量 patch
- 我们的两个文档都在 ~8000 chars 以内，远低于这个阈值
- 全量重写让 LLM 有机会重组结构、删除过时内容、压缩冗余
- 增量 patch 容易积累垃圾（旧内容不删、结构退化）

### 首次生成

第一个 session 之前没有 model。第一次 evaluate_and_decide 从零生成两个文档。prompt 中提供文档结构模板和质量要求作为参考。

---

## 六、Session：执行日志（独立于 Model）

Session 不是 World Model 的一部分——它记录的是 agent 的执行过程，不是对站点的认知。

```python
@dataclass
class Session:
    id: str
    run_id: str | None
    started_at: datetime | None
    ended_at: datetime | None
    outcome: str | None          # natural_stop / budget_exhausted / ...
    steps_taken: int | None
    trajectory_summary: str | None  # 工具名序列
    direction: str | None           # 本轮探索方向
```

Session 数据供 Planner 参考（"之前的 session 做了什么"），但不纳入 World Model 的三层架构。

---

## 七、DB Schema

```sql
-- Episodic Memory
CREATE TABLE IF NOT EXISTS locations (
    id          TEXT PRIMARY KEY,
    run_id      TEXT,
    domain      TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    how_to_reach TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS observations (
    id          SERIAL PRIMARY KEY,
    location_id TEXT REFERENCES locations(id),
    agent_step  INT,
    raw         JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Execution Log
CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT,
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    ended_at            TIMESTAMPTZ,
    outcome             TEXT,
    steps_taken         INT,
    trajectory_summary  TEXT
);

-- Semantic & Procedural Models（持久化）
CREATE TABLE IF NOT EXISTS models (
    domain      TEXT NOT NULL,
    model_type  TEXT NOT NULL,          -- 'semantic' 或 'procedural'
    content     TEXT NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (domain, model_type)
);
```

4 张表。前两张存 episodic memory，第三张存执行日志，第四张存 model 文档。

---

## 八、数据流全景

```
Agent Session 执行中
  ├─ 执行 Agent：探索和提取（12 个工具）
  ├─ 录制 Agent（并行）：维护 Observations（CRUD：创建/更新/合并/删除）
  └─ read_world_model → 两层检索：无参数返回完整 Models，指定 location 返回 Observations

Session 结束 → Planner 接管
  │
  ├─ 程序化：从 DB 取 observations, models, sessions → 格式化成文本
  │
  ├─ LLM evaluate_and_decide:
  │    输入：observations + 现有 models + sessions + requirement
  │    输出：更新后 semantic model + 更新后 procedural model + DONE/CONTINUE
  │    → 持久化两个 model 到 DB
  │
  └─ LLM generate_briefing（仅 CONTINUE）:
       输入：方向 + relevant observations + models
       输出：任务书 → 传给下一个 Session
```

---

## 九、跨 Run 复用

同一 domain 再次运行时：
1. 从 DB 加载该 domain 的 Semantic Model 和 Procedural Model
2. 从 DB 加载历史 observations 和 locations
3. LLM 看到时间戳，自己判断旧知识的可信度
4. 第一次 evaluate_and_decide 基于旧 model + 新 observations 更新

Model 天然支持跨 run——它是对站点的持久化理解，不绑定某次运行。

---

## 十、设计决策记录

### D1: 三层数据架构

Transcript（不可变原始记录）→ Observations（录制 Agent 维护的知识层）→ Models（Planner 精炼的理解）。每层有明确的维护者和可变性规则。

理由：CoALA 框架、AriGraph、Generative Agents 等系统证明多层记忆比单层显著提升 agent 能力。Transcript 保证审计追踪；Observations 由录制 Agent 维护（可合并去重，避免数量爆炸）；Models 由 Planner 全量重写。Procedural 独立于 Semantic 因为"世界是什么样"和"怎么操作"是不同类型的知识。

### D2: Model 是有界文档，LLM 全量重写

不是知识图谱、不是三元组、不是 JSON patch。是有字符上限的文本文档，每次由 LLM 整篇重写。

理由：Manus（todo.md 全量重写）和 MemGPT（core memory blocks 有字符上限）验证了这个模式。文档有界迫使 LLM 保持精炼。全量重写让 LLM 能重组结构、删除过时内容。

### D3: Session 不属于 World Model

Session 是执行日志，不是站点认知。两者分开存储，Planner 按需同时读取。

理由：World Model 表达"对站点的理解"，Session 表达"agent 做了什么"。概念混合会导致 model 膨胀且语义不清。

### D4: Observation 用 key 存在性区分类型

用 `"page_summary" in obs.raw` 而非 `obs.raw.get("type")`。

理由：之前 `type` 字段的 schema mismatch 导致 9/9 runs 失败。key 存在性直接匹配工具的实际写入行为。

### D5: Model 更新是决策的副产物

不设独立的 reflect 步骤。Model 更新在 evaluate_and_decide 的同一次 LLM 调用中完成。

理由：每次 LLM 调用完全独立。拆成 reflect → decide 会在序列化边界损失信息。合并后 LLM 在同一 context 中完成理解和决策。

### D6: 文档结构引导方向但不限制格式

Semantic Model 和 Procedural Model 有推荐的 section 结构，但 LLM 可以根据实际发现自行调整。不强制固定模板。

理由："不预设网站类型"硬约束。不同站点需要不同的理解结构。

### D7: 方法中立，不预设技术手段

Procedural Model 不预设提取/导航的技术方式。记录的是"在这个站点上什么有效"，不限定是什么技术。

理由："不预设数据 schema"硬约束的延伸。提取可以是 DOM 操作、API 调用、文件解析、脚本注入或任何其他方式。

---

*下一步：→ AgentSession设计.md（Session 内的 agent 设计）*
