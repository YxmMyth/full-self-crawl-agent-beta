"""Research Subagent — investigates the internet to answer specific questions.

Spawned by Planner via spawn_research. Has 4 tools:
web_search, web_fetch, bash, think.

No browser access. Pure HTTP + search.

See: docs/工具重新设计共识.md §2.2b, docs/SystemPrompts设计.md §五
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.config import Config
from src.llm.client import LLMClient
from src.utils.logging import get_logger

logger = get_logger(__name__)

_SYSTEM_PROMPT = """You are a research specialist for a web reconnaissance system. \
You investigate via HTTP and web search — you have no browser, \
so you cannot render JavaScript or interact with pages.

You receive a research topic and specific questions from the planner. \
These questions arise from ongoing reconnaissance — your findings \
will directly inform the next exploration steps.

## How to Research

1. PLAN BEFORE SEARCHING. Use think() to break the topic into \
2-4 specific search angles. What exactly do you need to find?

2. SEARCH ITERATIVELY, NOT ONCE. First round: broad searches to \
map the landscape. Second round: targeted searches based on \
what you learned. Follow promising leads deeper.

3. READ CAREFULLY. Use web_fetch() on the best sources. Extract \
specific facts — endpoints, parameters, auth methods, rate limits. \
Don't just skim titles.

4. REFLECT AFTER EACH ROUND. Use think() to assess: \
What questions are now answered? What gaps remain? \
What new questions emerged? Is another search round needed?

5. BUILD ON WHAT'S GIVEN. The planner may provide context about \
what's already been discovered. Don't re-research known facts.

6. FOLLOW THE CHAIN. One source often references another — API docs \
link to changelogs, blog posts reference GitHub repos, forums \
cite documentation. Follow these chains for authoritative answers.

## Output

State your key findings clearly:
- Confirmed facts (with source URLs)
- Relevant code examples, API signatures, or configuration details
- Questions that remain unanswered despite research
- Suggested next steps for the execution agent"""


# ── Tools ────────────────────────────────────────────────

_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web via DuckDuckGo. Returns titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "domain": {"type": "string", "description": "Limit to this domain (site: syntax)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a URL via HTTP (no browser/JS). HTML is converted to Markdown. "
                "Good for docs, blog posts, API references. Won't work for JS-rendered pages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command. Use for data processing or curl requests.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "think",
            "description": "Reason about your findings before the next step.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought": {"type": "string", "description": "Your reasoning."},
                },
                "required": ["thought"],
            },
        },
    },
]


# ── Tool handlers ────────────────────────────────────────

async def _handle_web_search(query: str, domain: str | None = None) -> str:
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            full_query = f"site:{domain} {query}" if domain else query
            results = list(ddgs.text(full_query, max_results=8))

        if not results:
            return "No results found."

        lines = []
        for r in results:
            lines.append(f"**{r.get('title', '')}**")
            lines.append(f"  URL: {r.get('href', '')}")
            lines.append(f"  {r.get('body', '')[:200]}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Search error: {e}"


async def _handle_web_fetch(url: str) -> str:
    try:
        import httpx
        from markdownify import markdownify

        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})

        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            return f"[JSON, {len(resp.text)}B]\n{resp.text[:5000]}"
        elif "html" in ct:
            md = markdownify(resp.text)
            if len(md) > 8000:
                md = md[:8000] + "\n\n[truncated]"
            return md
        else:
            return resp.text[:5000]
    except Exception as e:
        return f"Fetch error: {e}"


async def _handle_bash(command: str, workspace: Path) -> str:
    import asyncio
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(workspace),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode("utf-8", errors="replace")
        if len(output) > 10000:
            output = output[-10000:]
        return f"{output}\n[exit code: {proc.returncode}]"
    except Exception as e:
        return f"Error: {e}"


# ── Main agent loop ──────────────────────────────────────

async def run_research(
    llm: LLMClient,
    domain: str,
    topic: str,
    questions: str,
) -> dict[str, str]:
    """Run the Research Subagent.

    Returns:
        {key_findings, report_path}
    """
    research_dir = Config.run_dir(domain) / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    workspace = Config.run_dir(domain) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Sanitize topic for filename
    safe_topic = "".join(c if c.isalnum() or c in "-_ " else "_" for c in topic)[:50].strip()
    report_path = research_dir / f"{safe_topic}.md"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Research topic: {topic}\n\nQuestions:\n{questions}"},
    ]

    max_rounds = 15
    findings_text = ""

    for _ in range(max_rounds):
        response = await llm.chat_with_tools(messages, _TOOLS_SCHEMA)
        if response is None:
            break

        # Build assistant message
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if response.content:
            assistant_msg["content"] = response.content
            findings_text = response.content  # last content = final findings
        if response.tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                for tc in response.tool_calls
            ]
        messages.append(assistant_msg)

        if not response.tool_calls:
            break

        # Execute tools
        for tc in response.tool_calls:
            if tc.name == "web_search":
                result = await _handle_web_search(tc.arguments.get("query", ""), tc.arguments.get("domain"))
            elif tc.name == "web_fetch":
                result = await _handle_web_fetch(tc.arguments.get("url", ""))
            elif tc.name == "bash":
                result = await _handle_bash(tc.arguments.get("command", ""), workspace)
            elif tc.name == "think":
                result = tc.arguments.get("thought", "")
            else:
                result = f"Unknown tool: {tc.name}"

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})

    # Save report
    report_content = findings_text or "(No findings produced)"
    report_path.write_text(report_content, encoding="utf-8")
    logger.info(f"Research report saved to {report_path}", extra={"domain": domain})

    # Extract key findings (first 500 chars of final content)
    key_findings = findings_text[:500] if findings_text else "Research produced no findings."

    return {
        "key_findings": key_findings,
        "report_path": str(report_path.relative_to(Config.ARTIFACTS_DIR)),
    }
