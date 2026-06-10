"""The single wire contract between the Helmsman backend and the browser.

Every live event is a member of the `RunEvent` family, each keyed on `type`.
Request/response bodies for the REST endpoints are here too. Pydantic validates
on serialize, so backend drift is caught at the boundary rather than silently
corrupting the UI.

The browser receives these as Server-Sent Events: the SSE `event:` field is the
event `type`, the `data:` field is this model's JSON, and `id:` is the bus `seq`
(used for Last-Event-ID resume).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


# ── REST request / response bodies ───────────────────────


class LaunchRunRequest(BaseModel):
    """Body of POST /api/runs (accepted as an HTML form).

    requirement is optional ONLY for harvest-from-existing-run (where it is read
    from the recon run's requirement.txt); the endpoint enforces it for
    explore/auto. If from_run is set, the run is treated as harvest regardless
    of `mode`.
    """

    domain: str = Field(..., min_length=1)
    requirement: str = ""
    mode: Literal["explore", "auto", "harvest"] = "auto"
    gate: bool = True  # auto mode only; ask the operator at the recon→harvest gate
    headed: bool = True  # headed browser (login / CAPTCHA visible via noVNC)
    from_run: Optional[str] = None  # harvest from an existing recon run_id
    operator: str = ""  # who launched it (honor-system tag; no auth)


class GateDecision(BaseModel):
    decision: Literal["continue", "stop"]  # "edit" deferred (see design §6)


class RunSummary(BaseModel):
    run_id: str
    domain: str
    requirement: str = ""
    last_update: Optional[str] = None
    has_models: bool = False
    is_active: bool = False


class ActiveRunState(BaseModel):
    """Snapshot of the currently-running mission (GET /api/runs/active)."""

    active: bool = False
    run_id: Optional[str] = None
    domain: Optional[str] = None
    requirement: Optional[str] = None
    mode: Optional[str] = None
    phase: str = "idle"  # idle|launching|recon|gate|harvest|done|blocked|error|killed
    pid: Optional[int] = None
    started_at: Optional[str] = None
    gate_pending: bool = False
    assist_pending: bool = False
    ask_pending: bool = False  # ask_human text-question waiting on the operator
    operator: Optional[str] = None  # who launched it (honor-system tag)


# ── Live events (SSE firehose) ───────────────────────────


class Counts(BaseModel):
    locations: int = 0
    observations: int = 0
    sessions: int = 0
    samples: int = 0
    catalog: int = 0
    data_files: int = 0
    data_bytes: int = 0


class RunStartedEvent(BaseModel):
    type: Literal["run_started"] = "run_started"
    run_id: str
    domain: str
    requirement: str
    mode: str


class StatusEvent(BaseModel):
    type: Literal["status"] = "status"
    phase: str
    session_id: Optional[str] = None
    step: Optional[int] = None
    current_url: Optional[str] = None
    counts: Counts = Field(default_factory=Counts)
    heartbeat_age: Optional[float] = None
    gate_pending: bool = False
    assist_pending: bool = False
    ask_pending: bool = False


class ObservationEvent(BaseModel):
    type: Literal["observation"] = "observation"
    id: int
    location: str
    obs_type: str  # summary | insight | extract | other
    preview: str
    agent_step: Optional[int] = None
    created_at: Optional[str] = None


class LocationEvent(BaseModel):
    type: Literal["location"] = "location"
    pattern: str
    how_to_reach: Optional[str] = None


class SessionEvent(BaseModel):
    type: Literal["session"] = "session"
    session_id: str
    outcome: Optional[str] = None
    steps_taken: Optional[int] = None
    trajectory_summary: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


class ArtifactEvent(BaseModel):
    type: Literal["artifact"] = "artifact"
    dir: str  # samples | catalog | workspace | research | verification | data
    filename: str
    size: int
    change: Literal["added", "changed"] = "added"


class GatePendingEvent(BaseModel):
    type: Literal["gate_pending"] = "gate_pending"
    pending: bool = True


class AssistPendingEvent(BaseModel):
    type: Literal["assist_pending"] = "assist_pending"
    pending: bool
    uuid: Optional[str] = None
    reason: Optional[str] = None
    timeout_s: Optional[float] = None


class AskPendingEvent(BaseModel):
    """A text question (ask_human / intake / gate calibration) waiting on the
    operator. Sibling to AssistPendingEvent; carries the question, suggested
    options, and expects a typed answer back (vs assist's 完成/跳过 ack)."""
    type: Literal["ask_pending"] = "ask_pending"
    pending: bool
    uuid: Optional[str] = None
    question: Optional[str] = None
    timeout_s: Optional[float] = None
    options: Optional[list[str]] = None


class AuditEvent(BaseModel):
    type: Literal["audit"] = "audit"
    round: int = 1
    overall: str = ""  # PASS | BLOCKED
    blocking_summary: str = ""


class LogEvent(BaseModel):
    type: Literal["log"] = "log"
    level: str = "INFO"
    line: str = ""
    ts: Optional[str] = None


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    outcome: str  # mission_done | natural_stop | blocked | error | killed | ...
