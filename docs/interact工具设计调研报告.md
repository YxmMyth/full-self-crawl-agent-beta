# interact 工具深度设计调研报告

> 日期：2026-03-29
> 用途：为 interact 工具的实现提供完整设计方案
> 调研基础：Browser-Use, Agent-E, Stagehand, Playwright MCP, Vercel agent-browser, Skyvern, Manus, Anthropic Computer Use, OpenAI CUA, BrowserAgent (TMLR 2025), AgentOccam, D2Snap, 工具层调研报告.md, browse 工具深度设计报告.md
> 前置：工具层调研报告.md, browse 工具深度设计报告.md, AgentSession 设计.md

---

## 一、Action 集设计（最关键）

### 1.1 各系统 Action 集全景对比

#### Browser-Use（~20 个 action）

| Action | 参数 | 说明 |
|--------|------|------|
| go_to_url | url: str | 导航到 URL |
| click_element | index: int | 通过索引点击元素 |
| input_text | index: int, text: str | 输入文本到元素 |
| scroll_down | amount: int (px) | 向下滚动 |
| scroll_up | amount: int (px) | 向上滚动 |
| send_keys | keys: str | 发送按键（Enter, Escape 等） |
| get_dropdown_options | index: int | 获取下拉选项 |
| select_dropdown_option | index: int, option: str | 选择下拉选项 |
| go_back | - | 浏览器后退 |
| search_google | query: str | Google 搜索 |
| wait | seconds: int | 等待 |
| screenshot | - | 截图 |
| extract_content | - | LLM 提取内容 |
| open_tab | url: str | 新标签页 |
| switch_tab | tab_index: int | 切换标签页 |
| close_tab | - | 关闭标签页 |
| upload_file | index: int, path: str | 上传文件 |
| find_text | text: str | 滚动到包含指定文本的位置 |
| evaluate | js: str | 执行 JavaScript |
| done | text: str, success: bool | 任务完成 |

特点：每种 action 是独立的 tool（通过 `@controller.action()` 装饰器注册），结构化输出而非 tool calling。支持批量 action（单轮最多 5 个，页面变化后截断后续）。元素通过编号索引定位。

来源：Browser-Use 源码 + GitHub 讨论 + 官方文档。

#### Agent-E（5 个原始 skill）

| Skill | 说明 |
|-------|------|
| click | 给定 DOM query selector，点击元素 |
| enter_text | 文本输入 |
| open_url | 导航到 URL |
| press_keys | 按键组合 |
| get_dom_with_content_type | 感知技能：获取 DOM（text_only / input_fields / all_fields） |

特点：**极简 action 集**。论文明确指出 "we did not support drag, double click, right click, tab management, etc."。每个 skill 返回**语言化反馈**而非简单布尔值。核心创新是 change observation——每个 action 不仅执行操作，还通过 MutationObserver 观察并报告状态变化。元素通过注入的 mmid 属性定位。

来源：arXiv:2407.13032 Section 3.3 + GitHub 仓库。

#### Stagehand（3 个语义 API）

| API | 参数 | 说明 |
|-----|------|------|
| act(instruction) | instruction: str, variables?: Record, timeout?: number | 自然语言指令执行交互 |
| observe(instruction) | instruction: str | 预览 act 将操作的元素 |
| extract(instruction, schema) | instruction: str, schema?: ZodSchema | 结构化数据提取 |

底层 Action 接口（act 可接受确定性 Action）：
```typescript
interface Action {
  selector: string;      // XPath 或 CSS selector
  description: string;   // 用于自愈
  method: string;        // "click", "fill", "type" 等
  arguments: string[];   // 方法参数
}
```

特点：**最高层级抽象**。Agent 不需要知道具体 action 类型——用自然语言描述意图。每次交互需要 LLM 推理（慢但灵活）。v3 从 Playwright 切到 CDP 直连，快 44%。支持 iframe 和 shadow DOM 自动穿透。

来源：docs.stagehand.dev + browserbase.com/blog/stagehand-v3。

#### Playwright MCP（~20 个工具）

**交互类工具：**

| 工具 | 参数 | 说明 |
|------|------|------|
| browser_click | ref: str, element?: str, doubleClick?: bool, button?: str, modifiers?: array | 点击 |
| browser_type | ref: str, text: str, submit?: bool, slowly?: bool | 输入文本 |
| browser_hover | ref: str | 悬停 |
| browser_select_option | ref: str, values: array | 选择下拉选项 |
| browser_drag | startRef, endRef | 拖放 |
| browser_fill_form | fields: array | 批量填写表单 |
| browser_file_upload | paths: array | 文件上传 |
| browser_press_key | key: str | 按键 |
| browser_navigate | url: str | 导航 |
| browser_navigate_back | - | 后退 |
| browser_wait_for | time/text/textGone | 等待 |
| browser_handle_dialog | accept: bool, promptText?: str | 处理对话框 |
| browser_evaluate | function: str | 执行 JS |
| browser_snapshot | - | 获取 AX tree 快照 |

**额外能力（按 caps 启用）：**
- Vision 模式：`browser_mouse_click_xy`, `browser_mouse_move_xy`, `browser_mouse_drag_xy`, `browser_mouse_wheel`（坐标点击）
- 标签页：`browser_tabs`（list/create/close/select）
- 网络：`browser_route`（mock 请求）
- 存储：cookie / localStorage / sessionStorage CRUD

特点：**每个 action 是独立 MCP 工具**。元素通过 AX tree ref 定位（`[ref=eN]`）。分 capabilities 组，按需启用。注意 2026 年更新：Playwright CLI 比 MCP 省 4 倍 token。

来源：github.com/microsoft/playwright-mcp + README.md。

#### Manus（10 个浏览器工具）

| 工具 | 参数 | 说明 |
|------|------|------|
| browser_view | - | 查看当前页面状态 |
| browser_navigate | url: str | 导航 |
| browser_restart | url: str | 重启浏览器并导航 |
| browser_click | index?: int, coordinate_x?: float, coordinate_y?: float | 点击（索引或坐标） |
| browser_input | index/coordinate, text: str, press_enter: bool | 输入文本（覆盖式） |
| browser_move_mouse | coordinate_x, coordinate_y | 移动鼠标 |
| browser_press_key | key: str | 按键（支持 "Control+Enter" 组合） |
| browser_select_option | index: int, option: int | 选择下拉选项（按选项编号） |
| browser_scroll_up | to_top?: bool | 向上滚动（可直接到顶） |
| browser_scroll_down | to_bottom?: bool | 向下滚动（可直接到底） |

