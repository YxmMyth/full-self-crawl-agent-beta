# Bash 工具深度调研报告

> 日期：2026-03-29
> 用途：为 Agent Session 的 bash 工具设计提供完整调研依据
> 调研对象：Claude Code, OpenHands, Manus, Devin, SWE-Agent, mini-SWE-Agent, CodeAct, Vercel just-bash, Vercel d0
> 前置：AgentSession设计.md, 工具层调研报告.md

---

## 目录

1. [各系统的 bash/shell 工具对比](#一各系统的-bashshell-工具对比)
2. [命令执行模型](#二命令执行模型)
3. [超时设计](#三超时设计)
4. [输出处理](#四输出处理)
5. [安全边界](#五安全边界)
6. [预装环境](#六预装环境)
7. [参数设计](#七参数设计)
8. [返回值设计](#八返回值设计)
9. [与其他工具的协作](#九与其他工具的协作)
10. [LLM 写 bash 的常见问题](#十llm-写-bash-的常见问题)
11. [完整设计方案](#十一完整设计方案)

---

## 一、各系统的 bash/shell 工具对比

### 1.1 各系统概览

| 系统 | 工具名 | 参数 | Shell 模型 | 工具数量 | 来源 |
|------|--------|------|-----------|---------|------|
| **Claude Code** | Bash | command, timeout, description, run_in_background | 持久 bash session | ~18 | 系统 prompt + API 文档 |
| **OpenHands** | execute_bash | command, timeout, is_input, reset, security_risk | 持久 pexpect session + Docker | 6-9 | ICLR 2025 论文 + 源码 |
| **Manus** | shell_exec + shell_view + shell_wait + shell_write_to_process + shell_kill_process | id, exec_dir, command / id, seconds / id, input, press_enter | 多 session 管理 + 云 VM | ~29 | 泄露 prompt + tools.json |
| **Devin** | Shell Command | id, exec_dir | 持久 shell + 云机器 | ~35 | 泄露 prompt |
| **SWE-Agent** | bash（非 tool-calling） | 纯文本命令 | subprocess.run（无状态） | 仅 bash + 自定义 ACI | 论文 + 源码 |
| **mini-SWE-Agent** | bash（非 tool-calling） | 纯文本命令 | subprocess.run（无状态） | 仅 bash | 源码 |
| **CodeAct** | 代码块执行 | Python 代码 | 持久 IPython/Jupyter kernel | 代码即工具 | ICML 2024 论文 |
| **Vercel just-bash** | bash.exec() | command + ExecOptions | 隔离 shell 状态 + 共享文件系统 | 1 | 源码 |

### 1.2 关键差异分析

#### 单工具 vs 多工具拆分

**单工具派（bash 一个搞定）：**
- Claude Code：一个 Bash 工具，参数内控制行为
- SWE-Agent / mini-SWE-Agent：只有 bash，不用 tool-calling 接口
- Vercel d0 实验：删到只剩 ExecuteCommand + ExecuteSQL，成功率反升

**多工具拆分派（shell 操作拆成 5 个工具）：**
- Manus：shell_exec / shell_view / shell_wait / shell_write_to_process / shell_kill_process
- Devin：Shell Command + view_shell + write_to_shell_process + kill_shell_process

**证据指向：**
- 拆分的核心原因是**异步长时间命令**——需要发起、查看、等待、发送输入、终止五种操作
- 如果命令执行是同步的（等完再返回），一个工具就够了
- 工具数量与选择准确率非线性关系：4 工具 ~95% vs 46 工具 ~71%（arXiv:2601.04748）
- Vercel d0：16 工具→2 工具，成功率 80%→100%，速度 3.5x

**结论：** 对于我们的同步执行模型（30s 超时，等完返回），单工具是正确选择。异步命令管理不在 MVP 范围内。

#### 有状态 vs 无状态

**有状态（环境/目录持久化）：**
- Manus：显式 exec_dir 参数 + session id，工作目录和环境由调用方控制
- Devin：显式 exec_dir 参数 + shell id
- OpenHands：持久 pexpect session，状态自然保持
- CodeAct：持久 IPython kernel，变量跨 turn 存活

**无状态（每次独立执行）：**
- SWE-Agent / mini-SWE-Agent：subprocess.run，每次独立
- Vercel just-bash：shell 状态每次重置，但文件系统共享
- Claude Code：**理论上持久 session，实际环境变量不持久，只有工作目录持久**

**证据：**
- CaveAgent（双流架构）：持久状态 vs 无状态 → +10.5pp 成功率，-28.4% token
- Claude Code 文档矛盾：API 文档称环境变量持久，实际实现不持久（Issue #2508, #13735, #20503）
- Manus/Devin 的 exec_dir 强制绝对路径是**结构化防错**——消除工作目录歧义

**结论：** 对于我们的场景，采用**无状态 + 显式工作目录参数**模型。原因：(1) 实现简单；(2) 消除隐式状态导致的 bug；(3) Manus/Devin 的实践验证了这个模式。文件系统状态天然在 Docker 容器内持久。

---

## 二、命令执行模型

### 2.1 各系统实现方式

| 系统 | 执行模型 | 优点 | 缺点 |
|------|---------|------|------|
| Claude Code | 持久 bash session（Popen） | 状态保持、支持复杂 shell 特性 | 环境变量持久性有争议、shell 崩溃需处理 |
| OpenHands | 持久 pexpect session in Docker | 支持交互、状态保持 | 复杂的输出解析、pexpect 超时管理 |
| Manus | 多 session 管理 | 并行命令、异步支持 | 5 个工具增加选择负担 |
| SWE-Agent | subprocess.run | 实现最简单、每次隔离 | 无状态、每次独立 |
| Vercel just-bash | 虚拟 bash（TypeScript 实现） | 完全沙箱、可控安全 | 不是真实 bash，能力受限 |

### 2.2 subprocess 模型 vs persistent shell

**subprocess.run（推荐）：**
```python
result = subprocess.run(
    ["bash", "-c", command],
    capture_output=True, text=True,
    timeout=timeout, cwd=working_dir
)
```
- 每次独立，无泄露状态
- 超时清晰（subprocess.TimeoutExpired）
- exit code / stdout / stderr 干净分离
- mini-SWE-Agent 用此模型拿到 SWE-bench 74%+

**persistent shell（Popen + stdin/stdout pipe）：**
```python
process = Popen(["/bin/bash"], stdin=PIPE, stdout=PIPE, stderr=PIPE)
process.stdin.write(command + "\n")
```
- 环境变量、别名、函数跨命令保持
- 需要复杂的输出完成检测（sentinel / marker）
- shell 崩溃需要重建

**证据：** Claude Code 官方文档推荐 persistent session 以支持 `cd /tmp` 后 `cat test.txt` 这样的跨命令状态。但实际 Claude Code 实现中环境变量不持久（Issue #2508）。SWE-Agent 的 subprocess.run 模型在 SWE-bench 上表现强劲。

**结论：** 采用 **subprocess.run 模型**，通过显式 `working_dir` 参数解决工作目录问题。原因：(1) 实现简单可靠；(2) 我们的 agent 不需要跨命令的环境变量——它的主要用例是独立的 curl/python 调用；(3) 文件系统天然持久（Docker 容器）；(4) 无需处理 shell 崩溃恢复。

### 2.3 环境变量传递

各系统的做法：

| 系统 | 环境变量策略 |
|------|------------|
| Claude Code | 理论持久，实际不持久；建议写入 ~/.bashrc |
| Manus | 每次通过 exec_dir 明确工作目录，环境由 shell profile 提供 |
| Devin | 通过 ~/.bashrc 预配置 |
| OpenHands | 持久 session 中自然保持 |
| SWE-Agent | 每次从 Docker 环境继承 |

**结论：** 从 Docker 容器环境继承基础环境变量（DATABASE_URL 等），通过 subprocess.run 的 `env` 参数传递。Agent 如需设置环境变量可以 `export VAR=val && command` 在单次调用内完成。

---

## 三、超时设计

### 3.1 各系统的超时值

| 系统 | 默认超时 | 可配置 | Agent 可指定 | 特殊机制 |
|------|---------|--------|-------------|---------|
| **Claude Code** | 120s（2 分钟） | 是（BASH_DEFAULT_TIMEOUT_MS，最大 600s） | 是（timeout 参数，毫秒） | run_in_background 支持后台执行 |
| **OpenHands** | 10s（soft timeout） | 是 | 是（timeout 参数，PR #8106） | soft timeout + hard timeout 双层 |
| **Manus** | 未明确文档化 | — | 是（shell_wait 的 seconds 参数） | 异步模型：执行后查看/等待 |
| **Devin** | 300s（5 分钟） | 否 | 否 | 建议不给超过 5 分钟的命令 |
| **SWE-Agent** | 未明确 | — | 否 | 每步独立执行 |
| **mini-SWE-Agent** | 未明确 | — | 否 | 简单 subprocess |

### 3.2 超时策略分析

**OpenHands 的双层超时设计值得参考：**
- Soft timeout（10s）：命令运行但没有新输出→返回"命令仍在运行"
- Hard timeout：Agent 可以通过 timeout 参数指定强制终止时间

**Claude Code 的超时问题：**
- 默认 2 分钟，但用户报告 "bash commands always timeout after 2 minutes despite successful completion"（Issue #3505）
- 说明超时值和实际命令耗时之间需要缓冲

**对于我们的网站侦察场景：**
- curl 请求：通常 5-15s
- Python 数据处理脚本：通常 1-10s
- 搜索 API 调用：通常 3-10s
- 大文件处理：可能 10-30s
- 安装包：可能 30-120s（但 Docker 中应预装）

**结论：** 默认超时 **30 秒**，Agent 可通过参数覆盖至最大 **120 秒**。理由：(1) 30s 覆盖 95%+ 的正常操作；(2) 120s 上限覆盖偶尔的大数据处理；(3) 不设置更长的超时——超过 2 分钟的命令通常意味着 Agent 做法有问题。

### 3.3 超时后的清理

```python
try:
    result = subprocess.run(..., timeout=timeout)
except subprocess.TimeoutExpired as e:
    # subprocess.run 自动 kill 子进程
    # 返回明确的超时错误信息
    return ToolResult(
        error=f"Command timed out after {timeout}s. "
              "Consider: (1) simplifying the command, "
              "(2) adding timeout parameter for longer operations, "
              "(3) redirecting output to file for large data."
    )
```

subprocess.run 在 TimeoutExpired 时自动终止子进程。无需额外清理。

---

## 四、输出处理

### 4.1 各系统的截断策略

| 系统 | 截断阈值 | 截断方式 | 截断提示 |
|------|---------|---------|---------|
| **Claude Code** | 30,000 字符（可配置 BASH_MAX_OUTPUT_LENGTH） | **中间截断**：保留头尾，删除中间 | "... Output truncated (N total lines) ..." |
| **OpenHands** | 30KB | **中间截断**：head + tail，中间丢弃 | 截断内容保存到文件，提供路径 |
| **mini-SWE-Agent** | 10,000 字符（head 5000 + tail 5000） | **头尾保留** | "[{elided} characters elided]" |
| **Manus** | 未明确 | **全量存文件 + 精简引用** | full/compact 双表示；旧结果自动压缩 |
| **Devin** | 未明确 | **截断并写入文件** | "Long shell outputs will be truncated and written to a file" |
| **SWE-Agent** | 搜索结果限制 50 条 | **上限截断** + 旧步骤折叠为单行 | 提示"has more results" |

### 4.2 截断策略深度分析

#### 头截断（只保留前 N 行）

- 优点：简单；对于 ls、grep 等命令足够
- 缺点：丢失尾部的总结信息（如 `pip install` 最后的 "Successfully installed..."）

#### 头尾保留（推荐——mini-SWE-Agent / Claude Code 模式）

- 优点：保留命令开始和结束的信息，通常最有价值
- 缺点：丢失中间详细信息
- mini-SWE-Agent 的 5000+5000=10000 字符阈值在 SWE-bench 74%+ 上验证

#### 全量存文件 + 精简引用（Manus / Devin / OpenHands 模式）

- 优点：信息无损，Agent 可以按需读取
- 缺点：需要额外一步读取，增加交互轮次
- Anthropic 官方推荐："content can be dropped from context as long as the URL/path is preserved"

**对比数据：**
- Claude Code 25,000 token 限制 → 约 100KB 文本
- OpenHands 30KB 截断 + 文件保存
- mini-SWE-Agent 10,000 字符 → ~2,500 token → SWE-bench 74%+

**关键洞察：** mini-SWE-Agent 用最激进的截断（10k 字符）拿到了极好的成绩，说明 **LLM 不需要看完整输出就能做出正确决策**。

### 4.3 stderr 的处理

| 系统 | stderr 处理 |
|------|-----------|
| Claude Code | stdout 和 stderr **不保留交错顺序**——分别缓冲再合并 |
| OpenHands | 2>&1 合并输出 |
| Vercel just-bash | **分离返回** stdout + stderr + exitCode |
| mini-SWE-Agent | 合并输出 |

**结论：** 合并 stdout + stderr（`2>&1`），然后整体截断。原因：(1) 保持交错顺序对 Agent 理解错误上下文重要；(2) 分离返回增加 JSON 复杂度但对 LLM 帮助有限——LLM 不会单独分析 stderr。

### 4.4 推荐截断方案

```
如果 output_length <= MAX_OUTPUT_CHARS（8000 字符，约 2000 token）:
    原样返回

否则:
    保留前 4000 字符
    保留后 4000 字符
    中间插入:
    "\n\n[... 中间 {N} 字符已省略，完整输出已保存到 {filepath} ...]\n\n"
```

**为什么 8000 字符？**
- 约 2000 token，在 Agent 的 context 中是合理的占比
- mini-SWE-Agent 的 10,000 字符阈值在复杂代码任务中验证过
- 对于网站侦察场景，大部分输出（curl 响应、Python 脚本输出）在这个范围内
- 超过这个阈值的输出（如大型 JSON API 响应）应该鼓励 Agent 直接存文件

---

## 五、安全边界

### 5.1 各系统的安全模型

| 系统 | 沙箱 | 命令限制 | 网络限制 |
|------|------|---------|---------|
| **Claude Code** | 依赖用户环境（推荐 Docker） | 建议 allowlist，但默认无限制 | 无 |
| **OpenHands** | Docker 容器 | 无命令级限制，有 security_risk 参数做标记 | 容器网络 |
| **Manus** | 云 VM | "avoid commands requiring confirmation; use -y/-f" | 有互联网访问 |
| **Devin** | 云机器（Ubuntu 22.04） | "never use shell for file create/view/edit" | 有互联网访问 |
| **SWE-Agent** | Docker 容器 | 自定义 ACI 命令层 | 容器网络 |

### 5.2 我们的安全决策

**核心原则：Docker 容器是安全边界。容器内 Agent 有完整权限。**（Claude.md §七硬约束）

**不做命令级过滤。** 理由：
1. 我们不是 Claude Code 那样运行在用户机器上——我们在 Docker 容器内
2. 黑名单方式（禁 rm -rf, shutdown 等）永远不完整——Agent 可以 `python3 -c "import os; os.system('rm -rf /')"` 绕过
3. RedCode 研究（NeurIPS 2024）表明：CodeAct agent 比 ReAct agent 更不安全，因为代码能力让限制更容易绕过
4. 白名单方式会严重限制 Agent 能力，违反"Agent 自主性"原则
5. Manus、Devin、OpenHands 都不做命令级限制

**做什么：**
- Docker 容器资源限制（CPU、内存、磁盘）
- 容器网络隔离（只允许访问目标站点 + LLM API + DB）
- 不挂载宿主机敏感目录
- 命令审计日志（trace.jsonl 记录每个命令）

### 5.3 参数验证（防格式错误，不是防恶意）

```python
def validate_bash_input(command: str, timeout: int | None, working_dir: str | None):
    if not command or not command.strip():
        return error("Command cannot be empty")
    if timeout is not None and (timeout < 1 or timeout > 120):
        return error("Timeout must be 1-120 seconds")
    if working_dir is not None and not os.path.isabs(working_dir):
        return error("working_dir must be an absolute path")
```

---

## 六、预装环境

### 6.1 各系统预装情况

| 系统 | OS | Python | 其他运行时 | 关键库/工具 |
|------|-----|--------|-----------|-----------|
| **Manus** | Ubuntu 22.04 | 3.10.12 | Node.js 20.18.0 | sudo 权限 |
| **Devin** | Ubuntu 22.04 | 有 | Node.js | sudo 权限 |
| **OpenHands** | Debian-based Docker | 3.x | — | pip |
| **SWE-Agent** | Docker | 3.x | — | 最小化 |

### 6.2 网站侦察场景的预装需求

#### 必须预装的 Python 库

| 库 | 用途 | 理由 |
|----|------|------|
| **httpx** | HTTP 客户端 | 最常用——调用已发现的 API、下载页面 |
| **beautifulsoup4** | HTML 解析 | 解析 curl 下载的 HTML |
| **lxml** | XML/HTML 解析器 | BS4 的快速后端 |
| **duckduckgo-search** | 搜索 | 站点搜索 |
| **json**（标准库） | JSON 处理 | API 响应解析 |
| **csv**（标准库） | CSV 处理 | 数据导出 |
| **re**（标准库） | 正则表达式 | 文本模式匹配 |

#### 推荐预装的系统工具

| 工具 | 用途 | 理由 |
|------|------|------|
| **curl** | HTTP 请求 | LLM 最熟悉的 HTTP 工具；shell one-liner 最方便 |
| **jq** | JSON 处理 | 管道式 JSON 过滤/转换；与 curl 配合极佳 |
| **wget** | 文件下载 | 递归下载、续传 |
| **grep/sed/awk** | 文本处理 | 基础 Unix 工具 |
| **head/tail/wc/sort/uniq** | 数据分析 | 管道链工具 |
| **python3** | 脚本执行 | 复杂数据处理 |
| **file** | 文件类型检测 | 判断下载内容类型 |

#### 不需要预装的

| 不预装 | 理由 |
|--------|------|
| Node.js | 浏览器内 JS 执行由 extract 工具提供 |
| 编译器（gcc, go） | 不是开发场景 |
| 数据库客户端（psql） | Agent 不直接操作 World Model DB |
| Playwright | 浏览器由 browse/interact 工具管理 |

### 6.3 预装策略

在 Dockerfile 中：

```dockerfile
RUN apt-get update && apt-get install -y \
    curl wget jq \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    httpx beautifulsoup4 lxml \
    duckduckgo-search
```

**关键原则：** Agent 不应该需要在 session 中安装包。所有常用工具和库应该预装。如果 Agent 频繁执行 `pip install`，说明预装清单不完整。

---

## 七、参数设计

### 7.1 各系统参数对比

| 系统 | command | timeout | working_dir | 其他参数 |
|------|---------|---------|-------------|---------|
| **Claude Code** | string（必填） | int ms（可选，max 600000） | 无（持久 session） | description(string), run_in_background(bool), restart(bool) |
| **OpenHands** | string（必填） | int（可选） | 无（持久 session） | is_input(bool), reset(bool), security_risk(string) |
| **Manus** | string（必填） | 无 | exec_dir string（**必填，绝对路径**） | id string（必填，session 标识） |
| **Devin** | 无显式参数（命令嵌入 XML） | 无 | exec_dir string（**必填，绝对路径**） | id string（可选，默认 "default"） |
| **SWE-Agent** | 纯文本命令 | 无 | 无 | 无 |
| **Vercel just-bash** | string（必填） | 无（AbortSignal） | cwd string（可选） | env, stdin, args, rawScript, replaceEnv, signal |

### 7.2 参数分析

#### command（必填）— 唯一必填参数

所有系统一致：command 是 string 类型。没有系统将命令拆分为 executable + args 的模式——都是完整的 shell 命令字符串。

**理由：** LLM 最擅长生成完整的 shell 命令字符串（训练数据中大量存在），强制拆分会导致更多格式错误。

#### timeout（可选）— Agent 可控

**应该加。** 证据：
- Claude Code：timeout 参数，毫秒级，最大 600s
- OpenHands：PR #8106 专门为 bash 加了 timeout 参数
- 场景需要：curl 请求可能 3s，Python 数据分析脚本可能 60s

**设计：** 整数秒，默认 30，范围 1-120。

#### working_dir（可选）— 推荐加入

**Manus 和 Devin 都用 exec_dir 且设为必填。** 这是最强的结构化防错信号。

Anthropic ACI 原则："Change the arguments so that it is harder to make mistakes"。Claude Code 的经验：强制绝对路径后 "the model used this method flawlessly"。

**但设为可选更好，默认 /workspace。** 理由：
- 大部分命令不关心工作目录（curl、python3 -c）
- 必填会增加每次调用的 token 消耗
- 默认值已足够安全

#### description（不加）

Claude Code 有 description 参数用于人类审核。我们的场景是全自动执行，无人审核，不需要。

#### run_in_background（不加）

Claude Code 有此参数。我们的场景不需要后台执行——Agent 的命令应该在超时内完成。如果需要后台进程（如启动服务器），那是 browse/interact 的场景，不是 bash 的场景。

Claude Code 自己的 run_in_background 也有严重 bug（Issue #11716：无限 system-reminders + token 耗尽）。

#### id / session 管理（不加）

Manus/Devin 的 shell session 管理对应的是异步命令执行模型。我们用同步模型，不需要。

### 7.3 推荐参数 schema

```json
{
  "name": "bash",
  "description": "Execute a shell command in the Docker container. ...(详见§十一)",
  "parameters": {
    "type": "object",
    "properties": {
      "command": {
        "type": "string",
        "description": "The bash command to execute. Supports pipes, redirects, && chaining."
      },
      "timeout": {
        "type": "integer",
        "description": "Timeout in seconds. Default 30, max 120. Use higher values for data processing scripts.",
        "default": 30,
        "minimum": 1,
        "maximum": 120
      },
      "working_dir": {
        "type": "string",
        "description": "Working directory (absolute path). Default: /workspace. Use /workspace/artifacts/samples for data files.",
        "default": "/workspace"
      }
    },
    "required": ["command"]
  }
}
```

**3 个参数，1 个必填。** 这是各系统的最大公约数：Claude Code 的 command + timeout，Manus/Devin 的 exec_dir，去掉不需要的 description/background/id/restart。

---

## 八、返回值设计

### 8.1 各系统返回值

| 系统 | 返回格式 | 包含 exit code | 包含 stderr | 截断信息 |
|------|---------|---------------|------------|---------|
| **Claude Code** | 纯文本（stdout+stderr 合并） | 隐式（错误时 is_error=true） | 合并在输出中 | 截断标记 |
| **OpenHands** | Observation 对象 | exit code -1 表示仍在运行 | 合并 | 截断后保存到文件 |
| **Manus** | 事件流 + full/compact 双表示 | 通过 shell_view 查看 | 合并 | full 存文件，compact 引用 |
| **Vercel just-bash** | `{stdout, stderr, exitCode}` | 显式数字 | **分离** | 无 |
| **SWE-Agent** | 纯文本 | 隐式 | 合并 | 旧步骤折叠为单行 |

### 8.2 返回值设计分析

**显式 exit_code vs 隐式：**
- Vercel just-bash 显式返回 exitCode → Agent 可以判断命令是否成功
- Claude Code 用 is_error 标记 → 更简单但信息少
- **推荐显式返回**——exit code 是 bash 语义的核心部分

**合并 vs 分离 stdout/stderr：**
- 实际观察：Claude Code 的 stderr/stdout 不保留交错顺序（Issue #2734）
- Anthropic 建议返回"高信号内容"，避免底层技术细节
- **推荐合并**——LLM 不需要区分 stdout 和 stderr，合并后更紧凑

**执行时间信息：**
- 各系统都不返回执行时间
- **不返回**——Agent 不需要知道命令耗时

### 8.3 推荐返回格式

**成功（exit code 0）：**
```
[exit code: 0]
{output}
```

**成功但输出被截断：**
```
[exit code: 0]
{first 4000 chars}

[... 省略 {N} 字符，完整输出已保存至 /workspace/artifacts/workspace/bash_output_{step}.txt ...]

{last 4000 chars}
```

**失败（exit code != 0）：**
```
[exit code: {code}]
{output}
```

**超时：**
```
[error: timeout after {N}s]
Partial output (if any):
{partial_output}

Hint: Try a shorter command, or increase timeout (max 120s).
```

**命令为空或格式错误：**
```
[error: invalid command]
{error_description}
```

**设计原则：**
- exit code 始终在第一行——Agent 第一眼看到成功/失败
- 输出原样保留（不做格式转换）
- 截断时给出明确的文件路径
- 错误时给出可操作的建议（Anthropic 最佳实践："clear, actionable error messages"）

---

## 九、与其他工具的协作

### 9.1 bash vs extract 的选择边界

| 场景 | 用 bash | 用 extract | 理由 |
|------|---------|-----------|------|
| 调用已发现的 API | `bash("curl https://api.example.com/data")` | - | 不需要浏览器 session |
| 提取嵌入 JSON (__NEXT_DATA__) | - | `extract("JSON.stringify(JSON.parse(document.getElementById('__NEXT_DATA__').textContent))")` | 需要浏览器 context |
| 处理已下载的 JSON | `bash("python3 -c 'import json; ...'")` | - | 文件操作不需要浏览器 |
| 执行 DOM 提取 | - | `extract("document.querySelectorAll('.item').map(...)"))` | 需要 DOM 访问 |
| 检查 cookie | - | `extract("document.cookie")` | 需要浏览器 session |
| 下载文件 | `bash("curl -o file.json url")` | - | 纯 HTTP 操作 |
| 重放捕获的 API（带 cookie） | 取决于是否需要 session cookie | — | 如果 API 需要认证 cookie，用 extract 更可靠 |

**核心原则：** 需要浏览器状态（DOM、cookie、session）的操作用 extract，纯服务端操作用 bash。

### 9.2 bash vs browse 的选择边界

- bash curl 返回原始 HTML/JSON——没有 SPA 渲染、没有 JS 执行、没有元素索引
- browse 返回渲染后的页面摘要——有交互元素、有网络捕获、有嵌入数据检测
- **对于 API 端点和静态资源**：bash curl 更高效（不启动浏览器）
- **对于需要 JS 渲染的页面**：必须用 browse

### 9.3 tool description 中的引导

在 bash 的 tool description 中明确：

```
Use bash for:
- HTTP requests to discovered APIs: curl, python3 httpx
- Data processing: python3 scripts, jq, text processing
- File operations: read/write/organize files in artifacts/
- Search: python3 duckduckgo-search
- Any computation that doesn't need the browser

Do NOT use bash for:
- Viewing rendered web pages (use browse)
- Extracting data from the current browser page (use extract)
- Interacting with page elements (use interact)
```

在 system prompt 的工具使用原则中（§九.3 已有共识）：
```
提取优先级：嵌入 JSON > 捕获的 API > 直接 API 调用 > DOM 提取
- 嵌入 JSON / DOM：用 extract（浏览器 context）
- API 调用：用 bash（curl / python3 httpx）
- 选择依据：是否需要浏览器状态
```

---

## 十、LLM 写 bash 的常见问题

### 10.1 Claude Code Issue #19649 的教训

Claude Code 发现 67-100% 的 bash 使用是"过度使用"——有更好的专用工具可用：

| 模式 | 占比 | 问题 |
|------|------|------|
| `cat > file << EOF` 写文件 | 100% 错误 | 每次 heredoc 内容不同导致权限缓存失效 |
| `grep -r pattern` 搜索 | 67% 错误 | 有专用 Grep 工具更高效 |
| `sed -n 147,162p file` 读取 | 89% 错误 | 有专用 Read 工具 |

**对我们的影响：**
- 我们没有 Read/Write/Grep 等专用文件操作工具——bash 就是唯一的文件操作工具
- 所以这个问题**不适用于我们**——Agent 用 bash 做文件操作是**正确的**
- 但要注意：如果未来加了专用工具，需要在 tool description 中引导优先使用

### 10.2 LLM 写 shell 命令的常见错误

| 错误类型 | 示例 | 防护 |
|---------|------|------|
| **引号嵌套** | `python3 -c "print("hello")"` | 无法在工具层防护；依赖模型能力 |
| **Unicode 转义** | LLM 输出 `\u003e` 而非 `>` | JSON 解析后传给 shell |
| **多行命令格式** | 命令中间有换行导致解析错误 | 整个 command 作为 `bash -c` 的参数 |
| **相对路径** | `cat ../data/file.json`（不知道当前目录在哪） | working_dir 参数 + 默认 /workspace |
| **交互命令** | `vim`, `less`, `python`（进入 REPL） | tool description 明确禁止 |
| **超大输出** | `cat huge_file.json`（整个文件进 context） | 截断 + 提示 |
| **安装超时** | `pip install tensorflow`（编译很慢） | 预装 + timeout 参数 |

### 10.3 Python one-liner vs 写脚本文件

**各系统的态度：**
- Manus shell rules："Must save code to files before execution; direct code input to interpreter commands is forbidden"
- Claude Code：允许 `python3 -c "..."` one-liner
- SWE-Agent：任何形式都可以

**对于我们的场景：**

| 方式 | 适用 | 不适用 |
|------|------|--------|
| `python3 -c "..."` | 简单操作：JSON 解析、字符串处理、单步计算 | 多步逻辑、需要 try/except |
| 写脚本 + 执行 | 复杂逻辑：数据处理管道、多步 API 调用 | 一行搞定的简单操作 |
| `curl \| jq` 管道 | JSON API 快速查看 | 复杂数据转换 |

**在 tool description 中建议但不强制：**
```
For complex scripts (>3 lines), write to a file first:
  bash("cat > /workspace/artifacts/workspace/process.py << 'PYEOF'\nimport json\n...\nPYEOF && python3 /workspace/artifacts/workspace/process.py")
```

### 10.4 防错设计总结

| 防错措施 | 来源 | 适用 |
|---------|------|------|
| 强制绝对路径 | Claude Code ACI | working_dir 参数 |
| 禁止交互命令 | Devin, 所有系统 | tool description |
| 输出截断 | 所有系统 | 实现层 |
| 超时保护 | 所有系统 | timeout 参数 + 默认值 |
| 预装依赖 | 减少 pip install | Dockerfile |
| 命令为空检查 | 基本防错 | 参数验证 |
| 可操作的错误消息 | Anthropic 最佳实践 | 返回值设计 |

---

## 十一、完整设计方案

### 11.1 参数 Schema

```json
{
  "name": "bash",
  "description": "Execute a shell command in the Docker container. Use for HTTP requests (curl, httpx), data processing (python3, jq), file operations, search, and any computation that doesn't need the browser.\n\nCapabilities:\n- HTTP: curl URL, python3 -c 'import httpx; ...'\n- Data processing: python3 scripts, jq '.field' file.json, sort/uniq/wc\n- File operations: ls, cat, head/tail, cp, mv, mkdir\n- Search: python3 -c 'from duckduckgo_search import DDGS; ...'\n\nDo NOT use for:\n- Viewing rendered web pages (use browse)\n- Extracting from current browser page (use extract)\n- Interacting with page elements (use interact)\n- Recording discoveries (use note_insight)\n\nTips:\n- For API responses, pipe through jq: curl -s URL | jq '.data[:3]'\n- For complex scripts (>3 lines), write to file first, then execute\n- Large outputs are auto-truncated; save to file if you need the full data\n- Avoid interactive commands (vim, less, python REPL)\n- Pre-installed: curl, jq, wget, python3 (httpx, beautifulsoup4, lxml, duckduckgo-search)",
  "parameters": {
    "type": "object",
    "properties": {
      "command": {
        "type": "string",
        "description": "The bash command to execute. Supports pipes, redirects, && chaining."
      },
      "timeout": {
        "type": "integer",
        "description": "Timeout in seconds (1-120). Default: 30. Increase for data processing or large downloads."
      },
      "working_dir": {
        "type": "string",
        "description": "Working directory, must be absolute path. Default: /workspace"
      }
    },
    "required": ["command"]
  }
}
```

### 11.2 返回值 Schema

```python
@dataclass
class BashResult:
    """bash 工具的返回值，序列化为字符串呈现给 LLM。"""
    exit_code: int          # 命令退出码
    output: str             # stdout + stderr 合并输出
    truncated: bool         # 是否被截断
    output_file: str | None # 截断时完整输出保存路径
    timed_out: bool         # 是否超时
```

**呈现给 LLM 的格式（纯文本字符串）：**

```python
def format_for_llm(result: BashResult) -> str:
    if result.timed_out:
        lines = [f"[error: timeout after {timeout}s]"]
        if result.output:
            lines.append(f"Partial output:\n{result.output[:2000]}")
        lines.append("Hint: Simplify the command or increase timeout (max 120s).")
        return "\n".join(lines)

    header = f"[exit code: {result.exit_code}]"

    if not result.truncated:
        return f"{header}\n{result.output}"

    return (
        f"{header}\n"
        f"{result.output[:HEAD_CHARS]}\n\n"
        f"[... {result.omitted_chars} characters omitted. "
        f"Full output saved to {result.output_file} ...]\n\n"
        f"{result.output[-TAIL_CHARS:]}"
    )
```

### 11.3 实现要点

```python
import subprocess
import os
import time

# 常量
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
MAX_OUTPUT_CHARS = 8000
HEAD_CHARS = 4000
TAIL_CHARS = 4000
ARTIFACTS_DIR = os.environ.get("ARTIFACTS_DIR", "/workspace/artifacts")
WORKSPACE_DIR = os.path.join(ARTIFACTS_DIR, "workspace")

async def execute_bash(
    command: str,
    timeout: int = DEFAULT_TIMEOUT,
    working_dir: str = "/workspace",
    step_number: int = 0,  # 由 session 框架提供
) -> BashResult:
    """执行 bash 命令并返回结构化结果。"""

    # 1. 参数验证
    if not command or not command.strip():
        return BashResult(exit_code=1, output="Error: empty command", ...)

    timeout = max(1, min(timeout or DEFAULT_TIMEOUT, MAX_TIMEOUT))

    if working_dir and not os.path.isabs(working_dir):
        return BashResult(exit_code=1, output="Error: working_dir must be absolute path", ...)

    # 确保工作目录存在
    os.makedirs(working_dir, exist_ok=True)

    # 2. 执行命令
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=working_dir,
            env={**os.environ},  # 继承容器环境
        )

        # 3. 合并 stdout + stderr
        output = result.stdout
        if result.stderr:
            output = output + result.stderr if output else result.stderr

        # 4. 截断处理
        truncated = False
        output_file = None

        if len(output) > MAX_OUTPUT_CHARS:
            # 保存完整输出到文件
            os.makedirs(WORKSPACE_DIR, exist_ok=True)
            output_file = os.path.join(
                WORKSPACE_DIR, f"bash_output_step{step_number}.txt"
            )
            with open(output_file, "w") as f:
                f.write(output)
            truncated = True

        return BashResult(
            exit_code=result.returncode,
            output=output,
            truncated=truncated,
            output_file=output_file,
            timed_out=False,
        )

    except subprocess.TimeoutExpired as e:
        partial = ""
        if e.stdout:
            partial = e.stdout if isinstance(e.stdout, str) else e.stdout.decode(errors="replace")
        if e.stderr:
            stderr = e.stderr if isinstance(e.stderr, str) else e.stderr.decode(errors="replace")
            partial = partial + stderr if partial else stderr

        return BashResult(
            exit_code=-1,
            output=partial[:2000] if partial else "",
            truncated=False,
            output_file=None,
            timed_out=True,
        )

    except Exception as e:
        return BashResult(
            exit_code=-1,
            output=f"Execution error: {str(e)}",
            truncated=False,
            output_file=None,
            timed_out=False,
        )
```

### 11.4 Tool Description 设计原则

根据调研，tool description 是"最高杠杆的优化点"（Anthropic 工程文档 + Gorilla NeurIPS 2024）。遵循原则：

1. **当入职手册写**——Anthropic："treat tool descriptions as onboarding docs for a new employee"
2. **正面示例 + 反面示例**——"Use for ... / Do NOT use for ..."
3. **具体场景映射**——"For API responses, pipe through jq"
4. **预装信息**——让 Agent 知道有什么可用，避免不必要的 pip install
5. **限制说明**——"Avoid interactive commands"
6. **与其他工具的边界**——明确什么时候用 browse/extract 而不是 bash

### 11.5 设计决策总结

| 决策 | 选择 | 主要证据 |
|------|------|---------|
| 工具数量 | 1 个（bash） | Vercel d0 实验，mini-SWE-Agent，Manus 单 shell_exec |
| 执行模型 | subprocess.run（无状态） | mini-SWE-Agent 74%+ SWE-bench |
| 工作目录 | 可选参数，默认 /workspace | Manus/Devin exec_dir 实践 + ACI 防错 |
| 超时 | 默认 30s，Agent 可选 1-120s | Claude Code 经验 + 场景分析 |
| 输出截断 | 8000 字符，head 4000 + tail 4000 | mini-SWE-Agent 10k 阈值 + Claude Code 30k 上限 |
| 截断策略 | 完整输出存文件 + 精简引用 | OpenHands / Manus / Anthropic 推荐 |
| stdout/stderr | 合并返回 | 多数系统共识，LLM 不需要区分 |
| exit code | 显式在首行 | Vercel just-bash 实践 |
| 安全模型 | Docker 容器边界，无命令过滤 | 项目硬约束 + 各系统共识 |
| 参数验证 | 防格式错误，不防恶意 | 项目安全模型 |
| 预装环境 | curl, jq, wget, python3 + httpx/bs4/lxml | 网站侦察场景需求 |
| 环境变量 | 继承容器环境 | SWE-Agent 模式 |

---

## 参考来源

### 官方文档
- [Bash tool - Claude API Docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/bash-tool)
- [Anthropic: Writing Tools for Agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [Anthropic: Effective Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [OpenHands Runtime Architecture](https://docs.openhands.dev/openhands/usage/architecture/runtime)
- [OpenHands Tool System & MCP](https://docs.openhands.dev/sdk/arch/tool-system)
- [SWE-agent ACI Documentation](https://swe-agent.com/0.7/background/aci/)

### GitHub Issues & PRs
- [Claude Code #19649: Bash tool overuse (67-100%)](https://github.com/anthropics/claude-code/issues/19649)
- [Claude Code #3505: Bash commands always timeout](https://github.com/anthropics/claude-code/issues/3505)
- [Claude Code #12054: Massive tool outputs without truncation](https://github.com/anthropics/claude-code/issues/12054)
- [Claude Code #2508: Environment variables don't persist](https://github.com/anthropics/claude-code/issues/2508)
- [Claude Code #11716: Background processes cause infinite system-reminders](https://github.com/anthropics/claude-code/issues/11716)
- [Claude Code #2734: Stderr/stdout interleaving broken](https://github.com/anthropics/claude-code/issues/2734)
- [OpenHands #8106: Add timeout parameter to bash tool](https://github.com/All-Hands-AI/OpenHands/pull/8106)
- [OpenHands #12353: Context offloading for large tool outputs](https://github.com/OpenHands/OpenHands/issues/12353)
- [OpenHands #7422: Drop soft bash timeout to 10 seconds](https://github.com/All-Hands-AI/OpenHands/issues/7422)

### 泄露 Prompt 分析
- [Manus System Prompt (2025-03-10)](https://github.com/jujumilk3/leaked-system-prompts/blob/main/manus_20250310.md)
- [Manus tools.json](https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools/blob/main/Manus%20Agent%20Tools%20&%20Prompt/tools.json)
- [Devin System Prompt (2025-08-09)](https://github.com/EliFuzz/awesome-system-prompts/blob/main/leaks/devin/archived/2025-08-09_prompt_system.md)
- [Claude Code System Prompts Repository](https://github.com/Piebald-AI/claude-code-system-prompts)
- [Context Engineering in Manus](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)

### 学术论文
- CodeAct (ICML 2024): Executable Code Actions Elicit Better LLM Agents (arXiv:2402.01030)
- OpenHands (ICLR 2025): An Open Platform for AI Software Developers as Generalist Agents
- SWE-agent (NeurIPS 2024): Agent-Computer Interfaces Enable Automated Software Engineering
- RedCode (NeurIPS 2024): Risky Code Execution and Generation Benchmark for Code Agents
- D2Snap (arXiv:2508.04412): DOM Downsampling for LLM-Based Web Agents
- Beyond Browsing (ACL 2025, arXiv:2410.16464): API-based web agents
- CaveAgent (arXiv:2601.01569): 双流架构，持久运行时状态 +10.5pp

### 工具与源码
- [mini-SWE-Agent](https://github.com/SWE-agent/mini-swe-agent) — ~100 行，只有 bash，SWE-bench 74%+
- [Vercel just-bash](https://github.com/vercel-labs/just-bash) — TypeScript 虚拟 bash 环境
- [SWE-ReX](https://github.com/SWE-agent/SWE-ReX) — 沙箱代码执行

---

*本文档是 bash 工具设计的调研依据。具体实现以讨论后的共识为准。*
