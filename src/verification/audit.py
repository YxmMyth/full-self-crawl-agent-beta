"""Recon completion audit — replaces the old tool-use verification loop.

Two phases:
  Phase 1 — mechanical bash sanity (defends against the most obvious
            satisficing: empty WM, no locations, no samples). Cheap, deterministic.
  Phase 2 — LLM single-call audit of qualitative criteria (L1-L4 stages +
            scalability). Borrows codex continuation.md's evidence-by-evidence
            rigor. One generate() call, structured JSON output. No tool-use
            loop -> no deepseek "never call submit_verdict" deadlock.

Returns AuditResult with overall PASS or BLOCKED + per-criterion verdicts +
human-readable feedback for the planner.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

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


# ── Phase 2 system prompt (LLM checklist audit) ───────────

_AUDIT_SYSTEM_PROMPT = """You are auditing whether reconnaissance of a website is truly complete.

The planner has claimed mark_done. Your job is to verify the World Model and
on-disk artifacts contain enough understanding for the harvest stage to take
over without re-discovering basics.

## Completion Audit Discipline (borrowed from codex /goal)

Treat completion as UNPROVEN. Verify against the actual current state, not
intent or plausible-looking summaries.

- For every criterion below, identify authoritative EVIDENCE that would
  prove it: a specific quote from the semantic model, a procedural model
  paragraph, a file under samples/ or catalog/ or scripts/, a numeric
  count.
- Match verification scope to criterion scope. Do not use a narrow check
  to support a broad claim.
- Treat uncertain or indirect evidence as NOT achieved. Mark FAIL or PARTIAL
  rather than guess.
- Do NOT mark a criterion PASS merely because the model "talks about" the
  area — the model must contain concrete patterns, methods, or relations,
  not abstract gestures.
- The planner WANTS to stop. Your default bias is skepticism.

## The 6 Criteria

C1 — L1 Site Structure
  Semantic model describes URL patterns and how pages connect (parent/child,
  navigation paths). Not just single URLs — patterns ("/posts/{id}",
  "/api/v2/items?cursor=..."). Without patterns, scaling is impossible.

C2 — L2 Data Distribution
  Each significant location has an observation describing what data lives
  there (type, format, approximate volume, key fields). "Has items" is not
  enough — field names or sample shape required.

C3 — L3 Requirement Mapping
  The requirement names target data. The World Model contains explicit
  mapping: this requirement aspect maps to that location/pattern/endpoint.
  If the requirement asks for X and the model nowhere mentions where X
  lives, this criterion fails regardless of how much general exploration
  was done.

C4 — Sample Method Validation (qualitative coverage, not quantity)
  samples/ contains real primary-data files demonstrating the access
  methods documented in procedural model actually work end-to-end.

  WHAT COUNTS AS A SAMPLE. A sample is ONE concrete instance of the
  primary data, on disk, in its native consumable form:
    - News / blog → article full text (not headline list)
    - UI Kit / asset marketplace → the design file (.fig / .sketch / .zip)
    - Image / stock library → the image bytes (not gallery JSON)
    - Code sandbox / snippet sharing → runnable source files
    - Forum / Q&A → posts + replies (full content)
    - Documentation → full doc text

  catalog/ entries (lists, IDs, URL maps, listing JSONs) do **NOT** count
  as samples — those prove WHERE data lives, not THAT extraction works.
  100 catalog files in a fresh samples/ folder = still 0 samples. Don't
  let a healthy catalog/ paper over an empty samples/.

  Use bash to verify ACTUAL content of samples/ entries:
    - `ls -la samples/` for sizes (< 1KB is almost certainly a placeholder)
    - `file samples/*` for real format (an HTML disguise = wrong)
    - For archives: `unzip -l` or `tar tzf` to confirm structure

  COVERAGE BEAT QUANTITY. The count is not the point — the COVERAGE is.
  If procedural documents three access methods (search API / detail page
  scrape / API endpoint), samples/ should have at least one file
  demonstrating each that's load-bearing for harvest. samples/ is sketch
  grade — a few files is enough. Full-scale collection is harvest's job.

  FAIL: samples/ is empty, holds only HTML disguises / login pages, or
  holds only catalog-style listings mis-classified as samples.

