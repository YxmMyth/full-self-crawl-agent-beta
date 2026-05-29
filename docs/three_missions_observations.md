# Three Missions E2E Observations

> 日期: 2026-05-26 起步,2026-05-28 重启(headed)
> LLM: 小米 MiMo (mimo-v2.5-pro) — 已从 deepseek-v4-pro 切换
> Mode: `auto --no-gate`, **headed (parent.lock 自动清理修复后)**

## 关键修复 (2026-05-28)

**Camoufox headed launch 180s hang 真因 = `parent.lock` 残留**(非二进制 bug):
- 用户洞察: "只在连续两次打开 camoufox 时出现" → 直接验证为 Firefox profile lock 未释放
- 我们用 psutil SIGKILL 关 Camoufox → Firefox 没机会优雅退出 → `parent.lock` 留着
- 下次 launch → Firefox 等 lock 释放 → 永远等不到 → Playwright 180s timeout → 报 `Juggler removeProgressListener NS_ERROR_FAILURE`(误导性错误)
- Fix: `src/browser/manager.py` launch 前自动删 `parent.lock`(commit `ed8bcdb`)
- Cold launch 33.8s,warm 11.4s(verified)
- **之前归因 [[camoufox-headed-bug]] / daijro#612 是错的** — 那个 issue 是真 bug 但不是我们的根因

每个 mission 跑 `python -m src.main auto <domain> "<req>" --no-gate`,headless 默认。
失败 **不改代码**,只重试 / 跳过 / 转下一个。

---

## Mission 1: chemrxiv.org

**Requirement**:
```
采集 chemrxiv.org 化学预印本 30 篇,含元数据 (title, authors, abstract, DOI, subject) + PDF 全文。
```

**已知 ground truth (来自 STEM/samples/20_chemrxiv/STATUS.md)**:
- 本站 API / sitemap / paperscraper PyPI **全 403**
- 真路径: `https://www.cambridge.org/engage/coe/public-api/v1/items?term=CHEMRXIV`
- 元数据含 title/authors/abstract/DOI/subject + asset.original.url(PDF 直链)
- PDF 在 chemrxiv 自家 CDN

**关键测试点**:
- 🔴 agent 是否会被 403 吓退
- 🔴 agent 能否找到 Cambridge backend 这个非显眼入口
- 🟡 是否真的只采 30 篇(quantity 尊重测试)

(待跑后填)

### Attempt 1 (2026-05-26 headless, RUN dead)
- **Recon session 1**: 266 步 / 28min / 41 obs(发现 `api.crossref.org/{doi}` 跨域元数据通道)
- **Session 2 起步即撞 Cloudflare Turnstile** → request_human_assist → Tkinter 弹框
- 用户不在场 → **卡 39 小时** → 用户手动 cancel
- ⚠️ 也撞 `visual mode failed: ...kimi-k2.5...` — 某处 hardcoded vision 模型,MiMo gateway 拒
- **未找到 Cambridge engage backend**(STEM 调研知道这是 chemrxiv 唯一合法绕 CF 的路)
- Disk: 11MB catalog

### Attempt 2 (2026-05-28 headed, parent.lock 修复后)
- 10:36:17 启动,10:36:28 browser launched(headed,11s ✓)
- 10:36:30 Planner started("Injected 1 prior runs into Planner initial context" — 知道上次跑过)
- **10:57:09 — Planner 第一个 LLM call 吃 MiMo gateway 502**(openresty Bad Gateway)
- 20 分钟 hang 后才返错,提示 MiMo 上游不稳
- `outcome: crash, 0 sessions, 0 tool calls`
- Harvest 仍然启动(auto 流不查 recon outcome),但 samples 空 → 主动强停

### 结论
- **未能完成 chemrxiv mission**(2 次都失败)
- 失败 1: 撞 CF + 人工救援超时
- 失败 2: MiMo gateway 上游 502 — 不是 agent 问题
- **核心发现仍有价值**:agent 找到 `api.crossref.org` 跨域 DOI 元数据,但没发现 Cambridge engage backend(真核心入口)
- **未来 retry 建议**: MiMo 稳定后重跑,且在 system prompt 加 "撞 CF 先 7 种 alt path 再 human_assist"

### 评分(0-5)
- Universe 发现: 1.5 / 5(找到 Crossref 但漏 Cambridge)
- 入口找对: 1 / 5
- 反爬绕过: 0 / 5
- Quantity 尊重: N/A(没进 harvest)
- Audit 通过: N/A(没 mark_done)

---

## Mission 2: 66rpg.com (橙光)

**Requirement**:
```
采集 66rpg.com (橙光) 文字游戏 3 个,每个含完整剧本资源:剧情文本 + 立绘 + BGM + CG。
```