特点：**每个 action 是独立工具**，前缀 `browser_` 分类。双定位模式：索引编号 + 坐标坐标（fallback）。`browser_input` 是覆盖语义（clear + type），避免追加文本问题。`browser_view` 作为独立感知工具而非交互附带。内部使用 browser-use 开源库。

来源：泄露系统 prompt (github.com/jujumilk3/leaked-system-prompts) + Manus 技术分析 (gist by renschni)。

#### Skyvern（22 个 action type）

| Action Type | 类 | 关键参数 |
|-------------|-----|---------|
| CLICK | ClickAction | element_id, x, y, button, repeat |
| INPUT_TEXT | InputTextAction | element_id, text |
| UPLOAD_FILE | UploadFileAction | element_id, file_url |
| DOWNLOAD_FILE | DownloadFileAction | file_name, download_url |
| SELECT_OPTION | SelectOptionAction | element_id, option |
| CHECKBOX | CheckboxAction | element_id, is_checked |
| HOVER | HoverAction | element_id, hold_seconds |
| SCROLL | ScrollAction | x, y, scroll_x, scroll_y |
| KEYPRESS | KeypressAction | keys, hold, duration |
| MOVE | MoveAction | x, y |
| DRAG | DragAction | start_x, start_y, path |
| WAIT | WaitAction | seconds |
| GOTO_URL | GotoUrlAction | url |
| RELOAD_PAGE | ReloadPageAction | - |
| CLOSE_PAGE | ClosePageAction | - |
| EXTRACT | ExtractAction | data_extraction_goal |
| SOLVE_CAPTCHA | SolveCaptchaAction | captcha_type |
| NULL_ACTION | NullAction | - |
| TERMINATE | TerminateAction | errors |
| COMPLETE | CompleteAction | verified |
| VERIFICATION_CODE | VerificationCodeAction | code |
| LEFT_MOUSE | LeftMouseAction | direction, x, y |

特点：**最大 action 集**（22 种）。视觉优先（截图 + Vision LLM），Planner-Actor-Validator 三阶段。很多 action 面向表单填写场景（CHECKBOX, VERIFICATION_CODE, SOLVE_CAPTCHA）。

来源：Skyvern 源码 skyvern/webeye/actions/actions.py。

#### Anthropic Computer Use（~15 个 action）

| Action | 参数 | 说明 |
|--------|------|------|
| screenshot | - | 截屏 |
| left_click | coordinate: [x,y], text?: modifier | 左键点击 |
| right_click | coordinate: [x,y] | 右键 |
| middle_click | coordinate: [x,y] | 中键 |
| double_click | coordinate: [x,y] | 双击 |
| triple_click | coordinate: [x,y] | 三击 |
| type | text: str | 输入文本 |
| key | key: str | 按键/组合键 |
| mouse_move | coordinate: [x,y] | 移动鼠标 |
| scroll | coordinate, direction, amount | 四向滚动（up/down/left/right） |
| left_click_drag | start, end | 拖拽 |
| left_mouse_down | coordinate | 按下 |
| left_mouse_up | coordinate | 释放 |
| hold_key | key, duration | 持续按键 |
| wait | - | 暂停 |
| zoom | region: [x1,y1,x2,y2] | 放大查看区域（4.6+） |

特点：**像素级操作原语**。Schema 内嵌模型权重，不可定制。所有交互基于屏幕坐标而非 DOM 元素。视觉-动作循环（截图→推理→动作→截图）。GUI 定位准确率仅 18.9%。

来源：platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool。

#### OpenAI CUA（~8 个 action）

| Action | 参数 | 说明 |
|--------|------|------|
| screenshot | - | 截屏 |
| click | x, y, button | 点击 |
| double_click | x, y | 双击 |
| scroll | x, y, scrollX, scrollY | 滚动（像素精度） |
| type | text | 输入文本 |
| keypress | keys: array | 按键组合 |
| wait | - | 暂停 |
| drag | start, path | 拖拽 |
| move | x, y | 移动鼠标 |

特点：基于 GPT-4o 视觉能力 + 强化学习。像素坐标定位。WebVoyager 87% 成功率。已进化为 ChatGPT Agent。

来源：developers.openai.com/api/docs/guides/tools-computer-use。

#### BrowserAgent (TMLR 2025)（12 个 action）

| 分类 | Action | 参数 |
|------|--------|------|
| 页面操作 | click(id, content) | 元素 ID + 描述 |
| | hover(id, content) | 悬停 |
| | press(key_comb) | 按键组合 |
| | scroll(direction) | up/down |
| | type(id, content, press_enter_after) | 输入 + 可选回车 |
| Tab 管理 | new_tab | 新建标签页 |
| | tab_focus(tab_index) | 切换标签页 |
| | close_tab | 关闭标签页 |
| URL 导航 | goto(url) | 导航 |
| | go_back | 后退 |
| | go_forward | 前进 |
| 完成 | stop(answer) | 结束 |

特点：学术研究系统，human-inspired 设计。12 个原子 action 覆盖人类浏览行为。经 SFT + RFT 两阶段训练。

来源：arXiv:2510.10666 (TMLR 2025)。

#### Vercel agent-browser（~30+ 命令）

核心交互命令：

| 命令 | 用法 | 说明 |
|------|------|------|
| click | @ref | 基本点击 |
| dblclick | @ref | 双击 |
| hover | @ref | 悬停 |
| fill | @ref "text" | 清空后输入 |
| type | @ref "text" | 不清空直接输入 |
| focus | @ref | 聚焦 |
| press / key | key | 按键/组合键 |
| keydown | key | 按下键 |
| keyup | key | 释放键 |
| check | @ref | 勾选 |
| uncheck | @ref | 取消勾选 |
| select | @ref "value" | 选择下拉选项 |
| scroll | direction pixels | 滚动（默认 down 300px） |
| scrollintoview | @ref | 滚动元素到可见 |
| drag | @ref1 @ref2 | 拖放 |
| upload | @ref file | 文件上传 |
| back | - | 后退 |
| forward | - | 前进 |
| reload | - | 刷新 |

特点：CLI 风格，命令行语法。通过 `@eN` ref 定位（AX tree snapshot 生成）。极致 token 效率（200-400 tokens/页）。支持语义定位（`find role "button" click --name "Submit"`）。

来源：github.com/vercel-labs/agent-browser/blob/main/skills/agent-browser/references/commands.md。

### 1.2 Action 频率与必要性分析

#### 所有系统都实现的 action（P0 核心）

| Action | 系统覆盖率 | 说明 |
|--------|-----------|------|
| **click** | 10/10 | 所有系统的基础 action |
| **type/fill/input_text** | 10/10 | 文本输入，所有系统都有 |
| **scroll** | 10/10 | 滚动浏览，所有系统都有 |
| **press_key** | 9/10 | 按键操作，几乎所有系统 |
| **navigate/goto** | 9/10 | URL 导航（在我们系统中由 browse 承担） |
| **select_option** | 7/10 | 下拉选择，多数系统独立处理 |

