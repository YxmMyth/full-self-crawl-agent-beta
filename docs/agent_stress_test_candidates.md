# Agent Stress Test 候选清单 — XiaoSuData 真实数据需求

> 日期: 2026-05-26
> 用途: 从 XiaoSuData 里挑能**逼出 agent 弱点**的网站 + 数据需求,装作未知喂给 agent
> 评估维度:
> - **难度**: agent 现在可能挂在哪
> - **Ground Truth**: 我们已有的"正确答案"(用于评分 agent 表现)
> - **数据价值**: 对真实产线重要性
> - **Universe**: 有上限 / 无上限

---

## 一、Agent 已知弱点(测试时盯紧)

| 弱点 | 触发场景 | 关键候选 |
|---|---|---|
| 🔴 **第一眼 403 就放弃** | 主站 / sitemap 全 403,需绕 backend API | ChemRxiv / zbMATH |
| 🔴 **死域名认死理** | 给的 domain 是死站,真 catalog 在别的 host | icho-official.org / compadre.org / perimeterinstitute |
| 🔴 **不会反爬升级** | TCP RST / TLS fingerprint | GeoGebra / ChemRxiv |
| 🔴 **SPA 摸不到数据层** | 浏览器内 JS API 隐藏,不爬 HTML | XMind Gallery / 即梦 / JiMeng |
| 🔴 **Visual 内容看不见** | 数据本身是图(无 OCR / vision)→ deepseek 纯文本 image_url 400 | 图文问答全 11 类 Maps / Mind Map |
| 🟡 **被表面 robots 吓退** | robots 禁 AI 但 API 仍可用 | Codeforces / AoPS / ChemRxiv |
| 🟡 **过度信任 desk research** | 原表说"十万级"实际是万级 | phys.libretexts / OpenStax / MAA |
| 🟡 **错过非显眼入口** | 不是 sitemap / 不在主导航 | MIT OCW PDF / Stack Exchange dump |
| 🟡 **PDF URL 不在 sitemap** | 隐藏在 resource HTML 里,带 hash | MIT OCW |
| 🟡 **API 限速太慢循环卡死** | 600s/req 之类 | Physics World / phys.aps.org |
| 🟡 **失败下载反复 retry** | 已观察(OpenSLR Kashmiri × 3 partial) | 任何大文件下载 |
| 🟢 **跨域 universe 处理** | 主站 + mirror 都是 universe | OpenSLR(已通过)|
| 🟢 **多 part 归档** | .zip + .z01-.zNN | OpenSLR 高号 SLR |

---

## 二、推荐候选清单(按测试梯度)

### 🟢 Tier 1: Sanity Check(预期 agent 1 次过)

| Domain | 数据需求(给 agent 原文) | Ground Truth | 预期表现 |
|---|---|---|---|
| `projecteuler.net` | "采集 Project Euler 全部题目的题面 HTML、题号、难度、解决人数" | 987 题,`/problem=N`,1.2s/req,纯 HTML,无反爬,LaTeX 含在 MathJax script | 应该 30min 完事,recon 出 URL 模板,harvest 全量 |
| `metamath.org` | "采集 Metamath 全部形式化证明定理库 (含 statement + proof)" | 单文件 set.mm 49MB,47319 个定理,GitHub 镜像 `metamath/set.mm` | 应该发现 GitHub 路径,直接 git clone 或 wget tar |
| `hyperphysics.phy-astr.gsu.edu` | "采集 HyperPhysics 物理概念页全集" | 静态 HTML,无反爬,IA 镜像也有 | 经典抓取,无坑 |

### 🟡 Tier 2: 质疑能力(测 agent 是否 empirical)

| Domain | 数据需求(给 agent 原文) | Ground Truth | 测什么 |
|---|---|---|---|
| `phys.libretexts.org` | "采集 Phys LibreTexts 全部教材章节 HTML(物理 / 含公式)" | sitemap 19525 URL(不是"十万"),0.6s/req(不需 5s 礼让),MathJax 公式嵌 | agent 是否实测 sitemap 大小 / 速率,还是按 robots crawl-delay 死板 |
| `icho.sk` 或 `icho-official.org` | "采集 IChO 国际化学奥赛 1968-至今全部赛题 + 解答 PDF" | icho-official 是死站(虽 200 OK 但空内容),真档在 `icho.sk`,**只 28 个 PDF**(每个是 20 届合集) | agent 能不能跳过死站找到 icho.sk;能不能识别"实际只 28 个,远少于原表暗示" |
| `imo-official.org` | "采集 IMO 国际数学奥赛 1959-至今全部题面 PDF(多语)" | `/assets/documents/problems/<year>/<year>_<lang>.pdf`,~67 年 × 6 题 × N 种语言 | 多语种 universe enumerate,可能需 hreflang 类逻辑 |
| `chem.libretexts.org` | "采集 Chem LibreTexts 全部教材章节 HTML" | sitemap 100,204 URL(十万级),crawl-delay 5s 实测 0.6s 也行 | 跟 phys.libretexts 一样,但量级是真十万 — 测 agent 估算时间 + 决定是否 sample |

