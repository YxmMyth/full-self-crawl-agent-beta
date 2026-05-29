# Full-Self-Crawl Agent

**English** | [中文](./README-CN.md)

LLM-driven web reconnaissance + harvest agent. Give it a domain and a
natural-language data requirement; it autonomously explores the site,
maps its structure into a World Model, writes a reusable harvest script,
and produces the actual dataset.

```bash
python -m src.main auto openslr.org \
  "采集 OpenSLR 上的小语种 (low-resource, 非英语/中文/印欧大语种) 语音数据集,
   含音频文件 + 转写 + 元数据 (SLR 编号、语种、时长、许可证、采样率)。" \
  --no-gate
```

No URL patterns, no XPath selectors, no schema files — describe *what
you want* in plain language; the agent figures out the *how*, validates
its own output against a per-mission acceptance contract, and hands you
both a sample dataset and a runnable script.

---

## What this is (and isn't)

**It is:**

- A two-stage agent system: a **Recon** stage explores the site and
  builds a structured World Model + catalog + samples; a **Harvest**
  stage takes that handoff and produces the actual at-scale dataset,
  with an LLM auditor gating "done" claims against the on-disk evidence.
- **Domain-agnostic** by design — no hardcoded URL patterns, site
  classifications, or data schemas. Every operating assumption is
  discovered at runtime.
- **Mission-specific contracts** — for each run, a checklist is
  compiled from the requirement + catalog at harvest start, hash-pinned
  to prevent the agent from cheating, and used as the per-criterion
  acceptance contract by the audit.

**It isn't:**

- A scraper builder — there's no rule-config layer to author.
- A black-box service — everything the agent produces (catalog, samples,
  transcripts, scripts, audit verdicts) is on disk for human inspection.
- A magic site-bypass tool — for login walls, CAPTCHAs, and aggressive
  WAFs, it knows when to ask for help via desktop popup (`request_human_assist`)
  rather than silently fail.

---

## Quick Start

### Prerequisites

- **Python 3.10+**
- **PostgreSQL 16** (local install or any reachable instance — Neon /
  Supabase / cloud PG all work; the agent stores the World Model + run
  metadata here).
- **Camoufox** — a Firefox-based stealth browser, installed via pip
  below. Auto-downloads a ~250MB binary on first use.
- An OpenAI-compatible LLM endpoint (DeepSeek, MiMo, Qwen, GLM, GPT,
  Claude — anything that speaks `/v1/chat/completions`).

### Install

```bash
git clone <repo-url> full-self-crawl-agent
cd full-self-crawl-agent

# 1. Python deps
pip install -r requirements.txt

# 2. Camoufox browser binary (~250MB, one-time)
camoufox fetch

# 3. Database
psql -c "CREATE DATABASE recon_agent;"
psql recon_agent < src/world_model/schema.sql

# 4. Configuration
cp .env.example .env
# then fill in LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, DATABASE_URL
```

### Run

```bash
# Recon + Harvest in one shot, no human gate between stages:
python -m src.main auto <domain> "<requirement>" --no-gate

# Or run stages separately:
python -m src.main explore <domain> "<requirement>"            # recon only
python -m src.main harvest <domain> --from-run <run_id>        # harvest only
```

---

## Architecture

