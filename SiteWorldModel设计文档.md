# SiteWorldModel 设计文档

> 状态：已达成共识
> 日期：2026-03-17

---

## 一、设计哲学

World Model 不是一个预定义 schema 被填满的过程，是一份**持续增长的田野笔记**。

借鉴 Kosmos（AI Scientist）的核心创新：用结构化 world model 在多次 agent 调用之间共享信息，使系统能在数百次 agent 运行中保持连贯性。

映射到站点侦察：agent 每次行动后，观察被写入 world model；每次需要决策时，world model 被查阅。不预设网站应该长什么样，让发现在探索中涌现。

---

## 二、核心数据模型：3 + 1

能跨越所有网站的只有三样东西，加一个独立的运行记录：

### Location（位置）—— 你可以去的地方

不是具体 URL，是 URL pattern 或访问方式。

```python
@dataclass
class Location:
    id: str                          # 自动生成
    pattern: str                     # "/tag/*", "/api/pens", "homepage nav"
    how_to_reach: str                # "navigate to URL" / "search for X" / "curl API"
    observations: list[Observation]  # 在这个位置看到的/理解的所有东西
```

### Observation（观察）—— 你在那个位置看到的/理解的东西

**完全自由格式。** Observation 承载所有类型的知识——页面摘要、agent 的高层理解、提取方法、数据样本索引——通过内容自然区分，不预设 type 枚举。

```python
@dataclass
class Observation:
    timestamp: str
    agent_step: int
    raw: dict                        # 自由格式 JSONB，看到什么记什么
```

raw 的例子——各种知识都是 Observation：

```python
# 页面摘要（browse 自动写入）
{"page_summary": "产品列表页", "item_count": 48, "pagination": True,
 "filters": ["price", "brand"], "link_patterns": ["/product/*"]}

# Agent 的高层理解（note_insight 写入）
{"insight": "搜索结果是 tag 页的超集，去重后约 800 条"}

# 操作方法（note_insight 写入）
{"insight": "API 需要 X-Requested-With header 才返回 JSON",
 "procedural": True}

# 提取方法（extract 自动写入）
{"extraction_method": "js_extract",
 "script": "() => document.querySelectorAll('.pen-item')...",
 "success_count": 3, "fields": ["title", "author", "views"]}

# 数据样本索引（extract 自动写入，数据存文件）
{"sample_ref": "/artifacts/samples/tag_threejs_001.jsonl",
 "record_count": 24, "fields": ["title", "html", "css"]}

# API 响应结构
{"api_response": True, "format": "json", "total": 1847,
 "sample_fields": ["title", "html", "css", "js", "user"]}

# SPA 空壳页面
{"spa_shell": True, "js_rendered": True,
 "note": "content loads after scroll/interaction"}

# 登录墙
{"login_wall": True, "requires": "authentication"}

# ReconPlanner reflect 产出的高层 insight（reflect 写入）
{"reflection": "该站点有三条获取 pen 数据的路径，API 覆盖最广且最易用",
 "based_on": ["session_3_obs_1", "session_3_obs_2", "session_3_obs_3"]}

{"reflection": "三条路径的数据量关系：/tag(240) ⊂ /search(1200) ⊂ /api(1847)",
 "uncertainty": "数字基于页面显示，未验证实际翻页"}
```

### Relation（关系）—— 位置之间的联系

```python
@dataclass
class Relation:
    from_location: str               # location id
    to_location: str                 # location id
    relation_type: str               # 自由格式
    detail: str                      # 补充说明
```

relation_type 的例子（不预设枚举）：
- `items_link_to` — 列表页的条目链接到详情页
- `subset_of` — A 的数据是 B 的子集
- `same_entities_different_fields` — 同一批东西，但字段不同
- `navigates_to` — 导航链接
- `requires_first` — 必须先访问 A 才能到 B（如登录）

### Sessions（运行记录）—— 独立于站点知识

Sessions 不是对站点的观察，是对运行过程的记录，独立存储。

```python
@dataclass
class Session:
    id: str
    run_id: str
    started_at: str
    ended_at: str
    outcome: str
    steps_taken: int
    trajectory_summary: str
```

---

## 三、完整的 SiteWorldModel

```python
@dataclass
class SiteWorldModel:
    domain: str
    requirement: str

    # 核心三件套
    locations: list[Location]
    relations: list[Relation]

    # 运行记录
    sessions: list[Session]
```

没有 proven_extractions 字段、没有 samples 字段、没有 hypotheses 字段、没有 insights 字段。这些都是 Observation 的不同内容形态，统一存在 observations 里。

### 关键设计决策

**Observation.raw 是自由格式。** 因为不同网站的"同一种位置"看到的东西完全不同。预定义 schema 就是在假设所有网站长得一样。

**所有类型的知识都是 Observation。** 页面摘要、agent 理解、提取方法、样本索引——都挂在对应的 Location 下。不需要独立的表。

**关系也是自由格式。** 不预设关系类型的枚举。

**"同一个 pen 在不同位置有不同表示"自然表达：**

```
Location "/tag/threejs" 的 observations:
  {"page_summary": ..., "item_count": 240, "link_patterns": ["/pen/*"]}

Location "/pen/{id}" 的 observations:
  {"page_summary": ..., "fields_found": ["title", "html", "css", "js", ...]}
  {"extraction_method": "js_extract", "script": "...", "success_count": 3}

Location "/api/pens" 的 observations:
  {"api_response": True, "total": 1847, "sample_fields": [...]}
  {"insight": "无需认证，支持 q 和 page 参数"}

Relations:
  /tag/threejs → items_link_to → /pen/{id}
  /tag/threejs → subset_of → /api/pens
```