#### 多数系统实现的 action（P1 重要）

| Action | 系统覆盖率 | 说明 |
|--------|-----------|------|
| **go_back** | 7/10 | Browser-Use, BrowserAgent, Playwright MCP, agent-browser, Manus |
| **hover** | 6/10 | Playwright MCP, Skyvern, BrowserAgent, agent-browser, Manus, Anthropic CU |
| **wait** | 6/10 | Browser-Use, Skyvern, Playwright MCP, Anthropic CU, OpenAI CUA |

#### 部分系统实现的 action（P2 扩展）

| Action | 系统覆盖率 | 说明 |
|--------|-----------|------|
| tab_focus/switch_tab | 4/10 | BrowserAgent, Browser-Use, Playwright MCP, agent-browser |
| check/uncheck | 3/10 | Playwright MCP, Skyvern, agent-browser |
| double_click | 3/10 | Anthropic CU, OpenAI CUA, agent-browser |
| go_forward | 2/10 | BrowserAgent, agent-browser |
| upload_file | 3/10 | Browser-Use, Skyvern, agent-browser |

#### 罕见的 action（P3 — 侦察场景不需要）

| Action | 系统覆盖率 | 说明 |
|--------|-----------|------|
| drag | 3/10 | Skyvern, agent-browser, Anthropic CU |
| right_click | 2/10 | Anthropic CU, OpenAI CUA |
| triple_click | 1/10 | 仅 Anthropic CU |
| clipboard | 0/10 | 无系统原生支持 |

#### go_back 是否值得独立 action？

**证据支持保留：**
- 7/10 系统实现了 go_back
- BrowserAgent 论文将其列为 URL 导航的基础 action 之一
- Agent-E 论文明确提到 planner 的 backtracking 能力依赖 go_back
- 侦察场景中 agent 经常需要从详情页返回列表页

**但在我们系统中**，browse 工具已经承担了导航功能。Agent 可以 `browse(之前的 URL)` 实现后退。go_back 的优势仅在于保留浏览器历史状态（如 SPA 的 client-side routing 状态），但收益有限。

**结论：纳入 interact，成本很低（一个 Playwright `page.goBack()` 调用），在 SPA 导航中有不可替代的价值。**

#### hover 是否值得独立 action？

**证据支持保留：**
- 6/10 系统实现了 hover
- 工具层调研报告明确指出："没有 agent 框架有显式的 hover to reveal then interact 策略——依赖 LLM 推理"
- 许多网站有 hover 触发的 tooltip、下拉菜单、预览面板
- Playwright 原生 `locator.hover()` 实现简单

**反对的证据：**
- 工具层调研报告提到 Browser-Use 在点击前会自动 dispatch `mouseMoved` 事件
- 实际使用中 hover 频率远低于 click/fill/scroll

**结论：纳入 interact。在侦察场景中 hover 的触发菜单/tooltip 是重要的信息发现渠道。**

### 1.3 Scroll 的具体设计

#### 各系统 scroll 参数对比

| 系统 | 参数 | 单位 | 方向 |
|------|------|------|------|
| Browser-Use | amount: int | 像素 | 只有 down/up（两个独立 action） |
| Manus | to_top/to_bottom: bool | 一屏或到头 | 两个独立工具 |
| Playwright MCP | direction + pixels（mouse.wheel） | 像素 | 任意方向 |
| Anthropic CU | direction + amount | 抽象单位 | up/down/left/right |
| OpenAI CUA | scrollX, scrollY | 像素 | 任意方向 |
| Vercel agent-browser | direction + pixels（默认 down 300px） | 像素 | direction 参数 |
| BrowserAgent | scroll(down\|up) | 无参数，固定一屏 | up/down |
| Skyvern | scroll_x, scroll_y | 像素 | 任意方向 |

**关键发现：**
1. **像素 vs 视口**：多数系统使用像素，但 BrowserAgent 和 Manus 使用"一屏"作为单位。对 LLM 来说，"滚动 3 屏"比"滚动 2400 像素"更直觉。
2. **水平滚动**：仅 Anthropic CU、OpenAI CUA、Skyvern 支持左右。在侦察场景中水平滚动极罕见。
3. **scroll to text**：Browser-Use 的 `find_text` 和 agent-browser 的 `scrollintoview @ref` 提供了比盲目滚动更精确的定位。
4. **BrowserAgent 的研究发现**：有学术研究指出 "agents tend to engage in aimless and repetitive scrolling when an essential link is not visible at the top of the page"——一些系统甚至移除了 scroll action 改为直接加载全页。

**推荐方案：**
- `scroll_down` 和 `scroll_up` 作为 action 类型
- 参数 `amount`：整数，单位是"屏"（默认 1），内部转换为 `viewport_height * amount`
- 不支持水平滚动（侦察场景不需要）
- 不提供 `scroll_to_element`——如果需要，agent 可以 `click` 或 `interact(click, N)` 来聚焦元素，Playwright 会自动 scrollIntoView

### 1.4 单一粒度 vs 批量动作 vs code-as-action

#### 各系统对比

| 模式 | 系统 | 特点 | Token 效率 |
|------|------|------|-----------|
| 单动作/轮 | Manus, Agent-E | 每次必须观察结果再决定 | 最低（每 action 一轮 LLM） |
| 多动作批处理 | Browser-Use (<=5), OpenAI CUA | 独立 action 可批量，页面变化截断 | 中等 |
| 代码即动作 | OpenHands | 浏览器操作写成 DSL 字符串 | 最高 |
| 并行发射 | Devin | 多 action 同时发出 | 高 |

**工具层调研报告已有结论**：无系统使用"填写表单"等高层复合动作。

**对我们系统的推荐：单动作/轮。** 理由：
1. 侦察系统的核心是**观察和理解**，不是快速执行，每步观察反馈比速度更重要
2. Agent-E 以最小 action 集 + 变化观察拿到 WebVoyager SOTA，证明 action 数量不是瓶颈
3. 单动作简化实现——不需要批量截断逻辑、不需要处理批量中间失败

### 1.5 最终推荐 Action 集

**8 个 action，分 3 层：**

| 层级 | Action | 说明 | 理由 |
|------|--------|------|------|
| **核心交互** | `click` | 点击元素 | 10/10 系统 |
| | `fill` | 清空并输入文本 | 10/10 系统；用 fill 而非 type，避免追加文本问题 |
| | `select` | 选择下拉选项 | 7/10 系统；native `<select>` 不能 click 操作 |
| | `press_key` | 按键/组合键 | 9/10 系统；Enter 提交、Escape 关闭、Tab 切换 |
| **滚动** | `scroll_down` | 向下滚动 N 屏 | 10/10 系统 |
| | `scroll_up` | 向上滚动 N 屏 | 10/10 系统 |
| **导航辅助** | `go_back` | 浏览器后退 | 7/10 系统；SPA 状态不可替代 |
| | `hover` | 悬停元素 | 6/10 系统；触发隐藏菜单/tooltip |

