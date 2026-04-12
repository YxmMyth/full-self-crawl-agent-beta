# Planner 设计

> 状态：共识稿 v1
> 日期：2026-04-12
> 前置：架构共识文档.md、docs/WorldModel设计.md

---

## 一、定位

Planner 是两层架构的**战略决策层**，以 tool-use agent 形式运行。

- **持续 LLM 循环**：接收 domain + requirement → 反复调用工具 → 直到 mark_done 成功
- **纯决策**：不直接操作浏览器、不直接维护 observations、不直接更新 Model
- **工具就是派发能力**：通过 spawn_execution / spawn_research 派发子 agent，通过 read_model 获取认知状态
- **Requirement 的唯一持有者**：Requirement 在 Planner 的 initial user message 中，不传给 Execution Agent（通过 briefing 按阶段暴露）

与之前设计的关键区别：
- ~~evaluate_and_decide + generate_briefing 两次固定 LLM 调用~~ → tool-use 循环，LLM 自主决策
- ~~Planner 有 update_model 工具~~ → Model 由 Python 代码自动触发 maintain_model 更新
- ~~Planner 主动 spawn verification~~ → verification 由 mark_done 内部程序触发

---

## 二、工具集（5 个）

### 2.1 spawn_execution(briefing) → dict

派发一次执行 session（Execution Agent + Recording Agent 协作）。

**参数：**

| 名称 | 类型 | 说明 |
|------|------|------|
| `briefing` | str | 自然语言任务书（方向 + 已知信息 + 起点 + 完成标准） |

**内部 pipeline（Python 代码实现，Planner 不感知）：**

```
1. 创建 Session 记录（DB sessions 表）
2. 启动 Execution Agent（tool-use 循环，12 个工具）
   - System prompt: 探索思维原则
   - User message: briefing
3. Execution Agent 每次 tool call → transcript 追加
   - Python 代码把 transcript 增量推给单例 Recording Agent
   - Recording Agent 持续 CRUD observations
4. Session 停止（自然结束 / context 满 / 连续失败 ≥ 5）
5. 确认 Recording Agent 已处理完本 session 增量
6. 调 maintain_model LLM 函数（详见 §五）
   - 更新 Semantic + Procedural Model → 写回 DB
   - 生成 session summary + model_diff
7. 返回结果给 Planner
```

**返回值：**

```json
{
  "summary": "探索了 /tag/threejs 和 /tag/webgl，发现 API 端点 /api/v1/pens",
  "model_diff": "Semantic: 新增 2 locations；Procedural: 新增 curl 方法",
  "new_obs_count": 15,
  "session_id": "s003"
}
```

### 2.2 spawn_research(topic, questions) → dict

派发一次互联网调研任务（Research Subagent）。

**参数：**

| 名称 | 类型 | 说明 |
|------|------|------|
| `topic` | str | 调研主题 |
| `questions` | str | 具体要回答的问题 |

**内部：** 启动 Research Subagent（tool-use 循环，4 个工具：web_search / web_fetch / bash / think）。

**返回值：**

```json
{
  "key_findings": "codepen 有公开 API v1，文档在 blog.codepen.io/documentation/api/...",
  "report_path": "artifacts/codepen.io/research/api_docs.md"
}
```

### 2.3 read_model() → dict

查看当前 Semantic + Procedural Model 全文。

**参数：** 无

**返回值：**

```json
{
  "semantic_model": "## Site Overview\nCodePen 是...\n## Locations\n...",
  "procedural_model": "## Site Characteristics\n...\n## Working Methods\n...",
  "version": 3,
  "updated_after_session": "s003"
}
```

幂等，永远返回 DB `models` 表最新版本。Semantic ~8K chars，Procedural ~6K chars。

### 2.4 think(thought) → dict

无副作用推理。

**参数：**

| 名称 | 类型 | 说明 |
|------|------|------|
| `thought` | str | 推理内容 |

**返回值：**

```json
{
  "thought": "（回显推理内容）"
}
```

### 2.5 mark_done(reason) → dict

标记侦察完成。内部自动触发 Verification Subagent（如果 feature 启用）。

**参数：**

| 名称 | 类型 | 说明 |
|------|------|------|
| `reason` | str | 终止理由 |

**内部逻辑（Python 代码，Planner 不感知 verification 存在）：**

```python
async def mark_done(reason: str) -> dict:
    if config.VERIFICATION_SUBAGENT_ENABLED:
        model = await load_model(domain)
        verdict, gaps = await run_verification_subagent(model, requirement)
        if verdict == "PASS":
            return {"status": "DONE"}
        else:
            return {
                "status": "blocked",
                "verdict": verdict,        # FAIL / PARTIAL
                "gaps": gaps,
                "message": "Verification found gaps. Address them and try again."
            }
    else:
        return {"status": "DONE"}
```

**返回值：**
- 成功：`{"status": "DONE"}`
- 被驳回：`{"status": "blocked", "verdict": "FAIL", "gaps": "...", "message": "..."}`

Planner 看到 blocked 后继续 tool-use 循环，根据 gaps 派发新的 session/research。

