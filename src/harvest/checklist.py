"""checklist — compile + parse harvest acceptance criteria as descriptions.

Output is a markdown file (workspace/checklist.md) containing N qualitative
criterion descriptions — NOT bash check commands. The actual verification
runs in src/harvest/audit.py: an LLM auditor late-binds the criteria to
actual disk evidence at mark_done time.

Why no bash check field anymore (was removed 2026-05-25):
  - LLM at compile time fortune-tells the check command — frequently
    over-narrow (svg run: `file --mime-type | grep text/plain` rejected
    `application/javascript`). Once compiled + hash-pinned, the bash bug
    was unfixable.
  - Late-bound LLM judgment (audit.py) sees actual produced state and
    adapts evidence to what's there. Same satisficing defense as the
    bash approach: criteria are still hash-pinned (agent can't edit
    contract), but VERIFICATION of each criterion happens at mark_done
    time against live disk, not against frozen bash.

Why keep checklist.md as an artifact:
  - Agent reads it to know acceptance contract upfront — the "I'm done
    when X" anchor is essential. Hardcoding criteria in audit's system
    prompt would mean agent only learns criteria via verifier feedback
    AFTER first failed mark_done, wasting a turn.
  - Hash-pinning the file blocks the satisficing path where agent
    rewrites criteria to make them pass.
  - Mission-specific criteria (referencing this catalog file, this
    sample shape) beat generic hardcoded H1-H6.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Criterion:
    """A qualitative acceptance criterion. No bash check field — audit.py
    evaluates the description against on-disk evidence."""
    id: str        # e.g. "C1"
    name: str      # short label
    criterion: str # human-readable description (the contract text)


# Parser regexes — tolerant to spacing variants
_HEADER_RE = re.compile(r"^##\s+(C\d+)[.\s]+(.+?)\s*$")
_FIELD_RE = re.compile(r"^\*\*(\w+)\*\*:\s*(.+?)\s*$")
_CONFLICT_RE = re.compile(r"^\W{0,3}CONFLICT\s*:", re.IGNORECASE)


def is_conflict(text: str) -> bool:
    """True if a compile_checklist return value is the compiler's CONFLICT
    escape (kernel evidence standard unsatisfiable from the available data)."""
    return bool(text and _CONFLICT_RE.match(text.strip()))


def parse_checklist(text: str) -> list[Criterion]:
    """Parse checklist markdown into a list of Criterion.

    Tolerant: skips malformed sections. Returns empty list if no valid
    sections (caller treats this as a fail-closed signal — audit should
    not run with 0 criteria).
    """
    criteria: list[Criterion] = []
    current_id: str | None = None
    current_name: str = ""
    current_fields: dict[str, str] = {}

    def flush() -> None:
        nonlocal current_id, current_name, current_fields
        if current_id is None:
            return
        crit_text = current_fields.get("criterion", "").strip()
        if crit_text:
            criteria.append(Criterion(
                id=current_id,
                name=current_name,
                criterion=crit_text,
            ))
        current_id = None
        current_name = ""
        current_fields = {}

    for line in text.splitlines():
        header_match = _HEADER_RE.match(line)
        if header_match:
            flush()
            current_id = header_match.group(1)
            current_name = header_match.group(2)
            continue
        if current_id is None:
            continue
        field_match = _FIELD_RE.match(line)
        if field_match:
            key, val = field_match.group(1).lower(), field_match.group(2)
            current_fields[key] = val
    flush()
    return criteria


# ── Compile from requirement ────────────────────────────────


_COMPILE_SYSTEM_PROMPT = """You translate a natural-language harvest requirement \
into a structured acceptance checklist in markdown. The checklist is the \
contract the harvest agent MUST satisfy before declaring mission complete.

CRITICAL: produce qualitative DESCRIPTIONS, not bash check commands. The \
verifier (an LLM auditor at mark_done time) evaluates each criterion against \
actual on-disk evidence — file listings, content spot-checks, samples-vs-data \
diffs. Your job is to specify WHAT counts as done, not HOW to test it.

## Satisfiability check (do this FIRST, before writing anything)

When an intent kernel is provided, check: can its EVIDENCE STANDARD be honestly \
verified against the KIND of data actually available (the samples/catalog shown \
below)? Two outcomes:

- The available data IS the real deliverable per the evidence standard → compile \
the checklist normally.
- The available data matches a substitute the kernel explicitly REJECTS, or is \
simply not the kind of thing the kernel demands → do NOT write a checklist. \
Output a single block starting with `CONFLICT:` that quotes the kernel's \
rejection and states what the samples actually are (with a short excerpt). \
Nothing else.