**不纳入的 action 及理由：**

| Action | 排除理由 |
|--------|---------|
| navigate/goto | 由 browse 工具承担 |
| wait | browse 内部已有 DOM settle 等待；agent 可用 think 思考 |
| screenshot | browse 返回文本已足够；截图 token 成本高 |
| tab 管理 | 侦察场景单标签足够；复杂场景用 bash/browse 处理 |
| double_click | 3/10 系统，侦察场景极少需要 |
| drag | 3/10 系统，侦察场景不需要 |
| right_click | 2/10 系统，侦察场景不需要 |
| check/uncheck | 可通过 click 实现 |
| upload_file | 侦察场景不需要 |
| find_text/scroll_to | agent 可用 scroll + 观察找到目标 |

---

## 二、元素定位策略

### 2.1 各系统定位方式对比

| 系统 | 定位方式 | 机制 | 优势 | 劣势 |
|------|---------|------|------|------|
| **Browser-Use** | 编号索引 | DOM 遍历 → 可交互过滤 → 编号 | 精确，LLM 友好 | 动态页面索引易过期 |
| **Agent-E** | mmid 注入属性 | 遍历 DOM 注入 `mmid` 属性 | 页面存活期间稳定 | 导航后全部失效 |
| **Playwright MCP** | ref 编号 | AX tree → `[ref=eN]` | 语义化 | 每次 action 后重建 |
| **Manus** | 编号 + 坐标双模式 | 索引优先，坐标 fallback | 覆盖面广 | 增加 LLM 决策复杂度 |
| **Stagehand** | 自然语言 → LLM 推理 | observe() 分析 AX tree | 最灵活，自愈 | 每次需 LLM，慢 |
| **Vercel agent-browser** | @eN ref | AX tree snapshot | 极致简洁 | 信息密度低 |
| **Skyvern** | element_id + 坐标 | 视觉 + DOM 双通道 | 抗 DOM 变化 | 视觉定位精度低（18.9%） |
| **Anthropic CU** | 像素坐标 | 截图 → 推理坐标 | 通用 | 56.7% 点错目标 |

### 2.2 编号索引定位——推荐方案

**选择编号索引的理由：**

1. **与 browse 工具已对齐**：browse 输出格式已确定为 `[N]<tag attr="val">text</tag>`，interact 直接接受编号是最自然的接口
2. **业界趋同**：Browser-Use、Manus、Playwright MCP、Vercel agent-browser 全部走向编号索引
3. **Token 效率**：编号 `3` 远比 CSS selector `#main > div.card:nth-child(2) > button.submit` 省 token
4. **LLM 准确率**：工具层调研报告数据——编号索引的 action 可以在 10-15 tokens 内表达，结构化输出减少幻觉

**编号索引的生命周期（与 browse 工具设计报告一致）：**

```
browse(url)
  → 构建 DOM 树 → 过滤 → 编号 → 存入 ToolContext.selector_map
  → 返回包含编号的页面快照给 LLM

interact(click, 3)
  → 从 selector_map[3] 获取 DOMElement
  → 执行点击
  → 等待 DOM settle
  → 重建 selector_map（新的编号）
  → 返回变化摘要 + 新页面快照

browse(new_url)
  → 旧 selector_map 完全丢弃
  → 新页面从 1 重新编号
```

**过期索引处理：**
- 如果 `selector_map[N]` 不存在 → 返回明确错误："Element [N] does not exist. Available elements: [1]-[M]. The page may have changed since the last snapshot."
- 如果 DOM 元素已被移除（stale reference）→ 返回："Element [N] is no longer in the DOM. Use browse() to get a fresh page snapshot."

### 2.3 是否需要同时支持编号和 CSS selector？

**结论：主要用编号，CSS selector 作为不编号的 fallback。**

理由：
- browse 返回中不是所有元素都有编号（只有可交互元素编号），但 agent 有时需要操作未编号的元素
- CSS selector 通过 `target` 参数的字符串解析自然支持——如果 target 是纯数字就查 selector_map，否则当作 CSS selector
- 这与 browse 工具设计报告 2.4 节的设计一致：`interact(action, target) ← target 可以是编号 "[3]" 或 CSS selector`

**不需要自然语言定位**：Stagehand 的自然语言定位需要额外 LLM 调用（慢+贵），且我们的 agent 已经有 browse 返回的编号索引，不需要再用自然语言描述目标。

---

## 三、变化反馈设计（关键——避免 interact→browse 双步）

### 3.1 各系统变化反馈机制

| 系统 | 机制 | 返回内容 | 是否包含新页面快照 |
|------|------|---------|-----------------|
| **Agent-E** | MutationObserver + 属性监控 | 语言化反馈："Clicked element mmid 25. A popup appeared with..." | 否（只返回变化描述） |
| **Browser-Use** | 新旧快照对比 | 新元素标记 `*`，循环检测 | 是（返回完整新快照） |
| **Playwright MCP** | 每次 action 后获取新 snapshot | 完整 AX tree | 是（但需要额外调用 browser_snapshot） |
| **Manus** | browser_view 独立调用 | 新页面状态 | 否（需要手动调 browser_view） |
| **Stagehand** | act() 返回 ActResult | success + message + actions 列表 | 否（需要额外 observe/extract） |
| **Vercel agent-browser** | 命令输出 | 命令执行结果 | 否（需要额外 snapshot） |

### 3.2 Agent-E 的变化观察——最值得借鉴

Agent-E 的核心创新（arXiv:2407.13032）：

```
交互前：
  1. 注入 MutationObserver 到目标元素的 subtree
  2. 记录 aria-expanded 等关键属性的当前值

执行交互

交互后 100ms：
  1. 取消 MutationObserver 订阅
  2. 收集 childList 变化（新增/删除的 DOM 节点）
  3. 收集 characterData 变化（文本修改）
  4. 对比 aria-expanded 等属性变化
  5. 生成语言化反馈
```

反馈格式示例：
- "Clicked the element with mmid 25. As a consequence, a popup has appeared with following elements: [list of new elements]."
- "Entered text 'threejs' into search field mmid 42. The page now shows a dropdown with 5 suggestions: ..."
- "Clicked mmid 30. No visible changes occurred."

