"""Recon completion audit — read-only auditor agent (replaces the single-call LLM).

Two phases:
  Phase 1 — mechanical sanity (cheap, deterministic pre-gate: empty WM, no
            locations, no samples). NOT a judge — it just fails fast on obviously
            incomplete recon before spending the agent. Keeping it is fine; it
            never decides the semantic question.
  Phase 2 — read-only auditor AGENT (src/audit/agent.py): fresh context, reads
            the World Model's claims + the ACTUAL sample/catalog content, judges
            L1-L4 + universe against the pinned intent kernel's evidence-standard,
            quotes original text, returns PASS / FAIL / UNCERTAIN.

The agent reads file CONTENT (it can tell a real sample from a synopsis), which
the old listing-only single-call audit structurally could not — that blind spot
is what let kungal's plot summaries pass as game scripts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from src.audit.agent import run_audit_agent
from src.config import Config
from src.llm.client import LLMClient
from src.utils.logging import get_logger
from src.world_model import db

logger = get_logger(__name__)


# ── Phase 1 thresholds (mechanical sanity) ────────────────

_MIN_SEMANTIC_CHARS = 200
_MIN_PROCEDURAL_CHARS = 200
_MIN_LOCATIONS = 3
_MIN_ARTIFACT_FILES = 1  # samples/ + catalog/ combined


# ── Recon criteria (folded into the auditor agent's briefing) ──────

_RECON_CRITERIA = """## Recon completion criteria (C1-C6) — THIS is your ruler

Recon succeeds when harvest can take over without re-discovering basics.
These six criteria define "exploration-complete", NOT "requirement-fulfilled".

C1 — Site structure (L1): the semantic model describes URL PATTERNS and how
     pages connect ("/posts/{id}", "/api/v2/items?cursor="), not just single URLs.
C2 — Data distribution (L2): each significant location has an observation of
     WHAT data lives there (type, fields, rough volume) — field names / sample
     shape, not just "has items".
C3 — Requirement mapping (L3): the World Model explicitly maps the requirement's
     target data to a location/pattern/endpoint. If the requirement asks for X
     and nothing says where X lives, C3 fails.
C4 — Proof samples are the RIGHT KIND (the kungal check — do this by READING):
     samples/ holds real primary-data instances whose CONTENT matches the intent
     evidence standard (see below). Open them. A file that is the wrong kind of
     thing (a plot summary when the deliverable is dialogue; a listing/metadata
     when the deliverable is the artifact) is a FAIL even if its size/format look
     fine. catalog/ entries (lists, IDs, URL maps) are NOT samples — they prove
     WHERE data is, not THAT the real thing was extracted.
C5 — Scalability: the procedural model documents a REUSABLE method (API+cursor,
     a fetch script, a generalizing browser_eval snippet) — not "click each item".
C6 — Universe (harvest hand-off): catalog/ enumerates the universe (every entity
     in scope), OR procedural explicitly marks it "unbounded" with the enumeration
     method. Empty catalog/ + silent procedural = FAIL.