The one output you must NEVER produce is a checklist that quietly lowers the \
bar to fit the available data — dropping the kernel's rejection clauses, \
rewording "complete X" into "some X", accepting the substitute because it is \
what exists. If honest criteria would fail the available data, that is exactly \
what CONFLICT is for: a human will decide whether to accept the substitute \
(re-versioning the intent) or stop. Narrowing SCOPE via catalog (how many) is \
fine; narrowing the KIND of deliverable below the evidence standard is not.

Output format when compiling (strict):

# Acceptance Checklist

Generated from requirement: "<requirement>"

## C1. <short name>
**criterion**: <one-paragraph description of what proves this criterion is satisfied — concrete enough that the auditor can check it against disk evidence>

## C2. <short name>
**criterion**: ...

## How the universe drives coverage (READ FIRST)

The requirement describes WHAT to harvest qualitatively. The QUANTITATIVE
target — "how many" — comes from recon's `catalog/` and the procedural
model, NOT from a number written in the requirement.

- If catalog/ has an enumerable list of every target entity (the universe),
  the coverage criterion is: "data/ contains a record for every entity in
  catalog/". Inspect the actual catalog file(s) shown below to reference
  the right identifier field by name (e.g. "`id` field in
  svg_search_all_pages.json").
- If procedural says the universe is unbounded (no total, infinite feed),
  the operator likely narrowed scope in the requirement and/or catalog
  is a fixed snapshot. Use whatever subset catalog/ presents as the
  ground truth.
- If catalog/ is empty AND procedural says nothing about universe, fall
  back to "data/ contains at least one valid record" plus shape/quality
  criteria. Do NOT invent a number from thin air.

How to write criterion descriptions:
- Specific to THIS mission. Reference the actual catalog file, sample
  shape, and requirement scope by name.
- One paragraph. State the acceptance condition + how the auditor would
  check it (e.g. "comparing the set of IDs in catalog/X.json against
  the file-prefix set in data/").
- Note legitimate exceptions: e.g. for a pen with no JS panel, an empty
  `*_js.txt` is acceptable (recon's procedural model documents the
  optional-panels rule).
- Account for known data quirks: dead/404 entities documented in recon
  may be missing from data/ if there's an error log explaining them.

Coverage (write 3-6 criteria total):
1. Universe coverage — every entity in `../catalog/` represented in data/,
   with optional error-log accounting for legitimate gaps.
2. Output shape — records match the structure of `../samples/` (the recon
   shape contract).
3. Content quality — files are non-empty, non-placeholder, no error pages.
4. Topic relevance — if requirement specifies a topic/tag/keyword, content
   reflects it.
5. Reproducibility — workspace contains a runnable harvest script.
6. Error transparency — gaps must be documented (no silent dropping).

Be specific to THIS requirement and THIS catalog. Skip irrelevant categories.
Do NOT write bash commands. Do NOT recommend tools or methods. ONLY define
WHAT counts as acceptance evidence.

Output ONLY the markdown. No preamble, no commentary."""


class ChecklistCompileError(RuntimeError):
    """Raised when the checklist cannot be compiled (LLM failure / unusable
    output). FAIL-CLOSED: the old behavior wrote a weak stub and continued —
    the auditor then verified against a contract that wasn't the contract.
    No checklist, no harvest."""


async def compile_checklist(
    llm,
    requirement: str,
    samples_dir: Path,
    output_path: Path,
    catalog_dir: Path | None = None,
    procedural_model: str | None = None,
    intent_kernel: str | None = None,
) -> str:
    """Compile requirement + intent kernel + catalog + procedural model into
    checklist.md via one LLM call. Output is qualitative criterion descriptions
    (no bash check field — see module docstring for rationale).

    When intent_kernel is provided (the normal case — pinned at intake, possibly
    re-pinned at gate), the checklist is derived FROM it: the evidence standard
    in the kernel defines what "the real thing" is, and the checklist expands
    that into operational criteria. This ensures the two auditors (recon + harvest)
    use the same standard.

    Returns:
      - the compiled checklist markdown (also written to output_path), OR
      - a "CONFLICT: ..." string (nothing written) when the compiler judges the
        kernel's evidence standard cannot be honestly verified from the data
        actually available — the caller must route this to a human.

    Raises ChecklistCompileError on LLM failure / unusable output (fail-closed;
    the old write-a-stub-and-continue path is gone).
    """
    sample_listing = "(no samples available)"
    sample_preview = ""
    samples_layout_desc = "unknown (samples dir missing)"
    if samples_dir.is_dir():
        all_entries = list(samples_dir.iterdir())
        files = sorted(p for p in all_entries if p.is_file())
        subdirs = [p for p in all_entries if p.is_dir()]
        if files:
            sample_listing = "\n".join(
                f"- {p.name} ({p.stat().st_size} bytes)" for p in files[:8]
            )
            if len(files) > 8:
                sample_listing += f"\n- ... ({len(files) - 8} more files)"
        if not subdirs:
            samples_layout_desc = (
                f"FLAT — {len(files)} files directly under samples/, no subdirectories"
            )
        else:
            samples_layout_desc = (
                f"NESTED — {len(subdirs)} subdirectories, {len(files)} top-level files"
            )
        for p in files[:8]:
            if p.suffix.lower() == ".json" and p.stat().st_size < 4000:
                try:
                    sample_preview = (
                        f"\n\nExample sample file ({p.name}):\n```json\n"
                        f"{p.read_text(encoding='utf-8')[:3500]}\n```"
                    )
                    break
                except Exception:
                    continue

    catalog_section = (
        "Catalog directory:\n  (not provided — fall back to qualitative checks)"
    )
    if catalog_dir and catalog_dir.is_dir():
        catalog_files = sorted(p for p in catalog_dir.iterdir() if p.is_file())
        if catalog_files:
            lines = [
                "Catalog directory contents (read-only, at ../catalog/ from workspace):"
            ]
            for p in catalog_files[:10]:
                lines.append(f"- {p.name} ({p.stat().st_size} bytes)")
            if len(catalog_files) > 10:
                lines.append(f"- ... ({len(catalog_files) - 10} more files)")
            largest = max(catalog_files, key=lambda p: p.stat().st_size)
            try:
                preview = largest.read_text(encoding="utf-8", errors="replace")
                preview_head = preview[:3000]
                preview_tail = preview[-500:] if len(preview) > 3500 else ""
                lines.append("")
                lines.append(
                    f"Largest catalog file preview ({largest.name}, "
                    f"{len(preview)} chars total):"
                )
                lines.append("```")
                lines.append(preview_head)
                if preview_tail:
                    lines.append("... [middle truncated] ...")
                    lines.append(preview_tail)
                lines.append("```")
            except Exception as e:
                lines.append(f"(preview failed: {e})")
            catalog_section = "\n".join(lines)
        else:
            catalog_section = (
                "Catalog directory: empty (no universe defined — "
                "use qualitative coverage criterion only)"
            )

    procedural_section = (
        "Procedural model: (not provided)"
        if not procedural_model
        else f"Procedural model (recon's how-to):\n{procedural_model[:4000]}"
    )

    intent_section = ""
    if intent_kernel and intent_kernel.strip():
        intent_section = (
            "## Intent kernel (YOUR PRIMARY REFERENCE — derive criteria from this)\n"
            "The intent kernel defines WHAT the real deliverable is and what cheap\n"
            "substitutes to reject. Your criteria MUST be consistent with its\n"
            "evidence standard — do not contradict or weaken it.\n\n"
            f"{intent_kernel.strip()}\n\n"
        )

    user_msg = (
        f"Requirement (boundary, verbatim):\n{requirement}\n\n"
        f"{intent_section}"
        f"{procedural_section}\n\n"
        f"Samples directory layout (read-only reference, at ../samples/ from workspace):\n"
        f"  shape: {samples_layout_desc}\n"
        f"  contents:\n{sample_listing}\n\n"
        f"{catalog_section}\n\n"
        f"The harvest agent writes output to `data/` under the workspace. "
        f"Most agents mirror the samples layout — if samples are FLAT, expect "
        f"output like `data/<id>_<type>.txt`, not `data/<id>/foo.txt`."
        f"{sample_preview}\n\n"
        f"Compile the acceptance checklist now. Write 3-6 criterion descriptions, "
        f"each one paragraph, all specific to THIS requirement / catalog / samples. "
        f"No bash commands."
    )

    response = await llm.generate(prompt=user_msg, system=_COMPILE_SYSTEM_PROMPT)
    if not response or not response.strip():
        raise ChecklistCompileError("LLM returned empty checklist")
    text = response.strip()

    # The compiler's escape hatch: it judged the kernel unsatisfiable from the
    # available data. Nothing is written — the caller routes this to a human.
    if is_conflict(text):
        logger.warning(f"Checklist compile flagged CONFLICT: {text[:300]}")
        return text

    if "## " not in text:
        raise ChecklistCompileError(
            f"LLM output has no ## sections; first 200 chars: {text[:200]!r}"
        )
    # Sanity: parse it once to confirm it produces at least 1 criterion
    parsed = parse_checklist(text)
    if not parsed:
        raise ChecklistCompileError(
            f"LLM output parses to 0 criteria; first 300 chars: {text[:300]!r}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    logger.info(
        f"Checklist compiled to {output_path} ({len(text)} chars, "
        f"{len(parsed)} criteria)"
    )
    return text