---

## 三、信息获取模型

### Push（自动注入 Planner context）

| 来源 | 内容 | 大小 |
|---|---|---|
| spawn_execution tool_result | session summary + model_diff | 几百字 |
| spawn_research tool_result | key_findings | 几百字 |
| mark_done tool_result（被驳回时） | verdict + gaps | 几百字 |

Planner 每次 tool call 返回时自动看到，不需要主动读。

### Pull（Planner 主动调用）

| 工具 | 内容 | 大小 |
|---|---|---|
| read_model() | 完整 Semantic + Procedural Model | ~14K chars |

Planner 需要全景理解时主动调用。不是每次决策都需要——有时 summary 的 model_diff 够用。

### 为什么 Model 用 Pull 不用 Push

- Semantic 8K + Procedural 6K = 14K chars，每次都 push 会 context 爆炸
- 不是每次决策都需要完整 Model（summary 够用时跳过）
- read_model 幂等，不受 microcompact 影响（新调用 = 新 tool_result）
- Planner 自己判断何时需要全景

---

## 四、Requirement 处理

### 顶级不变量

系统的唯一外部输入是 domain + requirement。Requirement 贯穿整个 run。

### 触及范围

| 组件 | 看得到 requirement | 途径 |
|---|---|---|
| Planner | ✓ | initial user message |
| Verification Subagent | ✓ | mark_done 内部传入 |
| Research Subagent | ✓ | spawn_research 的 topic/questions 转述 |
| **Execution Agent** | **✗** | 只看 briefing |
| **Recording Agent** | **✗** | 通用记录原则 |

### L1-L4 阶段性暴露

Planner 根据当前 Model 状态判断处于哪个阶段，决定 briefing 中暴露多少 requirement：

| 阶段 | 目标 | briefing 中的 requirement |
|---|---|---|
| **L1** 站点结构 | 广度探索 URL pattern | 不含 |
| **L2** 数据分布 | 字段/格式/量 | 不含 |
| **L3** 需求映射 | requirement → 路径 | 含关键词 |
| **L4** 样本采集 | 精确提取 | 含完整 requirement |

阶段不是硬编码——Planner LLM 看 Model 的丰富度自己判断。L1-L4 是认知框架，不是代码分支。

---

## 五、maintain_model pipeline

### 触发时机

每次 spawn_execution 的 session 结束后，Python 代码**自动触发**。不是 Planner 的工具，不需要 Planner 主动调用。

这是**数据通道**（observations → model 的提炼），不是控制流（"下一步做什么"的决策）。自动触发不违反硬约束 #7。

### 实现（一次或两次 LLM 函数调用）

```python
async def maintain_and_summarize(domain: str, session_id: str) -> dict:
    """
    非 agent。函数式 LLM 调用。
    可合并为一次 LLM 调用（同时输出 new model + summary）。
    """
    current_model = await load_model(domain)
    new_obs = await load_observations_from_session(session_id)
    transcript_brief = await load_transcript_brief(session_id)

    result = await llm.generate(
        prompt=build_maintain_prompt(
            current_semantic=current_model.semantic,
            current_procedural=current_model.procedural,
            new_observations=new_obs,
            transcript_brief=transcript_brief
        )
    )

    new_model = parse_model(result)       # Semantic + Procedural 全文
    summary = parse_summary(result)       # 给 Planner 的 session 摘要
    model_diff = parse_diff(result)       # 给 Planner 的变化概览

    await save_model(domain, new_model)   # UPSERT 到 DB models 表

    return {"summary": summary, "model_diff": model_diff, ...}
```

### 为什么不是 agent

maintain_model 是**数据提炼**，不是**自主决策**：
- 输入确定（current model + new observations）
- 输出确定（new model + summary）
- 不需要工具调用（不需要 browse/click/search）
- 不需要多轮迭代（一次 LLM 调用完成）
- 不需要独立 context（没有持续对话）

agent 的特征是"自主决策 + 工具调用循环"。maintain_model 没有这些特征。

---

## 六、Context 管理

### Microcompact（跟 Execution Agent 同策略）

每次 Planner LLM 调用前程序化处理：

- 最近 **5 轮**的 tool_results 完整保留
- 更早的**大输出工具**（spawn_execution / spawn_research）的 tool_results 替换为 `[已清除，调 read_model 查看当前 Model]`
- **小输出工具**（think / read_model / mark_done）的 tool_results 不清除
- 所有 **tool_use blocks**（工具名 + 参数）永远保留

### Planner 的 Context 初始内容

```
System prompt:
  侦察规划原则 + L1-L4 认知 + 工具使用指南 + 工作节奏

User message:
  "Domain: {domain}
   Requirement: {requirement}"
```

后续 tool_use + tool_result 累积，microcompact 管理总量。

### 不需要 auto-compact

Planner 的 tool call 频率远低于 Execution Agent（后者每分钟 5-15 次，Planner 可能每几分钟一次），microcompact 足够控制 context 增长。

---

## 七、安全网

