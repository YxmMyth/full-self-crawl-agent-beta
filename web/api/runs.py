"""REST endpoints for launching and controlling runs.

The launch form (HTMX) posts here; on success we HX-Redirect to /run?run_id= so
the operator's dashboard tracks THEIR run (not just whichever is newest).

Control endpoints come in two flavours:
  - /runs/active/{stop,gate,assist} → the most-recent run (legacy single-run alias)
  - /runs/{run_id}/{stop,gate,assist} → a specific run (per-run, for N>1)
The 'active' routes are declared first so {run_id} never shadows 'active'.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, Response

from src.utils.logging import get_logger
from web.models import GateDecision, LaunchRunRequest
from web.services.supervisor import RunActiveError, RunRegistry

logger = get_logger("helmsman.api.runs")

router = APIRouter(prefix="/api", tags=["runs"])


def _registry(request: Request) -> RunRegistry:
    return request.app.state.registry


@router.post("/runs")
async def launch_run(
    request: Request,
    domain: str = Form(...),
    requirement: str = Form(""),
    mode: str = Form("auto"),
    gate: str | None = Form(None),  # checkbox: present = checked
    headed: str | None = Form(None),
    from_run: str | None = Form(None),
):
    """Launch a mission (form-encoded). Redirects to /run?run_id= on success."""
    from_run = (from_run or "").strip() or None
    req = LaunchRunRequest(
        domain=domain.strip(),
        requirement=requirement.strip(),
        mode=mode if mode in ("explore", "auto", "harvest") else "auto",
        gate=gate is not None,
        headed=headed is not None,
        from_run=from_run,
    )
    if not req.from_run and req.mode != "harvest" and not req.requirement:
        return _error_fragment("需求不能为空(explore / auto 模式)。", status=400)

    try:
        state = await _registry(request).launch(req)
    except RunActiveError as e:  # cap reached OR same-domain already running
        return _error_fragment(str(e), status=409)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Launch failed: {e}", exc_info=True)
        return _error_fragment(f"启动失败:{e}", status=500)

    target = f"/run?run_id={quote(state.run_id)}" if state.run_id else "/run"
    return Response(status_code=204, headers={"HX-Redirect": target})


# ── 'active' aliases → the most-recent run (frontend untouched at cap=1) ──────

@router.get("/runs/active")
async def active_run(request: Request):
    return JSONResponse(_registry(request).newest_state().model_dump())


@router.get("/runs/active_list")
async def active_list(request: Request):
    """All currently-live runs — drives the shared LIVE board on the home page."""
    states = _registry(request).active_states()
    return JSONResponse({"runs": [s.model_dump() for s in states]})


@router.post("/runs/active/stop")
async def stop_run(request: Request):
    h = _registry(request).newest()
    if h is not None:
        await h.stop()
    return Response(status_code=204)


@router.post("/runs/active/gate")
async def gate_decision(request: Request, decision: str = Form(...)):
    dec = GateDecision(decision="continue" if decision == "continue" else "stop")
    h = _registry(request).newest()
    ok = await h.answer_gate(dec.decision) if h is not None else False
    return JSONResponse({"ok": ok})


@router.post("/runs/active/assist")
async def assist_answer(
    request: Request,
    uuid: str = Form(...),
    status: str = Form("completed"),
):
    h = _registry(request).newest()
    ok = (
        await h.answer_assist(
            uuid.strip(), "cancelled" if status == "cancelled" else "completed"
        )
        if h is not None
        else False
    )
    return JSONResponse({"ok": ok})


# ── per-run (by run_id) → a specific run (for concurrent N>1) ─────────────────

@router.post("/runs/{run_id}/stop")
async def stop_run_by_id(request: Request, run_id: str):
    h = _registry(request).get(run_id)
    if h is not None:
        await h.stop()
    return Response(status_code=204)


@router.post("/runs/{run_id}/gate")
async def gate_decision_by_id(request: Request, run_id: str, decision: str = Form(...)):
    dec = GateDecision(decision="continue" if decision == "continue" else "stop")
    h = _registry(request).get(run_id)
    ok = await h.answer_gate(dec.decision) if h is not None else False
    return JSONResponse({"ok": ok})


@router.post("/runs/{run_id}/assist")
async def assist_answer_by_id(
    request: Request,
    run_id: str,
    uuid: str = Form(...),
    status: str = Form("completed"),
):
    h = _registry(request).get(run_id)
    ok = (
        await h.answer_assist(
            uuid.strip(), "cancelled" if status == "cancelled" else "completed"
        )
        if h is not None
        else False
    )
    return JSONResponse({"ok": ok})


@router.get("/runs")
async def list_runs(request: Request):
    return JSONResponse({"runs": request.app.state.artifacts.list_all_runs()})


def _error_fragment(msg: str, status: int = 400) -> Response:
    html = f'<div class="alert alert-error">{msg}</div>'
    return Response(content=html, status_code=status, media_type="text/html")
