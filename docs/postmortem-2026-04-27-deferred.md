# Post-mortem 2026-04-27 — 待讨论问题(5 & 6)

> 这次 ui8.net mission 复盘。问题 1-4 走另立文档落地。
> 5/6 暂缓,先记录现状 + 根因 + 候选修法。

---

## 问题 5 — Recording Agent 的 4 个 reliability sub-issue

### 5.1 `Failed to parse tool_call args` × 24

**现象:** 单次 mission 内 24 次 warning,DeepSeek 返回的 tool_call JSON 偶有残缺(尾随逗号、缺右括号等)。

**当前处理:** `src/llm/client.py` 里 `json.loads` 失败 → log warning → 静默丢弃这次 tool_call,LLM 调用钱白花。

**归因:** 模型 60% / 系统 40%。
- DeepSeek 的 tool-call JSON 质量弱于 Claude/GPT,这是事实
- 但我们 0 retry、0 反馈给模型,等于白挨

**候选修法:**
- A. 解析失败 → 把"你的 JSON 不合法,例如 `<片段>`,重发"作为 tool_result 喂回去,让模型纠错
- B. 切到 GPT/Claude 给 Recording Agent 用(成本 vs 稳定性 trade)
- C. JSON 修复尝试(jsonrepair lib)再喂回去,兜底失败再 A

### 5.2 Recording Agent 偶尔调 `bash`

**现象:** Recording Agent 的工具集里没有 bash,但 LLM 仍尝试调,工具层报 `Unknown tool: 'bash'`。

**之前修法失败:** prompt 加 "Your Tools (ONLY these 4)" 没压住。

**根因:** Recording Agent 读的是 execution agent 原始 transcript,里面密集出现 `"tool_call": {"name": "bash", ...}`、`"tool": "bash"`。LLM 在 in-context 里看到 `bash` 这个 token 被当作 function name 出现 30+ 次,prompt 的软约束压不住 in-context 频率。这是 LLM tool-calling 的通用弱点 — schema 是软约束,context 频率是硬影响。

**归因:** 系统 95%(transcript 未净化)/ 模型 5%。换什么 LLM 都会犯。

**候选修法:**
- A. **Transcript 过滤层** — Recording Agent 收 transcript 之前,把 execution-agent-only 的工具名 mask 掉:
  - `"name": "bash"` → `"name": "<exec_tool_bash>"`
  - 或更激进:把 tool_call block 整个扁平化成 `<exec_action>command:...</exec_action>` 字符串
- B. 只把 reasoning + output_summary 送给 Recording Agent,不送 tool_call 结构(loss 一些信息)
- C. Prompt 加 `<important>` 等 anti-overrride 标记(社区有报告效果一般)

A 是治本 — Recording Agent 的职责是看 agent 干了什么然后维护 obs,不需要看具体工具名。

### 5.3 Content filter → empty assistant message → API 400

**现象:**
```
Content filter hit (attempt 1/3), retrying in 2s
Content filter hit (attempt 2/3), retrying in 2s  
Content filter hit (attempt 3/3), retrying in 2s
LLM API error: 'Invalid assistant message: content or tool_calls must be set'
```

**机制:** DeepSeek content filter 触发后把 content **和** tool_calls 同时抽走,只留 `{role: "assistant"}` 给 client。我们 client 代码:
```python
assistant_msg = {"role": "assistant"}
if response.content: assistant_msg["content"] = ...
if response.tool_calls: assistant_msg["tool_calls"] = ...
messages.append(assistant_msg)  # ← 可能是空 role 消息
```
下一轮 API 调用前提条件 broken,DeepSeek 拒收 → retry → 再次空消息 → 死循环。

**归因:** 模型 50%(filter 模式不寻常)/ 系统 50%(没防御)。

**候选修法:**
- A. append 前检测:如果 content + tool_calls 都空,**skip 不 append,改 inject `{role: "user", content: "<前一轮被 content filter 截断,跳过该轮>"}`**
- B. 检测到 filter,直接 fallback 到另一个 model 重试
- C. Recording Agent 的 input(transcript)做 PII / sensitive content 预过滤

A 最低成本,B 最稳。

### 5.4 反复 update 同一个 observation

**现象:** observation #1004 被 update 4 次,#977 被 update 3 次,#967 被 update 7+ 次。每次只加几行 detail。

**机制:** Producer-Consumer 设计下,execution agent 每完成 1 个 step,Python 立即把新 transcript 推给 Recording Agent。Recording Agent 看着小片段决策"我之前写的 #1004 还有补充,update 一下"。下个 step 来了,又一次 update。

**归因:** 系统 100%。chunk size 太小 → recording agent 永远在"局部信息下做局部决策"。

**候选修法:**
- A. Increment 攒 batch — 累计 N step 或 X 秒后再喂 recording agent(简单)
- B. Recording agent prompt 加 "if uncertain whether to create new vs edit, defer decision"(软约束)
- C. 把 obs 分两阶段:execution 进行中只 append raw,session 结束后再统一整合(改架构)

A 最简单,B 配合 A 效果好。

---

## 问题 6 — 系统缺 self-monitoring

整个 mission 闷头跑,降级信号没有上行通路:

| 异常 | 当前是否被检测 | 当前是否上报 Planner |
|------|----------|------------|
| 22 tabs 累积 | 否 | 否 |
| sessionstore 文件膨胀 | 否 | 否 |
| browser_reset 命中 fallback Chromium | log warning,Planner 看不到 | 否 |
| Recording Agent 连续报错 | log error | 否 |
| Cloudflare 拦下 | 否(只有 page content 显示) | 否 |
| LLM content filter 触发 | log warning | 否 |

**根因:** 系统假设了"单链路顺利",降级反馈通路完全没设计。Planner 只能看 `session_outcome` 这个粗粒度字段,中间所有挣扎都被吃掉。

**候选修法:**
- A. **Health metrics in tool_result** — session 结束后聚合 `tab_count_peak`、`browser_reset_count`、`fallback_used`、`network_errors` 等指标 attach 到 spawn_execution 返回 JSON,Planner 能看见
- B. **Threshold-based escalation** — tab_count > 10 / fallback_used > 0 / consecutive_error_step > 3 时,session 自己中断并把 `degraded_reason` 上报
- C. **Real-time dashboard** — 单独 file 写运行时 metric,人/Planner 都能 tail
- D. **Recording Agent 把异常写入特殊 obs** — 让它的故障也出现在 World Model 里

A + B 是 MVP,C + D 是后续。

**优先级:** P3 — 等 1-4 修完、跑稳了再上,先别堆复杂度。

---

## 处理建议

P0 是 1 + 2 + 3 + 4(各自单独立修复方案文档)。
5 / 6 暂缓,等 1-4 跑通再回头。

5.4 + 6 都涉及架构改动,等系统跑稳后再讨论。
5.1 + 5.2 + 5.3 是低成本可独立修的 P2 项,1-4 完成后捎带修。