C5 — Scalability
  Procedural model describes a REUSABLE method that can run at scale —
  not "manually click each item". Look for: an API endpoint with cursor/
  pagination, a fetch script template, a browser_eval snippet that
  generalizes, or a script under scripts/. "Open every page in a browser"
  is NOT scalable.

C6 — Universe Definition (the harvest hand-off)
  catalog/ contains the enumerable universe (the list of every entity
  matching the requirement scope), OR the procedural model explicitly
  marks the universe as "unbounded — gate required" and documents the
  enumeration method (cursor / page range / sitemap / API endpoint).

  PASS evidence forms:
    (a) catalog/ has a file with all candidate IDs/URLs (cursor walked
        to completion); procedural says "universe size: N at catalog/X".
    (b) procedural explicitly says "universe unbounded" with the reason
        (no total / no exhaustion API / >>10K) AND documents the
        enumeration method harvest would use. The gate handles the
        scoping decision in this case.

  FAIL: catalog/ is empty or only has partial/incidental listings AND
  procedural doesn't address universe at all. Harvest would have no
  ground truth to verify completion against.

  PARTIAL: catalog/ has data but partial (not walked to end) and
  procedural doesn't acknowledge it's partial.

## Output Format (strict)

Return ONLY a single JSON object, no prose around it:

```json
{
  "criteria": [
    {"id": "C1", "verdict": "PASS|FAIL|PARTIAL", "evidence": "...", "reason": "..."},
    {"id": "C2", "verdict": "PASS|FAIL|PARTIAL", "evidence": "...", "reason": "..."},
    {"id": "C3", "verdict": "PASS|FAIL|PARTIAL", "evidence": "...", "reason": "..."},
    {"id": "C4", "verdict": "PASS|FAIL|PARTIAL", "evidence": "...", "reason": "..."},
    {"id": "C5", "verdict": "PASS|FAIL|PARTIAL", "evidence": "...", "reason": "..."},
    {"id": "C6", "verdict": "PASS|FAIL|PARTIAL", "evidence": "...", "reason": "..."}
  ],
  "universe": {
    "bounded": true|false,
    "size_estimate": "N or 'unbounded' or 'unknown'",
    "catalog_pointer": "catalog/<file> or '(none)'"
  },
  "overall": "PASS|BLOCKED",
  "blocking_summary": "If BLOCKED: 1-3 sentences of the biggest gaps the planner must address. Empty string if PASS."
}
```

The `universe` object is extracted from C6 evidence — the strategy report
shows it to the human gate so they can decide whether to narrow scope.

