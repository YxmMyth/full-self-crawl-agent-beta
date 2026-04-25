"""Human assistance gateway — runtime layer between agent tools and the human.

Architecture:
  - Agent calls tool `request_human_assist(reason)` (declarative API)
  - Tool dispatches to HumanAssistGateway.request() (stable interface)
  - Concrete Gateway implementation handles the actual UX (terminal / web / push)

The interface is the contract; implementations swap freely.

Design rules (don't violate):
  - reason is plain str, no formatting assumptions (no ANSI, no width hint)
  - HumanResponse carries no UI state ("which key the user pressed")
  - Tool function does NOT call print/input directly — it goes through gateway
  - page reference stays inside gateway, doesn't leak up

See conversation 2026-04-25 for full architectural rationale.
"""

from __future__ import annotations

import asyncio
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class HumanResponse:
    """Result of a human assist request.

    Status semantics:
      - "completed":  human signalled they're done
      - "cancelled":  caller aborted (Ctrl+C / explicit cancel)
      - "timeout":    no signal within configured timeout
    """
    status: Literal["completed", "cancelled", "timeout"]
    message: str | None = None  # optional human-provided note (future use)


class HumanAssistGateway(ABC):
    """Abstract gateway for surfacing requests to a human.

    Subclasses implement different UX channels (terminal, web UI, mobile push,
    Slack, etc.). The agent and tool layers are blind to which is in use.
    """

    @abstractmethod
    async def request(
        self,
        reason: str,
        page: Any,
        timeout_s: float | None = None,
    ) -> HumanResponse:
        """Block until a human handles the situation, then return.

        Args:
            reason: free-text description of what's needed (set by agent's LLM).
            page:   playwright Page so gateway can foreground the browser window.
            timeout_s: optional max wait. None = wait forever (MVP default).
        """
        ...


class TerminalGateway(HumanAssistGateway):
    """MVP gateway: terminal print + signal-file polling.

    Flow:
      1. page.bring_to_front() — surface the browser window
      2. print formatted prompt to stdout with the agent's reason
      3. poll for signal file appearance (every 2s, heartbeat every 30s)
      4. consume signal, return HumanResponse(completed)

    Signal file path is per-instance (typically per-domain workspace dir),
    so multiple gateways for different domains don't collide.
    """

    HEARTBEAT_INTERVAL_S = 30
    POLL_INTERVAL_S = 2

    def __init__(self, signal_dir: Path | str) -> None:
        self.signal_dir = Path(signal_dir)
        self.signal_dir.mkdir(parents=True, exist_ok=True)
        self.signal_file = self.signal_dir / "HUMAN_DONE"

    async def request(
        self,
        reason: str,
        page: Any,
        timeout_s: float | None = None,
    ) -> HumanResponse:
        # Clean any stale signal from a prior run before we start watching
        if self.signal_file.exists():
            try:
                self.signal_file.unlink()
            except Exception:
                pass

        # Best-effort window foregrounding — may fail silently on some OSes
        try:
            await page.bring_to_front()
        except Exception as e:
            logger.warning(f"page.bring_to_front() failed (non-fatal): {e}")

        self._print_prompt(reason)
        logger.info(
            "Awaiting human assist",
            extra={"url": str(self.signal_file)},
        )

        try:
            await self._wait_for_signal(timeout_s)
        except asyncio.TimeoutError:
            logger.warning(f"Human assist timed out after {timeout_s}s")
            self._print_done(status="timeout")
            return HumanResponse(status="timeout", message=None)
        except asyncio.CancelledError:
            logger.warning("Human assist cancelled")
            self._print_done(status="cancelled")
            return HumanResponse(status="cancelled", message=None)

        # Consume signal
        try:
            self.signal_file.unlink()
        except FileNotFoundError:
            pass

        self._print_done(status="completed")
        logger.info("Human assist completed")
        return HumanResponse(status="completed", message=None)

    # ── Internal ─────────────────────────────────────────

    async def _wait_for_signal(self, timeout_s: float | None) -> None:
        """Wait for either signal file or stdin Enter (whichever first).

        Stdin path is only used when running in a real TTY — otherwise
        (background process, subprocess) we fall back to file-only.
        """
        tasks = [asyncio.create_task(self._poll_signal_file())]

        if sys.stdin and sys.stdin.isatty():
            tasks.append(asyncio.create_task(self._wait_for_stdin_enter()))

        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=timeout_s,
            )
            for p in pending:
                p.cancel()
            if not done:
                raise asyncio.TimeoutError()
            # Surface any exception from the winning task
            for d in done:
                d.result()
        except BaseException:
            for t in tasks:
                if not t.done():
                    t.cancel()
            raise

    async def _poll_signal_file(self) -> None:
        """Poll for signal file; heartbeat every 30s."""
        elapsed = 0
        while not self.signal_file.exists():
            await asyncio.sleep(self.POLL_INTERVAL_S)
            elapsed += self.POLL_INTERVAL_S
            if elapsed % self.HEARTBEAT_INTERVAL_S == 0:
                logger.info(f"Still waiting for human assist ({elapsed}s elapsed)")

    async def _wait_for_stdin_enter(self) -> None:
        """Block on stdin Enter via executor.

        Empty return = EOF (piped / closed stdin, e.g. when running under a
        non-interactive subprocess). EOF should NOT count as user input,
        otherwise we spuriously fire on subprocess startup. So we sleep forever
        on EOF and let the file-signal path win the race.
        """
        loop = asyncio.get_event_loop()
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if line == "":
            # EOF — wait forever; file signal wins
            await asyncio.Future()
        # else: real keypress, return successfully

    def _print_prompt(self, reason: str) -> None:
        bar = "=" * 64
        interactive = sys.stdin and sys.stdin.isatty()

        print(f"\n{bar}", flush=True)
        print("⏸  HUMAN ASSIST NEEDED", flush=True)
        print(bar, flush=True)
        print(f"Reason: {reason}", flush=True)
        print("", flush=True)
        print("在浏览器窗口完成所需操作,然后:", flush=True)
        if interactive:
            print("  → 回到这个终端按 Enter 继续", flush=True)
            print(f"  → 或创建信号文件: touch \"{self.signal_file}\"", flush=True)
        else:
            print(f"  → 创建信号文件: touch \"{self.signal_file}\"", flush=True)
        print(f"{bar}\n", flush=True)

    def _print_done(self, status: str) -> None:
        marker = {"completed": "✓", "timeout": "⏱", "cancelled": "✗"}.get(status, "•")
        print(f"\n{marker} Human assist {status}, agent resuming...\n", flush=True)
