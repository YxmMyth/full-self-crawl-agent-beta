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