**已知 ground truth (来自 文字游戏/调研报告_路径D.md, agent 看不到)**:
- gindex (数字 ID) → `66rpg.com/f/{gindex}/ref/d3d3LjY2cnBnLmNvbQ==` (redirect)
- 重定向链含 guid (32 字符 UUID)
- `66rpg.com/api/common/versions?guid={guid}` → 版本列表
- `wcdn1.cgyouxi.com/web/{guid}/{ver}/Map.bin` (~400KB,资源清单 明文 UTF-8)
- `wcdn1.cgyouxi.com/shareres/{md5[:2]}/{md5}` (per-resource)

### Attempt 1 (2026-05-28 deepseek, headed): **RECON PASS audit 6/6 ✓**
Run ID: `20260528-1102_采集-66rpg.com-(橙光)-文字游戏-3-个,每个含`

**Wall clock: 1h48min (5 sessions, 26 planner tool calls)**

#### 关键发现 — agent 完全独立重发现了同一 CDN 协议

- CDN: `dlcdn1.cgyouxi.com` / `c2.cgyouxi.com`(我们调研写 wcdn1,同源不同子域)
- Map.bin: `/web/{guid}/{ver}/Map.bin` ✓
- Game.bin: `/web/{guid}/{ver}/Game_mini.bin` ✓
- 资源: `/shareres/{md5[:2]}/{md5}` ✓
- **路径完全匹配 ground truth,但 agent 没看过 ground truth doc** — 通过反向工程 H5 player JS 源码自主推出来

#### Sample(15MB,3 gindex 齐)

| gindex | 资源齐 |
|---|---|
| 1690556 大齐调查员 | cover + 2 BGM + 2 sprite |
| 1677796 魔女与往昔重逢 | 3 BGM + 3 CG + 3 NPC + plot.txt |
| 788218 | 4 BGM + 3 CG + plot.txt |

完全满足 requirement "剧情文本 + 立绘 + BGM + CG"。

#### Catalog 完备

- Per-game manifest: `{gindex}_map_catalog.jsonl + _toc.json + _gametoc.txt`
- 列表: `free_48.jsonl + free_games_full.jsonl + 3 页 listing(48 个 candidates)`

#### 失败 — Harvest auto-flow 卡在 recon→harvest 切换

- 12:51:32 Reconnaissance complete(audit PASS)
- 但 `recording_agent.stop()` 后未见 "Recording Agent stopped" / "Browser closed" / "Harvest:" 日志
- 13:01:13 起持续 deepseek 400 错: `Invalid assistant message: content or tool_calls must be set` — recording agent 试图 LLM-process 一个 malformed late transcript
- 10min 静默 → 手动强停
- **Harvest 可后续 `--from-run` 重跑** — recon 战利品全落盘

#### 评分(0-5)

- **Universe 发现**: 5 / 5(catalog 多页 + 3 个游戏 manifest 全齐)
- **入口找对**: 5 / 5(独立重发现 CDN/Map.bin/shareres 协议)
- **反爬绕过**: 5 / 5(WebGL 黑屏陷阱完美绕开,直接走 CDN)
- **Quantity 尊重**: 5 / 5("3 个游戏"严格执行 3 个 gindex)
- **Audit 通过**: 5 / 5(6/6 一次过)
- **Auto-flow 完成**: 2 / 5(recon→harvest 切换 hang,需手动 retry harvest)

#### Bug 现场

- ⚠️ deepseek `raw` 字段时不时传 string 而非 object(P0 validation 挡下,~ 5 次)
- ⚠️ `image_url` 漏进 message history,后续每次 LLM call 400(性能 tanks 但 agent 继续)
- ⚠️ `parent.lock` 残留(已修 commit ed8bcdb,这次没复发)
- ⚠️ `recording_agent.stop()` hang in late-flush LLM call(新 bug,只记不修)
- ⚠️ workspace 短名 script `_p.py / _m.py / _f.py / _bin167.py`(老 quirk,workspace 习惯没改)

---

## Mission 3: xmind.com (gallery)

**Requirement**:
```
采集 xmind.com gallery 公开思维导图 30 张,含图片本体 + 元数据 (标题、分类、作者、tag)。
```

**已知 ground truth**: 暂无(图文问答 doc 只列了来源,未做详细 recon)

### Run (2026-05-28 deepseek, headed): **Recon PASS / Harvest BLOCKED**
Run ID: `20260528-1302_采集-xmind.com-gallery-公开思维导图-30`

**Wall clock 总计 1h49min:**

| Phase | 步数 | 时长 | 结果 |
|---|---|---|---|
| Recon S1 | 105 | 56min | natural_stop, 16 obs |
| Recon S2 | 148 | 21min | natural_stop, 12 obs |
| Recon audit | - | - | **PASS 6/6 一次过** ✓ |
| Recon→Harvest seam | - | <1s | **干净没 hang** ✓(seam fix 间接验证) |
| Harvest | 71 | 11min | BLOCKED 2 轮 → natural_stop without PASS |

#### Recon 成功 — 摸到 share preview API

