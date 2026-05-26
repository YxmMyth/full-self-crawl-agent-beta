# OpenSLR Recon+Harvest E2E 观察日志

> 日期: 2026-05-26
> Domain: openslr.org
> Mode: `auto --no-gate` (recon → 自动 harvest, 无人审 gate)

## Requirement (传给 planner 的)

```
采集 OpenSLR 站上小语种(low-resource,非英语/中文/印欧大语种)的语音音频
数据集,需包含:音频文件本身 + 转写文本(若有) + 元数据(SLR 编号、语种、
总时长、许可证、采样率)。
```

## 启动配置

- 命令: `python -m src.main auto openslr.org "<requirement>" --no-gate`
- LLM: deepseek-v4-pro (gateway)
- 浏览器: Camoufox headed (recon 阶段) → headless (harvest 阶段)
- Per-domain profile: `artifacts/_profiles/openslr.org/`
- 起始时间: 2026-05-26 10:46:30
- Run ID: `20260526-1046_采集-OpenSLR-站上小语种(low-resource,`
- Background bash ID: `b1gawi26z`
- 日志: `artifacts/openslr_run.log`

## 观察记录(实时追加)

### Phase 1: Recon

**T+0 (10:46:30)** — Run started
- ⚠️ **run_id 异常**: slug 含中文 + 逗号 + 左括号,在 Windows 路径里能 work 但 lexicographic 排序怪;后续 `_resolve_source_run_id` 还能匹配吗?盯下
- DB 已连(localhost:5432/recon_agent)

**T+5min (10:51:29)** — ❌ Browser launch 180s timeout, run died
- Launched pid=1788, Juggler pipe 起来了,Dispatcher/BrowserHandler 创建成功,Installed 1 addon ✓
- 然后卡住,180s 后 Playwright timeout
- 关键报错: `JavaScript error: chrome://juggler/content/Helper.js:82: NS_ERROR_FAILURE [nsIWebProgress.removeProgressListener]`
- **CLEAN STATE** — 之前 0 个 camoufox/firefox 残留进程,所以 ≠ GPU 残留导致(之前的 hypothesis 错了)
- 同 harvest 改 headless 前撞的一模一样的错,根因没真正调查清楚
- 用 `-foreground` 也被 Firefox warn "unrecognized command line flag",但这不是 fatal
- 跑到 `Installed 1 addon(s)` 后什么也不再发生,Playwright 握手永远不完成

### 决策点 → 选 1 (headless workaround)

加 `RECON_HEADLESS` env var(`main.py`),不动默认值,opt-in。重启 run。

**T+~6min** — Relaunch with `RECON_HEADLESS=1` (attempt 2)
- Background bash ID: `b69stf6cm`
- 浏览器 OK(headed=False),Planner 起来,做了 ~6 分钟 web_search(spawn_research 估计)
- **11:15:47 后日志静默**,python 进程消失,run_dir 只有 requirement.txt
- 没有任何 "Reconnaissance complete" / "Fatal error" / "Interrupted" 日志 → 进程被外部 kill
- **怀疑 Bash tool 默认 120s timeout 把 background 也 kill 了**(`run_in_background` 行为待验证)

## ⭐ 阶段总结 (T+~3h, 14:34)

Run 仍 alive(python PID 35848 + 7 Camoufox 进程,3+ 小时未死)。
**Session 1-7 完成,session 8 在跑**。Recon 一直没 mark_done,planner 在 satisficing 防御 — 持续要求更多 sample/verification。

**Samples/ 收获 ~1.6 GB primary data**(recon 阶段就拉了):
- SLR22 Uyghur THUYG-20: resource.tar.gz 26MB + transcripts/ + audio/
- SLR30 Sinhala: 260MB tar + audio/ + transcripts/
- SLR36 Sundanese: 280B partial
- SLR42 Khmer: 50MB partial zip
- SLR122 Kashmiri: 3× partial(断点续传反复)
- SLR137 Lithuanian: fragments.zip 232MB + words.zip 210MB + sentences.zip 87MB
- SLR149 Tibetan greeting: 372MB tgz + 多个 partial

**catalog/ 输出完备:**
- low_resource_candidates.json (61 候选,结构化)
- openslr_full_catalog.json
- 70+ × SLRXX_info.txt
- low_resource_summary.md + summary.txt

**Session 时间线:**
| Sess | 起 | 止 | 步 | 时长 | 终止 |
|---|---|---|---|---|---|
| s001 | 11:27:32 | 11:35:17 | 71 | 405s | natural_stop + flush timeout |
| s002 | 11:36:55 | 11:48:18 | 136 | 623s | natural_stop |
| s003 | 11:52:08 | 12:09:48 | 129 | 1060s | natural_stop |
| s004 | 12:13:38 | 13:54:00 | 170 | **6013s (100min!)** | **context_exhausted** |
| s005 | 13:57:15 | ~ | ? | ? | (无日志,可能 natural) |
| s006 | ~14:20 | 14:29:43 | 92 | 599s | natural_stop, maintain_model 12 步, sem=20231 chars |
| s007 | 14:31:44 | 14:34:24 | 36 | 156s | natural_stop, maintain_model 无 edit |
| s008 | 14:34:42 | running | - | - | - |

