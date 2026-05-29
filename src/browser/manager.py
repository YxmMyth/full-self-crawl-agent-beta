"""Browser lifecycle management.

Connection priority (in order):
  1. BROWSER_WS_URL → playwright.firefox.connect(ws_url) — remote Camoufox
  2. BROWSER_CDP_URL → playwright.connect_over_cdp(url) — remote Chromium
  3. AsyncCamoufox(persistent_context=True, user_data_dir=per-domain) — default

If all three fail, launch raises RuntimeError. There is intentionally no
silent fallback to vanilla Playwright Chromium — it would have
`navigator.webdriver=true` and trigger Cloudflare instantly, masking the
real failure and pulling the agent into a reset loop.
See: docs/postmortem-2026-04-27-deferred.md, docs/fix-plan-2026-04-27-P0.md

One BrowserManager per domain — its profile is persisted at
`artifacts/_profiles/{domain}/` across runs (cookies, localStorage, IndexedDB).
Within same engine, headed↔headless can switch freely without losing state.

See: 架构共识文档.md §六 浏览器环境与策略
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import psutil

from src.browser.context import ToolContext
from src.browser.firefox_prefs import (
    SESSION_RESTORE_OFF,
    sanitize_profile,
    write_user_js,
)
from src.browser.network_capture import setup_network_capture
from src.config import Config
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Camoufox process name varies slightly across versions / OS. Keep both
# lowercased for case-insensitive match (`process_iter` returns the actual
# OS name, which on Windows is 'camoufox.exe').
_CAMOUFOX_PROC_NAMES = {"camoufox.exe", "camoufox"}


def _snapshot_camoufox_pids() -> set[int]:
    """Return the set of currently-alive camoufox process PIDs."""
    pids: set[int] = set()
    for p in psutil.process_iter(["name"]):
        try:
            name = (p.info.get("name") or "").lower()
            if name in _CAMOUFOX_PROC_NAMES:
                pids.add(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def _expand_with_children(pids: set[int]) -> set[int]:
    """Add descendants of each PID. Camoufox spawns a Firefox process tree
    (parent + content/GPU children); killing only the parent leaves children
    holding the profile lock, which is the exact zombie pattern that caused
    the 2026-05-24 svg-run cascade.
    """
    expanded: set[int] = set(pids)
    for pid in list(pids):
        try:
            proc = psutil.Process(pid)
            for child in proc.children(recursive=True):
                expanded.add(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return expanded

# Sized for Windows 11 @ 150% scaling (WorkingArea ≈ 1707×1019).
# Viewport accounts for ~84px of Firefox chrome (tab bar + address bar).
DEFAULT_WINDOW = (1280, 900)
DEFAULT_VIEWPORT = {"width": 1280, "height": 816}


class BrowserManager:
    """Manages browser lifecycle: launch, reset, close.

    Creates and maintains the ToolContext shared by all tools.
    Tied to a single domain — profile persists per-domain across runs.
    """

    def __init__(self, domain: str) -> None:
        self.domain = domain
        self._playwright: Any = None
        self._browser: Any = None  # Set only for non-persistent (Browser type)
        self._pw_context: Any = None  # The active BrowserContext (always set after launch)
        self._camoufox_ctx: Any = None  # AsyncCamoufox context manager (for cleanup)
        self._browser_type: str = "camoufox"
        self._headed: bool = True
        self._proxy: str | None = None
        self.ctx: ToolContext | None = None
        # Gateway is set by main.py once; auto-attached to ctx on every launch
        # so browser_reset doesn't lose the assist channel.
        self.gateway: Any = None
        # PIDs spawned by THIS manager's most recent launch. Computed as the
        # diff of process-name snapshots taken before vs after AsyncCamoufox
        # __aenter__. Used at close() to kill exactly the processes we own —
        # not all camoufox.exe on the machine. Borrowed from Codex's owned
        # `tokio::process::Child` pattern (utils/pty/src/process_group.rs).
        self._spawned_pids: set[int] = set()

    @property
    def profile_dir(self) -> Path:
        """Per-domain persistent profile dir. Auto-created on access."""
        d = Config.ARTIFACTS_DIR / "_profiles" / self.domain
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def launch(
        self,
        browser_type: str | None = None,
        headed: bool = True,
        proxy: str | None = None,
    ) -> ToolContext:
        """Launch browser and create ToolContext.

        Defaults to headed so human_assist can surface a visible window.

        Only `camoufox` is supported. If launch fails, raises RuntimeError —
        we do NOT silently fall back to vanilla Chromium because that would
        be detected by Cloudflare instantly, masking the real failure and
        sending the agent into a reset loop.
        """
        if browser_type and browser_type != "camoufox":
            raise ValueError(
                f"browser_type='{browser_type}' is not supported. "
                f"Only 'camoufox' is available. (Vanilla Chromium fallback was "
                f"removed on 2026-04-28 — see docs/fix-plan-2026-04-27-P0.md.)"
            )
        self._browser_type = "camoufox"
        self._headed = headed
        self._proxy = proxy

        page = None
        pw_context = None

        # Priority 1: Remote Camoufox via WebSocket (no per-domain profile)
        if Config.BROWSER_WS_URL:
            try:
                page, pw_context = await self._connect_ws(Config.BROWSER_WS_URL)
                logger.info("Connected via WebSocket", extra={"url": Config.BROWSER_WS_URL})
            except Exception as e:
                logger.warning(f"WS connection failed, trying next: {e}")

        # Priority 2: Remote Chromium via CDP (no per-domain profile)
        if page is None and Config.BROWSER_CDP_URL:
            try:
                page, pw_context = await self._connect_cdp(Config.BROWSER_CDP_URL)
                logger.info("Connected via CDP", extra={"url": Config.BROWSER_CDP_URL})
            except Exception as e:
                logger.warning(f"CDP connection failed, trying next: {e}")

        # Priority 3: Local Camoufox with persistent context (default)
        if page is None:
            # Firefox writes parent.lock at launch and deletes it on graceful
            # exit. Our close() uses psutil SIGKILL (psutil for owned-PID
            # tracking, 5ad5a2f), which leaves parent.lock orphaned. Next
            # launch then waits for the lock to release — Playwright times
            # out after 180s with the symptom "Juggler removeProgressListener
            # NS_ERROR_FAILURE". Confirmed 2026-05-28 by deleting parent.lock
            # before launch — headed boots in ~34s. Cheap defensive sweep:
            lock = self.profile_dir / "parent.lock"
            if lock.exists():
                try:
                    lock.unlink()
                    logger.info(f"Removed stale parent.lock from {self.profile_dir}")
                except OSError as e:
                    logger.warning(f"Could not remove parent.lock: {e}")
            page, pw_context = await self._launch_camoufox()
            logger.info(
                f"Launched Camoufox (headed={self._headed}, "
                f"profile={self.profile_dir})"
            )

        if page is None:
            raise RuntimeError(
                "All browser engines failed to launch. Check Camoufox install "
                "(`pip show camoufox`), profile dir permissions, or disk space."
            )

        self.ctx = ToolContext(pw_context=pw_context, tabs=[page])
        # Re-attach human_assist gateway across launches/resets
        if self.gateway is not None:
            self.ctx.human_assist = self.gateway

        self.ctx.setup_popup_close()
        setup_network_capture(page, self.ctx)
        self._setup_dialog_handler(page)

        return self.ctx

    async def reset(
        self,
        browser_type: str | None = None,
        headed: bool | None = None,
        proxy: str | None = None,
    ) -> ToolContext:
        """Close current browser and relaunch with new config.

        Same profile → cookies/state preserved across reset.

        Closes non-active tabs first to keep sessionstore.jsonlz4 small —
        even though we disable session restore via prefs, smaller writes
        mean faster close.
        """
        bt = browser_type or self._browser_type
        hd = headed if headed is not None else self._headed
        px = proxy if proxy is not None else self._proxy

        # Close non-active tabs to minimize what gets written to sessionstore.
        if self.ctx and len(self.ctx.tabs) > 1:
            keep = self.ctx.tabs[self.ctx.active_tab_idx]
            for tab in list(self.ctx.tabs):
                if tab is not keep:
                    try:
                        await tab.close()
                    except Exception:
                        pass

        await self.close()
        return await self.launch(browser_type=bt, headed=hd, proxy=px)

    async def close(self) -> None:
        """Close browser and clean up all resources."""
        if self.ctx:
            for tab in self.ctx.tabs:
                try:
                    await tab.close()
                except Exception:
                    pass
            self.ctx = None

        if self._camoufox_ctx:
            try:
                await self._camoufox_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._camoufox_ctx = None
            self._pw_context = None  # Owned by camoufox_ctx, already cleaned
        elif self._pw_context:
            # Standalone context (WS / CDP) — Camoufox path goes through _camoufox_ctx above.
            try:
                await self._pw_context.close()
            except Exception:
                pass
            self._pw_context = None

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        # PID-tracked cleanup — kill exactly the camoufox processes WE
        # spawned, verify each one is actually gone. Replaces the previous
        # `taskkill /F /IM camoufox.exe` (process-name based) which:
        #   - silently failed on Windows (stderr was DEVNULLed, return code ignored)
        #   - would race a concurrent mission if there ever were one
        #   - accumulated zombies that held the profile lock and broke the
        #     next launch within seconds (2026-05-24 svg-run cascade)
        # Inspired by Codex's owned-Child pattern (utils/pty/src/process_group.rs).
        residual = await self._kill_spawned_processes()
        if residual:
            # Genuinely couldn't kill them — log loud so operator can intervene
            # rather than letting the next launch race a stale process.
            logger.warning(
                f"Failed to kill {len(residual)} camoufox PID(s): {sorted(residual)}. "
                f"Next launch may collide on profile lock."
            )

        for name in ("parent.lock", ".startup-incomplete"):
            try:
                (self.profile_dir / name).unlink(missing_ok=True)
            except Exception as e:
                logger.debug(f"Profile lock cleanup skipped ({name}): {e}")

        logger.info(
            f"Browser closed (killed {len(self._spawned_pids) - len(residual)}/"
            f"{len(self._spawned_pids)} tracked PIDs)"
        )
        self._spawned_pids = set()

    async def _kill_spawned_processes(self) -> set[int]:
        """Kill tracked PIDs, verify each is gone. Returns set of PIDs that
        could not be killed (empty = clean kill).
        """
        if not self._spawned_pids:
            return set()

        # Step 1: SIGKILL each one we can still see. psutil.kill() = SIGKILL
        # on POSIX, TerminateProcess on Windows — both unconditional, no
        # cleanup handler runs.
        procs: list[psutil.Process] = []
        for pid in self._spawned_pids:
            try:
                p = psutil.Process(pid)
                p.kill()
                procs.append(p)
            except psutil.NoSuchProcess:
                continue  # already dead, fine
            except psutil.AccessDenied as e:
                logger.warning(f"AccessDenied killing camoufox PID {pid}: {e}")

        # Step 2: wait for each to actually exit (up to ~3s total).
        # psutil.wait_procs handles already-dead processes gracefully.
        if procs:
            gone, alive = psutil.wait_procs(procs, timeout=3)
            if alive:
                # Last resort — try kill once more, then surface remaining
                for p in alive:
                    try:
                        p.kill()
                    except Exception:
                        pass
                _, still_alive = psutil.wait_procs(alive, timeout=2)
                return {p.pid for p in still_alive}
        return set()

    def setup_page_listeners(self, page: Any) -> None:
        """Setup network capture and dialog handling on a new page/tab."""
        if self.ctx:
            setup_network_capture(page, self.ctx)
        self._setup_dialog_handler(page)

    # ── Private launch methods ───────────────────────────

    async def _launch_camoufox(self) -> tuple:
        """Launch local Camoufox with per-domain persistent context.

        Profile preparation (Tier 3 sanitize + Tier 2 user.js) happens before
        AsyncCamoufox — see firefox_prefs.py for the layered model.

        persistent_context=True → AsyncCamoufox returns BrowserContext directly,
        no separate Browser object.
        """
        from camoufox.async_api import AsyncCamoufox

        # Tier 3: clean stale volatile state (parent.lock, sessionstore-*) so a
        # prior unclean shutdown can't make this launch hang at session restore.
        sanitize_profile(self.profile_dir)

        # Tier 2: write user.js so Firefox reads our prefs at the very start
        # of boot, BEFORE it decides whether to restore. firefox_user_prefs
        # below is a defense-in-depth re-injection at runtime.
        write_user_js(self.profile_dir, SESSION_RESTORE_OFF)

        kwargs: dict[str, Any] = {
            "headless": not self._headed,
            "window": DEFAULT_WINDOW,
            "os": "windows",
            "humanize": True,
            "persistent_context": True,
            "user_data_dir": str(self.profile_dir),
            "viewport": DEFAULT_VIEWPORT,
            "firefox_user_prefs": SESSION_RESTORE_OFF,
        }
        if self._proxy:
            kwargs["proxy"] = {"server": self._proxy}

        # PID tracking — snapshot before/after so we can later kill exactly
        # the camoufox.exe processes WE spawned (and their descendants),
        # not all camoufox.exe on the machine (which would race a concurrent
        # mission and was previously a silent-fail mode anyway).
        before_pids = _snapshot_camoufox_pids()

        self._camoufox_ctx = AsyncCamoufox(**kwargs)
        pw_context = await self._camoufox_ctx.__aenter__()
        self._pw_context = pw_context
        page = pw_context.pages[0] if pw_context.pages else await pw_context.new_page()

        # Capture only the NEW PIDs (delta). Include descendants since
        # Camoufox spawns content/GPU child processes after the main one.
        after_pids = _snapshot_camoufox_pids()
        spawned = after_pids - before_pids
        self._spawned_pids = _expand_with_children(spawned)
        logger.debug(
            f"Spawned camoufox PIDs (incl. children): {sorted(self._spawned_pids)} "
            f"(direct={sorted(spawned)})"
        )

        return page, pw_context

    async def _connect_ws(self, ws_url: str) -> tuple:
        """Connect to remote browser via WebSocket (no profile persistence)."""
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.firefox.connect(ws_url)

        pw_context = await self._browser.new_context(viewport=DEFAULT_VIEWPORT)
        self._pw_context = pw_context
        page = await pw_context.new_page()

        return page, pw_context

    async def _connect_cdp(self, cdp_url: str) -> tuple:
        """Connect to remote Chromium via CDP."""
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)

        pw_context = (
            self._browser.contexts[0]
            if self._browser.contexts
            else await self._browser.new_context(viewport=DEFAULT_VIEWPORT)
        )
        self._pw_context = pw_context
        page = pw_context.pages[0] if pw_context.pages else await pw_context.new_page()

        return page, pw_context

    @staticmethod
    def _setup_dialog_handler(page: Any) -> None:
        """Auto-accept dialogs (alert/confirm/prompt). Transparent to agent."""
        async def handle_dialog(dialog: Any) -> None:
            logger.debug(f"Dialog auto-accepted: {dialog.type} '{dialog.message[:80]}'")
            await dialog.accept()

        page.on("dialog", handle_dialog)
