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


# ── Browser overlay gateway (default for headed local runs) ──────────


_OVERLAY_INIT_JS = r"""
(() => {
  const ID = '__claude_assist_overlay';

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }

  function ensureBody(cb) {
    if (document.body) return cb();
    const obs = new MutationObserver(() => {
      if (document.body) { obs.disconnect(); cb(); }
    });
    obs.observe(document.documentElement || document, { childList: true, subtree: true });
  }

  window.__renderAssistOverlay = (reason) => {
    ensureBody(() => {
      let el = document.getElementById(ID);
      if (!el) {
        el = document.createElement('div');
        el.id = ID;
        document.body.appendChild(el);
      }
      el.style.cssText = (
        'position:fixed;top:16px;right:16px;z-index:2147483647;' +
        'background:#fef3c7;color:#1f2937;' +
        'border:2px solid #f59e0b;border-radius:10px;' +
        'padding:16px 18px;max-width:360px;' +
        'box-shadow:0 10px 30px rgba(0,0,0,0.18);' +
        'font:14px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif;'
      );
      el.innerHTML = (
        '<div style="font-weight:600;color:#92400e;margin-bottom:8px;">' +
        '\u23F8 HUMAN ASSIST NEEDED</div>' +
        '<div id="' + ID + '_reason" style="margin-bottom:14px;white-space:pre-wrap;word-break:break-word;"></div>' +
        '<div style="display:flex;gap:8px;">' +
          '<button id="' + ID + '_done" style="flex:1;padding:8px 12px;background:#10b981;color:white;border:none;border-radius:6px;font-weight:600;cursor:pointer;">' +
          '\u5B8C\u6210 \u2713</button>' +
          '<button id="' + ID + '_cancel" style="padding:8px 12px;background:#e5e7eb;color:#374151;border:none;border-radius:6px;cursor:pointer;">' +
          '\u53D6\u6D88</button>' +
        '</div>'
      );
      document.getElementById(ID + '_reason').textContent = reason;
      document.getElementById(ID + '_done').onclick = () => {
        el.remove();
        if (window.humanAssistDone) window.humanAssistDone();
      };
      document.getElementById(ID + '_cancel').onclick = () => {
        el.remove();
        if (window.humanAssistCancel) window.humanAssistCancel();
      };
    });
  };

  window.__hideAssistOverlay = () => {
    const el = document.getElementById(ID);
    if (el) el.remove();
  };
})();
"""


