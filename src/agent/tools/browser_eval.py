"""browser_eval — execute JavaScript in the browser page context.

Runs arbitrary JS (supports async/await). Can save results to files.
Provides programmatic hints for common errors.

See: docs/工具重新设计共识.md §2.2
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.browser.context import ToolContext
from src.config import Config
from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "browser_eval"
TOOL_DESCRIPTION = (
    "Execute JavaScript in the current page's browser context.\n\n"
    "Use for:\n"
    "- Extracting embedded JSON data (e.g. window.__NEXT_DATA__, __NUXT__)\n"
    "- Probing global variables and framework state\n"
    "- Extracting structured data from the DOM\n"
    "- Running async operations (fetch, etc.)\n\n"
    "The script runs in the page context with full DOM and JS access. "
    "Supports async/await. The last expression's value is returned.\n\n"
    "save_as: optional FILENAME (no directory, no '..') to save the result.\n"
    "kind: REQUIRED when save_as is given. Picks the directory:\n"
    "  'sample'    → samples/{save_as}    primary data (the deliverable)\n"
    "  'catalog'   → catalog/{save_as}    indexes / listings / API metadata / IDs\n"
    "  'workspace' → workspace/{save_as}  exploration / debug / scratch\n\n"
    "What counts as each (BE STRICT — verification will check):\n"
    "  sample = ONE concrete instance of the primary data, in native form. "
    "A list of pen IDs is NOT a sample.\n"
    "  catalog = metadata about samples (listings, API responses, IDs, URL maps).\n"
    "  workspace = exploration that may or may not pan out.\n\n"
    "Large results (>50KB) without save_as are auto-saved to workspace/."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "script": {
            "type": "string",
            "description": "JavaScript code to execute. Supports async/await. Last expression value is returned.",
        },
        "save_as": {
            "type": "string",
            "description": (
                "FILENAME ONLY (no directory, no '..'). Required-with kind. "
                "Examples: 'pens.json', 'lion_dom.html', 'extract_result.txt'."
            ),
        },
        "kind": {
            "type": "string",
            "enum": ["sample", "catalog", "workspace"],
            "description": (
                "REQUIRED when save_as is given. 'sample' for primary data, "
                "'catalog' for listings/IDs/metadata, 'workspace' for debug/scratch."
            ),
        },
    },
    "required": ["script"],
}

_KIND_DIRS = {"sample": "samples", "catalog": "catalog", "workspace": "workspace"}

_MAX_INLINE = 50 * 1024  # 50KB — beyond this, auto-save to workspace
_TIMEOUT_MS = 30000

# Common error patterns → helpful hints
_ERROR_HINTS = [
    ("Cannot read properties of null", "The element or object doesn't exist. Check the selector/variable name."),
    ("Cannot read properties of undefined", "The property chain has an undefined step. Try checking each part."),
    ("is not defined", "The variable doesn't exist in page scope. Check spelling or use window.varName."),
    ("is not a function", "The object exists but doesn't have that method. Check the API."),
    ("timeout", "Script exceeded 30s timeout. Try a simpler operation or break it into steps."),
    ("NetworkError", "Network request failed. The page might block cross-origin fetches."),
]


async def handle(ctx: ToolContext, **kwargs: Any) -> str:
    script: str = kwargs.get("script", "")
    save_as: str | None = kwargs.get("save_as")
    kind: str | None = kwargs.get("kind")
    if kind is not None:
        kind = kind.strip().lower() or None

    if not script.strip():
        return "Error: empty script"

    # Validate save_as + kind
    if save_as:
        if "/" in save_as or "\\" in save_as or ".." in save_as:
            return (
                "Error: save_as must be a FILENAME only (no directory, no '..'). "
                "The kind parameter picks the directory."
            )
        if not kind:
            return (
                "Error: kind is REQUIRED when save_as is given. "
                "Pick 'sample' (primary data deliverable), 'catalog' "
                "(metadata/listings/IDs), or 'workspace' (debug/scratch). "
                "If unsure, this content is probably catalog or workspace, not sample."
            )
        if kind not in _KIND_DIRS:
            return f"Error: kind must be one of {list(_KIND_DIRS)}, got '{kind}'."

    page = ctx.page
    domain = getattr(ctx, "_domain", "unknown")

    try:
        # Smart wrapping: auto-return the last expression if no explicit return
        wrapped_script = _wrap_script(script)
        wrapped = f"""
            Promise.race([
                (async () => {{ {wrapped_script} }})(),
                new Promise((_, reject) =>
                    setTimeout(() => reject(new Error('Script timeout ({_TIMEOUT_MS}ms)')), {_TIMEOUT_MS})
                )
            ])
        """
        result = await page.evaluate(wrapped)

    except Exception as e:
        error_msg = str(e)
        hints = _get_hints(error_msg)
        parts = [f"Error: {error_msg}"]
        if hints:
            parts.append(f"Hint: {hints}")
        return "\n".join(parts)

    # Format result
    if result is None:
        result_str = "(no return value)"
        result_type = "void"
    elif isinstance(result, (dict, list)):
        result_str = json.dumps(result, ensure_ascii=False, indent=2)
        result_type = "JSON"
    else:
        result_str = str(result)
        result_type = type(result).__name__

    result_size = len(result_str)

    # Save to file if requested (kind validated above)
    if save_as:
        artifacts_dir = Config.run_dir(domain)
        rel_path = f"{_KIND_DIRS[kind]}/{save_as}"
        save_path = artifacts_dir / rel_path
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(result_str, encoding="utf-8")
        logger.info(
            f"Saved to {save_path} (kind={kind})",
            extra={"tool": "browser_eval", "kind": kind},
        )
        return (
            f"[{result_type}, {_human_size(result_size)}] "
            f"Saved to {rel_path} (kind={kind})\n\nPreview:\n{result_str[:2000]}"
        )

    # Auto-save large results
    if result_size > _MAX_INLINE:
        artifacts_dir = Config.run_dir(domain)
        workspace = artifacts_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        # Generate filename
        import time
        filename = f"eval_{int(time.time())}.txt"
        filepath = workspace / filename
        filepath.write_text(result_str, encoding="utf-8")

        preview = result_str[:2000]
        return (
            f"[{result_type}, {_human_size(result_size)}] "
            f"Result too large for inline — saved to workspace/{filename}\n\n"
            f"Preview:\n{preview}\n...\n"
            f"(full result: {_human_size(result_size)} in workspace/{filename})"
        )

    # Normal inline result
    return f"[{result_type}, {_human_size(result_size)}]\n{result_str}"


def _wrap_script(script: str) -> str:
    """Auto-add return to last expression if user didn't write one.

    - Has explicit 'return' → use as-is (user knows what they're doing)
    - Single expression → return (expr)
    - Multi-statement, no return → add return before last line
    """
    stripped = script.strip().rstrip(";")
    if "return " in script or "return;" in script:
        return script

    lines = stripped.split("\n")
    # Filter out empty/comment-only lines from the end
    meaningful_lines = [l for l in lines if l.strip() and not l.strip().startswith("//")]

    if not meaningful_lines:
        return script

    if len(meaningful_lines) == 1:
        # Single expression — wrap with return
        return f"return ({stripped})"

    # Multi-statement: add return before last meaningful line
    last_line = meaningful_lines[-1].strip().rstrip(";")
    # Find and replace the last meaningful line in the original
    result_lines = list(lines)
    for i in range(len(result_lines) - 1, -1, -1):
        if result_lines[i].strip() == meaningful_lines[-1].strip():
            indent = len(result_lines[i]) - len(result_lines[i].lstrip())
            result_lines[i] = " " * indent + f"return ({last_line})"
            break

    return "\n".join(result_lines)


def _get_hints(error_msg: str) -> str:
    for pattern, hint in _ERROR_HINTS:
        if pattern.lower() in error_msg.lower():
            return hint
    return ""


def _human_size(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes}B"
    elif nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.0f}KB"
    else:
        return f"{nbytes / (1024 * 1024):.1f}MB"