| 参数 | 默认值 | 说明 |
|---|---|---|
| `MAX_PLANNER_TOOL_CALLS` | 200 | Planner 总 tool call 数上限 |
| `MAX_SESSIONS` | 15 | spawn_execution 调用次数上限 |
| `MAX_CONSECUTIVE_SAME_TOOL` | 5 | 连续调用同一工具次数上限（防死循环） |

触发时注入终止信号，Planner 循环结束。这些是兜底参数，正常运行时不应触发。

---

## 八、System Prompt 要素

### 角色定义

```
你是侦察规划专家。你管理对目标站点的系统性侦察。

你的职责：
1. 派发执行 session 探索目标站点
2. 派发调研任务查阅互联网资料
3. 阅读 Model 理解当前认知状态
4. 在认知充分时标记完成

你不直接操作浏览器，不直接维护 observations，不直接更新 Model。
```

### L1-L4 层级认知

```
侦察按四个层级推进：
- L1 站点结构：URL pattern 和连接关系（广度探索）
- L2 数据分布：每个 pattern 的数据格式和数量（深度了解）
- L3 需求映射：requirement → 数据路径（对齐需求）
- L4 样本采集：实际提取数据样本（验证方法）

前两层是纯探索（briefing 不含 requirement），后两层对照需求。
你的 briefing 应当反映当前阶段——看 Model 的丰富度判断。
```

### Briefing 原则

```
briefing 是你给 Execution Agent 的任务书：
- 方向：做什么
- 已知信息：相关 Model 摘要（agent 需要的背景）
- 具体起点：从哪个 URL 开始
- 完成标准：什么算"做完了"

Execution Agent 只看 briefing，不看 requirement 全文。
```

### 工作节奏

```
每次 spawn_execution 或 spawn_research 返回后：
1. 看返回的 summary 和 model_diff
2. 如果需要全景理解 → read_model()
3. think 评估当前进度和下一步方向
4. 决定：继续探索 / 换方向 / spawn_research / mark_done

mark_done 前建议先 read_model 确认 Model 的完整性。
mark_done 可能被驳回（gaps），驳回后根据 gaps 继续。
```

---

## 九、设计决策记录

### D1: Planner 是 tool-use agent 而非固定 LLM 调用

之前设计：evaluate_and_decide + generate_briefing 两次固定 LLM 调用。改为 tool-use agent。

理由：
- 硬约束 #7（不硬编码控制流）要求决策由 LLM 做，不由代码分支决定
- Research 触发时机不清 → LLM 自主判断比硬编码 signal 机制更灵活
- 首次 briefing 特殊分支 → 不需要——Planner 第一次 tool call 就是 spawn_execution
- 执行 Agent 和 Research Agent 平级派发，逻辑一致
- 两层都是 tool-use agent，架构对称

### D2: maintain_model 是自动触发的 LLM 函数，不是 agent

之前设计：Planner 有 update_model 工具（方案 B）或独立 Maintainer Agent（方案 C）。改为 Python 代码自动触发的函数。

理由：
- "宏观决策"和"抽象提炼"是不同抽象层级的工作，塞同一个 LLM 循环会偷懒/乱序
- 行为教学 + 安全网是打补丁，不治本
- 但独立 Agent 过度设计——"observations → model" 就是一次 LLM 调用的事
- 物理分离：Planner 纯决策，提炼由 Python 代码自动触发
- maintain_model 是数据通道不是控制流，自动触发不违反硬约束 #7

### D3: Verification 由 mark_done 内部程序触发

之前设计：Planner 通过 system prompt 约束"mark_done 前必须调 spawn_verification"。改为 mark_done 的实现内部触发。

理由：
- LLM 容易偷懒跳过 verification，system prompt 约束不可靠
- 程序触发不依赖 LLM 良心
- Planner 不需要知道 verification 存在——只知道 mark_done 可能失败
- mark_done 返回 blocked + gaps → Planner 自然处理

### D4: Recording Agent 单例化

之前设计：每 session 一个 Recording Agent。改为单例 + Producer-Consumer。

理由：
- 并发多 session 时，多个 Recording Agent 写 observations 会重叠/冲突
- 单例消费者保证没有写冲突
- 单例有全局视角，跨 session 的 location 重叠能正确去重/合并
- Producer-Consumer 模式自然支持并发和串行

### D5: 信息获取 Push 摘要 + Pull 详情

spawn_* 返回摘要（push），read_model 按需读完整 Model（pull）。

理由：
- Session summary 短文本，每次必看 → push 合理
- 完整 Model 14K 字符，每次都 push 会 context 爆炸
- read_model 幂等，不受 microcompact 影响
- Planner 自己判断何时需要全景 vs 摘要够用

### D6: Requirement 收窄到 Planner 层

Execution Agent 不直接看 requirement，只看 briefing。

理由：
- L1-L4 层级性 → 早期阶段 requirement 多余
- Planner 根据当前阶段生成对应 briefing，按需暴露
- 完美符合硬约束 #8（system prompt 教怎么想，briefing 告诉想什么）

---

*关联文档：架构共识文档.md、docs/WorldModel设计.md、docs/工具重新设计共识.md、docs/AgentSession设计.md*
