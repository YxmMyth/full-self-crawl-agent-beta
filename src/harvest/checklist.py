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

Output format (strict):

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


async def compile_checklist(
    llm,
    requirement: str,
    samples_dir: Path,
    output_path: Path,
    catalog_dir: Path | None = None,
    procedural_model: str | None = None,
) -> bool:
    """Compile requirement + catalog + procedural model into checklist.md
    via one LLM call. Output is qualitative criterion descriptions (no
    bash check field — see module docstring for rationale).

    Writes to output_path. On failure, writes a minimal stub + returns
    False (harvest mission proceeds with the stub criterion as the only
    contract; agent will likely still mark_done with audit since data
    coverage is provable).
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

    user_msg = (
        f"Requirement (boundary, verbatim):\n{requirement}\n\n"
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

    try:
        response = await llm.generate(prompt=user_msg, system=_COMPILE_SYSTEM_PROMPT)
        if not response or not response.strip():
            raise RuntimeError("LLM returned empty checklist")
        if "## " not in response:
            raise RuntimeError(
                f"LLM output has no ## sections; first 200 chars: {response[:200]!r}"
            )
        # Sanity: parse it once to confirm it produces at least 1 criterion
        parsed = parse_checklist(response)
        if not parsed:
            raise RuntimeError(
                f"LLM output parses to 0 criteria; first 300 chars: {response[:300]!r}"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(response, encoding="utf-8")
        logger.info(
            f"Checklist compiled to {output_path} ({len(response)} chars, "
            f"{len(parsed)} criteria)"
        )
        return True
    except Exception as e:
        logger.warning(f"Checklist compilation failed: {e}; writing stub")
        stub = (
            "# Acceptance Checklist (STUB — LLM compilation failed)\n\n"
            f'Generated from requirement: "{requirement}"\n\n'
            f"Compilation error: {e}\n\n"
            "## C1. Workspace produces output\n"
            "**criterion**: workspace/data/ contains at least one non-empty file "
            "with content that plausibly satisfies the requirement boundary above.\n"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(stub, encoding="utf-8")
        return False