**发现 5 — Session 4 失控**
- 100 分钟、170 步、context_exhausted 才停
- 12:15~13:54 几乎只 update 同几个 obs(#1305, #1310, #1254, #1310, #1312)
- 推断: agent 卡在某个下载循环 / 一直在试 SLR137 + SLR149 的不同 partial 但没新发现
- planner 没 watchdog 拦,跑到 context 满才停

**发现 6 — Cross-host universe**
- agent 发现 `openslr.trmal.net::/resources/137/`(Povey mirror 子域),记入 obs #1310
- 系统设计 domain-agnostic,正确处理

**发现 7 — deepseek `raw` 字段时不时传 string 而非 dict**
- P0 validation 挡下(`expected object, got str`),返回 LLM 错误反馈让它 retry
- 防御层工作正常

**发现 8 — agent 选址精准**
- 已下载样本几乎全是低资源(Uyghur/Tibetan/Lithuanian/Kashmiri/Khmer/Sundanese/Sinhala)
- 没浪费带宽下英文/中文大语种

---

**T+~30min (11:20:21)** — Relaunch with explicit `timeout: 3600000` (attempt 3, b53a4lfod)
- `python -u`(unbuffered), 新日志: `artifacts/openslr_run2.log`
- 11:20:54 browser launched ✓
- 11:21:03 Recording Agent + Planner started ✓

**T+6min (11:27:32)** — First execution session `s001_350ab2` spawned
- Planner 用了 6 分钟 spawn_research(连续 web_search ~30 次,扫 openslr 历史/反爬/格式情报)
- 然后才 spawn_execution。**第一个 session 之前的 LLM 调用很贵** — 5+ rounds research

**T+7-10min** — Agent 探索 /resources 列表页 + 建模 URL 模板
| Obs | Location | 含义 |
|---|---|---|
| #1231 | `openslr.org::/resources` | catalog 入口 |
| #1232 | `openslr.org::/` | 主页 |
| #1233 | `openslr.org::/resources.php` | catalog PHP backing |
| #1234 | `openslr.org::openslr.org::/{id}` | per-SLR 短链模板 |
| #1235 | `openslr.org::openslr.org::/resources/{id}` | per-SLR 长链模板 |
| #1236 | `openslr.org::openslr.org::/resources/{id}/info.txt` | 🎯 metadata 文件模板 |

**发现 1 — Recording Agent location 名重复前缀 bug**
- 应该是 `openslr.org::/{id}` 但写成 `openslr.org::openslr.org::/{id}`
- Agent 把完整 URL `openslr.org/{id}` 当 location_path 传,Recording Agent 没去重
- 不阻塞,但 catalog/Model 里 location 名会显得脏

**发现 2 — `info.txt` 是 metadata 通道**
- requirement 要的 "SLR编号/语种/时长/许可证/采样率" 都在 `/resources/{id}/info.txt`
- Agent 自主发现,非常关键 — 后续 harvest 不用爬 HTML 拼装

**发现 3 — Visual mode 不可用(11:30:38)**
- `browse(visual=True)` → LLM 400: `"unknown variant 'image_url', expected 'text'"`
- deepseek-v4-pro 是纯文本模型,gateway 不收 multimodal
- Tool 自动回退 text-only(`Visual mode failed:` warning),不阻塞
- 影响: agent 只能靠 HTML/Markdown 文本理解页面;长期看应配支持 vision 的模型或禁掉 visual=True 参数

**发现 4 — Recon Research 阶段非常贵**
- 6 分钟纯 web_search,30+ queries
- 触发了 5+ 个搜索引擎(brave/yahoo/duckduckgo/yandex/mojeek/wiki/grokipedia)
- 收益要看后续 session 是否真用了这些 finding;若 agent 直接 fetch openslr 也能得知,这段时间是浪费

### Phase 2: Harvest

(待 recon 完成后追加)

## 最终总结 (用户在 harvest 中段手动终止)

**胜利条件 (用户定): recon→harvest handoff 工作 = ✅ 达成**

### 端到端时间线

| 阶段 | 起止 | 时长 | 结果 |
|---|---|---|---|
| Recon (8 sessions) | 10:46 → 14:49 | ~4h(含 2 次 launch 失败 + 重启) | audit PASS 6/6 |
| Harvest | 14:49 → 15:0X | ~30min(被手动停)| 61/61 candidates iterated,~10/61 完整 archive |

### Recon 产出 (audit PASS)

- `catalog/low_resource_candidates.json` — **61 候选,结构化 schema**(slr/name/languages/family/region/license/transcriptions)
- `catalog/openslr_full_catalog.json` — 全 162 资源目录
- `catalog/SLRXX_info.txt` × 70+ — per-resource metadata 缓存
- `catalog/low_resource_summary.md` + `summary.txt`
- `samples/` — **2.7GB primary data**,6 个数据集真音频(SLR22/30/42/122/137/149)+ format 实测
- Procedural model: `curl -L --max-time 600 https://openslr.trmal.net/resources/{id}/{file}`(mirror discovery)
- Strategy report: `strategy_report.md`

### Harvest 产出 (停在中段)

- `workspace/checklist.md` — 5 criteria, hash-pinned, 真读 catalog schema 生成 mission-specific contract
- `workspace/crawl.py` — 412 行 resumable + Apache HTML parser + per-file retry + CLI flags
- `workspace/data/` — **637MB,61 个 SLR 目录全建**(SLR1/6 是 agent bash 测试余物,不在 candidates;其余 59 都在)
- `workspace/state.json` — agent 跑了 metadata-only + 部分 full,completed 列表含全 61 candidates
- `workspace/data/harvest_summary.json` — C5 audit artifact 已写
- `workspace/errors.json` — 空(无下载失败)

### 主要发现汇总

1. **Browser headed launch 仍 hang**(同 harvest 之前一样,Camoufox + Python 3.14 + Juggler removeProgressListener bug)。Workaround: `RECON_HEADLESS=1` env var,recon 也加了。Root cause 没查
2. **Bash 默认 120s timeout 可能 kill background run**(attempt 2 silent death 推测原因)。后续显式 `timeout: 3600000`
3. **Recon Session 4 失控 100min context_exhausted** — planner 无 watchdog 拦下载循环
4. **Recording Agent location key 双/三前缀 bug**(`openslr.org::openslr.org::openslr.org::/{id}`),滚雪球,dedup 异步才修
5. **Workspace 垃圾文件**:agent bash 写错把命令名当文件名(`cp`/`echo`/`ls`/`mkdir`/`-d`/`-p`),recon 留 ~8 个,harvest 没清(也不该清)
6. **Visual mode 不可用**:deepseek-v4-pro 纯文本,`browse(visual=True)` → 400,自动回退 text-only(预期)
7. **deepseek `raw` 字段类型混乱**:P0 strict validation 挡下,返 LLM 错误反馈(预期)
8. **Recon 写真 sample 到 `samples/`**:架构原本是 sample 在 recon 阶段就该收,这次实测 confirm(2.7GB)
9. **Recon→Harvest handoff 仅靠 disk + DB,无显式契约文件**,但 LLM compile checklist 时真去读 catalog schema → 自然形成契约
10. **Audit PASS 真实**:每条 criterion 都有具体 disk evidence(不是 satisficing 一句话)
11. **state.json append-without-dedup 是小 bug**:`setdefault().append()` 应改 `set` 或 `if not in`
12. **Harvest 双进程**:agent 同时跑 `crawl.py`(全下)+ `crawl.py --metadata-only`(占位),独立 bash 子进程 — 显示 agent 自己做了并行编排

### 系统级评价

| 维度 | 评价 |
|---|---|
| **架构正确性** | ⭐⭐⭐⭐⭐ 两阶段隔离 + universe handoff + checklist 都按设计运作 |
| **代码质量(agent 写的 crawl.py)** | ⭐⭐⭐⭐ 412 行 resumable script,无 fatal bug |
| **效率** | ⭐⭐ recon 4h(简单站),session 4 跑飞,planner 无防御 |
| **可靠性** | ⭐⭐⭐ browser headed bug 拒绝深查;deepseek args quirks 已挡 |
| **可观察性** | ⭐⭐⭐⭐ Recording Agent + log 充分,obs key 命名 bug 影响小 |

### 下次跑应该改的事

- [ ] **必修**: Recon planner 加 watchdog,N 个 session 都 natural_stop 没新发现就 force mark_done(避免 session 4 飞)
- [ ] **必修**: Recording Agent location key normalize,去重 domain 前缀
- [ ] **应修**: Browser headed launch 真 root cause(Camoufox / Python 3.14 / Juggler 哪儿来的)
- [ ] **建议**: agent 系统 prompt 加 "workspace/ 写文件用 `>` 别让 bash 命令名变文件名"
- [ ] **建议**: state.json / errors.json append 改 set 语义
- [ ] **可选**: Bash tool background 默认 timeout 改长,或文档化 "需要长跑要显式 timeout"

---

## 已知风险/我会盯的点

- **小语种 vs 大语种判定**: requirement 没列白名单/黑名单, agent 怎么 reason? 是逐 SLR 看 README, 还是按 OpenSLR 自有分类?
- **Universe 边界**: 162 个资源里有多少符合"小语种"? recon 应写到 catalog 让 harvest 能 verify 覆盖
- **音频实际下载量**: 全量小语种音频可能 10s of GB. headless 默认下载, 磁盘/网络压力
- **OpenSLR 反爬**: 应该温和, 但 SLR 编号枚举若过快会 ban
- **deepseek args quirks**: 历史有 80+ 次 schema fail, P0 strict 验证应该挡住
- **checklist 编译**: 当前 catalog 可能没"小语种" identifier 字段, audit 怎么判定 universe coverage?
