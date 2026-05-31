"""RunSupervisor — owns the single serial mission subprocess.

The agent runs in its OWN process (`python -u -m src.main …`) so the console
survives an agent crash / Camoufox OOM and can show the failure. The supervisor:

  - spawns the subprocess (env: HELMSMAN_RUN=1, headed flags, inherited DISPLAY)
  - captures RUN_ID two ways: the `Run ID:` log line, then a filesystem-newest
    fallback (the same auto-discovery harvest itself uses)
  - tails stdout + stderr → `log` events; infers phase; detects the gate prompt
  - runs a poll loop → DB deltas (locations / observations / sessions) +
    filesystem deltas (artifacts) + a coalesced `status` event with counts
  - drives the gate via stdin and stop() via terminate()

Single active run, enforced here (409 on a second launch).

Phase 1 infers phase from logs. Phase 2 adds status.json as the authoritative
source; the log-based inference stays as the fallback.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Optional

from src.utils.logging import get_logger
from web.config import WebConfig
from web.models import (
    ActiveRunState,
    ArtifactEvent,
    Counts,
    DoneEvent,
    GatePendingEvent,
    LaunchRunRequest,
    LocationEvent,
    LogEvent,
    ObservationEvent,
    RunStartedEvent,
    SessionEvent,
    StatusEvent,
)
from web.services.artifacts import ArtifactService
from web.services.db_read import (
    DbReadService,
    classify_observation,
    observation_preview,
)
from web.services.eventbus import EventBus

logger = get_logger("helmsman.supervisor")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_LEVEL = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")
_RUN_ID_PATTERNS = [re.compile(r"Run ID:\s*(\S+)"), re.compile(r"run_id=(\S+)")]
_GATE_MARKERS = ("RECON COMPLETE", "Continue to harvest?")
_HARVEST_MARKER = "Harvest:"


class RunActiveError(RuntimeError):
    """Raised when a launch is attempted while a run is already active."""


class RunSupervisor:
    """Owns the single serial run. One instance lives in app.state."""

    def __init__(
        self, bus: EventBus, db_read: DbReadService, artifacts: ArtifactService
    ) -> None:
        self.bus = bus
        self.db = db_read
        self.artifacts = artifacts

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

        self._run_id: Optional[str] = None
        self._domain: Optional[str] = None
        self._requirement: Optional[str] = None
        self._mode: Optional[str] = None
        self._phase: str = "idle"
        self._started_at: Optional[float] = None
        self._gate_pending: bool = False
        self._assist_pending: bool = False
        self._stopping: bool = False

        self._runs_before: set[str] = set()
        self._obs_watermark: Optional[int] = None
        self._obs_count: int = 0
        self._seen_locations: set[str] = set()
        self._seen_sessions: dict[str, Optional[str]] = {}

    # ── Public API ───────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def state(self) -> ActiveRunState:
        return ActiveRunState(
            active=self.is_active,
            run_id=self._run_id,
            domain=self._domain,
            requirement=self._requirement,
            mode=self._mode,
            phase=self._phase,
            pid=self._proc.pid if self._proc else None,
            started_at=_iso(self._started_at),
            gate_pending=self._gate_pending,
            assist_pending=self._assist_pending,
        )

    async def launch(self, req: LaunchRunRequest) -> ActiveRunState:
        async with self._lock:
            if self.is_active:
                raise RunActiveError("A mission is already running.")

            argv, mode = self._build_argv(req)
            self._reset_for_new_run(req, mode)
            self._runs_before = self._existing_run_dirs(req.domain)

            env = os.environ.copy()
            env["HELMSMAN_RUN"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            headless = "0" if req.headed else "1"
            env["RECON_HEADLESS"] = headless
            env["HARVEST_HEADLESS"] = headless

            logger.info(f"Launching: {' '.join(argv)}")
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(WebConfig.PROJECT_ROOT),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._started_at = time.time()
            self._phase = "harvest" if mode == "harvest" else "launching"
            if mode == "harvest" and req.from_run:
                self._set_run_id(req.from_run)

            self._spawn(self._tail(self._proc.stdout, is_err=False))
            self._spawn(self._tail(self._proc.stderr, is_err=True))
            self._spawn(self._poll_loop())
            self._spawn(self._wait_exit())
            return self.state()

    async def stop(self) -> None:
        if not self.is_active or self._proc is None:
            return
        self._stopping = True
        logger.info("Stopping mission (terminate).")
        try:
            self._proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass

    async def answer_gate(self, decision: str) -> bool:
        if not self.is_active or self._proc is None or self._proc.stdin is None:
            return False
        if not self._gate_pending:
            return False
        line = "y\n" if decision == "continue" else "n\n"
        try:
            self._proc.stdin.write(line.encode())
            await self._proc.stdin.drain()
        except Exception as e:
            logger.warning(f"Failed to write gate decision: {e}")
            return False
        self._gate_pending = False
        self._phase = "harvest" if decision == "continue" else "done"
        self._emit_status()
        return True

    # ── Subprocess argv ──────────────────────────────────

    def _build_argv(self, req: LaunchRunRequest) -> tuple[list[str], str]:
        base = [WebConfig.PYTHON_EXE, "-u", "-m", "src.main"]
        if req.from_run or req.mode == "harvest":
            argv = base + ["harvest", req.domain]
            if req.from_run:
                argv += ["--from-run", req.from_run]
            return argv, "harvest"
        if req.mode == "explore":
            return base + ["explore", req.domain, req.requirement], "explore"
        argv = base + ["auto", req.domain, req.requirement]
        if not req.gate:
            argv.append("--no-gate")
        return argv, "auto"

    # ── Tailing stdout / stderr ──────────────────────────

    async def _tail(self, stream, is_err: bool) -> None:
        if stream is None:
            return
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = _ANSI.sub("", raw.decode("utf-8", errors="replace")).rstrip("\n")
            if not line:
                continue
            self._inspect_line(line)
            level_m = _LEVEL.search(line)
            self.bus.publish(
                LogEvent(
                    level=level_m.group(1) if level_m else ("ERR" if is_err else "OUT"),
                    line=line,
                    ts=_iso(time.time()),
                )
            )

    def _inspect_line(self, line: str) -> None:
        if self._run_id is None:
            for pat in _RUN_ID_PATTERNS:
                m = pat.search(line)
                if m:
                    self._set_run_id(m.group(1))
                    break
        if _HARVEST_MARKER in line and self._phase in ("launching", "recon", "auto"):
            self._phase = "harvest"
            self._emit_status()
        if any(mk in line for mk in _GATE_MARKERS) and not self._gate_pending:
            self._gate_pending = True
            self._phase = "gate"
            self.bus.publish(GatePendingEvent(pending=True))
            self._emit_status()

    # ── RUN_ID handling ──────────────────────────────────

    def _set_run_id(self, run_id: str) -> None:
        if self._run_id is not None:
            return
        self._run_id = run_id
        if self._phase == "launching":
            self._phase = "recon" if self._mode in ("explore", "auto") else "harvest"
        logger.info(f"Captured run_id={run_id}")
        self.bus.publish(
            RunStartedEvent(
                run_id=run_id,
                domain=self._domain or "",
                requirement=self._requirement or "",
                mode=self._mode or "",
            )
        )

    def _existing_run_dirs(self, domain: str) -> set[str]:
        root = WebConfig.runs_root(domain)
        if not root.is_dir():
            return set()
        return {d.name for d in root.iterdir() if d.is_dir()}

    def _fallback_run_id(self) -> Optional[str]:
        if self._domain is None:
            return None
        fresh = self._existing_run_dirs(self._domain) - self._runs_before
        return max(fresh) if fresh else None  # run_ids are timestamp-prefixed

    # ── Poll loop (DB + filesystem deltas → events) ──────

    async def _poll_loop(self) -> None:
        deadline = time.time() + 30
        while self._run_id is None and self.is_active:
            await asyncio.sleep(1.0)
            fb = self._fallback_run_id()
            if fb:
                self._set_run_id(fb)
                break
            if time.time() > deadline:
                logger.warning("RUN_ID not captured within 30s; poller idle.")
        if self._run_id is None:
            return

        domain, run_id = self._domain, self._run_id
        assert domain is not None
        while True:
            running = self.is_active
            try:
                await self._poll_once(domain, run_id)
            except Exception as e:
                logger.debug(f"poll error: {e}")
            if not running:
                break  # one final sweep after exit, then stop
            await asyncio.sleep(WebConfig.POLL_INTERVAL_S)

    async def _poll_once(self, domain: str, run_id: str) -> None:
        for loc in await self.db.list_locations(domain):
            if loc.run_id not in (run_id, None):
                continue
            if loc.pattern in self._seen_locations:
                continue
            self._seen_locations.add(loc.pattern)
            self.bus.publish(
                LocationEvent(pattern=loc.pattern, how_to_reach=loc.how_to_reach)
            )

        for o in await self.db.list_observations_since(domain, self._obs_watermark, run_id):
            self._obs_watermark = (
                o.id if self._obs_watermark is None else max(self._obs_watermark, o.id)
            )
            self._obs_count += 1
            self.bus.publish(
                ObservationEvent(
                    id=o.id,
                    location=o.location_id,
                    obs_type=classify_observation(o.raw),
                    preview=observation_preview(o.raw),
                    agent_step=o.agent_step,
                    created_at=_iso_dt(o.created_at),
                )
            )

        sessions = await self.db.list_sessions(run_id)
        for s in sessions:
            ended = _iso_dt(s.ended_at)
            if self._seen_sessions.get(s.id, "__missing__") == ended:
                continue
            self._seen_sessions[s.id] = ended
            self.bus.publish(
                SessionEvent(
                    session_id=s.id,
                    outcome=s.outcome,
                    steps_taken=s.steps_taken,
                    trajectory_summary=s.trajectory_summary,
                    started_at=_iso_dt(s.started_at),
                    ended_at=ended,
                )
            )

        for ch in self.artifacts.diff_new_files(domain, run_id):
            self.bus.publish(ArtifactEvent(**ch))

        self._emit_status(domain=domain, run_id=run_id, sessions=sessions)

    def _emit_status(
        self, domain: str | None = None, run_id: str | None = None, sessions=None
    ) -> None:
        domain = domain or self._domain
        run_id = run_id or self._run_id
        counts = Counts(
            locations=len(self._seen_locations),
            observations=self._obs_count,
            sessions=len(self._seen_sessions),
        )
        if domain and run_id:
            try:
                fs = self.artifacts.counts(domain, run_id)
                counts.samples = fs.get("samples", 0)
                counts.catalog = fs.get("catalog", 0)
                counts.data_files = fs.get("data_files", 0)
                counts.data_bytes = fs.get("data_bytes", 0)
            except Exception:
                pass
        last_session = sessions[-1].id if sessions else None
        self.bus.publish(
            StatusEvent(
                phase=self._phase,
                session_id=last_session,
                counts=counts,
                gate_pending=self._gate_pending,
                assist_pending=self._assist_pending,
            )
        )

    # ── Exit handling ────────────────────────────────────

    async def _wait_exit(self) -> None:
        if self._proc is None:
            return
        rc = await self._proc.wait()
        logger.info(f"Mission process exited rc={rc}")
        await asyncio.sleep(WebConfig.POLL_INTERVAL_S + 0.5)  # let poller sweep
        if self._stopping:
            outcome, self._phase = "killed", "killed"
        elif rc == 0:
            outcome, self._phase = "completed", "done"
        else:
            outcome, self._phase = "error", "error"
        self._emit_status()
        self.bus.publish(DoneEvent(outcome=outcome))

    # ── Helpers ──────────────────────────────────────────

    def _reset_for_new_run(self, req: LaunchRunRequest, mode: str) -> None:
        self.bus.reset()
        self.artifacts.reset()
        self._run_id = None
        self._domain = req.domain
        self._requirement = req.requirement
        self._mode = mode
        self._phase = "launching"
        self._gate_pending = False
        self._assist_pending = False
        self._stopping = False
        self._obs_watermark = None
        self._obs_count = 0
        self._seen_locations = set()
        self._seen_sessions = {}

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _iso(ts: Optional[float]) -> Optional[str]:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts is not None else None


def _iso_dt(dt) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.strftime("%H:%M:%S")
    except Exception:
        return str(dt)
