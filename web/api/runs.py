"""REST endpoints for launching and controlling runs.

The launch form (HTMX) posts here; on success we tell HTMX to navigate to the
live dashboard via the HX-Redirect header. The control endpoints (/runs/active/*)
resolve to the most-recent run via RunRegistry.newest() so the existing
single-run frontend is untouched; per-run-id addressing is a later step.
"""

from __future__ import annotations

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
    """Launch a mission (form-encoded). Redirects to /run on success."""
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
        await _registry(request).launch(req)
    except RunActiveError as e:  # cap reached OR same-domain already running
        return _error_fragment(str(e), status=409)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Launch failed: {e}", exc_info=True)
        return _error_fragment(f"启动失败:{e}", status=500)

    return Response(status_code=204, headers={"HX-Redirect": "/run"})


@router.get("/runs/active")
async def active_run(request: Request):
    return JSONResponse(_registry(request).newest_state().model_dump())


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
    """Answer a pending human-assist request (operator clicked 完成 / 跳过)."""
    h = _registry(request).newest()
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