```
┌─────────────────────── Recon Stage ──────────────────────┐
│                                                          │
│   ReconPlanner (tool-use agent, 5 tools)                 │
│     ├─ spawn_execution ──► Execution Agent Session       │
│     │                        ├─ 14 tools (browse, fetch, │
│     │                        │   bash, click, ...)       │
│     │                        └─ pushes increments ────►  │
│     │                                       Recording   │
│     │                                       Agent ────► │
│     │                                       (singleton) │
│     │                                       maintains   │
│     │                                       Observations│
│     ├─ spawn_research  ──► Research Subagent             │
│     │                        (web_search, web_fetch,     │
│     │                         bash, think)               │
│     ├─ read_model      ──► load Semantic + Procedural    │
│     ├─ think           ──► no side effects               │
│     └─ mark_done       ──► triggers Recon Audit          │
│                            (6 criteria, LLM single-call) │
│                                                          │
│   on PASS ────────────────────────────────────────────►  │
└──────────────────────────────────────────────────────────┘
                              │ catalog/, samples/, WM
                              ▼
              ┌─── optional human gate ───┐
              │  edit requirement.txt or  │
              │  abort before harvest     │
              └────────────┬──────────────┘
                           ▼
┌────────────────── Harvest Stage ─────────────────────────┐
│                                                          │
│   Launcher compiles checklist.md from requirement +      │
│   catalog + samples + procedural model (hash-pinned).    │
│                                                          │
│   Harvest Agent Session                                  │
│     ├─ 14 recon tools + apply_patch + mark_done          │
│     ├─ Reads checklist before doing anything             │
│     ├─ Writes data/ + crawler script + state.json        │
│     └─ Calls mark_done → Harvest Audit                   │
│                            (loads checklist, evaluates   │
│                             each criterion against disk) │
│                                                          │
│   PASS ────────────────► mission done                    │
│   BLOCKED ──────────────► agent iterates, re-mark_done   │
└──────────────────────────────────────────────────────────┘
```

**Data architecture (3 layers, immutability rules):**

| Layer | Source of Truth | Mutability |
|-------|----------------|------------|
| **Transcripts** | Each session's full LLM dialogue (JSONL) | Append-only, never edited |
| **Observations** | Recording Agent's structured per-location notes | CRUD by Recording Agent only |
| **Models** (Semantic + Procedural) | LLM-distilled understanding of the site | Full rewrite per session by maintain_model |

See `docs/WorldModel设计.md` for the rationale.

---

## Real-World Examples

### 1. OpenSLR — low-resource language speech data

```bash
python -m src.main auto openslr.org \
  "采集 OpenSLR 站上小语种(non-English/Chinese/major-IE) 语音数据集,
   含音频文件 + 转写 + 元数据 (SLR 编号、语种、时长、许可证、采样率)。" \
  --no-gate
```

Recon discovers `/resources.php` catalog + `info.txt` metadata format,
identifies the `dlcdn1.cgyouxi.com` mirror as more reliable than the
main host, and writes `catalog/low_resource_candidates.json` with 61
filtered datasets. Harvest runs a resumable script with retry +
streaming download + post-hoc verification.

### 2. 66rpg.com (Orange Light) — text-game asset bundles

```bash
python -m src.main auto 66rpg.com \
  "采集 66rpg.com 文字游戏 3 个,每个含完整剧本资源:
   剧情文本 + 立绘 + BGM + CG。" \
  --no-gate
```

The agent independently reverses the H5 player's CDN protocol via
JavaScript inspection — `/web/{guid}/{ver}/Map.bin` for the resource
manifest, `/shareres/{md5[:2]}/{md5}` for individual assets — bypassing
the WebGL-rendered front-end entirely.

### 3. xmind.com Gallery — public mind maps

```bash
python -m src.main auto xmind.com \
  "采集 xmind.com gallery 公开思维导图 30 张,
   含图片本体 + 元数据 (标题、分类、作者、tag)。" \
  --no-gate
```

Recon walks the SPA via API discovery (`share.xmind.app/previews/{id}.png`),
catalogs 990 maps in `all_maps_dedup.jsonl`, and selects 30 for harvest
based on the requirement quantity.

---

## Output

Each run is isolated under its own directory:

```
artifacts/{domain}/runs/{run_id}/
├── samples/        ★ Primary data — actual files (audio / pdf / source / etc.)
├── catalog/        Indexes, listings, API metadata — recon's universe handoff
├── workspace/      Harvest agent's work area: scripts, checklist, errors.log
│                   └── data/        Harvested dataset (the deliverable)
├── transcripts/    Per-session LLM dialogue (JSONL, append-only)
├── sessions/       Per-session traces + screenshots
├── research/       Research subagent reports
├── verification/   Audit reports + per-criterion verdicts
├── requirement.txt The mission text
└── strategy_report.md  Planner's final strategy summary
```

