"""Harvest completion audit — read-only auditor agent (replaces the single LLM call).

Same shape as recon's audit (src/verification/audit.py):
  Phase 1 — mechanical sanity (data/ has a non-trivial file, requirement +
            checklist exist). Cheap, deterministic; fails fast, never judges.
  Phase 2 — read-only auditor AGENT (src/audit/agent.py): fresh context, reads
            the actual harvested data/ content + catalog/ universe, judges the
            compiled checklist criteria against the pinned intent kernel's
            evidence-standard, and returns PASS / FAIL / UNCERTAIN.

The agent reads file CONTENT (it can tell a real record from a wrong-kind
substitute and count coverage against the catalog itself), which the old
listing-only single-call audit could not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.audit.agent import run_audit_agent
from src.config import Config
from src.harvest.checklist import Criterion, parse_checklist
from src.llm.client import LLMClient
from src.utils.logging import get_logger
from src.world_model import db

logger = get_logger(__name__)


# ── Phase 1 thresholds (mechanical sanity, harvest-specific) ────────────

_MIN_DATA_FILES = 1            # data/ must have at least 1 file
_MIN_DATA_TOTAL_BYTES = 1024   # combined size > 1KB (defends 0-byte placeholders)

_MAX_LISTING_FILES = 200       # cap per directory in the evidence map
_MAX_CATALOG_PREVIEW = 6000    # chars of largest catalog file shown


# ── Criteria loading (the compiled checklist) ─────────────


def _render_criteria_block(criteria: list[Criterion]) -> str:
    if not criteria:
        return "(No criteria parsed from workspace/checklist.md — this is a launcher bug.)"
    lines: list[str] = []
    for c in criteria:
        lines.append(f"### {c.id}. {c.name}")
        lines.append("")
        lines.append(c.criterion)
        lines.append("")
    return "\n".join(lines)


def _load_criteria(domain: str) -> list[Criterion]:
    """Load + parse criteria from workspace/checklist.md. Empty list if
    missing/unparseable — the caller fails closed (the contract must exist)."""
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


def _read_intent_kernel(domain: str) -> str:
    p = Config.run_dir(domain) / "intent_kernel.md"
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else ""
    except Exception:
        return ""


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
class AuditResult:
    overall: str  # PASS | FAIL | UNCERTAIN
    phase: str  # "mechanical" | "agent"
    report: str = ""  # the auditor agent's full verdict text
    blocking_summary: str = ""
    mechanical_failures: list[str] = field(default_factory=list)
    criteria: list[CriterionResult] = field(default_factory=list)
    raw_llm_response: str = ""

    def feedback(self) -> str:
        if self.overall == "PASS":
            return "Audit PASS."
        parts = [f"Audit {self.overall} (phase: {self.phase})."]
        if self.mechanical_failures:
            parts.append("Mechanical sanity failures (fix these first):")
            for f in self.mechanical_failures:
                parts.append(f"  - {f}")
        if self.report:
            parts.append(self.report)
        elif self.blocking_summary:
            parts.append(f"Blocking summary: {self.blocking_summary}")
        return "\n".join(parts)


# ── Phase 1 implementation ────────────────────────────────


async def _run_mechanical(domain: str) -> list[str]:
    """Mechanical sanity on the harvest workspace. Returns failure messages."""
    failures: list[str] = []

    run_dir = Config.run_dir(domain)
    workspace = run_dir / "workspace"
    if not workspace.is_dir():
        failures.append(f"workspace/ directory missing at {workspace}")
        return failures

    req_file = run_dir / "requirement.txt"
    if not req_file.is_file():
        failures.append("requirement.txt missing — harvest had no boundary")

    data_dir = workspace / "data"
    if not data_dir.is_dir():
        failures.append("workspace/data/ does not exist — agent produced no data")
        return failures

    data_files = [p for p in data_dir.rglob("*") if p.is_file()]
    if len(data_files) < _MIN_DATA_FILES:
        failures.append(
            f"data/ contains {len(data_files)} file(s); need at least {_MIN_DATA_FILES}"
        )
        return failures

    total_bytes = sum(p.stat().st_size for p in data_files)
    if total_bytes < _MIN_DATA_TOTAL_BYTES:
        failures.append(
            f"data/ total size is {total_bytes} bytes; need at least "
            f"{_MIN_DATA_TOTAL_BYTES} (defends against pure-placeholder dumps)"
        )

    return failures


# ── Phase 2 implementation (auditor agent) ────────────────


def _gather_context(domain: str) -> str:
    """A compact evidence map (samples shape / catalog universe + preview /
    data output / scripts) so the agent knows what to open."""
    run_dir = Config.run_dir(domain)
    workspace = run_dir / "workspace"
    sections: list[str] = []

    samples_dir = run_dir / "samples"
    sections.append("### samples/ (shape contract, from recon)")
    if samples_dir.is_dir():
        files = sorted(p for p in samples_dir.iterdir() if p.is_file())
        if not files:
            sections.append("  (empty)")
        else:
            for p in files[:_MAX_LISTING_FILES]:
                sections.append(f"  - {p.name} ({p.stat().st_size} bytes)")
            if len(files) > _MAX_LISTING_FILES:
                sections.append(f"  ... ({len(files) - _MAX_LISTING_FILES} more)")
    else:
        sections.append("  (directory missing)")

    catalog_dir = run_dir / "catalog"
    sections.append("\n### catalog/ (universe ground truth, from recon)")
    if catalog_dir.is_dir():
        cfiles = sorted(p for p in catalog_dir.iterdir() if p.is_file())
        if not cfiles:
            sections.append("  (empty)")
        else:
            for p in cfiles[:_MAX_LISTING_FILES]:
                sections.append(f"  - {p.name} ({p.stat().st_size} bytes)")
            largest = max(cfiles, key=lambda p: p.stat().st_size)
            try:
                preview = largest.read_text(encoding="utf-8", errors="replace")
                sections.append(
                    f"\n  Largest catalog file preview ({largest.name}, "
                    f"{len(preview)} chars):"
                )
                sections.append("  ```")
                sections.append(preview[:_MAX_CATALOG_PREVIEW])
                if len(preview) > _MAX_CATALOG_PREVIEW:
                    sections.append(f"  ... [{len(preview) - _MAX_CATALOG_PREVIEW} chars truncated]")
                sections.append("  ```")
            except Exception as e:
                sections.append(f"  (preview failed: {e})")
    else:
        sections.append("  (directory missing)")

    data_dir = workspace / "data"
    sections.append("\n### workspace/data/ (harvest output — OPEN THESE)")
    if data_dir.is_dir():
        dfiles = sorted(p for p in data_dir.rglob("*") if p.is_file())
        sections.append(f"Total files: {len(dfiles)}")
        if dfiles:
            sections.append(f"Total size: {sum(p.stat().st_size for p in dfiles)} bytes")
            for p in dfiles[:_MAX_LISTING_FILES]:
                sections.append(f"  - {p.relative_to(data_dir)} ({p.stat().st_size} bytes)")
            if len(dfiles) > _MAX_LISTING_FILES:
                sections.append(f"  ... ({len(dfiles) - _MAX_LISTING_FILES} more)")
    else:
        sections.append("  (directory missing)")

    # scripts / error logs (reproducibility + gap transparency)
    if workspace.is_dir():
        scripts = sorted(
            p for p in workspace.iterdir()
            if p.is_file() and p.suffix.lower() in (".py", ".sh", ".js")
        )
        errlogs = sorted(
            p for p in workspace.iterdir()
            if p.is_file() and ("error" in p.name.lower() or p.name in ("state.json", "progress.md"))
        )
        sections.append("\n### workspace/ scripts + state/error logs")
        sections.append("Scripts: " + (", ".join(p.name for p in scripts) or "(none)"))
        sections.append("State/errors: " + (", ".join(p.name for p in errlogs) or "(none)"))

    return "\n".join(sections)


def _build_harvest_briefing(
    domain: str,
    requirement: str,
    intent_kernel: str,
    criteria_list: list[Criterion],
    procedural: str,
    context: str,
) -> str:
    ruler = intent_kernel.strip() or (
        f"(no compiled intent kernel found — fall back to the raw requirement)\n{requirement}"
    )
    return (
        "# Mission intent kernel (YOUR RULER — anchor every judgment on the "
        "evidence standard here)\n"
        f"{ruler}\n\n"
        f"# Original requirement (verbatim)\n{requirement}\n\n"
        "# Acceptance criteria (compiled checklist — the kernel's operational "
        "expansion; verify each by READING real files, not by trusting the worker)\n"
        "PRECEDENCE RULE: these criteria CANNOT override the intent kernel above. "
        "If the data satisfies a criterion's wording but is a substitute the "
        "kernel's evidence standard rejects, the kernel wins — report the "
        "conflict and return UNCERTAIN, never PASS.\n\n"
        f"{_render_criteria_block(criteria_list)}\n\n"
        f"# Recon procedural model (universe definition + how-to)\n{procedural or '(empty)'}\n\n"
        "# On-disk evidence map (the harvest output is under workspace/data/)\n"
        f"{context}\n\n"
        "Investigate now. Two things especially, by READING content (sizes are "
        "not proof):\n"
        "1. COVERAGE — every entity in catalog/ has a record in workspace/data/. "
        "Count it yourself (list_dir / grep against the catalog IDs); account for "
        "gaps the error log documents.\n"
        "2. AUTHENTICITY — open data/ files and confirm their CONTENT is the real "
        "deliverable per the evidence standard, NOT a wrong-kind substitute "
        "(e.g. a summary where dialogue was required, metadata where the artifact "
        "was required).\n\n"
        "End your verdict with exactly one line: OVERALL: PASS|FAIL|UNCERTAIN"
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
    with overall ∈ {PASS, FAIL, UNCERTAIN}. Writes harvest_round_{n}.md."""
    global _audit_round
    _audit_round += 1
    round_num = _audit_round

    # Phase 1 — mechanical sanity
    mechanical_failures = await _run_mechanical(domain)
    if mechanical_failures:
        result = AuditResult(
            overall="FAIL",
            phase="mechanical",
            blocking_summary="Mechanical sanity failed before the auditor agent.",
            mechanical_failures=mechanical_failures,
        )
        _write_report(domain, round_num, result, mark_done_reason)
        logger.info(f"Harvest audit round {round_num}: FAIL at mechanical phase")
        return result

    # Acceptance contract must exist (fail closed — never auto-PASS against no criteria).
    criteria_list = _load_criteria(domain)
    if not criteria_list:
        result = AuditResult(
            overall="FAIL",
            phase="mechanical",
            blocking_summary="checklist.md missing or unparseable — no criteria to verify.",
            mechanical_failures=["workspace/checklist.md missing or 0 parseable criteria"],
        )
        _write_report(domain, round_num, result, mark_done_reason)
        logger.warning(f"Harvest audit round {round_num}: FAIL — 0 criteria loaded")
        return result

    # Phase 2 — read-only auditor agent
    intent_kernel = _read_intent_kernel(domain)
    _, procedural = await db.load_both_models(domain)
    context = _gather_context(domain)
    briefing = _build_harvest_briefing(
        domain, requirement, intent_kernel, criteria_list, procedural or "", context
    )

    overall, report = await run_audit_agent(llm, Config.run_dir(domain), briefing)
    result = AuditResult(
        overall=overall,
        phase="agent",
        report=report,
        raw_llm_response=report,
    )
    _write_report(domain, round_num, result, mark_done_reason)
    logger.info(f"Harvest audit round {round_num}: {overall} (auditor agent)")
    return result


def _write_report(
    domain: str, round_num: int, result: AuditResult, mark_done_reason: str,
) -> None:
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
        if result.report:
            lines.append("## Auditor verdict")
            lines.append("")
            lines.append(result.report)
            lines.append("")

        (ver_dir / f"harvest_round_{round_num}.md").write_text(
            "\n".join(lines), encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to write harvest audit report round {round_num}: {e}")
