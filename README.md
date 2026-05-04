# Full-Self-Crawl-Agent

LLM-driven web reconnaissance agent. Give it a domain and a natural-language
requirement; it autonomously explores the site, discovers data sources,
collects samples, and builds a structured Model of how the site works.

```bash
python src/main.py "ui8.net" "采集 5 个 free UI Kit 完整文件"
```

No URL patterns, no XPath selectors, no schema definitions — you describe
*what you want* in plain language, and the agent figures out the *how*.

---

## What this is (and isn't)

**It is:**
- A multi-layer agent system: a Planner directs Execution Agents that
  autonomously navigate browsers + run scripts + call APIs, while a
  Recording Agent maintains a structured World Model in the background
  and a Verification Subagent gates "done" claims.
- Designed for **understanding + sampling**, not exhaustive extraction.
  The output is a few representative samples + a Model that documents
  how to get the rest.
- Headed-browser-first: agents share a real Camoufox window with you
  so you can see what they're doing and step in (login, CAPTCHA,
  manual verification) when they ask.

**It isn't:**
- A scraper builder — there's no rule-config layer to write.
- A headless server-side worker — the design assumes you (or a person)
  is around to handle login walls. The `request_human_assist` tool pops
  a desktop dialog when the agent needs you.
- Plug-and-play in CI — see "Why no Docker" below.

---

## Setup

### Prerequisites

- **Python 3.10+**
- **PostgreSQL 16** (local install or any reachable instance — Supabase
  / Neon / cloud PG all work). The agent stores the World Model + run
  metadata here.
- **Camoufox** (a Firefox-based stealth browser, installed via pip below
  — also auto-downloads a ~250MB browser binary on first use).
- A desktop environment (the agent shows a real browser window and uses
  Tkinter for human-assist popups; pure-headless servers are not the
  target environment).

### Install

```bash
git clone <repo-url> full-self-crawl-agent
cd full-self-crawl-agent

# 1. Python deps
pip install -r requirements.txt

# 2. Camoufox browser binary (~250MB, one-time)
camoufox fetch

# 3. Database
#    (a) make sure PostgreSQL is running and accessible
#    (b) create the database and load the schema:
psql -c "CREATE DATABASE recon_agent;"
psql recon_agent < src/world_model/schema.sql

# 4. Configuration
cp .env.example .env
#    then edit .env with your LLM credentials
#    (see "Configuration" section below)
```

### Configuration

Copy `.env.example` to `.env`. Required:

| Variable | Description |
|----------|-------------|
| `LLM_API_KEY` | Your OpenAI-compatible API key |
| `LLM_BASE_URL` | Endpoint, e.g. `https://api.deepseek.com/v1` |
| `LLM_MODEL` | Model name, e.g. `deepseek-chat` or `deepseek-v4-pro` |
| `DATABASE_URL` | PostgreSQL connection string |

Optional ones documented in `.env.example`.

---

## Run

```bash
python src/main.py "<domain>" "<natural-language requirement>"
```

Examples:

```bash
python src/main.py "ui8.net" \
  "采集 5 个 free UI Kit 完整文件"

python src/main.py "codepen.io" \
  "找出 codepen.io 上 webgl 相关的优质代码,采集 5 个样本"
```

The agent launches a Camoufox window. As it runs, it may pop up
desktop dialogs asking you to handle login / CAPTCHA / 2FA — these
are the only times it needs you. You can scan a QR or type credentials,
then the agent observes the new page state and continues.

---

## Output

Each run is isolated under its own directory:

```
artifacts/{domain}/runs/{run_id}/
├── samples/        ★ Primary data — the actual deliverable files
│                   (zip / pdf / source code / images / full text)
├── catalog/        Indexes, listings, API metadata about samples
│                   (NOT primary data; recon notes only)
├── workspace/      Exploration / debug / scratch
├── transcripts/    Full LLM conversation per session (JSONL)
├── sessions/       Per-session traces + screenshots
├── research/       Research subagent reports
└── verification/   Verification subagent reports + verdict
```

The browser's persistent profile (cookies, login state, localStorage)
lives separately at `artifacts/_profiles/{domain}/` and is shared
across runs — log in once, future runs of the same domain reuse it.

---

## Why no Docker

The repo previously had a `Dockerfile` + `docker-compose.yml` from an
earlier vision. They've been removed because they conflict with how the
agent actually works:

- **Tkinter human-assist popup needs a host display** — no desktop in a
  container, no popup.
- **Camoufox stealth depends on real OS context** — running it inside a
  Linux container changes the fingerprint surface in ways anti-bot
  systems can detect (cgroup/namespace traces, Linux-vs-Windows
  inconsistencies).
- **Per-domain profile persistence** would have to bridge container
  filesystem and the human's interactive login on the host — fragile.

Run the agent natively. If you don't want to install PostgreSQL on your
host, you can run *just* PG in Docker:

```bash
docker run -d --name recon-pg \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=recon_agent \
  -p 5432:5432 \
  postgres:16

# Then load schema:
psql -h localhost -U postgres recon_agent < src/world_model/schema.sql
```

---

## Architecture

For the design rationale behind the agent layers, the World Model, and
the abstraction-boundary principle that informs what's agent-visible vs
infrastructure-internal:

| Doc | What it covers |
|-----|----------------|
| `CLAUDE.md` | Implementation blueprint — module layout, technical constraints, hard architectural rules |
| `docs/抽象边界原则.md` | Agent vs Infrastructure boundary — when to expose information, when to hide it (cookies, tabs, model quirks) |
| `docs/Planner设计.md` | Planner: top-level tool-use agent, 6 tools, decides when to spawn execution / research / mark done |
| `docs/WorldModel设计.md` | The 3-layer data architecture: Transcripts → Observations → Models |
| `docs/工具重新设计共识.md` | Per-tool design notes for the 14 execution agent tools |
| `docs/SystemPrompts设计.md` | System prompt structure for each agent layer |

---

## Development

```bash
# Run tests (lightweight, no actual browser/LLM calls)
# (test scripts live under scripts/ for now)

# Run a smoke test of the LLM gateway
python -c "import asyncio; from src.llm.client import LLMClient; asyncio.run(LLMClient().chat_with_tools([{'role':'user','content':'hi'}], []))"

# Inspect the World Model after a run
psql recon_agent -c "SELECT id, pattern FROM locations WHERE domain = 'ui8.net' ORDER BY id;"
```

Migration scripts (one-time DB or filesystem migrations) live in
`scripts/` — typically you don't run them; they're run-once helpers
for schema upgrades during development.

---

## Status

MVP. Tested against codepen.io (code samples), ui8.net (UI Kit
extraction), douyin.com (login + content scraping). The system is
domain-agnostic — there are no hardcoded site assumptions, but each
new site type may surface new edge cases worth documenting in the
World Model.

License: not yet specified.
