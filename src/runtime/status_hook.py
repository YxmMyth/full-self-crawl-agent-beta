"""status.json heartbeat — the authoritative live-status contract for Helmsman.

The web console (web/services/supervisor.py) can infer most of a run's state from
logs + Postgres + the filesystem, but two things are awkward that way: the exact
current step / URL the agent is on, and whether the run is *alive* (heartbeat
age) vs silently stalled. This module writes a small, atomically-replaced
`run_dir/status.json` the console reads to fill those gaps and de-fragilize the
RUN_ID / phase capture.

Design rules:
  - **Zero cost off Helmsman.** Every function is a no-op unless the env var
    HELMSMAN_RUN=1 is set (the web supervisor sets it on the subprocess). Plain
    terminal runs never touch disk here.
  - **Additive only.** The engine never reads this back; it is write-only
    telemetry. Losing or corrupting it cannot affect a run.
  - **Atomic.** Write to a temp file then os.replace() (atomic on Win + POSIX),
    so a reader never sees a half-written file.

Wiring (all guarded internally, so call sites stay clean):
  - main.py:        configure(run_dir); set_phase("recon"); gate flags; phase done
  - harvest/launcher: configure(run_dir); set_phase("harvest")
  - agent/session:  heartbeat(session_id, step, current_url) once per tool step
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

_ENABLED = os.getenv("HELMSMAN_RUN") == "1"

# Module-level current snapshot. Whole dict is rewritten on every update so the
# console always sees a complete, consistent status object.
_state: dict[str, Any] = {
    "phase": "launching",
    "run_id": None,
    "domain": None,
    "session_id": None,
    "step": None,
    "current_url": None,
    "gate_pending": False,
    "assist_pending": False,
    "assist_reason": None,
    "assist_uuid": None,
    "assist_timeout_s": None,
    "updated_at": None,
}
_path: Optional[Path] = None


def enabled() -> bool:
    return _ENABLED


def configure(run_dir: Path, domain: str | None = None, run_id: str | None = None) -> None:
    """Point the hook at a run dir. Idempotent; safe to call per phase."""
    global _path
    if not _ENABLED:
        return
    _path = Path(run_dir) / "status.json"
    if domain is not None:
        _state["domain"] = domain
    if run_id is not None:
        _state["run_id"] = run_id
    _flush()


def set_phase(phase: str, **fields: Any) -> None:
    """Record a phase transition (launching|recon|gate|harvest|done|blocked|...)."""
    if not _ENABLED:
        return
    _state["phase"] = phase
    for k, v in fields.items():
        _state[k] = v
    _flush()


def set_flag(**flags: Any) -> None:
    """Set boolean/aux flags, e.g. gate_pending=True, assist_pending=False."""
    if not _ENABLED:
        return
    _state.update(flags)
    _flush()


def heartbeat(
    session_id: str | None = None,
    step: int | None = None,
    current_url: str | None = None,
) -> None:
    """Per-step liveness ping. Cheap; called from the agent loop each tool step."""
    if not _ENABLED:
        return
    if session_id is not None:
        _state["session_id"] = session_id
    if step is not None:
        _state["step"] = step
    if current_url is not None:
        _state["current_url"] = current_url
    _flush()


def _flush() -> None:
    if not _ENABLED or _path is None:
        return
    _state["updated_at"] = time.time()
    try:
        # Atomic write: temp file in the same dir, then os.replace.
        fd, tmp = tempfile.mkstemp(
            dir=str(_path.parent), prefix=".status-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_state, f, ensure_ascii=False)
            os.replace(tmp, _path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception:
        # Telemetry must never break a run.
        pass