The browser's persistent profile lives separately at
`artifacts/_profiles/{domain}/` and is shared across runs — log in
once, future runs of the same domain reuse it.

---

## Configuration (`.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `LLM_API_KEY` | yes | OpenAI-compatible API key |
| `LLM_BASE_URL` | yes | Endpoint, e.g. `https://api.deepseek.com/v1` |
| `LLM_MODEL` | yes | Model name, e.g. `deepseek-v4-pro` |
| `DATABASE_URL` | yes | PostgreSQL connection string |
| `VISION_LLM_MODEL` | no | Multimodal model for `browse(visual=True)`; default `Doubao-Seed-2.0-pro` |
| `BROWSER_WS_URL` | no | Remote Camoufox WebSocket URL |
| `BROWSER_CDP_URL` | no | Remote Chromium CDP URL |
| `RECON_HEADLESS` | no | Set to `1` to force headless recon (default headed) |
| `ARTIFACTS_DIR` | no | Override artifacts root path (default `./artifacts`) |
| `MAX_PLANNER_TOOL_CALLS` | no | Recon planner safety net (default 200) |
| `MAX_SESSIONS` | no | Recon planner session cap (default 15) |

The agent has been verified against the following endpoints:

- DeepSeek official API
- DeepSeek via company gateway (proxy)
- Xiaomi MiMo Token Plan (`mimo-v2.5-pro`)
- Doubao (`Doubao-Seed-2.0-pro`) for vision
- Claude (`claude-opus-4-7`) for vision

---

## Tested Domains

| Site | Stage Tested | Outcome | Notes |
|------|------|---------|-------|
| `openslr.org` | recon + harvest | recon PASS 6/6, harvest in-progress | 61 datasets cataloged, ~2.7GB samples |
| `66rpg.com` (橙光) | recon | PASS 6/6 | Agent independently reverse-engineered CDN protocol |
| `xmind.com` | recon + harvest | recon PASS 6/6, harvest blocked (audit caught satisficing) | 30 maps + previews delivered |
| `chemrxiv.org` | recon | Did not complete | Hit Cloudflare Turnstile; agent escalated to `request_human_assist` |
| `codepen.io` | recon | PASS | Earlier MVP test, SPA + public code |
| `douyin.com` | recon | PASS | Earlier MVP test, login-required content |

See `docs/three_missions_observations.md` for the detailed run-by-run
findings.

---

## Documentation

Design docs live under `docs/` and are organized by area:

| Doc | Area |
|-----|------|
| `CLAUDE.md` | Implementation blueprint — hard architectural constraints |
| `docs/Planner设计.md` | Planner tool-use loop, 5 tools, decision policy |
| `docs/AgentSession设计.md` | Execution agent loop, stop conditions, microcompact |
| `docs/WorldModel设计.md` | 3-layer data architecture rationale |
| `docs/工具重新设计共识.md` | Per-tool design notes for the 14 execution tools |
| `docs/抽象边界原则.md` | Agent vs Infrastructure boundary — what to expose / hide |
| `docs/SystemPrompts设计.md` | System prompt structure per agent layer |
| `docs/three_missions_observations.md` | Latest E2E run log — 3 missions (chemrxiv / 66rpg / xmind) |
| `docs/部门部署架构_local-recon_server-harvest.md` | Deployment direction discussion |
| `docs/agent_stress_test_candidates.md` | Candidate stress-test targets sourced from internal needs |

A Chinese README is also available: [README-CN.md](./README-CN.md).

---

## Status

MVP, actively iterating. Core pipeline (recon → handoff → harvest →
audit) is verified end-to-end. Known limitations and follow-up bugs
are tracked in `docs/three_missions_observations.md`.

Built and tested primarily on Windows 11 + Python 3.14 with Camoufox
135 / 150. The headed-browser hang historically tied to the Camoufox
binary turned out to be an orphaned `parent.lock` from psutil-killed
sessions; fixed in commit `ed8bcdb`.

License: not yet specified.