**为什么语言化反馈比原始 DOM diff 更好：**
1. LLM 可以直接理解，不需要解析结构化 diff
2. Token 成本更低——一句话 vs 完整的 DOM 变化树
3. 引导 agent 下一步决策——"popup appeared" 暗示下一步应该在 popup 中操作

### 3.3 推荐的变化反馈方案

**核心设计：交互后返回"变化摘要 + 新页面快照"双层信息。**

```
interact 执行流程：
  1. 记录交互前状态（URL, 页面内容指纹, 关键属性）
  2. 注入 MutationObserver（监控 childList + characterData + attributes）
  3. 执行 action
  4. 等待 DOM settle（MutationObserver 静默 1.5s 或硬超时）
  5. 收集变化
  6. 重建 selector_map
  7. 生成返回值
```

**返回值分三种情况：**

**情况 1：URL 变化（导航发生）**
```
=== Navigation Occurred ===
Previous: https://codepen.io/search
Current:  https://codepen.io/pen/abc123

=== Page: Three.js Particle System ===
URL: https://codepen.io/pen/abc123
Status: 200

--- Content ---
# Three.js Particle System
Author: John Doe | Views: 1,234

[1]<button>Like</button>
[2]<a href="/johndoe">John Doe</a>
...（完整新页面快照）
```

**情况 2：页面内容变化（AJAX、modal、展开/折叠）**
```
=== Changes After Click [5] ===
- Modal appeared: "Login Required"
- 3 new elements added

--- Current Page State ---
（完整页面快照，新元素标记 *）
...
*[15]<input type="email" placeholder="Email">
*[16]<input type="password" placeholder="Password">
*[17]<button type="submit">Log In</button>
...
```

**情况 3：无明显变化**
```
=== No Visible Changes ===
Action: click [5] (button "Load More")
Observation: No DOM changes detected after 2 seconds.
Hint: The button may require authentication, or the content may be loading asynchronously.

--- Current Page State ---
（页面快照不变）
```

### 3.4 何时返回完整页面快照？何时只返回变化摘要？

**核心原则：始终返回完整页面快照。**

理由：
1. **避免 interact→browse 双步**：这是设计目标。如果 interact 不返回新快照，agent 每次交互后都需要调 browse，浪费一轮 LLM。
2. **新的 selector_map 需要新快照**：interact 执行后 selector_map 已重建，如果不把新编号返回给 LLM，LLM 会用过期编号。
3. **Agent-E 的教训**：Agent-E 只返回变化描述不返回完整快照，agent 经常需要额外调 get_dom 感知完整页面——反而增加了步骤。
4. **Browse-Use 的实践验证**：Browser-Use 在每次 action 后返回完整新快照（含 `*` 标记新元素），这被证明是有效的。

**Token 成本控制：**
- 只返回视口内容（与 browse 工具一致）
- 列表截断（>5 个同构项显示前 3 个 + 省略提示）
- 非交互变化的页面，快照可以标记 `(unchanged)` 让 LLM 快速跳过
- 滚动操作返回新视口内容（这是 scroll 的核心价值）

---

## 四、参数设计

### 4.1 统一 action + target + value vs 每种动作独立工具

#### 各系统对比

| 模式 | 系统 | 优劣 |
|------|------|------|
| **统一工具 + action 参数** | 我们的设计, Skyvern | 工具定义少，token 省；action 间共享 target 语义 |
| **每种 action 独立工具** | Manus, Playwright MCP, Browser-Use | 参数 schema 更精确；但工具数量膨胀 |
| **自然语言指令** | Stagehand | 最灵活；但每次需要 LLM 推理 |

**量化证据（工具层调研报告 0.2 节）：**
- 4 工具 ~1,200 tokens → ~95% 选择准确率
- 46 工具 ~42,000 tokens → ~71% 选择准确率
- 工具描述 token 开销：独立工具模式下，8 个浏览器 action 就需要 8 个工具定义，每个 ~100 tokens = ~800 tokens
- 统一工具模式下，1 个工具定义 ~200 tokens，enum 约束 action 类型

**Google ADK 模式的量化数据：**
- 统一工具减少 41.2% 总 LLM tokens
- 结构化返回值虽然更长，但 LLM 处理效率更高

**结论：统一工具模式。** 理由：
1. 我们的 agent 已经有 10 个工具（browse, interact, extract, bash, execute_code, think, note_insight, note_relation, read_world_model, search_site），再把 interact 拆成 8 个独立工具会达到 17+ 个，接近性能退化区间
2. 统一工具的 enum 约束 `action` 类型足以防止参数错误
3. 所有 action 共享 `target` 参数的语义——都是"操作哪个元素"

### 4.2 推荐参数 Schema

```python
interact(
    action: str,      # enum: click, fill, select, press_key, scroll_down, scroll_up, go_back, hover
    target: str = "", # 元素编号（如 "3"）或 CSS selector（如 ".btn-submit"）
    value: str = ""   # fill 的文本, select 的选项, press_key 的按键
)
```

**各 action 的参数使用：**

| action | target | value | 示例 |
|--------|--------|-------|------|
| `click` | 元素编号或 selector | （不用） | `interact("click", "3")` |
| `fill` | 元素编号或 selector | 要输入的文本 | `interact("fill", "8", "threejs")` |
| `select` | 元素编号或 selector | 选项文本或值 | `interact("select", "10", "Newest")` |
| `press_key` | （可选，指定焦点元素） | 按键名称 | `interact("press_key", "", "Enter")` |
| `scroll_down` | （不用） | 屏数（默认 "1"） | `interact("scroll_down")` |
| `scroll_up` | （不用） | 屏数（默认 "1"） | `interact("scroll_up")` |
| `go_back` | （不用） | （不用） | `interact("go_back")` |
| `hover` | 元素编号或 selector | （不用） | `interact("hover", "5")` |

### 4.3 参数防错设计

基于工具层调研报告 0.5 节的 poka-yoke 原则：

1. **action 用 enum 约束**：JSON Schema 中 `action` 字段有 `enum` 列表，LLM 不会幻觉出不存在的 action
2. **target 自动解析**：纯数字 → 查 selector_map；其他 → 当 CSS selector 处理
3. **缺失 target 的防错**：`click/fill/select/hover` 需要 target，如果缺失返回明确错误："action 'click' requires a target element. Specify an element number from the page snapshot or a CSS selector."
4. **fill 清空语义**：fill 内部先 Ctrl+A + Backspace 清空，再输入。避免"字符追加到已有文本"的常见坑（工具层调研报告 2.5 节确认）
5. **press_key 格式**：接受 Playwright 的键名格式（"Enter", "Tab", "Escape", "Control+a", "Shift+Tab" 等）
6. **scroll 量默认**：scroll_down/scroll_up 的 value 默认为 "1"（一屏），避免 LLM 需要猜像素值

