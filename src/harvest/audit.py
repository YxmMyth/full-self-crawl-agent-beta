"""Harvest completion audit — replaces the previous bash-checklist mechanism.

Why we replaced bash checklist:
  - LLM compiled bash checks at launcher start, before any harvest output
    existed → checks were fortune-telling.
  - Checks frequently used over-narrow predicates that didn't match their
    own criterion description (svg run: `file -b --mime-type | grep
    text/plain` rejected `application/javascript` for JS files, even
    though "non-binary" criterion was satisfied).
  - Agent couldn't fix the bash bug without tampering (which the
    sha256 pin correctly blocked) → permanent deadlock.

This module uses the same shape as recon's audit (src/verification/audit.py):
  Phase 1 — mechanical bash sanity (data/ has at least one non-trivial
            file, requirement.txt exists). Cheap, deterministic.
  Phase 2 — single LLM generate() call. Late-bound to actual disk state
            so it can't fortune-tell wrong bash. Sees the qualitative
            criteria, the samples shape contract, the catalog universe,
            spot-checks of actual harvested files.

No tool-use loop (no "agent must call submit_verdict" deadlock).
No pre-compiled bash (no fortune-telling).
Agent provides evidence at mark_done time via `reason`; verifier weighs it
against the actual disk state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.config import Config
from src.harvest.checklist import Criterion, parse_checklist
from src.llm.client import LLMClient
from src.utils.logging import get_logger
from src.world_model import db

logger = get_logger(__name__)


# ── Phase 1 thresholds (mechanical sanity, harvest-specific) ────────────

_MIN_DATA_FILES = 1            # data/ must have at least 1 file
_MIN_DATA_TOTAL_BYTES = 1024   # combined size > 1KB (defends against
                               # 0-byte placeholders or pure-stub data)


# ── Phase 2 system prompt ──────────────────────────────────
#
# Criteria are NOT hardcoded here. The launcher compiles them to
# workspace/checklist.md at mission start (mission-specific, references
# this catalog/sample/universe by name). The audit loads them at
# verification time and renders them into the prompt below.

_AUDIT_SYSTEM_PROMPT_TEMPLATE = """You are auditing whether a harvest agent has \
truly completed its mission.

The agent has called mark_done. Your job is to verify the produced dataset
against the criteria compiled at mission start (shown below), the recon-
provided universe and shape contracts, and the actual on-disk state. The
agent WANTS to stop; your default bias is skepticism.

## Completion Audit Discipline (borrowed from codex /goal)

Treat completion as UNPROVEN. Verify against the actual current state, not
intent or plausible-looking claims.

- For every criterion below, identify authoritative EVIDENCE that would
  prove it: a specific catalog ID set vs data ID set comparison, a field
  shape diff between a sample and a harvested record, a content spot-check,
  a script path.
- Match verification scope to criterion scope. Do NOT use a narrow check
  to reject (or accept) a broad claim. If the agent's `reason` references
  evidence (e.g. "ran diff catalog vs data, 0 missing"), confirm that
  evidence is reflected in the on-disk listing you were given.
- Treat uncertain or indirect evidence as NOT achieved. Mark FAIL or
  PARTIAL rather than guess.
- Do NOT mark a criterion PASS merely because the agent says so — agent
  reasoning is one input, the on-disk reality is the other.

## Criteria from checklist.md (compiled at mission start, hash-pinned)

{criteria_block}

## Output Format (strict)

Return ONLY a single JSON object, no prose around it:

```json
{{
  "criteria": [
    {{"id": "<criterion id>", "verdict": "PASS|FAIL|PARTIAL", "evidence": "...", "reason": "..."}}
    // ... one entry per criterion above
  ],
  "overall": "PASS|BLOCKED",
  "blocking_summary": "If BLOCKED: 1-3 sentences of the biggest gaps the agent must address. Empty string if PASS."
}}
```

Rules:
- Output ONE criterion verdict per criterion in the checklist above —
  match by `id` exactly (C1, C2, ...).
