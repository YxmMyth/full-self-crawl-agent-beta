"""Load the harvest system prompt from prompts/harvest_prompt.md."""

from __future__ import annotations

from pathlib import Path

HARVEST_SYSTEM_PROMPT: str = (
    Path(__file__).parent / "prompts" / "harvest_prompt.md"
).read_text(encoding="utf-8")