### 4.4 JSON Schema（给 LLM 的工具定义）

```json
{
    "name": "interact",
    "description": "Interact with elements on the current page. Use this after browse() to click buttons, fill forms, scroll, etc.\n\nActions:\n- click: Click an element. Target: element number from page snapshot.\n- fill: Clear a field and type text. Target: element number. Value: text to enter.\n- select: Choose a dropdown option. Target: element number of <select>. Value: option text.\n- press_key: Press a key or combination. Value: key name (Enter, Tab, Escape, Control+a, etc.).\n- scroll_down / scroll_up: Scroll the page. Value: number of screens (default 1).\n- go_back: Go back to previous page in browser history.\n- hover: Hover over an element to reveal tooltips or menus.\n\nAfter each interaction, you'll receive the updated page snapshot with new element numbers. Previous element numbers become invalid.\n\nDo NOT use interact for navigation to new URLs — use browse() instead.\nDo NOT call browse() after interact — the response already includes the updated page state.",
    "parameters": {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["click", "fill", "select", "press_key", "scroll_down", "scroll_up", "go_back", "hover"],
                "description": "The interaction to perform"
            },
            "target": {
                "type": "string",
                "description": "Element number from page snapshot (e.g. '3') or CSS selector. Required for click, fill, select, hover.",
                "default": ""
            },
            "value": {
                "type": "string",
                "description": "Text to fill, option to select, key to press, or number of screens to scroll.",
                "default": ""
            }
        }
    }
}
```

---

## 五、点击回退链和错误处理

### 5.1 Browser-Use 的完整点击回退链（工具层调研报告 2.4 节）

```
1. DOM.scrollIntoViewIfNeeded(backendNodeId)    ← 确保可见
   ↓ (50ms wait)
2. 获取元素坐标（bounding rect）
   ↓
3. 可见性检查：计算与视口相交的最大可见四边形
   ↓
4. 遮挡检查：document.elementFromPoint(x, y)
   ├── 验证目标元素会接收点击
   ├── 检查双向包含关系（点击点落在父/子元素上也算有效）
   └── 检查 label-input 关联（向上 3 层祖先）
   ↓
5a. 未被遮挡 → CDP mouse events (mouseMoved, mousePressed, mouseReleased) @ 中心点
5b. 被遮挡 → JS fallback: DOM.resolveNode → Runtime.callFunctionOn('function() { this.click(); }')
5c. 无坐标 → 直接 JS click
   ↓
6. 特殊验证：Checkbox 记录 pre/post 状态，未变化 → JS fallback
```

### 5.2 推荐的简化回退链

我们的场景是侦察而非精确表单填写，可以简化：

```
Step 1: 定位元素
  ├── target 是数字 → selector_map[N] → 获取 Playwright Locator
  ├── target 是 CSS selector → page.locator(selector)
  └── 找不到 → 返回错误

Step 2: Playwright 原生点击（包含自动等待 + 自动滚动）
  locator.click(timeout=5000)
  ├── 成功 → Step 3
  └── 超时/遮挡 → Step 2b

Step 2b: Force click（跳过 actionability 检查）
  locator.click(force=True, timeout=3000)
  ├── 成功 → Step 3
  └── 失败 → Step 2c

Step 2c: JS click（最后手段）
  locator.evaluate("el => el.click()")
  ├── 成功 → Step 3
  └── 失败 → 返回诊断错误

Step 3: 等待 DOM settle + 重建快照 + 返回结果
```

**为什么 Playwright 原生优先而非 CDP 原语：**
1. Playwright 已内置 auto-wait + auto-scroll + actionability checks
2. Playwright 处理跨 frame 点击
3. 我们用 Playwright 连接 Camoufox，CDP 操作增加复杂度
4. `force=True` 已经覆盖 Browser-Use 的"被遮挡 → JS fallback"逻辑

### 5.3 各 action 的错误处理

| 错误类型 | 处理方式 | 返回给 LLM 的信息 |
|---------|---------|------------------|
| **元素不存在** | 直接返回错误 | "Element [N] does not exist. The page has [M] interactive elements. Use browse() to refresh." |
| **元素类型不匹配** | 直接返回错误 | "Cannot fill element [3] (it's a button, not an input field). Use click instead." |
| **点击被遮挡** | 回退链处理 | 如果回退链成功，正常返回；失败则："Could not click [5]: element is obscured by another element. Try scrolling or closing overlapping content." |
| **超时** | 5 秒硬超时 | "Interaction timed out after 5 seconds. The page may be unresponsive." |
| **Select 选项不存在** | 返回可用选项 | "Option 'xyz' not found in select [10]. Available options: Trending, Newest, Popular." |
| **Stale element** | 建议 browse | "Element [3] is no longer in the DOM. The page may have changed. Call browse() to get a fresh snapshot." |
| **导航被阻止** | 返回当前状态 | "Navigation was blocked (popup blocker or same-page anchor). Current URL unchanged." |

### 5.4 Dialog 自动处理

Playwright 支持 `page.on('dialog')` 事件。参考 Playwright MCP 的 `browser_handle_dialog`。

**推荐策略：自动 accept，不暴露给 agent。**
```python
page.on("dialog", lambda dialog: dialog.accept())
```

理由：
- 侦察场景中 dialog（alert/confirm/prompt）通常是干扰
- Agent-E 和 Browser-Use 都不把 dialog 处理暴露给 LLM
- 如果 dialog 确实包含重要信息，在变化反馈中提及即可

### 5.5 Cookie Banner 处理

**不自动处理。原因：**
- Cookie banner 的形式千差万别（overlay, bottom bar, modal, inline）
- 自动检测不可靠
- Agent 有能力自己识别和处理（click "Accept" 按钮）
- 第一次 browse 时 agent 会看到 banner 并决定如何处理

---

## 六、返回值设计

### 6.1 成功时的返回值

**统一返回格式：**

```python
@dataclass
class InteractResult:
    success: bool          # 交互是否成功执行
    action: str            # 执行的 action
    target_desc: str       # 操作的元素描述（如 "button 'Search'"）
    change_summary: str    # 变化摘要（语言化）
    page_snapshot: str     # 新的页面快照（与 browse 返回格式一致）
    error: str | None      # 错误信息（成功时为 None）
```

**返回给 LLM 的文本格式：**

