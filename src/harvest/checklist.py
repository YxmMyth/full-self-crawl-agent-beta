"""checklist — parse workspace/checklist.md and run mechanical acceptance checks.

Replaces the LLM Verification Subagent for the harvest stage. Instead of an
LLM loop that decides when to call submit_verdict (which can fail to commit
under thinking-mode quirks), the launcher pre-compiles requirement.txt into
checklist.md with executable bash check commands, and mark_done runs them.

No LLM is in the verification loop — eliminates the
"agent-never-calls-submit_verdict" failure mode by removing the
LLM-decides-when-to-stop step entirely.

Schema (workspace/checklist.md):

    # Acceptance Checklist

    ## C1. <short name>
    **criterion**: <human-readable description>
    **check**: `<bash command — exit 0 means PASS, non-zero means FAIL>`

    ## C2. <short name>
    **criterion**: ...
    **check**: `...`

The check command runs in workspace cwd. Stdout+stderr (tail-truncated)
becomes evidence shown to the agent when a criterion fails.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from src.utils.logging import get_logger

logger = get_logger(__name__)


# Resolve bash explicitly so Windows doesn't fall back to WSL's bash.exe
# (which is broken without a WSL distro and shadows git-bash in PATH lookup
# when called via create_subprocess_exec).
_BASH_PATH = shutil.which("bash") or "/bin/bash"


@dataclass
class Criterion:
    id: str        # e.g. "C1"
    name: str      # short label
    criterion: str # human description
    check: str     # bash command string


@dataclass
class CheckResult:
    criterion: Criterion
    passed: bool
    exit_code: int
    output: str  # combined stdout + stderr (truncated)


# Parser regexes — tolerant to spacing variants
_HEADER_RE = re.compile(r"^##\s+(C\d+)[.\s]+(.+?)\s*$")
_FIELD_RE = re.compile(r"^\*\*(\w+)\*\*:\s*(.+?)\s*$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")

_MAX_OUTPUT_PER_CHECK = 2000
_CHECK_TIMEOUT_SECS = 60


def parse_checklist(text: str) -> list[Criterion]:
    """Parse checklist markdown into a list of Criterion.

    Tolerant: skips malformed sections, accepts criterion/check fields in
    any order within a section. Returns empty list if no valid sections.
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
        check_text = current_fields.get("check", "").strip()
        # Extract bash command from backticks if present
        m = _BACKTICK_RE.search(check_text)
        if m:
            check_text = m.group(1)
        if crit_text and check_text:
            criteria.append(Criterion(
                id=current_id,
                name=current_name,
                criterion=crit_text,
                check=check_text,
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


async def run_check(criterion: Criterion, cwd: Path) -> CheckResult:
    """Run one criterion's check command in bash. PASS = exit code 0.

    Uses `bash -c` explicitly (not the default OS shell) so bash builtins
    like `test`, `$()`, `[[ ]]` work consistently across Windows + Unix.
    The LLM-compiled check commands are written in bash syntax.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            _BASH_PATH, "-c", criterion.check,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_CHECK_TIMEOUT_SECS,
        )
        exit_code = proc.returncode or 0
        output = stdout.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        return CheckResult(
            criterion=criterion, passed=False, exit_code=-1,
            output=f"[check timed out > {_CHECK_TIMEOUT_SECS}s]",
        )
    except Exception as e:
        return CheckResult(
            criterion=criterion, passed=False, exit_code=-2,
            output=f"[check exec error: {e}]",
        )

    if len(output) > _MAX_OUTPUT_PER_CHECK:
        output = output[-_MAX_OUTPUT_PER_CHECK:]  # tail-truncate

    return CheckResult(
        criterion=criterion,
        passed=(exit_code == 0),
        exit_code=exit_code,
        output=output.strip(),
    )


async def run_all_checks(checklist_path: Path, cwd: Path) -> list[CheckResult]:
    """Parse checklist file and run all checks sequentially.

    Returns empty list if file missing or parses to no criteria.
    """
    if not checklist_path.is_file():
        logger.warning(f"checklist.md not found at {checklist_path}")
        return []
    text = checklist_path.read_text(encoding="utf-8")
    criteria = parse_checklist(text)
    if not criteria:
        logger.warning(f"checklist.md at {checklist_path} parsed to 0 criteria")
        return []
    results: list[CheckResult] = []
    for c in criteria:
        results.append(await run_check(c, cwd))
    return results


_COMPILE_SYSTEM_PROMPT = """You translate a natural-language harvest requirement \
into a structured acceptance checklist in markdown. The checklist is the contract \
the harvest agent MUST satisfy before declaring mission complete.

Output format (strict):

# Acceptance Checklist

Generated from requirement: "<requirement>"

## C1. <short name>
**criterion**: <human-readable description>
**check**: `<bash command — exit 0 means PASS, non-zero means FAIL>`

## C2. <short name>
**criterion**: ...
**check**: `...`

Rules for check commands:
- Each check is a bash one-liner. Workspace cwd = harvest workspace dir.
- Output data lives under the workspace (agent picks subdir, often `data/`).
- Read-only reference samples are at `../samples/` (recon's output).
- PREFER `python -c "..."` over `jq` for JSON parsing (jq may not be installed).
- Use bash builtins / utils: `test`, `[[ ]]`, `wc`, `ls`, `find`, `grep`, `head`, `cat`.
- Each check must finish in < 60s and be idempotent.
- Quote variables; expect filenames with spaces or commas (run_dir often has them).

Coverage (write 3-7 criteria total):
1. Output count — does data quantity meet the boundary stated in the requirement?
2. Output shape — do output files have the same field set / structure as `../samples/`?
3. Content non-stub — is the actual content non-empty and plausibly real, not placeholder?
4. Topic relevance — if requirement specifies a topic/tag/keyword, does content match?
5. Reproducibility — does workspace contain a runnable crawler script (crawl.py or similar)?

Be specific to THIS requirement. Skip irrelevant categories (e.g. no count criterion \
for a "find one canonical example" requirement). Do NOT recommend tools or methods; \
ONLY define acceptance evidence.

Output ONLY the markdown. No preamble, no commentary."""


async def compile_checklist(
    llm,  # LLMClient — typed loosely to avoid circular import
    requirement: str,
    samples_dir: Path,
    output_path: Path,
) -> bool:
    """Compile requirement.txt into a checklist.md via one LLM call.

    Reads up to 5 sample filenames from samples_dir as shape context.
    Writes the result to output_path. On failure, writes a minimal stub
    and returns False — does NOT raise (harvest mission proceeds with
    weaker checks rather than aborting).
    """
    sample_listing = "(no samples available)"
    sample_preview = ""
    if samples_dir.is_dir():
        files = sorted(p for p in samples_dir.iterdir() if p.is_file())[:8]
        if files:
            sample_listing = "\n".join(
                f"- {p.name} ({p.stat().st_size} bytes)" for p in files
            )
        # Try to include one small sample's full content as shape reference
        for p in files:
            if p.suffix.lower() == ".json" and p.stat().st_size < 4000:
                try:
                    sample_preview = (
                        f"\n\nExample sample file ({p.name}):\n```json\n"
                        f"{p.read_text(encoding='utf-8')[:3500]}\n```"
                    )
                    break
                except Exception:
                    continue

    user_msg = (
        f"Requirement (boundary, verbatim):\n{requirement}\n\n"
        f"Samples directory (read-only shape reference, at ../samples/ from workspace):\n"
        f"{sample_listing}"
        f"{sample_preview}\n\n"
        f"Compile the acceptance checklist now."
    )

    try:
        response = await llm.generate(prompt=user_msg, system=_COMPILE_SYSTEM_PROMPT)
        if not response or not response.strip():
            raise RuntimeError("LLM returned empty checklist")
        if "## " not in response:
            raise RuntimeError(
                f"LLM output has no ## sections; first 200 chars: {response[:200]!r}"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(response, encoding="utf-8")
        logger.info(f"Checklist compiled to {output_path} ({len(response)} chars)")
        return True
    except Exception as e:
        logger.warning(f"Checklist compilation failed: {e}; writing stub")
        stub = (
            "# Acceptance Checklist (STUB — LLM compilation failed)\n\n"
            f'Generated from requirement: "{requirement}"\n\n'
            f"Compilation error: {e}\n\n"
            "The harvest agent should still treat the requirement above as the boundary "
            "and audit completion against it manually. The mark_done tool will run the "
            "single weak check below; agent self-audit per the system prompt is required.\n\n"
            "## C1. Workspace produces output\n"
            "**criterion**: workspace has at least one output file besides crawl artifacts\n"
            '**check**: `test "$(find . -maxdepth 2 -type f | wc -l)" -gt 1`\n'
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(stub, encoding="utf-8")
        return False


def format_results(results: list[CheckResult]) -> str:
    """Format results as agent-readable text (used in mark_done response)."""
    if not results:
        return "(no criteria — checklist.md missing or empty)"

    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]

    lines = [f"Checklist: {len(passed)}/{len(results)} PASS"]

    if passed and not failed:
        lines.append("")
        lines.append("Passed:")
        for r in passed:
            lines.append(f"- {r.criterion.id} ({r.criterion.name})")

    if failed:
        lines.append("")
        lines.append("Failed criteria (must address before mark_done):")
        for r in failed:
            lines.append(f"- {r.criterion.id} ({r.criterion.name}): {r.criterion.criterion}")
            lines.append(f"  check: `{r.criterion.check}`")
            lines.append(f"  exit_code: {r.exit_code}")
            if r.output:
                snippet = r.output[:600].replace("\n", " | ")
                lines.append(f"  output: {snippet}")

    return "\n".join(lines)
