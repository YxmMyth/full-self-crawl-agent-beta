"""bash — execute system commands outside the browser.

Spawns a new process each time (stateless). Working directory fixed to
artifacts/{domain}/workspace/. Output tail-truncated at 30K chars.
Large outputs auto-saved to workspace/.

Use for: API replay with curl/curl_cffi, data processing, file operations,
running extraction scripts, HTTP requests with browser TLS fingerprinting.

See: docs/工具重新设计共识.md §2.2
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from src.config import Config
from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "bash"
TOOL_DESCRIPTION = (
    "Execute a shell command outside the browser.\n\n"
    "Each call spawns a fresh process — no state carries between calls.\n\n"
    "IMPORTANT — Working directory and file paths:\n"
    "- CWD is artifacts/{domain}/workspace/\n"
    "- Saved samples are at ../samples/ (one level up from cwd)\n"
    "- Scripts are at ../scripts/\n"
    "- Example: cat ../samples/data.json  |  ls ../  |  ls .\n\n"
    "Use for:\n"
    "- API replay: curl_cffi for browser-like TLS fingerprints\n"
    "- Data processing: Python scripts\n"
    "- File verification: check saved samples\n\n"
    "Tip: curl_cffi example:\n"
    "  python -c \"from curl_cffi import requests; r = requests.get('URL', impersonate='chrome'); print(r.text[:500])\"\n\n"
    "Output capped at 30,000 chars (tail-truncated). Large outputs auto-saved."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "Shell command to execute.",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in milliseconds (default 120000, max 600000).",
        },
    },
    "required": ["command"],
}

_MAX_OUTPUT = 30000  # chars
_DEFAULT_TIMEOUT_MS = 120000
_MAX_TIMEOUT_MS = 600000
_AUTO_SAVE_THRESHOLD = 30000


async def handle(ctx: Any, **kwargs: Any) -> str:
    command: str = kwargs.get("command", "")
    timeout_ms: int = kwargs.get("timeout", _DEFAULT_TIMEOUT_MS)

    if not command.strip():
        return "Error: empty command"

    timeout_ms = min(timeout_ms, _MAX_TIMEOUT_MS)
    timeout_s = timeout_ms / 1000.0

    domain = getattr(ctx, "_domain", "unknown")
    workspace = Config.artifacts_for(domain) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(workspace),
        )

        try:
            stdout_bytes, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            process.kill()
            return f"Command timed out after {timeout_ms}ms. Consider breaking into smaller steps or increasing timeout."

    except Exception as e:
        return f"Failed to execute command: {e}"

    exit_code = process.returncode
    output = stdout_bytes.decode("utf-8", errors="replace")

    # Auto-save large outputs
    saved_path = None
    if len(output) > _AUTO_SAVE_THRESHOLD:
        filename = f"bash_{int(time.time())}.txt"
        filepath = workspace / filename
        filepath.write_text(output, encoding="utf-8")
        saved_path = f"workspace/{filename}"
        logger.info(f"Large bash output saved to {filepath}", extra={"tool": "bash"})

    # Truncate for display
    truncated = False
    if len(output) > _MAX_OUTPUT:
        removed = len(output) - _MAX_OUTPUT
        output = output[-_MAX_OUTPUT:]  # keep tail (most recent output)
        truncated = True

    # Build result
    parts = [f"[cwd: {workspace}]"]
    if truncated:
        removed_kb = removed / 1024
        parts.append(f"[output truncated — {removed_kb:.0f}KB removed from beginning]")
    parts.append(output.rstrip())
    parts.append(f"\n[exit code: {exit_code}]")
    if saved_path:
        parts.append(f"[full output saved to: {saved_path}]")

    return "\n".join(parts)