```
=== Interaction Result ===
Action: click [5] (button "Load More")
Result: Success

=== Changes Detected ===
- URL unchanged
- 10 new elements appeared (product cards)
- Scroll position: increased (content expanded below)

=== Page: Search Results - threejs ===
URL: https://codepen.io/search?q=threejs
Status: 200

--- Content ---
# Search Results for "threejs"
Showing 40 results

<div class="results">
  ... (existing cards)

  *[25]<a href="/pen/xyz789">WebGL Particles</a>
  *[26]<button>Like</button>
  *[27]<a href="/pen/abc012">3D Globe</a>
  *[28]<button>Like</button>
  ... (8 more new cards)
</div>

[29]<button>Load More</button>

--- Scroll Position ---
0.5 pages above | 2.3 pages below
```

### 6.2 各 Action 的返回值特化

| Action | 额外返回信息 |
|--------|------------|
| click | 新元素标记 `*`，URL 变化提示 |
| fill | 字段当前值确认："Field [8] now contains: 'threejs'" |
| select | 选中值确认："Selected 'Newest' in [10]" |
| press_key | 按键效果：如果 Enter 触发了表单提交/搜索，在变化摘要中说明 |
| scroll_down/up | 新视口内容（这是 scroll 的核心价值——展示之前不可见的内容） |
| go_back | 新页面的完整快照（视为导航） |
| hover | hover 触发的新元素（tooltip/menu）标记 `*` |

### 6.3 导航发生时的处理

当 action 触发了页面导航（URL 变化）：

```
=== Navigation Occurred ===
Action: click [4] (link "View Pen")
From: https://codepen.io/search?q=threejs
To:   https://codepen.io/johndoe/pen/abc123

=== Page: Three.js Particle System ===
URL: https://codepen.io/johndoe/pen/abc123
Status: 200

--- Content ---
（完整新页面快照，等同于 browse 的返回值）

--- Data Signals ---
（如果新页面有 embedded JSON 等，也包含）

--- Network Requests ---
（新页面加载时捕获的 API 请求）
```

**关键：导航触发时，interact 的返回值等同于一次完整的 browse 调用。** 这包括：
- 自动创建/更新 Location（与 browse 一致）
- 自动追加 Observation（与 browse 一致）
- 返回 Data Signals 和 Network Requests（与 browse 一致）

### 6.4 失败时的诊断信息

```
=== Interaction Failed ===
Action: fill [3] (button "Submit")
Error: Cannot fill element [3] — it is a <button>, not an input field.
Suggestion: Use click instead, or find the correct input element.

--- Current Page State ---
（页面快照不变，帮助 agent 重新选择目标）
```

---

## 七、与 browse 的关系

### 7.1 核心设计：interact 后不需要再调 browse

**这是本设计的关键决策。** 各系统的做法对比：

| 系统 | interact 后是否需要额外感知 | 效率 |
|------|--------------------------|------|
| Agent-E | 需要调 get_dom 获取完整状态 | 较低 |
| Manus | 需要调 browser_view 获取新状态 | 较低 |
| Browser-Use | 不需要——action 结果包含新快照 | **高** |
| Playwright MCP | 需要调 browser_snapshot | 较低 |
| Stagehand | 需要调 observe 或 extract | 较低 |

**Browser-Use 的模式最高效**——每次 action 后自动返回新快照，LLM 可以连续决策而不需要"interact→browse→interact→browse"的乒乓循环。

### 7.2 Agent 什么时候应该用 browse vs interact

| 场景 | 用哪个 | 理由 |
|------|--------|------|
| 访问新 URL | browse | browse 处理导航 + 等待 + 自动记账 |
| 点击按钮/链接 | interact(click) | 页面交互 |
| 填写表单 | interact(fill) | 表单交互 |
| 滚动看更多内容 | interact(scroll_down) | 不需要导航 |
| 点击链接后到了新页面 | interact(click) 就够了 | interact 检测到导航后返回完整新页面（等同 browse） |
| 想要重新获取干净的页面状态 | browse(当前URL) | 强制刷新 + 重建快照 |
| 想看之前访问过的页面 | browse(那个URL) | browse 处理导航 |

### 7.3 Tool Description 中的关键提示

在 interact 的 tool description 中必须说清：

> "After each interaction, you'll receive the updated page snapshot with new element numbers. Previous element numbers become invalid. Do NOT use interact for navigation to new URLs — use browse() instead. Do NOT call browse() after interact — the response already includes the updated page state."

在 browse 的 tool description 中补充：

> "Use browse() to navigate to new URLs or refresh the current page. For interacting with elements on the current page (clicking, filling, scrolling), use interact() instead."

### 7.4 interact 触发导航时的 World Model 记账

当 interact 检测到 URL 变化时，内部调用与 browse 相同的 World Model 记账逻辑：

```python
async def _handle_navigation(self, old_url: str, new_url: str, page_snapshot: str):
    """interact 触发导航时，执行与 browse 相同的 WM 记账"""
    # 1. 创建或更新 Location
    location = await self.wm.upsert_location(new_url, ...)
    # 2. 追加 Observation（页面摘要）
    await self.wm.add_observation(location_id=location.id, ...)
    # 3. 添加 Relation（来源页面 → 目标页面）
    await self.wm.add_relation(from_url=old_url, to_url=new_url, relation="navigated_via_click")
```

---

## 八、完整设计方案

### 8.1 参数 Schema

```python
class InteractParams(BaseModel):
    action: Literal["click", "fill", "select", "press_key", "scroll_down", "scroll_up", "go_back", "hover"]
    target: str = ""   # 元素编号（如 "3"）或 CSS selector
    value: str = ""    # fill 的文本 / select 的选项 / press_key 的按键 / scroll 的屏数
```

### 8.2 返回值 Schema

```python
@dataclass
class InteractResult:
    success: bool
    action: str
    target_desc: str       # "button 'Search'" / "input [placeholder='Email']" / ""
    change_summary: str    # 语言化变化摘要
    page_snapshot: str     # 新页面快照（与 browse 格式一致）
    navigated: bool        # 是否发生了 URL 变化
    new_url: str | None    # 导航后的新 URL
    error: str | None      # 错误信息
```

给 LLM 的返回格式化为文本（不是 JSON），与 browse 返回格式统一。

### 8.3 实现要点

#### 核心执行流程

