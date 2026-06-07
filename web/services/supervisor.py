"""RunHandle — owns the single serial mission subprocess.

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
import json
import os
import re
import time
from typing import Optional

from src.config import Config as AgentConfig
from src.utils.logging import get_logger
from web.config import WebConfig
from web.models import (
    ActiveRunState,
    ArtifactEvent,
    AssistPendingEvent,
    AuditEvent,
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


class RunHandle:
    """Owns the single serial run. One instance lives in app.state."""

    def __init__(
        self, bus: EventBus, db_read: DbReadService, artifacts: ArtifactService
    ) -> None:
        self.bus = bus
        self.db = db_read
        self.artifacts = artifacts

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._container_name: Optional[str] = None  # set when CONTAINERIZED
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

        self._run_id: Optional[str] = None
        self._domain: Optional[str] = None
        self._requirement: Optional[str] = None
        self._operator: str = ""
        self._mode: Optional[str] = None
        self._phase: str = "idle"
        self._started_at: Optional[float] = None
        self._gate_pending: bool = False
        self._assist_pending: bool = False
        self._stopping: bool = False
        self._finished: bool = False  # set terminal in _wait_exit → frees registry slot
        self._vnc_port: Optional[int] = None  # registry assigns a host port → noVNC

        self._runs_before: set[str] = set()
        self._obs_watermark: Optional[int] = None
        self._obs_count: int = 0
        self._seen_locations: set[str] = set()
        self._seen_sessions: dict[str, Optional[str]] = {}
        self._seen_audits: set[str] = set()
        self._status_json: dict = {}  # last-read status.json snapshot

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
            operator=self._operator or None,
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

            # Containerized (option B): wrap the mission in `docker run`. explore/
            # auto get a host-generated run_id injected via FSC_RUN_ID so host and
            # container agree on the run dir (poll loop starts at t=0); harvest
            # reuses from_run. Secrets ride env (forwarded by name), never argv.
            precomputed_run_id: Optional[str] = None
            if WebConfig.CONTAINERIZED:
                if not (req.from_run or mode == "harvest"):
                    precomputed_run_id = AgentConfig.make_run_id(req.requirement)
                    env["FSC_RUN_ID"] = precomputed_run_id
                env["DATABASE_URL"] = self._rewrite_db_host_for_container(
                    AgentConfig.DATABASE_URL
                )
                mission_args = argv[argv.index("src.main") + 1:]
                # Random suffix, not just int(time.time()): two runs launched in
                # the SAME second would otherwise collide on the docker --name and
                # the 2nd `docker run` fails with "name already in use".
                self._container_name = f"fsc-run-{int(time.time())}-{os.urandom(3).hex()}"
                label_run_id = precomputed_run_id or req.from_run or ""
                argv = self._docker_argv(mission_args, label_run_id)

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
            elif precomputed_run_id:
                self._set_run_id(precomputed_run_id)

            self._spawn(self._tail(self._proc.stdout, is_err=False))
            self._spawn(self._tail(self._proc.stderr, is_err=True))
            self._spawn(self._poll_loop())
            self._spawn(self._wait_exit())
            return self.state()

    async def stop(self) -> None:
        if not self.is_active or self._proc is None:
            return
        self._stopping = True

        # Containerized: terminating the local `docker run` client does NOT
        # reliably stop the container — stop it by name (SIGTERM→grace→SIGKILL);
        # --rm then reaps it. The client (_proc) exits once the container is gone.
        if WebConfig.CONTAINERIZED and self._container_name:
            logger.info(f"Stopping mission container {self._container_name}.")
            try:
                p = await asyncio.create_subprocess_exec(
                    "docker", "stop", "--time", "15", self._container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(p.wait(), timeout=30)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"docker stop failed: {e}")
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
            return

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

    # ── Containerized launch helpers (option B) ──────────

    def _docker_argv(self, mission_args: list[str], run_id_label: str = "") -> list[str]:
        """Wrap the mission command in `docker run` (option B). Secrets are
        forwarded by NAME only (-e VAR), pulled from the docker client's env, so
        they never appear in argv / logs. PostgreSQL is reached over the bridge
        via host.docker.internal; the container's noVNC :6080 is published to a
        host loopback port the existing /vnc proxy already targets."""
        host_artifacts = str(AgentConfig.ARTIFACTS_DIR.resolve()).replace("\\", "/")
        forward = [
            "HELMSMAN_RUN", "PYTHONUNBUFFERED", "RECON_HEADLESS",
            "HARVEST_HEADLESS", "DATABASE_URL", "LLM_API_KEY", "LLM_BASE_URL",
            "LLM_MODEL", "VISION_LLM_MODEL", "FSC_RUN_ID",
        ]
        argv = [
            "docker", "run", "--rm", "--init",
            "--name", self._container_name or "fsc-run",
            "--label", "fsc-run",
        ]
        if run_id_label:
            argv += ["--label", f"fsc-run-id={run_id_label}"]
        for var in forward:
            argv += ["-e", var]
        argv += ["--add-host", "host.docker.internal:host-gateway"]
        # Each run publishes its container noVNC (:6080) to its own assigned host
        # port; the /vnc/{run_id}/ proxy routes to it. Set by the registry.
        if self._vnc_port:
            argv += ["-p", f"127.0.0.1:{self._vnc_port}:6080"]
        argv += [
            "-v", f"{host_artifacts}:/app/artifacts",
            WebConfig.RUN_IMAGE,
            "python", "-u", "-m", "src.main",
        ]
        return argv + mission_args

    @staticmethod
    def _rewrite_db_host_for_container(db_url: str) -> str:
        """Rewrite a localhost DATABASE_URL so it resolves from inside a
        container. 127.0.0.1/localhost inside a container is the container's own
        loopback, not the host; host.docker.internal routes to the host (Docker
        Desktop provides it; on Linux --add-host=host-gateway does)."""
        return (
            db_url.replace("@localhost:", "@host.docker.internal:")
            .replace("@127.0.0.1:", "@host.docker.internal:")
        )

    async def answer_gate(self, decision: str) -> bool:
        """Answer the recon→harvest gate by writing its response file.

        The mission (src/cli/gate.py) under HELMSMAN_RUN=1 raises gate_pending
        via status.json and polls workspace/gate_response.json — the same
        cross-process signal-file pattern human-assist uses. We write the
        decision here; the gate consumes it and clears gate_pending on its next
        status.json flush. No stdin: this works when the mission is a docker-run
        container with no attached pipe (the legacy stdin "y\\n"/"n\\n" path is
        retired).
        """
        if not self.is_active or not self._domain or not self._run_id:
            return False
        if not self._gate_pending:
            return False
        decision = "continue" if decision == "continue" else "stop"
        workspace = WebConfig.run_dir(self._domain, self._run_id) / "workspace"
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "gate_response.json").write_text(
                json.dumps({"decision": decision}), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Failed to write gate decision: {e}")
            return False
        self._gate_pending = False
        self._phase = "harvest" if decision == "continue" else "done"
        self._emit_status()
        return True

    async def answer_assist(self, req_uuid: str, status: str) -> bool:
        """Answer a pending human-assist request by writing its response file.

        The WebGateway in the mission subprocess polls for
        workspace/assist_response_{uuid}.json. We write it here; the gateway
        picks it up and clears assist_pending on its next status.json flush.
        """
        if not self.is_active or not self._domain or not self._run_id:
            return False
        if status not in ("completed", "cancelled"):
            status = "completed"
        workspace = WebConfig.run_dir(self._domain, self._run_id) / "workspace"
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / f"assist_response_{req_uuid}.json").write_text(
                json.dumps({"status": status}), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Failed to write assist response: {e}")
            return False
        # Optimistic local clear; the next status.json read reconciles truth.
        self._assist_pending = False
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

        # status.json — authoritative phase/step/url + flags when present.
        self._read_status_json(domain, run_id)
        # audit verdicts (harvest)
        self._emit_new_audits(domain, run_id)

        self._emit_status(domain=domain, run_id=run_id, sessions=sessions)

    def _read_status_json(self, domain: str, run_id: str) -> None:
        """Read run_dir/status.json; let it override phase + flags, and publish
        gate/assist transitions. Falls back silently if absent (log inference
        from _inspect_line stays in effect)."""
        p = WebConfig.run_dir(domain, run_id) / "status.json"
        if not p.is_file():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        self._status_json = data

        phase = data.get("phase")
        if phase:
            self._phase = phase

        new_gate = bool(data.get("gate_pending"))
        if new_gate and not self._gate_pending:
            self.bus.publish(GatePendingEvent(pending=True))
        self._gate_pending = new_gate

        new_assist = bool(data.get("assist_pending"))
        if new_assist != self._assist_pending:
            self.bus.publish(
                AssistPendingEvent(
                    pending=new_assist,
                    uuid=data.get("assist_uuid"),
                    reason=data.get("assist_reason"),
                    timeout_s=data.get("assist_timeout_s"),
                )
            )
        self._assist_pending = new_assist

    def _emit_new_audits(self, domain: str, run_id: str) -> None:
        """Publish AuditEvent for any new verification/audit_round_N.json."""
        verif = WebConfig.run_dir(domain, run_id) / "verification"
        if not verif.is_dir():
            return
        for f in sorted(verif.glob("audit_round_*.json")):
            if f.name in self._seen_audits:
                continue
            self._seen_audits.add(f.name)
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            m = re.search(r"audit_round_(\d+)", f.name)
            self.bus.publish(
                AuditEvent(
                    round=int(m.group(1)) if m else 1,
                    overall=data.get("overall", ""),
                    blocking_summary=data.get("blocking_summary", ""),
                )
            )

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
        sj = self._status_json
        step = sj.get("step")
        current_url = sj.get("current_url")
        heartbeat_age = None
        if sj.get("updated_at"):
            heartbeat_age = max(0.0, time.time() - float(sj["updated_at"]))
        session_id = sj.get("session_id") or (sessions[-1].id if sessions else None)
        self.bus.publish(
            StatusEvent(
                phase=self._phase,
                session_id=session_id,
                step=step,
                current_url=current_url,
                counts=counts,
                heartbeat_age=heartbeat_age,
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
        self._finished = True  # terminal — the registry stops counting this slot

    # ── Helpers ──────────────────────────────────────────

    def _reset_for_new_run(self, req: LaunchRunRequest, mode: str) -> None:
        self.bus.reset()
        self.artifacts.reset()
        self._run_id = None
        self._domain = req.domain
        self._requirement = req.requirement
        self._operator = req.operator
        self._mode = mode
        self._phase = "launching"
        self._gate_pending = False
        self._assist_pending = False
        self._stopping = False
        self._obs_watermark = None
        self._obs_count = 0
        self._seen_locations = set()
        self._seen_sessions = {}
        self._seen_audits = set()
        self._status_json = {}

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


def _idle_state() -> ActiveRunState:
    """The 'no active run' state — what the legacy /api/runs/active returns when
    nothing has been launched yet."""
    return ActiveRunState(
        active=False, run_id=None, domain=None, requirement=None, mode=None,
        phase="idle", pid=None, started_at=None,
        gate_pending=False, assist_pending=False,
    )


class RunRegistry:
    """Owns all runs as a list of RunHandle, with admission control: a per-domain
    serial lock (one run per domain — Firefox single-profile) + a configurable
    concurrency cap (WebConfig.MAX_CONCURRENT_RUNS). Each run gets its OWN
    EventBus + ArtifactService for full isolation between concurrent runs.

    At cap=1 this is byte-identical to the old single supervisor: the legacy
    /api/stream + /api/runs/active/* resolve to the most-recent run via newest().
    Per-run addressing (/api/stream/{run_id}, the multi-run board) is a later
    step.
    """

    def __init__(self, db_read: DbReadService) -> None:
        self._db = db_read
        self._handles: list[RunHandle] = []
        self._admit_lock = asyncio.Lock()

    # ── views ────────────────────────────────────────────

    def _live(self) -> list[RunHandle]:
        """Handles still occupying a slot (launching or running) — for admission."""
        return [h for h in self._handles if not h._finished]

    def newest(self) -> Optional[RunHandle]:
        """The most-recent run (live or just-finished) — the single-run alias
        backing /api/stream + /api/runs/active/* so the frontend stays untouched."""
        return self._handles[-1] if self._handles else None

    def get(self, run_id: str) -> Optional[RunHandle]:
        """Resolve a handle by run_id (for the per-run endpoints, later step)."""
        return next((h for h in self._handles if h._run_id == run_id), None)

    def newest_state(self) -> ActiveRunState:
        h = self.newest()
        return h.state() if h is not None else _idle_state()

    def active_states(self) -> list[ActiveRunState]:
        """All live runs (for the multi-run board)."""
        return [h.state() for h in self._live()]

    def _prune_terminal(self, keep: int = 30) -> None:
        """Drop the oldest finished handles (keep all live + the last `keep`
        finished) so the registry doesn't grow without bound. A dropped run's
        /api/stream/{run_id} then idles — its DoneEvent was long since delivered."""
        finished = [h for h in self._handles if h._finished]
        if len(finished) <= keep:
            return
        drop = set(finished[: len(finished) - keep])
        self._handles = [h for h in self._handles if h not in drop]

    # ── admission + launch ───────────────────────────────

    async def launch(self, req: LaunchRunRequest) -> ActiveRunState:
        async with self._admit_lock:
            live = self._live()
            cap = WebConfig.MAX_CONCURRENT_RUNS
            if len(live) >= cap:
                raise RunActiveError(f"已达并发上限({cap}个)。先停止一个任务再发起。")
            if any(h._domain == req.domain for h in live):
                raise RunActiveError(
                    f"该域名({req.domain})已有任务在跑 —— 同域串行(共用登录档案)。"
                )
            # Reserve the slot + claim the domain ATOMICALLY before any await: a
            # fresh handle with its OWN bus + ArtifactService. Setting _domain now
            # makes a concurrent same-domain launch see the reservation (no TOCTOU).
            handle = RunHandle(EventBus(), self._db, ArtifactService())
            handle._domain = req.domain
            # Assign this run a free host port for its noVNC (each concurrent run
            # gets its own, reached via /vnc/{run_id}/).
            used = {h._vnc_port for h in self._live() if h._vnc_port is not None}
            base = WebConfig.NOVNC_HOST_PORT
            handle._vnc_port = next(
                p for p in range(base, base + WebConfig.MAX_CONCURRENT_RUNS + 1)
                if p not in used
            )
            self._handles.append(handle)
            self._prune_terminal(keep=30)  # bound memory: keep live + recent finished
        # Launch OUTSIDE the admission lock so different-domain launches are not
        # serialized behind the slow `docker run`.
        try:
            return await handle.launch(req)
        except Exception:
            handle._finished = True  # free the reservation on launch failure
            raise

    async def shutdown(self) -> None:
        """Stop every live run (lifespan shutdown)."""
        for h in self._live():
            try:
                await h.stop()
            except Exception:  # noqa: BLE001
                pass

    async def reap_orphans(self) -> None:
        """At STARTUP (no run live yet), remove leftover fsc-run containers from a
        previous console session. Done once here, NOT per-launch — a per-launch
        reap would kill live sibling runs (they all carry label=fsc-run)."""
        try:
            p = await asyncio.create_subprocess_exec(
                "docker", "ps", "-aq", "--filter", "label=fsc-run",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await p.communicate()
            ids = out.decode().split()
            if ids:
                logger.warning(
                    f"Reaping {len(ids)} orphan fsc-run container(s) at startup"
                )
                rm = await asyncio.create_subprocess_exec(
                    "docker", "rm", "-f", *ids,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await rm.wait()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"reap orphans failed: {e}")