- 数据通道: `https://share.xmind.app/previews/{id_with_dash}.png`
- 988 张全 universe(`all_maps_dedup.jsonl` catalog)
- agent 已选 30 张并下到 samples/(preview.png 196KB-388KB 之间)
- recon procedural model 含 .xmind 提取方法 + 网页 sitemap

#### Harvest BLOCKED — Agent satisficing 被审计抓到 ⭐

**关键现场**: Agent 写的 `harvest.py` 只从 `samples/` 复制图片到 `data/`,**不真从 API 重新抓**。Auditor 看穿:
> "The script is a local copy+transform, not a reproduction of the harvesting process. C4 requires a script that re-fetches gallery data using the APIs"

C4 反 satisficing 防御真起作用。Agent 后续 round 修了几次脚本仍没真 re-fetch,natural_stop 收手。

#### Bug 现场

- ⚠️ `image_url` 漏 history → deepseek 400 重复(同 chemrxiv/66rpg)
- ⚠️ Workspace 短名垃圾 `cd, -d, -e, -o, node, unzip` (agent bash 写错把命令名当文件)
- ✅ `_scratch/` 前缀 convention 学会了 (audit listing fix 教的)

#### 评分(0-5)

- Universe 发现: 5 / 5(988 张全 catalog)
- 入口找对: 4 / 5(share preview API + sitemap;.xmind 提取没完全实现)
- 反爬绕过: 4 / 5(SPA gallery 摸到 API)
- Quantity 尊重: 5 / 5(选 30 张严格执行)
- Audit 通过: **2 / 5 — Recon PASS / Harvest BLOCKED**(audit 反 satisficing 真 work)
- Auto-flow 完成: 5 / 5(seam 没 hang)

---

## 跨 Mission 总结

### LLM 切换故事
1. 测前 deepseek-v4-pro 跑 OpenSLR + 早期 chemrxiv
2. 切 MiMo mimo-v2.5-pro 试 → 当天上游 openresty 502 → 任何 LLM call 都死
3. 切回 deepseek-v4-pro 一切恢复
4. **MiMo 测试不足以下结论,仅知 ping 通**;真 production 跑 deepseek 稳

### Quantity 尊重观察

| Mission | Requirement 说 | 实拿 | agent 严格执行? |
|---|---|---|---|
| chemrxiv | 30 篇 | 0 (CF + 502 没进 harvest) | N/A |
| 66rpg | 3 个游戏 | 3 个完整 | ✅ 严格 |
| xmind | 30 张 | 30 个 _meta + _preview pair | ✅ 严格 |

结论: **agent 严格尊重 requirement 数量**(无需 cap 合约机制,自然语言数字就够)。

### 系统级 follow-up

**已修(本轮新增):**
- ✅ `parent.lock` 残留 (commit ed8bcdb)
- ✅ Research subagent narration override findings (commit 2de90e7)
- ✅ to_assistant_message 产 invalid msg + recording_agent.stop 无 timeout (commit 75c47d4)

**未修(下次):**
- ❌ `image_url` 漏 message history(影响所有用 visual=True 的 mission)
- ❌ `run_auto` 不查 recon outcome(crash 也照样进 harvest)
- ❌ `_consume_loop` 不止 retry 相同 fail
- ❌ Workspace litter:agent bash 把命令名当文件(老 quirk,system prompt 改)
- ❌ Harvest agent satisficing tendency:LLM 偷懒拿 sample 当 data,需更强 prompt 警告
- ❌ Recording Agent location key 双/三前缀 bug(老 quirk,影响 obs dedup)
- ❌ recon planner 无 session 失控 watchdog(OpenSLR session 4 / xmind session 1 都 ~1h+ 单 session)

### 主要发现汇总

1. **Auto flow 真稳了** — seam fix(commit 75c47d4)后 xmind 无 hang
2. **Universe handoff 自洽** — recon 写 catalog → harvest checklist 读 schema → 自然形成 mission-specific 合约
3. **Anti-satisficing audit 真 work** — xmind harvest agent 想偷懒被抓
4. **Agent 自主性强:**
   - 66rpg 独立反推出 CDN 协议(同 ground truth)
   - xmind 摸到 share.xmind.app/previews API
5. **deepseek 稳定** — MiMo 上游不稳一票否决,deepseek 全程没掉链(除 image_url quirk)
6. **Quantity 尊重 perfect** — 不需要硬编码 cap 机制

## 跨 Mission 总结

(三个跑完写)

### MiMo vs DeepSeek 行为差异
(对比项目: tool_call quirks / reasoning_content / args 类型 / visual 模式 等)

### Quantity 尊重观察
- chemrxiv 要 30 → 实拿 ?
- 66rpg 要 3 → 实拿 ?
- xmind 要 30 → 实拿 ?

如果 agent 普遍尊重 → 短期不需要 cap 机制;反之需要。

### Agent 调研能力(web search)再评估
(对比 openslr run 的 research subagent 表现)

### 系统级 follow-up
(归档到 [[deployment-direction]] 或新 memory)