```python
async def execute_interact(params: InteractParams, ctx: ToolContext) -> str:
    action = params.action
    target = params.target
    value = params.value

    # 1. 参数验证
    validate_params(action, target, value)

    # 2. 解析 target → Playwright Locator
    if action in ("click", "fill", "select", "hover"):
        locator = resolve_target(target, ctx.selector_map, ctx.page)

    # 3. 记录交互前状态
    pre_url = ctx.page.url
    # 注入 MutationObserver（可选，用于变化摘要）

    # 4. 执行 action
    match action:
        case "click":
            await click_with_fallback(locator)
        case "fill":
            await locator.fill(value)  # Playwright fill 自动清空
        case "select":
            await locator.select_option(label=value)
        case "press_key":
            if target:
                locator = resolve_target(target, ctx.selector_map, ctx.page)
                await locator.press(value)
            else:
                await ctx.page.keyboard.press(value)
        case "scroll_down":
            amount = int(value) if value else 1
            await ctx.page.evaluate(f"window.scrollBy(0, window.innerHeight * {amount})")
        case "scroll_up":
            amount = int(value) if value else 1
            await ctx.page.evaluate(f"window.scrollBy(0, -window.innerHeight * {amount})")
        case "go_back":
            await ctx.page.go_back(wait_until="domcontentloaded")
        case "hover":
            await locator.hover()

    # 5. 等待 DOM settle
    await wait_for_dom_settle(ctx.page)

    # 6. 检测变化
    post_url = ctx.page.url
    navigated = (post_url != pre_url)
    change_summary = detect_changes(...)  # MutationObserver 结果 + URL 变化

    # 7. 重建页面快照 + selector_map
    page_snapshot = await build_page_snapshot(ctx.page)
    ctx.selector_map = build_selector_map(ctx.page)

    # 8. 如果导航发生，执行 WM 记账
    if navigated:
        await handle_navigation(pre_url, post_url, page_snapshot, ctx.wm)

    # 9. 格式化返回值
    return format_interact_result(...)
```

#### 关键子模块

**resolve_target：元素定位**
```python
def resolve_target(target: str, selector_map: dict, page: Page) -> Locator:
    # 纯数字 → 查 selector_map
    if target.isdigit():
        index = int(target)
        if index not in selector_map:
            raise ElementNotFoundError(f"Element [{index}] does not exist")
        return selector_map[index].to_locator(page)

    # 否则当 CSS selector
    locator = page.locator(target)
    if await locator.count() == 0:
        raise ElementNotFoundError(f"No element matches selector '{target}'")
    return locator.first
```

**click_with_fallback：点击回退链**
```python
async def click_with_fallback(locator: Locator):
    try:
        # Step 1: 正常点击（含 auto-wait + auto-scroll + actionability）
        await locator.click(timeout=5000)
    except TimeoutError:
        try:
            # Step 2: Force click（跳过遮挡检查）
            await locator.click(force=True, timeout=3000)
        except Exception:
            # Step 3: JS click（最后手段）
            await locator.evaluate("el => el.click()")
```

**wait_for_dom_settle：DOM 稳定等待**
```python
async def wait_for_dom_settle(page: Page, timeout_ms=3000, quiet_ms=1500):
    """等待 DOM 变化静默 quiet_ms 毫秒，或达到 timeout_ms 硬超时"""
    await page.evaluate(f"""
        new Promise(resolve => {{
            let timer = setTimeout(resolve, {quiet_ms});
            const observer = new MutationObserver(() => {{
                clearTimeout(timer);
                timer = setTimeout(resolve, {quiet_ms});
            }});
            observer.observe(document.body, {{ childList: true, subtree: true }});
            setTimeout(() => {{ observer.disconnect(); resolve(); }}, {timeout_ms});
        }})
    """)
```

**detect_changes：变化检测**
```python
def detect_changes(pre_url, post_url, pre_elements, post_elements):
    """对比交互前后状态，生成语言化变化摘要"""
    changes = []

    # URL 变化
    if post_url != pre_url:
        changes.append(f"Navigation: {pre_url} → {post_url}")

    # 新增元素
    new_elements = set(post_elements.keys()) - set(pre_elements.keys())
    if new_elements:
        changes.append(f"{len(new_elements)} new elements appeared")

    # 消失的元素
    removed = set(pre_elements.keys()) - set(post_elements.keys())
    if removed:
        changes.append(f"{len(removed)} elements removed")

    # 无变化
    if not changes:
        changes.append("No visible changes detected")

    return "\n".join(f"- {c}" for c in changes)
```

### 8.4 与 ToolContext 的集成

```python
class ToolContext:
    page: Page                          # Playwright page
    selector_map: dict[int, DOMElement] # 当前编号 → 元素映射
    wm: SiteWorldModel                  # World Model 引用
    captured_requests: list             # 捕获的网络请求（与 browse 共享）
```

`selector_map` 被 browse 和 interact 共享。browse 创建初始 map，interact 在每次执行后更新。

### 8.5 与 browse 的数据流

```
用户需求 → ReconPlanner → Agent Session

Agent: browse("https://codepen.io")
  → selector_map = {1: nav_link, 2: search_input, ...}
  → 返回页面快照

Agent: interact("fill", "2", "threejs")
  → 查找 selector_map[2] → search_input
  → 执行 fill
  → DOM settle
  → 重建 selector_map = {1: nav_link, 2: search_input_filled, 3: suggestion_1, ...}
  → 返回变化摘要 + 新快照

Agent: interact("press_key", "", "Enter")
  → 执行 Enter
  → 如果触发导航（URL 变化）→ 自动 WM 记账
  → 重建 selector_map = {1: result_1, 2: result_2, ...}
  → 返回新页面快照（等同 browse 返回）

Agent: interact("click", "1")
  → 点击第一个搜索结果
  → 导航到详情页
  → 自动 WM 记账
  → 返回详情页完整快照
```

---

## 九、总结

### 关键设计决策及其证据

| 决策 | 选项 | 选择 | 关键证据 |
|------|------|------|---------|
| Action 集大小 | 5 (Agent-E) ~ 22 (Skyvern) | **8 个** | Agent-E 5 个拿 SOTA；BrowserAgent 12 个覆盖 human-inspired 全集；8 个是侦察场景最小有效集 |
| 元素定位 | 编号/selector/自然语言/坐标 | **编号优先 + selector fallback** | 6/10 系统用编号；与 browse 返回格式对齐；10-15 token/action |
| 工具形式 | 统一工具 / 独立工具 | **统一工具 + enum action** | 41.2% token 减少（Google ADK）；避免工具数膨胀到 17+ |
| 变化反馈 | 变化摘要 / 完整快照 / 无 | **变化摘要 + 完整快照** | Browser-Use 验证了每 action 返回快照的效率；Agent-E 验证了语言化反馈的价值 |
| interact 后是否需要 browse | 需要 / 不需要 | **不需要** | Browser-Use 模式最高效；避免 interact→browse 乒乓 |
| 点击回退 | 单一尝试 / 回退链 | **3 级回退链** | Browser-Use 源码验证的实践方案 |
| scroll 单位 | 像素 / 屏 / 无参数 | **屏（默认 1）** | LLM 对"屏"比"像素"更直觉；BrowserAgent 用"一屏" |
| Dialog 处理 | 暴露给 agent / 自动处理 | **自动 accept** | 侦察场景 dialog 是干扰；Agent-E 和 Browser-Use 都不暴露 |