### 🔴 Tier 3: 真硬骨头(逼 agent 多次尝试)

| Domain | 数据需求(给 agent 原文) | Ground Truth | 测什么 |
|---|---|---|---|
| `chemrxiv.org` | "采集 ChemRxiv 化学预印本全集(元数据 + PDF 全文)" | 本站 API / sitemap / paperscraper **全 403** ,真路径 `https://www.cambridge.org/engage/coe/public-api/v1/items?term=CHEMRXIV` | 🎯 经典 "不被 403 吓退 + 找 backend"。agent 大概率挂在这,跑成功就是质的飞跃 |
| `codeforces.com` | "采集 Codeforces 全部 problems(题面 + tags + 通过数)" | API(REST)+ HTML 双通道,HTML 浏览器 UA 即可 200,robots 禁 AI 但不影响技术,11206 题 | 测 agent 不被 robots ai-train=no 吓退 + 不误判 Cloudflare |
| `geogebra.org` | "采集 GeoGebra 全部 materials 元数据 + GGB 文件" | 连续访问 **TCP RST**,需慢速 + UA rotation;~75 万 materials | 反爬升级测试 — agent 能否识别 TCP RST 不是 timeout 而是 banned |
| `maa.org` | "采集 MAA 历年 AMC / AIME 题目 PDF" | 公开通道**无索引**,wp-content 路径无规律,brute force 300 URL 命中 0 | 测 agent 何时承认拿不到 — satisficing 防御 |
| `zbmath.org` | "采集 zbMATH 数学评论元数据" | Cloudflare 403,需走官方 REST API + OAI-PMH | 类似 ChemRxiv 但是另一个领域 |
| `cmap.ihmc.us` | "采集 CmapTools 公开 concept maps + 标题 + 元数据" | 文档说 "Public Servers" 复数,真实结构未知 — 没人做过 ground truth | **真未知 + agent 能力上限测试**(我们也学不到东西) |
| `xmind.com` (gallery) | "采集 XMind Gallery 公开 mind map 图片 + 标题 + 类别" | SPA + 图片密集,无 ground truth | 测 SPA 探测能力 + visual mode(MiMo 多模态可能终于能用)|

### 🟣 Tier 4: 高价值但需特殊准备(暂不优先)

| Domain | 为什么不优先 |
|---|---|
| `audible.com` | DRM 加密,技术不可行(已调研) |
| `66rpg.com`(橙光) | 已完整解决方案,recon 无意义 |
| Anthropic news 类 | 需登录 / VPN / 已有手写 scraper |
| Mozilla Common Voice | 需注册账号 + 30 次/天限额,体验差 |
| `audacityteam.org` 等小众 | 数据价值有限 |
| LinkedIn (CompanyCrawl) | 登录强制 + 法律灰色,已有专门 scraper |

---

## 三、按数据领域的真实需求清单(给采购 / 部门看的)

> 这部分是 XiaoSuData 里已识别的所有数据需求,不限于 agent 测试

### A. STEM 学科(来源 `STEM/STEM_调研.md` + `STEM/samples/_SUMMARY.md`)

**已完成手工 recon 的 20 个必拿站(可作 oracle)**:
metamath, IMC, AoPS(skip), IMO, projecteuler, math.stackexchange, mathoverflow, MAA, Wolfram Alpha(skip), GeoGebra, arXiv, MIT OCW, OpenStax, codeforces, physics.stackexchange, hyperphysics, phys.libretexts, IChO, chem.libretexts, chemrxiv

**已有手工调研结论的 13 个想拿 + 7 个备选**(`STEM_调研.md` "想拿"+"备选"两个表):
encyclopediaofmath / planetmath / ncatlab / zbmath / cut-the-knot / reference.wolfram / khanacademy / usaco / kattis / physicsforums / compadre / phys.aps.org / perimeterinstitute(实际 pirsa.org)
- 备选: openreview / nasa.gov 数据 / Github STEM repos / 等

### B. 小语种 / 多语种语音(来源 `小语种音频调研/调研报告.md`)