不需要一个"Pen 实体"的预定义。"Pen 是什么"从不同位置的观察中涌现。

---

## 四、三层记忆

三层记忆（Semantic / Procedural / Episodic）不对应独立的表或字段，是对同一个 World Model 的三种查询视角：

```
Semantic（知道什么）
  → 查 locations + observations 中的结构性知识
  → 查 relations

Procedural（怎么做）
  → 查 observations 中的操作方法和提取脚本
  → 例：raw 包含 extraction_method 或 procedural 字段的 observations

Episodic（做过什么）
  → 查 sessions 记录
  → 查 observations 的时间序列
```

三层记忆的价值是指导 agent 行为（知道要记什么）和指导 generate_briefing() 组织信息（怎么呈现），不是数据结构。

---

## 五、DB Schema

```sql
CREATE TABLE locations (
    id          TEXT PRIMARY KEY,
    run_id      TEXT,
    domain      TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    how_to_reach TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE observations (
    id          SERIAL PRIMARY KEY,
    location_id TEXT REFERENCES locations(id),
    agent_step  INT,
    raw         JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE relations (
    id              SERIAL PRIMARY KEY,
    from_location   TEXT REFERENCES locations(id),
    to_location     TEXT REFERENCES locations(id),
    relation_type   TEXT NOT NULL,
    detail          TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE sessions (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT,
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    ended_at            TIMESTAMPTZ,
    outcome             TEXT,
    steps_taken         INT,
    trajectory_summary  TEXT
);
```

JSONB 让 observations.raw 能存任何东西，又支持查询：

```sql
-- 找所有提取方法
SELECT * FROM observations WHERE raw ? 'extraction_method';

-- 找所有 agent 洞察
SELECT * FROM observations WHERE raw ? 'insight';

-- 找所有 API 端点
SELECT * FROM observations WHERE raw->>'api_response' = 'true';

-- 找有分页的列表页
SELECT * FROM observations WHERE raw->'pagination' IS NOT NULL;

-- 找某个 location 的最新观察
SELECT * FROM observations WHERE location_id = ? ORDER BY created_at DESC LIMIT 1;

-- 找所有提取脚本（Procedural 视角查询）
SELECT l.pattern, o.raw->>'script' as script, o.raw->>'success_count' as success
FROM observations o JOIN locations l ON o.location_id = l.id
WHERE o.raw ? 'extraction_method';

-- 找所有 reflect 产出的高层 insight
SELECT * FROM observations WHERE raw ? 'reflection';
```

---

## 六、读写机制

### 写入

**程序自动写入（agent 不感知）：**
- browse(url) → 自动创建/更新 Location + 追加 Observation（页面摘要、信号）
- extract(script, key) → 自动追加 Observation（提取方法 + 样本索引）
- session 结束 → 自动写 sessions 表 + trajectory_summary

**Agent 主动写入（通过工具）：**
- note_insight(content, location?) → 追加 Observation（agent 的理解/洞察）
- note_relation(from, to, relation) → 写入 Relation

**ReconPlanner reflect 写入（session 间）：**
- reflect() → 追加 Observation（从碎片中提炼的高层 insight）
- 参考 Generative Agents 的 Reflection 机制
- insight 就是 observation，存在同一张表，跟原始 observation 同等对待
- 原始 observations 不动，只追加新的 insight
- 形成递归结构：后续 reflect 可以基于之前的 insight 进一步提炼

### 读取

**被动（session 开始时）：**
- ReconPlanner 的 generate_briefing() 用 LLM 读取完整 World Model（含 reflect 的 insights）
- 生成自然语言任务简报，注入 agent 的 context
- LLM 自己决定怎么组织信息、突出什么重点，不硬编码模板

**主动（session 中按需）：**
- agent 调 read_world_model(section?) 查具体细节
- 直接查 DB 的原始 observations（含 agent 的 note 和 reflect 的 insight）

---

## 七、跨 Run 复用

World Model 通过 DB 自然持久化。同一域名再次运行时：

```python
cached = SiteWorldModel.load_from_db(domain)
if cached:
    # 直接作为 generate_briefing 的输入
    # LLM 看到时间戳，自己判断旧知识的可信度
    # 不硬编码过期策略
```

不需要程序判断"哪些过期了"。generate_briefing 的 LLM 看到"3 天前的站点结构"会认为大概率有效，看到"半年前的提取脚本"会建议重新验证。Agent 可以选择验证旧知识或探索新方向。

---

## 八、设计约束

1. **不预设网站类型。** 没有"电商站""文档站""论坛"的分类。
2. **不预设数据 schema。** 没有"产品有标题和价格"的假设。
3. **不预设关系类型的枚举。** Agent 自由描述发现的关系。
4. **Observation 只追加不修改。** 原始观察永远不动。reflect 产出的 insight 作为新 observation 追加，不覆盖旧的。
5. **所有知识都是 Observation。** 页面摘要、agent 洞察、提取方法、样本索引、reflect 的高层 insight——不因知识类型不同而拆分独立表。
6. **智能优先。** 信息的组织、筛选、呈现交给 LLM（generate_briefing），不硬编码模板或淘汰规则。