class BrowserOverlayGateway(HumanAssistGateway):
    """Default gateway: yellow overlay injected into the agent's browser window.

    UX: prompt appears as a fixed-position card in top-right of every page.
    User completes whatever the reason describes, clicks 完成 in the overlay.
    Click → window.humanAssistDone() (Playwright-exposed) → resolves the
    Future Python is awaiting.

    Cross-navigation: if user navigates during assist, framenavigated handler
    re-renders overlay on the new page (init_script ensures __renderAssistOverlay
    is available on every page).

    Setup is lazy: first request() installs init_script + expose_function on
    the BrowserContext. If context changes (browser_reset), re-installs
    automatically.
    """

    def __init__(self) -> None:
        self._pending_future: asyncio.Future[HumanResponse] | None = None
        self._setup_for_context_id: int | None = None

    async def request(
        self,
        reason: str,
        page: Any,
        timeout_s: float | None = None,
    ) -> HumanResponse:
        ctx = page.context
        await self._ensure_setup(ctx)

        loop = asyncio.get_event_loop()
        self._pending_future = loop.create_future()

        # Show overlay on current page (best effort)
        try:
            await page.evaluate("(r) => window.__renderAssistOverlay(r)", reason)
        except Exception as e:
            logger.warning(f"Initial overlay render failed: {e}")

        # Best-effort: re-render on every navigation while still pending
        async def re_render(frame: Any) -> None:
            if frame is not page.main_frame:
                return
            if not self._pending_future or self._pending_future.done():
                return
            try:
                await page.evaluate("(r) => window.__renderAssistOverlay(r)", reason)
            except Exception as e:
                logger.debug(f"Re-render after navigation failed: {e}")

        def on_nav(frame: Any) -> None:
            asyncio.create_task(re_render(frame))

        page.on("framenavigated", on_nav)
        logger.info(f"Awaiting human assist (overlay): {reason[:80]}")

        try:
            try:
                await page.bring_to_front()
            except Exception:
                pass

            if timeout_s is None:
                response = await self._pending_future
            else:
                response = await asyncio.wait_for(self._pending_future, timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning(f"Overlay assist timed out after {timeout_s}s")
            response = HumanResponse(status="timeout", message=None)
        except asyncio.CancelledError:
            logger.warning("Overlay assist cancelled")
            response = HumanResponse(status="cancelled", message=None)
        finally:
            try:
                page.remove_listener("framenavigated", on_nav)
            except Exception:
                pass
            try:
                await page.evaluate("() => window.__hideAssistOverlay && window.__hideAssistOverlay()")
            except Exception:
                pass
            self._pending_future = None

        logger.info(f"Overlay assist {response.status}")
        return response

    async def _ensure_setup(self, ctx: Any) -> None:
        """Install init_script + expose_function on the context, idempotent.

        add_init_script applies only to FUTURE page loads — for already-open
        pages we additionally evaluate the script directly so helpers exist now.
        """
        ctx_id = id(ctx)
        if self._setup_for_context_id == ctx_id:
            return
        try:
            await ctx.add_init_script(_OVERLAY_INIT_JS)
        except Exception as e:
            logger.debug(f"add_init_script failed: {e}")
        try:
            await ctx.expose_function("humanAssistDone", self._on_done)
            await ctx.expose_function("humanAssistCancel", self._on_cancel)
        except Exception as e:
            # expose_function raises if name already taken on this context.
            # Persistent profile doesn't carry expose_function bindings across
            # processes, so this only fires on hot re-setup of same context.
            logger.debug(f"expose_function partial: {e}")
        # Apply helpers to already-open pages (init_script is future-only)
        for existing in list(getattr(ctx, "pages", []) or []):
            try:
                await existing.evaluate(_OVERLAY_INIT_JS)
            except Exception as e:
                logger.debug(f"inject into existing page failed: {e}")
        self._setup_for_context_id = ctx_id

    def _on_done(self) -> None:
        if self._pending_future and not self._pending_future.done():
            self._pending_future.set_result(HumanResponse(status="completed"))

    def _on_cancel(self) -> None:
        if self._pending_future and not self._pending_future.done():
            self._pending_future.set_result(HumanResponse(status="cancelled"))


# ── Tkinter desktop popup gateway (default) ──────────────────────────


def _show_tk_popup_blocking(reason: str) -> str:
    """Modal Tk dialog, always-on-top. Runs in an executor thread.

    Returns "completed" on 完成 click, "cancelled" on 跳过 / window close.
    """
    import tkinter as tk

    result = {"value": "cancelled"}  # default if user closes via X

    root = tk.Tk()
    root.title("Recon Agent — 需要你介入")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    body = tk.Frame(root, padx=24, pady=20)
    body.pack()

    tk.Label(
        body,
        text="⏸  HUMAN ASSIST NEEDED",
        font=("Microsoft YaHei UI", 11, "bold"),
        fg="#92400e",
    ).pack(anchor="w", pady=(0, 10))

    tk.Label(
        body,
        text=reason,
        font=("Microsoft YaHei UI", 10),
        wraplength=400,
        justify=tk.LEFT,
    ).pack(anchor="w", pady=(0, 16))

    btns = tk.Frame(body)
    btns.pack(anchor="center")

    def on_done():
        result["value"] = "completed"
        root.quit()
        root.destroy()

    def on_skip():
        result["value"] = "cancelled"
        root.quit()
        root.destroy()

    tk.Button(
        btns, text="完成 ✓", command=on_done, width=12, height=1,
        font=("Microsoft YaHei UI", 10, "bold"),
    ).pack(side=tk.LEFT, padx=6)
    tk.Button(
        btns, text="跳过", command=on_skip, width=12, height=1,
        font=("Microsoft YaHei UI", 10),
    ).pack(side=tk.LEFT, padx=6)

    # Center on screen
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

    root.protocol("WM_DELETE_WINDOW", on_skip)
    root.bind("<Escape>", lambda e: on_skip())
    root.lift()
    root.focus_force()

    root.mainloop()
    return result["value"]


class TkinterPopupGateway(HumanAssistGateway):
    """Always-on-top desktop dialog with 完成 / 跳过 buttons.

    Default gateway. Works regardless of which app the user is looking at —
    Tk dialog floats above all windows. User reads reason → switches to
    browser to handle (login/CAPTCHA/etc) → returns to dialog → clicks 完成.

    No browser dependency — works even if Camoufox/Chromium isn't running
    (useful if agent has crashed before browser launch and needs help).

    Tradeoff vs BrowserOverlay: requires user to switch focus between dialog
    and browser. Pro: visible from anywhere; con: extra context switches.
    """

    async def request(
        self,
        reason: str,
        page: Any,
        timeout_s: float | None = None,
    ) -> HumanResponse:
        # Best-effort: also try to surface the browser so user knows where to go.
        # No-op if it fails (Windows focus-stealing, headless, etc).
        try:
            if page is not None:
                await page.bring_to_front()
        except Exception:
            pass

        logger.info(f"Awaiting human assist (popup): {reason[:80]}")

        loop = asyncio.get_event_loop()
        try:
            if timeout_s is None:
                status = await loop.run_in_executor(None, _show_tk_popup_blocking, reason)
            else:
                status = await asyncio.wait_for(
                    loop.run_in_executor(None, _show_tk_popup_blocking, reason),
                    timeout=timeout_s,
                )
            response = HumanResponse(status=status)
        except asyncio.TimeoutError:
            logger.warning(f"Popup assist timed out after {timeout_s}s")
            response = HumanResponse(status="timeout")
        except asyncio.CancelledError:
            logger.warning("Popup assist cancelled")
            response = HumanResponse(status="cancelled")

        logger.info(f"Popup assist {response.status}")
        return response