| 资源类 | 站点示例 | Universe | 价值 |
|---|---|---|---|
| **学术 dataset** | OpenSLR(已测)、Mozilla Common Voice、FLEURS、Voxpopuli、MLS、MMS、CMU Wilderness | 286 语 / 数万小时 | ★★★ 商用许可宽 |
| **印度全家桶** | AI4Bharat IndicVoices / Kathbath / Shrutilipi / BhasaAnuvaad | 22 印度语 / 6 万-44万小时 | ★★★ 印度市场必拿 |
| **非洲专题** | DSFSI / Masakhane / NCHLT / SADiLaR / BibleTTS | 50+ 非洲数据集 | ★★ |
| **东南亚专题** | SEACrowd | 498 数据集 / 980 SEA 语 | ★★ |
| **美洲原住民** | AILLA / AmericasNLP | 400+ 美洲语 / 7500h | ★ |
| **国家级广播** | VOA / RFA / RFE-RL / BBC WS / DW / RFI / NHK | 49 语 / 持续更新 | ★★ 注意 USAGM 风波 |
| **Radio Browser** | api.radio-browser.info | 4 万+ 电台流 / 372 语 | ★ 风险高(各电台版权) |
| **Tatoeba 音频** | tatoeba.org | 400+ 短句 | ★ |

### C. 图文问答 Maps(来源 `图文问答/图文问答需求最终整理.md`)

**11 类 Maps × 各 10-20w 数据需求,总计百万级**:

| 类型 | 数量 | 目标站 | Agent 难点 |
|---|---|---|---|
| Weather Map | 20w | NOAA / AccuWeather Historical | 图片下载 + 时间维度 |
| **Mind Map** | 20w | **XMind Gallery** | SPA + 图片 |
| Thematic | 20w | World Bank / Our World in Data / NASA SEDAC | 多源拼装 |
| Land use | 20w | USGS NLCD | 地图瓦片 |
| Environmental | 20w | Global Forest Watch / NASA Earth Observatory | 同上 |
| Astronomical | 10w | Stellarium / ESO Archive | 专业接口 |
| **Concept** | 10w | **CmapTools Public Servers** | 多 server 发现 |
| Crime | 10w | CrimeMapping.com / Citizen App | 反爬严 |
| Migration | 10w | UN Migration Data Portal | 报表 PDF |
| Road / Route | 各 10w | OSM / Google Maps | API 限速 + 协议 |

### D. 其他(零散)

| 来源 | 站点 | 状态 |
|---|---|---|
| `文字游戏/调研报告_路径D.md` | 66rpg.com(橙光) | 已完整解决 |
| `audible/CLAUDE.md` | audible.com | 不可行(DRM) |
| `依托大边/` | 各国新闻站 ~50 个(news.csv) | 已有手写 scraper |
| `CompanyCrawl/` | LinkedIn | 已有 scraper |
| `JiMeng/` | 即梦 AI 视频(?) | 有 auth.json/cookies.db,登录依赖,需 recon |

---

## 四、测试时给 agent 的需求文本模板

格式建议(我们历次测试的 best practice):
- **完全质性,无数字**(让 recon 自己定 universe)
- **明确包含什么**(audio / text / metadata 等字段)
- **不暗示路径**(让 agent 探索)

### 模板示例

```
采集 {domain} 上 {内容描述},需包含:{字段 1}、{字段 2}、{字段 3}。
```

**示例 - projecteuler:**
```
采集 projecteuler.net 全部数学题目,需包含:题号、题面 HTML、难度等级(若有)、解决人数(若有)。
```

**示例 - icho.sk(故意写 icho-official 看 agent 能不能纠正):**
```
采集 icho-official.org 国际化学奥赛历年题目 + 解答(PDF),需包含:年份、届数、题目语言、文件本体。
```

**示例 - chemrxiv:**
```
采集 chemrxiv.org 化学预印本全集,需包含:title、authors、abstract、DOI、subject、PDF 全文。
```

---

## 五、推荐测试顺序

| Round | Domain | 目的 | 时长估算 |
|---|---|---|---|
| 1 | `projecteuler.net` | MiMo + pipeline sanity | recon 30min + harvest 30min |
| 2 | `icho.sk` 或故意写 `icho-official.org` | 错域名纠正能力 | recon 30min + harvest 10min(只 28 PDF) |
| 3 | `phys.libretexts.org` | 质疑 desk research | recon 1h + harvest 跳过(量大)|
| 4 | `chemrxiv.org` | 真硬骨头:绕 403 找 backend | recon 2h+,可能失败 |
| 5 | `xmind.com` gallery | SPA + 测 MiMo 多模态 | recon 1-2h |
| 6 | `cmap.ihmc.us` | 真未知 + 多 server 发现 | recon 2h+ |

---

## 六、评分标准(测完每个 mission 用)

| 维度 | 满分项 |
|---|---|
| **Universe 发现** | catalog/ 含 ground truth 内 entity 数 ≥ 80%,且不偏 |
| **入口找对** | 找到我们手工调研用的真实路径(API / mirror / 等)|
| **反爬绕过** | 遇 403/RST 后能切策略而非放弃 |
| **样本质量** | samples/ 含真数据(不是 HTML 错误页)|
| **Procedural model** | 写的 curl/script 模板可执行 |
| **Audit 通过** | 6/6 criteria PASS,且 evidence 真实 |
| **效率** | wall clock < 已知手工调研的 3x |
