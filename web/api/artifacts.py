"""On-demand artifact + model reads (pulled by the dashboard, not streamed).

Heavy markdown (semantic / procedural models, strategy report, audit) and file
contents are fetched here when the operator opens a panel - keeping the SSE
firehose lean. All filesystem reads are path-jailed to the run dir.

domain + run_id are QUERY params, not path segments: a domain can be a full URL
like "https://www.kungal.org/" whose embedded slashes break path-segment routing
(ASGI decodes %2F back to "/" before route matching, so the param never matches).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from src.world_model import db
from web.services.artifacts import ArtifactService

router = APIRouter(prefix="/api/runs", tags=["artifacts"])


def _arts(request: Request) -> ArtifactService:
    return request.app.state.artifacts


@router.get("/model")
async def get_model(
    domain: str = Query(...),
    run_id: str = Query(...),
    type: str = Query("semantic", pattern="^(semantic|procedural)$"),
):
    content = await db.load_model(domain, type, run_id=run_id)
    return JSONResponse({"type": type, "markdown": content or ""})


@router.get("/tree")
async def get_tree(request: Request, domain: str = Query(...), run_id: str = Query(...)):
    return JSONResponse(_arts(request).inventory(domain, run_id))


@router.get("/file")
async def get_file(
    request: Request,
    domain: str = Query(...),
    run_id: str = Query(...),
    path: str = Query(...),
):
    arts = _arts(request)
    target = arts.resolve_in_run(domain, run_id, path)
    if target is None or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found / outside run dir")
    return PlainTextResponse(arts.read_text_file(target))