End your verdict with these two lines (in this order):
UNIVERSE: bounded=<true|false|unknown> | size=<N or unbounded or unknown> | catalog=<catalog/<file> or none>
OVERALL: PASS|FAIL|UNCERTAIN"""


# ── Result dataclasses ────────────────────────────────────


@dataclass
class CriterionResult:
    """Kept for back-compat with report writers; the agent reports per-criterion
    inside its free-text `report`, so this list is usually empty now."""
    id: str
    verdict: str
    evidence: str = ""
    reason: str = ""


@dataclass
class UniverseInfo:
    bounded: bool = False
    size_estimate: str = "unknown"
    catalog_pointer: str = "(none)"


@dataclass
class AuditResult:
    overall: str  # PASS | FAIL | UNCERTAIN
    phase: str  # "mechanical" | "agent"
    report: str = ""  # the auditor agent's full verdict text
    blocking_summary: str = ""
    mechanical_failures: list[str] = field(default_factory=list)
    criteria: list[CriterionResult] = field(default_factory=list)
    universe: UniverseInfo = field(default_factory=UniverseInfo)
    raw_llm_response: str = ""

    def feedback(self) -> str:
        """Human-readable feedback string for the planner."""
        if self.overall == "PASS":
            return "Audit PASS."
        parts = [f"Audit {self.overall} (phase: {self.phase})."]
        if self.mechanical_failures:
            parts.append("Mechanical sanity failures:")
            for f in self.mechanical_failures:
                parts.append(f"  - {f}")
        if self.report:
            parts.append(self.report)
        elif self.blocking_summary:
            parts.append(self.blocking_summary)
        return "\n".join(parts)


# ── Phase 1 implementation ────────────────────────────────


async def _run_mechanical(domain: str) -> list[str]:
    """Mechanical sanity checks. Returns a list of failure messages (empty = pass)."""
    failures: list[str] = []

    semantic, procedural = await db.load_both_models(domain)
    if not semantic or len(semantic) < _MIN_SEMANTIC_CHARS:
        failures.append(
            f"semantic.md is empty or under {_MIN_SEMANTIC_CHARS} chars "
            f"(actual: {len(semantic) if semantic else 0})."
        )
    if not procedural or len(procedural) < _MIN_PROCEDURAL_CHARS:
        failures.append(
            f"procedural.md is empty or under {_MIN_PROCEDURAL_CHARS} chars "
            f"(actual: {len(procedural) if procedural else 0})."
        )

    locations = await db.list_locations(domain)
    if len(locations) < _MIN_LOCATIONS:
        failures.append(
            f"World Model has only {len(locations)} location(s); "
            f"need at least {_MIN_LOCATIONS}."
        )

    run_dir = Config.run_dir(domain)
    artifact_count = 0
    for sub in ("samples", "catalog"):
        d = run_dir / sub
        if d.is_dir():
            artifact_count += sum(1 for p in d.iterdir() if p.is_file())
    if artifact_count < _MIN_ARTIFACT_FILES:
        failures.append(
            f"samples/ + catalog/ contain {artifact_count} file(s); "
            f"need at least {_MIN_ARTIFACT_FILES} (recon must demonstrate "
            "at least one successful extraction or listing)."
        )

    return failures


# ── Phase 2 implementation (auditor agent) ────────────────


def _gather_artifact_listing(domain: str) -> str:
    """Format on-disk file inventory so the agent knows what to open."""
    run_dir = Config.run_dir(domain)
    lines: list[str] = []
    for sub in ("samples", "catalog", "scripts"):
        d = run_dir / sub
        lines.append(f"### {sub}/")
        if not d.is_dir():
            lines.append("  (directory does not exist)")
            continue
        files = sorted(p for p in d.iterdir() if p.is_file())
        if not files:
            lines.append("  (empty)")
            continue
        for p in files[:30]:
            try:
                size = p.stat().st_size
            except Exception:
                size = -1
            lines.append(f"  - {p.name} ({size} bytes)")
        if len(files) > 30:
            lines.append(f"  ... ({len(files) - 30} more files)")
    return "\n".join(lines)


def _read_intent_kernel(domain: str) -> str:
    p = Config.run_dir(domain) / "intent_kernel.md"
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else ""
    except Exception:
        return ""


def _parse_universe(report: str) -> UniverseInfo:
    """Best-effort parse of the agent's 'UNIVERSE: bounded=.. | size=.. | catalog=..'
    line for the strategy report. Missing/garbled → unknown (gate still works)."""
    if not report:
        return UniverseInfo()
    m = re.search(r"UNIVERSE\s*:\s*(.+)", report, re.IGNORECASE)
    if not m:
        return UniverseInfo()
    line = m.group(1)
    bounded = bool(re.search(r"bounded\s*=\s*true", line, re.IGNORECASE))
    size = "unknown"
    sm = re.search(r"size\s*=\s*([^|]+)", line, re.IGNORECASE)
    if sm:
        size = sm.group(1).strip() or "unknown"
    catalog = "(none)"
    cm = re.search(r"catalog\s*=\s*([^|]+)", line, re.IGNORECASE)
    if cm:
        catalog = cm.group(1).strip() or "(none)"
    return UniverseInfo(bounded=bounded, size_estimate=size, catalog_pointer=catalog)


def _build_recon_briefing(
    domain: str,
    requirement: str,
    intent_kernel: str,
    semantic: str,
    procedural: str,
    artifact_listing: str,
) -> str:
    evidence_standard = intent_kernel.strip() or (
        f"(no compiled intent kernel — fall back to raw requirement)\n{requirement}"
    )
    return (
        "# Recon completion criteria (YOUR RULER — exploration readiness for harvest)\n\n"
        "Recon's job is NOT to fulfil the requirement — it is to EXPLORE enough "
        "that harvest can take over without re-discovering basics. Judge against "
        "the L1-L4 criteria below, NOT the full requirement.\n\n"
        f"{_RECON_CRITERIA}\n\n"
        f"# Original requirement (the ultimate harvest goal — context only)\n{requirement}\n\n"
        "# Intent evidence standard (reference for C4 sample authenticity ONLY)\n"
        "Use this ONLY to judge whether proof samples are the RIGHT KIND of thing "
        "(real dialogue vs synopsis, real artifact vs metadata). Do NOT use it as "
        "the overall completion bar — that is C1-C6 above.\n\n"
        f"{evidence_standard}\n\n"
        "# The World Model's CLAIMS (verify against real evidence — do not "
        "take them on faith; the worker wrote them)\n\n"
        f"## Semantic model\n{semantic or '(empty)'}\n\n"
        f"## Procedural model\n{procedural or '(empty)'}\n\n"
        "# On-disk artifacts (OPEN these with read_file — sizes are not proof)\n"
        f"{artifact_listing}\n\n"
        f"Run dir layout: samples/, catalog/, scripts/, workspace/ are all under "
        f"the run root. Domain: {domain}.\n\n"
        "Investigate now. For C4 especially, OPEN sample files and read their "
        "content — decide if they are the right KIND of thing per the evidence "
        "standard, not just whether files exist."
    )


# ── Public entrypoint ─────────────────────────────────────


_audit_round = 0


async def run_audit(
    llm: LLMClient,
    domain: str,
    requirement: str,
    mark_done_reason: str,
) -> AuditResult:
    """Phase 1 (mechanical) gates Phase 2 (auditor agent). Returns an AuditResult
    with overall ∈ {PASS, FAIL, UNCERTAIN}. Writes a report under
    verification/round_{n}.md."""
    global _audit_round
    _audit_round += 1
    round_num = _audit_round

    # Phase 1 — mechanical sanity (fail fast, no agent spend)
    mechanical_failures = await _run_mechanical(domain)
    if mechanical_failures:
        result = AuditResult(
            overall="FAIL",
            phase="mechanical",
            blocking_summary=(
                "Mechanical sanity failed before the auditor agent. "
                "Address these basics first."
            ),
            mechanical_failures=mechanical_failures,
        )
        _write_report(domain, round_num, result, mark_done_reason)
        logger.info(
            f"Audit round {round_num}: FAIL at mechanical phase "
            f"({len(mechanical_failures)} failures)"
        )
        return result

    # Phase 2 — read-only auditor agent
    intent_kernel = _read_intent_kernel(domain)
    semantic, procedural = await db.load_both_models(domain)
    listing = _gather_artifact_listing(domain)
    briefing = _build_recon_briefing(
        domain, requirement, intent_kernel, semantic or "", procedural or "", listing
    )

    overall, report = await run_audit_agent(llm, Config.run_dir(domain), briefing)
    result = AuditResult(
        overall=overall,
        phase="agent",
        report=report,
        raw_llm_response=report,
        universe=_parse_universe(report),
    )
    _write_report(domain, round_num, result, mark_done_reason)
    logger.info(f"Audit round {round_num}: {overall} (auditor agent)")
    return result


def _write_report(
    domain: str, round_num: int, result: AuditResult, mark_done_reason: str,
) -> None:
    """Write the audit report markdown under verification/."""
    try:
        ver_dir = Config.run_dir(domain) / "verification"
        ver_dir.mkdir(parents=True, exist_ok=True)

        lines = [
            f"# Audit Round {round_num}",
            "",
            f"**Overall:** {result.overall}",
            f"**Phase:** {result.phase}",
            "",
            "## Planner's mark_done reason",
            "",
            mark_done_reason or "(none)",
            "",
        ]
        if result.mechanical_failures:
            lines.append("## Mechanical sanity failures")
            lines.append("")
            for f in result.mechanical_failures:
                lines.append(f"- {f}")
            lines.append("")
        if result.report:
            lines.append("## Auditor verdict")
            lines.append("")
            lines.append(result.report)
            lines.append("")

        (ver_dir / f"round_{round_num}.md").write_text(
            "\n".join(lines), encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to write audit report round {round_num}: {e}")