- `evidence` quotes or cites the actual artifact you read (e.g., "catalog
  has 89 IDs per svg_search_all_pages.json, data has 89 distinct prefixes
  per file listing" or "all 267 *.txt files in data/ are >1KB").
- `reason` explains why this verdict given the evidence. Concrete, not
  generic.
- `overall` = PASS only if ALL criteria are PASS. Any FAIL or PARTIAL
  means BLOCKED.
- `blocking_summary` is what the agent reads first — make it actionable.
"""


def _render_criteria_block(criteria: list[Criterion]) -> str:
    """Format checklist criteria for inclusion in the audit system prompt."""
    if not criteria:
        return (
            "(No criteria parsed from workspace/checklist.md. This is a "
            "bug — the launcher should have compiled at least 1 criterion. "
            "Default to overall=BLOCKED with blocking_summary explaining "
            "the checklist was unreadable.)"
        )
    lines: list[str] = []
    for c in criteria:
        lines.append(f"### {c.id}. {c.name}")
        lines.append("")
        lines.append(c.criterion)
        lines.append("")
    return "\n".join(lines)


def _load_criteria(domain: str) -> list[Criterion]:
    """Load + parse criteria from workspace/checklist.md.

    Returns empty list if missing / unparseable. Caller (audit phase 2)
    treats empty as a blocking error rather than auto-PASS — the contract
    is supposed to exist.
    """
    checklist_path = Config.run_dir(domain) / "workspace" / "checklist.md"
    if not checklist_path.is_file():
        logger.warning(f"audit: checklist.md missing at {checklist_path}")
        return []
    try:
        text = checklist_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"audit: failed to read checklist.md: {e}")
        return []
    return parse_checklist(text)


# ── Result dataclasses ────────────────────────────────────


@dataclass
class CriterionResult:
    id: str
    verdict: str  # PASS | FAIL | PARTIAL
    evidence: str = ""
    reason: str = ""


@dataclass
class AuditResult:
    overall: str  # PASS | BLOCKED
    phase: str  # "mechanical" | "both"
    blocking_summary: str = ""
    mechanical_failures: list[str] = field(default_factory=list)
    criteria: list[CriterionResult] = field(default_factory=list)
    raw_llm_response: str = ""

    def feedback(self) -> str:
        """Human-readable feedback string for the harvest agent."""
        if self.overall == "PASS":
            return "Audit PASS — all 6 criteria verified against evidence."

        parts = [f"Audit BLOCKED (phase: {self.phase})."]
        if self.mechanical_failures:
            parts.append("Mechanical sanity failures (fix these first):")
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
    """Mechanical sanity checks on harvest workspace. Returns list of
    failure messages (empty if all pass).
    """
    failures: list[str] = []

    run_dir = Config.run_dir(domain)
    workspace = run_dir / "workspace"
    if not workspace.is_dir():
        failures.append(f"workspace/ directory missing at {workspace}")
        return failures  # rest of checks meaningless without workspace

    req_file = run_dir / "requirement.txt"
    if not req_file.is_file():
        failures.append("requirement.txt missing — harvest had no boundary")

    data_dir = workspace / "data"
    if not data_dir.is_dir():
        failures.append(
            "workspace/data/ directory does not exist — agent produced "
            "no harvested data"
        )
        return failures

    data_files = [p for p in data_dir.rglob("*") if p.is_file()]
    if len(data_files) < _MIN_DATA_FILES:
        failures.append(
            f"data/ contains {len(data_files)} file(s); need at least "
            f"{_MIN_DATA_FILES}"
        )
        return failures

    total_bytes = sum(p.stat().st_size for p in data_files)
    if total_bytes < _MIN_DATA_TOTAL_BYTES:
        failures.append(
            f"data/ total size is {total_bytes} bytes; need at least "
            f"{_MIN_DATA_TOTAL_BYTES} (defends against pure-placeholder "
            "data dumps)"
        )

    return failures


# ── Phase 2 implementation ────────────────────────────────


_MAX_LISTING_FILES = 200  # cap per directory in the LLM-facing listing
_MAX_CATALOG_PREVIEW = 6000  # chars of largest catalog file shown to LLM


def _gather_context(domain: str) -> str:
    """Format on-disk evidence the LLM auditor needs:
    - samples/ listing (recon's shape contract)
    - catalog/ listing + preview of largest catalog file (universe ground truth)
    - workspace/ top-level + scripts (reproducibility evidence)
    - data/ listing (the actual harvest output)
    """
    run_dir = Config.run_dir(domain)
    workspace = run_dir / "workspace"
    sections: list[str] = []

    # samples/ (shape contract from recon)
    samples_dir = run_dir / "samples"
    sections.append("### samples/ (shape contract, from recon)")
    if samples_dir.is_dir():
        files = sorted(p for p in samples_dir.iterdir() if p.is_file())
        if not files:
            sections.append("  (empty)")
        else:
            for p in files[:_MAX_LISTING_FILES]:
                try:
                    size = p.stat().st_size
                except Exception:
                    size = -1
                sections.append(f"  - {p.name} ({size} bytes)")
            if len(files) > _MAX_LISTING_FILES:
                sections.append(
                    f"  ... ({len(files) - _MAX_LISTING_FILES} more files)"
                )
    else:
        sections.append("  (directory missing)")

    # catalog/ (universe ground truth)
    catalog_dir = run_dir / "catalog"
    sections.append("\n### catalog/ (universe ground truth, from recon)")
    if catalog_dir.is_dir():
        cfiles = sorted(p for p in catalog_dir.iterdir() if p.is_file())
        if not cfiles:
            sections.append("  (empty)")
        else:
            for p in cfiles[:_MAX_LISTING_FILES]:
                try:
                    size = p.stat().st_size
                except Exception:
                    size = -1
                sections.append(f"  - {p.name} ({size} bytes)")
            if len(cfiles) > _MAX_LISTING_FILES:
                sections.append(
                    f"  ... ({len(cfiles) - _MAX_LISTING_FILES} more files)"
                )
            # Show preview of the largest catalog file so the LLM can see
            # the universe schema (IDs/URLs) for H1 coverage diff.
            largest = max(cfiles, key=lambda p: p.stat().st_size)
            try:
                preview = largest.read_text(encoding="utf-8", errors="replace")
                head = preview[:_MAX_CATALOG_PREVIEW]
                sections.append(
                    f"\n  Largest catalog file preview ({largest.name}, "
                    f"{len(preview)} chars total):"
                )
                sections.append("  ```")
                sections.append(head)
                if len(preview) > _MAX_CATALOG_PREVIEW:
                    sections.append(
                        f"  ... [{len(preview) - _MAX_CATALOG_PREVIEW} chars truncated]"
                    )
                sections.append("  ```")
            except Exception as e:
                sections.append(f"  (preview failed: {e})")
    else:
        sections.append("  (directory missing)")

    # data/ (the actual harvest output)
    data_dir = workspace / "data"
    sections.append("\n### workspace/data/ (harvest output)")
    if data_dir.is_dir():
        dfiles = sorted(p for p in data_dir.rglob("*") if p.is_file())
        sections.append(f"Total files: {len(dfiles)}")
        if dfiles:
            total_bytes = sum(p.stat().st_size for p in dfiles)
            sections.append(f"Total size: {total_bytes} bytes")
            # Show first N + last 5 file names + sizes for sanity / spotcheck
            preview_n = min(_MAX_LISTING_FILES, len(dfiles))
            for p in dfiles[:preview_n]:
                rel = p.relative_to(data_dir)
                sections.append(f"  - {rel} ({p.stat().st_size} bytes)")
            if len(dfiles) > _MAX_LISTING_FILES:
                sections.append(
                    f"  ... ({len(dfiles) - _MAX_LISTING_FILES} more files)"
                )
    else:
        sections.append("  (directory missing)")

    # workspace/ top-level (scripts, state, error logs)
    sections.append("\n### workspace/ top-level (scripts, state, error logs)")
    if workspace.is_dir():
        wfiles = sorted(
            p for p in workspace.iterdir()
            if p.is_file() and not p.name.startswith(".")
        )
        for p in wfiles[:50]:
            try:
                size = p.stat().st_size
            except Exception:
                size = -1
            sections.append(f"  - {p.name} ({size} bytes)")
        if len(wfiles) > 50:
            sections.append(f"  ... ({len(wfiles) - 50} more files)")
    else:
        sections.append("  (workspace dir missing)")

    return "\n".join(sections)


def _parse_llm_audit(
    raw: str,
) -> tuple[list[CriterionResult], str, str] | None:
    """Parse LLM JSON output. Defensive against deepseek quirks (markdown
    code fences, trailing junk, unbalanced JSON).

    Returns (criteria, overall, blocking_summary) or None if unparseable.
    """
    if not raw:
        return None

    text = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    start = text.find("{")
    if start < 0:
        return None
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
        logger.warning(f"Harvest audit JSON parse failed: {e}")
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
    # the LLM said overall (deepseek sometimes says overall=PASS while
    # listing FAIL criteria).
    if any(c.verdict != "PASS" for c in criteria):
        overall = "BLOCKED"

    blocking_summary = str(data.get("blocking_summary", "")).strip()
    return criteria, overall, blocking_summary


async def _run_llm_audit(
    llm: LLMClient,
    domain: str,
    requirement: str,
    mark_done_reason: str,
    criteria_list: list[Criterion],
) -> tuple[list[CriterionResult], str, str, str]:
    """Run the LLM audit. Returns (criteria_results, overall, blocking_summary, raw).

    criteria_list is the parsed checklist.md criteria (from launcher's
    compile_checklist call). Each gets a verdict from the LLM against
    actual disk evidence.
    """
    # Load recon's procedural model — it has the universe definition and
    # documented methods the auditor needs to evaluate criteria like
    # universe coverage / shape compliance / requirement fit.
    _, procedural = await db.load_both_models(domain)
    context = _gather_context(domain)
    system_prompt = _AUDIT_SYSTEM_PROMPT_TEMPLATE.format(
        criteria_block=_render_criteria_block(criteria_list)
    )

    user_msg = (
        f"# Requirement\n{requirement}\n\n"
        f"# Agent's mark_done reason\n{mark_done_reason or '(none)'}\n\n"
        f"# Recon Procedural Model (how-to, universe definition)\n"
        f"{procedural or '(empty)'}\n\n"
        f"# On-disk Evidence\n{context}\n\n"
        f"Domain: {domain}\n\n"
        "Run the audit. Output the JSON object specified in the system prompt."
    )

    raw = await llm.generate(prompt=user_msg, system=system_prompt)
    raw = raw or ""

    parsed = _parse_llm_audit(raw)
    if parsed is None:
        logger.warning(
            "Harvest audit LLM returned unparseable response; treating as BLOCKED"
        )
        return (
            [],
            "BLOCKED",
            "Audit LLM returned unparseable output. Operator should inspect "
            "the raw response in the verification report.",
            raw,
        )
    criteria_results, overall, blocking_summary = parsed
    return criteria_results, overall, blocking_summary, raw


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
    auditing an empty data/ directory. LLM phase only runs when sanity
    is clean.

    Writes a report to artifacts/{domain}/runs/{run_id}/verification/
    harvest_round_{n}.md.
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
                "Mechanical sanity failed before LLM audit. Address these "
                "basics first."
            ),
            mechanical_failures=mechanical_failures,
        )
        _write_report(domain, round_num, result, mark_done_reason)
        logger.info(
            f"Harvest audit round {round_num}: BLOCKED at mechanical phase "
            f"({len(mechanical_failures)} failures)"
        )
        return result

    # Load criteria from checklist.md (compiled at mission start by launcher).
    # If missing/unparseable, fail closed — agent can't be "done" against an
    # unreadable contract. We do NOT fall back to hardcoded criteria; the
    # whole point of a per-mission checklist is mission-specific evidence.
    criteria_list = _load_criteria(domain)
    if not criteria_list:
        result = AuditResult(
            overall="BLOCKED",
            phase="mechanical",
            blocking_summary=(
                "checklist.md missing or unparseable — no acceptance criteria "
                "to verify against. This is a launcher bug (compile_checklist "
                "should have written at least one criterion)."
            ),
            mechanical_failures=[
                "workspace/checklist.md missing or contains 0 parseable criteria"
            ],
        )
        _write_report(domain, round_num, result, mark_done_reason)
        logger.warning(
            f"Harvest audit round {round_num}: BLOCKED — 0 criteria loaded"
        )
        return result

    # Phase 2
    criteria, overall, blocking_summary, raw = await _run_llm_audit(
        llm, domain, requirement, mark_done_reason, criteria_list,
    )
    result = AuditResult(
        overall=overall,
        phase="both",
        blocking_summary=blocking_summary,
        mechanical_failures=[],
        criteria=criteria,
        raw_llm_response=raw,
    )
    _write_report(domain, round_num, result, mark_done_reason)
    logger.info(
        f"Harvest audit round {round_num}: {overall} — "
        f"{sum(1 for c in criteria if c.verdict == 'PASS')}/{len(criteria)} "
        "criteria PASS"
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
            f"# Harvest Audit Round {round_num}",
            "",
            f"**Overall:** {result.overall}",
            f"**Phase:** {result.phase}",
            "",
            "## Agent's mark_done reason",
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

        (ver_dir / f"harvest_round_{round_num}.md").write_text(
            "\n".join(lines), encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to write harvest audit report round {round_num}: {e}")