Rules:
- `evidence` quotes or cites the actual artifact you read (e.g., "semantic.md
  paragraph 2 lists /pen/{id} pattern" or "samples/ has 0 files per ls -la").
- `reason` explains why this verdict given the evidence. Concrete, not
  generic.
- `overall` = PASS only if ALL 6 criteria are PASS. Any FAIL or PARTIAL
  means BLOCKED. Universe being "unbounded but documented" still PASSes
  C6 — the gate handles the scope decision later.
- `universe` is informational metadata derived from C6 — never blocks
  audit on its own, but feeds the strategy report.
- `blocking_summary` is what the planner reads first — make it actionable.
"""


# ── Result dataclasses ────────────────────────────────────


@dataclass
class CriterionResult:
    id: str
    verdict: str  # PASS | FAIL | PARTIAL
    evidence: str = ""
    reason: str = ""


@dataclass
class UniverseInfo:
    bounded: bool = False
    size_estimate: str = "unknown"
    catalog_pointer: str = "(none)"


@dataclass
class AuditResult:
    overall: str  # PASS | BLOCKED
    phase: str  # "mechanical" | "llm" | "both"
    blocking_summary: str = ""
    mechanical_failures: list[str] = field(default_factory=list)
    criteria: list[CriterionResult] = field(default_factory=list)
    universe: UniverseInfo = field(default_factory=UniverseInfo)
    raw_llm_response: str = ""

    def feedback(self) -> str:
        """Human-readable feedback string for the planner."""
        if self.overall == "PASS":
            return "Audit PASS — all 5 criteria verified against evidence."

        parts = [f"Audit BLOCKED (phase: {self.phase})."]
        if self.mechanical_failures:
            parts.append("Mechanical sanity failures:")
            for f in self.mechanical_failures:
                parts.append(f"  - {f}")
        if self.blocking_summary:
            parts.append(f"Blocking summary: {self.blocking_summary}")
        if self.criteria:
            parts.append("Per-criterion verdicts:")
            for c in self.criteria:
                if c.verdict != "PASS":
                    parts.append(f"  {c.id} ({c.verdict}): {c.reason}")
                    if c.evidence:
                        parts.append(f"    evidence: {c.evidence}")
        return "\n".join(parts)


# ── Phase 1 implementation ────────────────────────────────


async def _run_mechanical(domain: str) -> list[str]:
    """Run mechanical sanity checks. Returns list of failure messages
    (empty if all pass).
    """
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


# ── Phase 2 implementation ────────────────────────────────


def _gather_artifact_listing(domain: str) -> str:
    """Format on-disk file inventory for the LLM auditor."""
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


def _parse_llm_audit(
    raw: str,
) -> tuple[list[CriterionResult], str, str, UniverseInfo] | None:
    """Parse the LLM's JSON output. Defensive against deepseek quirks
    (markdown code fences, trailing junk, extra whitespace).

    Returns (criteria_list, overall, blocking_summary, universe_info) or
    None if unparseable.
    """
    if not raw:
        return None

    # Strip markdown code fences if present
    text = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # Find the JSON object (first { to matching })
    start = text.find("{")
    if start < 0:
        return None
    # Walk to find matching close brace
    depth = 0
    end = -1
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return None
    json_blob = text[start : end + 1]

    try:
        data = json.loads(json_blob)
    except json.JSONDecodeError as e:
        logger.warning(f"Audit JSON parse failed: {e}")
        return None

    raw_criteria = data.get("criteria") or []
    criteria: list[CriterionResult] = []
    for c in raw_criteria:
        if not isinstance(c, dict):
            continue
        verdict = str(c.get("verdict", "")).upper()
        if verdict not in ("PASS", "FAIL", "PARTIAL"):
            verdict = "FAIL"
        criteria.append(
            CriterionResult(
                id=str(c.get("id", "?")),
                verdict=verdict,
                evidence=str(c.get("evidence", "")),
                reason=str(c.get("reason", "")),
            )
        )

    overall = str(data.get("overall", "")).upper()
    if overall not in ("PASS", "BLOCKED"):
        overall = "BLOCKED"
    # Cross-check: any non-PASS criterion forces BLOCKED regardless of what
    # the LLM said overall. Defends against the LLM saying overall=PASS while
    # listing FAIL criteria (deepseek does this sometimes).
    if any(c.verdict != "PASS" for c in criteria):
        overall = "BLOCKED"

    blocking_summary = str(data.get("blocking_summary", "")).strip()

    raw_universe = data.get("universe") or {}
    if not isinstance(raw_universe, dict):
        raw_universe = {}
    universe = UniverseInfo(
        bounded=bool(raw_universe.get("bounded", False)),
        size_estimate=str(raw_universe.get("size_estimate", "unknown")).strip()
            or "unknown",
        catalog_pointer=str(raw_universe.get("catalog_pointer", "(none)")).strip()
            or "(none)",
    )
    return criteria, overall, blocking_summary, universe


async def _run_llm_audit(
    llm: LLMClient,
    domain: str,
    requirement: str,
    mark_done_reason: str,
) -> tuple[list[CriterionResult], str, str, UniverseInfo, str]:
    """Run the LLM checklist audit. Returns
    (criteria, overall, blocking_summary, universe, raw_response).
    """
    semantic, procedural = await db.load_both_models(domain)
    artifact_listing = _gather_artifact_listing(domain)

    user_msg = (
        f"# Requirement\n{requirement}\n\n"
        f"# Planner's reason for stopping\n{mark_done_reason or '(none)'}\n\n"
        f"# Current Semantic Model\n{semantic or '(empty)'}\n\n"
        f"# Current Procedural Model\n{procedural or '(empty)'}\n\n"
        f"# On-disk Artifacts (samples/ catalog/ scripts/)\n{artifact_listing}\n\n"
        f"Domain: {domain}\n\n"
        "Run the audit. Output the JSON object specified in the system prompt."
    )

    raw = await llm.generate(prompt=user_msg, system=_AUDIT_SYSTEM_PROMPT)
    raw = raw or ""

    parsed = _parse_llm_audit(raw)
    if parsed is None:
        # JSON unparseable -> blocked with explicit gap
        logger.warning("Audit LLM returned unparseable response; treating as BLOCKED")
        return (
            [],
            "BLOCKED",
            "Audit LLM returned unparseable output. Try again, or fix the verifier prompt.",
            UniverseInfo(),
            raw,
        )
    criteria, overall, blocking_summary, universe = parsed
    return criteria, overall, blocking_summary, universe, raw


# ── Public entrypoint ─────────────────────────────────────


_audit_round = 0


async def run_audit(
    llm: LLMClient,
    domain: str,
    requirement: str,
    mark_done_reason: str,
) -> AuditResult:
    """Run both audit phases. Phase 1 (mechanical) gates Phase 2 (LLM).

    Mechanical failures short-circuit — no point spending LLM tokens
    auditing an empty WM. LLM phase only runs when sanity is clean.

    Writes a report to artifacts/{domain}/runs/{run_id}/verification/round_{n}.md.
    """
    global _audit_round
    _audit_round += 1
    round_num = _audit_round

    # Phase 1
    mechanical_failures = await _run_mechanical(domain)
    if mechanical_failures:
        result = AuditResult(
            overall="BLOCKED",
            phase="mechanical",
            blocking_summary=(
                "Mechanical sanity failed before LLM audit. "
                "Address these basics first."
            ),
            mechanical_failures=mechanical_failures,
        )
        _write_report(domain, round_num, result, mark_done_reason)
        logger.info(
            f"Audit round {round_num}: BLOCKED at mechanical phase "
            f"({len(mechanical_failures)} failures)"
        )
        return result

    # Phase 2
    criteria, overall, blocking_summary, universe, raw = await _run_llm_audit(
        llm, domain, requirement, mark_done_reason,
    )
    result = AuditResult(
        overall=overall,
        phase="both",
        blocking_summary=blocking_summary,
        mechanical_failures=[],
        criteria=criteria,
        universe=universe,
        raw_llm_response=raw,
    )
    _write_report(domain, round_num, result, mark_done_reason)
    logger.info(
        f"Audit round {round_num}: {overall} — "
        f"{sum(1 for c in criteria if c.verdict == 'PASS')}/{len(criteria)} criteria PASS"
    )
    return result


def _write_report(
    domain: str, round_num: int, result: AuditResult, mark_done_reason: str,
) -> None:
    """Write audit report markdown to artifacts/{domain}/runs/{run_id}/verification/."""
    try:
        ver_dir = Config.run_dir(domain) / "verification"
        ver_dir.mkdir(parents=True, exist_ok=True)

        lines = [
            f"# Audit Round {round_num}",
            "",
            f"**Overall:** {result.overall}",
            f"**Phase:** {result.phase}",
            "",
            f"## Planner's mark_done reason",
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
        if result.blocking_summary:
            lines.append("## Blocking summary")
            lines.append("")
            lines.append(result.blocking_summary)
            lines.append("")
        if result.criteria:
            lines.append("## Per-criterion verdicts")
            lines.append("")
            for c in result.criteria:
                lines.append(f"### {c.id}: {c.verdict}")
                lines.append("")
                if c.evidence:
                    lines.append(f"**Evidence:** {c.evidence}")
                    lines.append("")
                if c.reason:
                    lines.append(f"**Reason:** {c.reason}")
                    lines.append("")
        if result.raw_llm_response:
            lines.append("## Raw LLM response")
            lines.append("")
            lines.append("```")
            lines.append(result.raw_llm_response[:4000])
            lines.append("```")
            lines.append("")

        (ver_dir / f"round_{round_num}.md").write_text(
            "\n".join(lines), encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to write audit report round {round_num}: {e}")
