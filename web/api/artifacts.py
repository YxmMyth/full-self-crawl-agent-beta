"""On-demand artifact + model reads (pulled by the dashboard, not streamed).

Heavy markdown (semantic / procedural models, strategy report, audit) and file
contents are fetched here when the operator opens a panel — keeping the SSE
firehose lean. All filesystem reads are path-jailed to the run dir.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from src.world_model import db
from web.services.artifacts import ArtifactService

router = APIRouter(prefix="/api/runs", tags=["artifacts"])


def _arts(request: Request) -> ArtifactService:
    return request.app.state.artifacts


@router.get("/{domain}/{run_id}/model")
async def get_model(
    domain: str,
    run_id: str,
    type: str = Query("semantic", pattern="^(semantic|procedural)$"),
):
    content = await db.load_model(domain, type, run_id=run_id)
    return JSONResponse({"type": type, "markdown": content or ""})


@router.get("/{domain}/{run_id}/tree")
async def get_tree(domain: str, run_id: str, request: Request):
    return JSONResponse(_arts(request).inventory(domain, run_id))


@router.get("/{domain}/{run_id}/file")
async def get_file(domain: str, run_id: str, request: Request, path: str = Query(...)):
    arts = _arts(request)
    target = arts.resolve_in_run(domain, run_id, path)
    if target is None or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found / outside run dir")
    return PlainTextResponse(arts.read_text_file(target))
