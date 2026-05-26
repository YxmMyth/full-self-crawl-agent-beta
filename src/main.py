"""CLI entry point — run the Full-Self-Crawl-Agent.

Subcommands:
  explore <domain> <requirement>          # recon only (build World Model + samples)
  harvest <domain> [--from-run <id>]      # harvest against an existing recon run
  auto    <domain> <requirement> [--no-gate]  # recon → gate → harvest

Back-compat: positional `<domain> <requirement>` (no subcommand) is still
accepted and treated as `explore`.

See: CLAUDE.md §一 系统概述
"""

from __future__ import annotations

import argparse
import asyncio
import os
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
    human_assist as human_assist_tool, fetch,
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
    """Register all 14 execution agent tools."""
    registry = ToolRegistry()
    tools = [
        think, read_wm, browse, read_network, browser_eval, browser_reset,
        click, input_tool, press_key, scroll, go_back, bash_tool,
        human_assist_tool, fetch,
    ]
    for t in tools:
        registry.register(t.TOOL_NAME, t.TOOL_DESCRIPTION, t.TOOL_PARAMETERS, t.handle)
    return registry


async def run_explore(domain: str, requirement: str) -> None:
    """Full reconnaissance run — initialize, plan, execute, cleanup."""

    # Validate required config
    Config.require("LLM_API_KEY", "LLM_BASE_URL", "DATABASE_URL")

    # Generate run_id for this mission and create per-run artifacts dir
    run_id = Config.set_run_id(requirement)
    run_dir = Config.run_dir(domain)
    for subdir in ["samples", "catalog", "sessions", "workspace", "research", "verification"]:
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
    # Default headed (recon's human_assist relies on visible window). Override
    # via RECON_HEADLESS=1 when headed Camoufox launch hangs (Juggler/
    # removeProgressListener bug observed on Windows + Python 3.14 + recent
    # Camoufox — see docs/openslr_run_observations.md 2026-05-26).
    recon_headless = os.getenv("RECON_HEADLESS", "0") == "1"
    ctx = await browser_manager.launch(headed=not recon_headless)
    logger.info(
        f"Browser launched (headless={recon_headless}), "
        f"human_assist gateway = TkinterPopup"
    )

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


async def run_harvest_only(domain: str, source_run_id: str | None) -> None:
    """Run harvest standalone against an existing recon run."""
    from src.harvest.launcher import run_harvest
    await run_harvest(domain, source_run_id=source_run_id)


async def run_auto(domain: str, requirement: str, no_gate: bool) -> None:
    """End-to-end: recon → optional human gate → harvest."""
    from src.cli.gate import ask_continue_to_harvest, open_requirement_for_edit
    from src.harvest.launcher import run_harvest

    # Phase 1: recon (sets Config.RUN_ID as a side effect)
    await run_explore(domain, requirement)
    source_run_id = Config.RUN_ID

    # Phase 2: human gate between recon and harvest
    if not no_gate:
        decision = ask_continue_to_harvest(domain)
        if decision == "stop":
            logger.info("Operator chose to stop after recon. Skipping harvest.")
            return
        if decision == "edit":
            new_req = open_requirement_for_edit(domain)
            logger.info(f"Requirement after edit: {new_req[:200]}")
        # else: 'continue' → fall through

    # Phase 3: harvest against the same run_id
    await run_harvest(domain, source_run_id=source_run_id)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI args. Falls back to legacy positional form if no subcommand."""
    parser = argparse.ArgumentParser(
        prog="full-self-crawl",
        description="LLM-driven website reconnaissance + harvest agent.",
    )
    sub = parser.add_subparsers(dest="mode")

    p_explore = sub.add_parser("explore", help="Reconnaissance only (build WM + samples)")
    p_explore.add_argument("domain")
    p_explore.add_argument("requirement")

    p_harvest = sub.add_parser("harvest", help="Harvest against an existing recon run")
    p_harvest.add_argument("domain")
    p_harvest.add_argument(
        "--from-run",
        dest="from_run",
        default=None,
        help="Source recon run_id (default: latest run for the domain)",
    )

    p_auto = sub.add_parser("auto", help="Recon → human gate → Harvest")
    p_auto.add_argument("domain")
    p_auto.add_argument("requirement")
    p_auto.add_argument(
        "--no-gate",
        action="store_true",
        help="Skip the human gate between recon and harvest",
    )

    # Legacy form: `<domain> <requirement>` (no subcommand) → treat as explore
    if len(argv) >= 2 and argv[0] not in {"explore", "harvest", "auto", "-h", "--help"}:
        # Looks like the old positional form. Synthesize an `explore` subcommand.
        if len(argv) == 2:
            argv = ["explore", argv[0], argv[1]]

    return parser.parse_args(argv)


def main() -> None:
    """Entry point — dispatch on subcommand."""
    setup(level="INFO")
    args = _parse_args(sys.argv[1:])

    if args.mode is None:
        print(
            "Usage:\n"
            "  python -m src.main explore <domain> <requirement>\n"
            "  python -m src.main harvest <domain> [--from-run <run_id>]\n"
            "  python -m src.main auto    <domain> <requirement> [--no-gate]\n"
        )
        sys.exit(2)

    try:
        if args.mode == "explore":
            asyncio.run(run_explore(args.domain, args.requirement))
        elif args.mode == "harvest":
            asyncio.run(run_harvest_only(args.domain, args.from_run))
        elif args.mode == "auto":
            asyncio.run(run_auto(args.domain, args.requirement, args.no_gate))
        else:
            print(f"Unknown mode: {args.mode}")
            sys.exit(2)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
