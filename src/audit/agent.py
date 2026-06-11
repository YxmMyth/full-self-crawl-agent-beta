"""Read-only auditor agent — the completion judge for recon and harvest.

This replaces the old single-`generate()` checklist audit. The auditor is a
fresh-context agent (it does NOT see the worker's conversation or rationale)
with READ-ONLY file tools sandboxed to the run directory. It opens the actual
produced files, judges each criterion against the mission's intent kernel
(pinned evidence-standard), quotes the supporting text verbatim, then stops
naturally — its final no-tool message IS the verdict.

Why no submit tool / no Python judge (design 终稿, 审计员设计.md):
  - A mandatory "submit_verdict" tool is exactly the deepseek tool-use deadlock
    (it investigates forever, never submits). Here the verdict is the agent's
    natural final message, like any agent finishing — no deadlock.
  - The anti-satisficing hardness is NOT Python: it's a sharp content-checkable
    checklist + a fresh independent context + forcing it to quote original text.
  - UNCERTAIN is the dead-end default — never PASS by accident.

The caller (src/verification/audit.py for recon, src/harvest/audit.py for
harvest) builds the mission-specific briefing (intent kernel + criteria +
evidence). This module is the generic loop + read-only tools + verdict parse.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.llm.client import LLMClient
from src.utils.logging import get_logger

logger = get_logger(__name__)

_MAX_ROUNDS = 14
_READ_LIMIT = 12000  # max chars returned per read_file (tail-truncate beyond)
_GREP_HITS = 60

# Pushback when the auditor declares PASS without ever opening a file. The whole
# threat model (synopsis-vs-real-deliverable) turns on reading CONTENT, so a
# verdict reached purely from the briefing's listings/numbers cannot stand.
_NO_READ_PUSHBACK = (
    "You returned PASS without ever opening a file this session (no read_file or "
    "grep). You CANNOT certify the deliverable is real without reading its CONTENT "
    "— a synopsis and a real script are identical by size and listing; only the "
    "words inside tell them apart. Open the actual files now, read enough of each "
    "to judge it against the evidence standard, quote the supporting lines, then "
    "give your verdict. If you genuinely cannot read them, OVERALL: UNCERTAIN."
)


_AUDITOR_SYSTEM_PROMPT = """You are an adversarial completion auditor. A worker agent claims its mission is done. You decide PASS / FAIL / UNCERTAIN by checking the ACTUAL produced files against the mission's intent and acceptance criteria — never the worker's say-so (you cannot even see the worker's reasoning).

You have READ-ONLY tools over the run directory: list_dir, read_file, grep, think. You cannot change anything.

Discipline:
- Treat completion as UNPROVEN. The worker WANTS to stop; your default stance is skepticism.
- The intent's EVIDENCE STANDARD is your ruler. It names what counts as the REAL deliverable AND which cheap substitutes to REJECT. Anchor every judgment on it.
- For EVERY criterion, open the actual files and find evidence in their CONTENT. Quote the supporting lines verbatim. Citing only a file's size or type does NOT prove a semantic criterion — a synopsis and a real script can have the same size and extension; you must read the words and judge what they are.
- Example of the trap you exist to catch: if the real deliverable is the player-read DIALOGUE, a file that reads as a third-person plot SUMMARY is a FAIL even when its size/format look right. Read enough of each file to tell which it is.
- Coverage / counting against a catalog: do it yourself with the tools (list_dir, grep), don't assume.
- If after genuinely investigating you cannot tell (content ambiguous, format unreadable, evidence too thin either way), return UNCERTAIN — never guess PASS.

When you have investigated enough, STOP calling tools and write your final verdict as your last message:
- Go criterion by criterion: achieved / not achieved / unsure, each with the quoted evidence you found (or the absence you found).
- Then end with EXACTLY one line, nothing after it:
  OVERALL: PASS    (every criterion is genuinely met)
  OVERALL: FAIL    (one or more criteria are not met — say which, so the worker can fix it)
  OVERALL: UNCERTAIN  (you could not establish the answer either way)

There is no submit tool. Your final message with no tool call IS the verdict."""


_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and subdirectories (with byte sizes) under a path relative to the run dir. Use '.' for the run root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path relative to run dir, e.g. 'samples', 'workspace/data', '.'."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's content (relative to run dir). Text is returned as-is (tail-truncated if huge); binary returns a note with size + leading magic bytes so you can reason about the format.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path relative to run dir."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents for a regex (relative to run dir). Returns matching 'path:line' hits. Use to count coverage or find a structural tell across many files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression."},
                    "path": {"type": "string", "description": "Dir or file to search, relative to run dir (default '.')."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "think",
            "description": "Reason about evidence before the next read. No side effects.",
            "parameters": {
                "type": "object",
                "properties": {"thought": {"type": "string"}},
                "required": ["thought"],
            },
        },
    },
]


# ── Read-only, sandboxed tool handlers ───────────────────


def _resolve(run_dir: Path, rel: str) -> Path:
    """Resolve `rel` under run_dir, refusing any path that escapes the sandbox."""
    base = run_dir.resolve()
    p = (base / (rel or ".")).resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"path '{rel}' escapes the run-dir sandbox")
    return p


def _handle_list_dir(run_dir: Path, path: str) -> str:
    try:
        p = _resolve(run_dir, path)
    except ValueError as e:
        return f"[refused] {e}"
    if not p.exists():
        return f"(path does not exist: {path})"
    if p.is_file():
        return f"{p.name} is a file ({p.stat().st_size} bytes). Use read_file."
    lines = []
    for entry in sorted(p.iterdir()):
        try:
            if entry.is_dir():
                n = sum(1 for _ in entry.iterdir())
                lines.append(f"  {entry.name}/  ({n} entries)")
            else:
                lines.append(f"  {entry.name}  ({entry.stat().st_size} bytes)")
        except Exception as e:  # noqa: BLE001
            lines.append(f"  {entry.name}  (stat failed: {e})")
    return "\n".join(lines) if lines else "(empty)"


def _handle_read_file(run_dir: Path, path: str) -> str:
    try:
        p = _resolve(run_dir, path)
    except ValueError as e:
        return f"[refused] {e}"
    if not p.is_file():
        return f"(not a file: {path})"
    try:
        data = p.read_bytes()
    except Exception as e:  # noqa: BLE001
        return f"(read failed: {e})"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        head = data[:32].hex(" ")
        return (
            f"[binary file, {len(data)} bytes; not UTF-8 text. "
            f"Leading bytes (hex): {head}]\n"
            "If the real deliverable should be readable text, a binary blob here "
            "may mean it still needs decoding — judge per the intent's evidence standard."
        )
    if len(text) > _READ_LIMIT:
        text = text[:_READ_LIMIT] + f"\n... [truncated, {len(text)} chars total]"
    return text


def _handle_grep(run_dir: Path, pattern: str, path: str = ".") -> str:
    try:
        root = _resolve(run_dir, path)
        rx = re.compile(pattern)
    except (ValueError, re.error) as e:
        return f"[refused] {e}"
    if not root.exists():
        return f"(path does not exist: {path})"
    files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
    hits: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                rel = f.relative_to(run_dir.resolve())
                hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(hits) >= _GREP_HITS:
                    hits.append(f"... [stopped at {_GREP_HITS} hits]")
                    return "\n".join(hits)
    return "\n".join(hits) if hits else "(no matches)"


# ── Verdict parse ────────────────────────────────────────


def parse_overall(text: str) -> str:
    """Extract the OVERALL verdict from the auditor's final message. Lenient
    (accepts OVERALL/VERDICT, :/=), takes the LAST occurrence. Defaults to
    UNCERTAIN — NEVER PASS by accident."""
    if not text:
        return "UNCERTAIN"
    for kw in ("OVERALL", "VERDICT"):
        m = re.findall(rf"{kw}\s*[:=]\s*\*{{0,2}}\s*(PASS|FAIL|UNCERTAIN)", text, re.IGNORECASE)
        if m:
            return m[-1].upper()
    return "UNCERTAIN"


# ── Main loop ────────────────────────────────────────────


async def run_audit_agent(
    llm: LLMClient,
    run_dir: Path | str,
    user_briefing: str,
    *,
    max_rounds: int = _MAX_ROUNDS,
) -> tuple[str, str]:
    """Run the read-only auditor agent over `run_dir`.

    Returns (overall, report) where overall ∈ {PASS, FAIL, UNCERTAIN} and report
    is the agent's full final verdict message (used as feedback to the worker).
    Hitting the round cap with no natural stop → UNCERTAIN (never PASS).
    """
    run_dir = Path(run_dir)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _AUDITOR_SYSTEM_PROMPT},
        {"role": "user", "content": user_briefing},
    ]

    report = ""
    content_reads = 0      # read_file / grep executions — proof it opened files
    nudged_no_read = False
    for _ in range(max_rounds):
        response = await llm.chat_with_tools(messages, _TOOLS_SCHEMA)
        if response is None:
            break
        messages.append(response.to_assistant_message())

        if not response.tool_calls:
            # Natural stop. But a PASS reached without ever reading a file is the
            # rubber-stamp failure: a synopsis and a real script are identical by
            # size/listing — only CONTENT distinguishes them. Nudge once to force
            # a real read; the end-of-function gate below is the hard backstop.
            if (
                parse_overall(response.text) == "PASS"
                and content_reads == 0
                and not nudged_no_read
            ):
                nudged_no_read = True
                messages.append({"role": "user", "content": _NO_READ_PUSHBACK})
                continue
            report = response.text
            break

        for tc in response.tool_calls:
            a = tc.arguments or {}
            if tc.name == "list_dir":
                result = _handle_list_dir(run_dir, a.get("path", "."))
            elif tc.name == "read_file":
                content_reads += 1
                result = _handle_read_file(run_dir, a.get("path", ""))
            elif tc.name == "grep":
                content_reads += 1
                result = _handle_grep(run_dir, a.get("pattern", ""), a.get("path", "."))
            elif tc.name == "think":
                result = a.get("thought", "")
            else:
                result = f"Unknown tool: {tc.name}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
    else:
        # Round cap with no natural stop → force one final verdict turn.
        messages.append({
            "role": "user",
            "content": (
                "You've investigated enough. Stop calling tools and write your final "
                "verdict now: criterion by criterion with quoted evidence, then a last "
                "line `OVERALL: PASS|FAIL|UNCERTAIN`. If you still can't establish it, "
                "OVERALL: UNCERTAIN."
            ),
        })
        synth = await llm.chat_with_tools(messages, [])  # empty tools = forced text
        if synth:
            report = synth.text

    overall = parse_overall(report)
    # Hard fail-closed gate (covers natural-stop-after-nudge AND round-cap paths):
    # never certify PASS if the auditor never opened a single file. This verifies a
    # boolean fact (did it call read_file/grep?), not semantics — it cannot misfire
    # on a real read, and closes the "rubber-stamp without reading" escape hatch.
    if overall == "PASS" and content_reads == 0:
        logger.warning(
            "Audit agent returned PASS with zero content reads → forcing UNCERTAIN"
        )
        overall = "UNCERTAIN"
        report = (report or "") + (
            "\n\n[auditor gate] Verdict overridden to UNCERTAIN: PASS was reached "
            "without opening any file (no read_file/grep), so completion is unproven."
        )
    if not report:
        report = "(auditor produced no verdict text)"
        overall = "UNCERTAIN"
    logger.info(
        f"Audit agent verdict: {overall} "
        f"(report {len(report)} chars, {content_reads} content reads)"
    )
    return overall, report
