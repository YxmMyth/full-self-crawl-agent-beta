"""CLI entry point — run the Full-Self-Crawl-Agent.

Initializes all components, runs ReconPlanner, cleans up.
MVP: hardcoded domain + requirement (no CLI argument parsing).

See: CLAUDE.md §一 系统概述
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure project root on sys.path so `python src/main.py` works the same as
# `python -m src.main`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.agent.tools.registry import ToolRegistry
from src.agent.tools import (
    think, read_wm, browse, read_network, browser_eval, browser_reset,
    click, input as input_tool, press_key, scroll, go_back, bash_tool,
    human_assist as human_assist_tool,
)
from src.browser.manager import BrowserManager
from src.config import Config
from src.llm.client import LLMClient
from src.planner.recon_planner import ReconPlanner
from src.recording.agent import RecordingAgent
from src.runtime.human_assist import TkinterPopupGateway
from src.utils.logging import setup, get_logger
from src.world_model import db

logger = get_logger(__name__)


def build_execution_registry() -> ToolRegistry:
    """Register all 13 execution agent tools."""
    registry = ToolRegistry()
    tools = [
        think, read_wm, browse, read_network, browser_eval, browser_reset,
        click, input_tool, press_key, scroll, go_back, bash_tool,
        human_assist_tool,
    ]
    for t in tools:
        registry.register(t.TOOL_NAME, t.TOOL_DESCRIPTION, t.TOOL_PARAMETERS, t.handle)
    return registry


async def run(domain: str, requirement: str) -> None:
    """Full reconnaissance run — initialize, plan, execute, cleanup."""

    # Validate required config
    Config.require("LLM_API_KEY", "LLM_BASE_URL", "DATABASE_URL")

    # Generate run_id for this mission and create per-run artifacts dir
    run_id = Config.set_run_id(requirement)
    run_dir = Config.run_dir(domain)
    for subdir in ["samples", "sessions", "workspace", "research", "verification"]:
        (run_dir / subdir).mkdir(parents=True, exist_ok=True)
    # Record this run's requirement so other runs (and humans) can see why it ran
    (run_dir / "requirement.txt").write_text(requirement, encoding="utf-8")
    logger.info(f"Run ID: {run_id}")
    logger.info(f"Run dir: {run_dir}")

    # Initialize components
    await db.connect()
    logger.info("Database connected")

    browser_manager = BrowserManager(domain=domain)
    # Set gateway BEFORE first launch so it auto-attaches to ctx and survives
    # any subsequent browser_reset(). Tkinter desktop popup is the default —
    # always-on-top regardless of which app the user is currently looking at.
    browser_manager.gateway = TkinterPopupGateway()
    ctx = await browser_manager.launch()
    logger.info("Browser launched, human_assist gateway = TkinterPopup")

    llm = LLMClient()
    logger.info(f"LLM client ready (model={Config.LLM_MODEL})")

    # Build execution tool registry
    execution_registry = build_execution_registry()
    logger.info(f"Execution registry: {len(execution_registry.names())} tools")

    # Start singleton Recording Agent
    recording_agent = RecordingAgent(llm, domain)
    await recording_agent.start()
    logger.info("Recording Agent started")

    # Run ReconPlanner
    planner = ReconPlanner(
        domain=domain,
        requirement=requirement,
        llm=llm,
        browser_manager=browser_manager,
        recording_agent=recording_agent,
        execution_registry=execution_registry,
    )

    logger.info(f"Starting reconnaissance: {domain}")
    logger.info(f"Requirement: {requirement}")

    result = await planner.run()

    logger.info(f"Reconnaissance complete: {result}")

    # Cleanup
    await recording_agent.stop()
    await browser_manager.close()
    await llm.close()
    await db.close()
    logger.info("All resources cleaned up")


def main() -> None:
    """Entry point — accepts domain + requirement as CLI args or uses defaults."""
    setup(level="INFO")

    if len(sys.argv) >= 3:
        domain = sys.argv[1]
        requirement = sys.argv[2]
    else:
        # Default MVP target
        domain = "codepen.io"
        requirement = "找出 threejs 相关的 pen 数据：页面结构、数据来源（API/嵌入JSON/DOM）、提取方法、样本"

    try:
        asyncio.run(run(domain, requirement))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
