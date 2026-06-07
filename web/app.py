"""Helmsman FastAPI application.

Single async process, single worker, bound to 127.0.0.1 (SSH tunnel is the auth
boundary — see web/config.py). Serves the static assets + Jinja templates, the
REST routers, and the SSE firehose. Phase 2 adds the noVNC reverse proxy.

Run it:
    python -m web
    # on the server, under the virtual display:
    DISPLAY=:99 .venv/bin/python -m web
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config import Config
from src.utils.logging import get_logger, setup as setup_logging
from src.world_model import db
from web.api import artifacts as artifacts_api
from web.api import runs as runs_api
from web.api import stream as stream_api
from web.config import WebConfig
from web.services.artifacts import ArtifactService
from web.services.db_read import DbReadService
from web.services.eventbus import EventBus
from web.services.novnc_proxy import NoVncProxy
from web.services.supervisor import RunRegistry

logger = get_logger("helmsman.app")

templates = Jinja2Templates(directory=str(WebConfig.TEMPLATES_DIR))
# Cache-busting token available to every template as {{ asset_v }}.
templates.env.globals["asset_v"] = WebConfig.ASSET_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(level="INFO")
    Config.require("LLM_API_KEY", "LLM_BASE_URL", "DATABASE_URL")
    await db.connect()
    logger.info("Database connected (read-only console use)")

    arts = ArtifactService()  # shared, read-only — for REST reads + pages
    registry = RunRegistry(DbReadService())
    vnc = NoVncProxy()
    app.state.artifacts = arts
    app.state.registry = registry
    app.state.vnc = vnc
    await registry.reap_orphans()  # clear orphan containers from a prior session

    logger.info(f"Helmsman ready on http://{WebConfig.HOST}:{WebConfig.PORT}")
    try:
        yield
    finally:
        await registry.shutdown()
        await vnc.aclose()
        await db.close()
        logger.info("Helmsman shut down")


app = FastAPI(title="Helmsman", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(WebConfig.STATIC_DIR)), name="static")
app.include_router(runs_api.router)
app.include_router(stream_api.router)
app.include_router(artifacts_api.router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    arts: ArtifactService = request.app.state.artifacts
    registry: RunRegistry = request.app.state.registry
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "recent_runs": arts.list_all_runs(limit=20),
            "active": registry.newest_state().model_dump(),
        },
    )


@app.get("/run", response_class=HTMLResponse)
async def run_dashboard(request: Request, run_id: str | None = None):
    registry: RunRegistry = request.app.state.registry
    if run_id:
        h = registry.get(run_id)
        state = h.state() if h is not None else registry.newest_state()
    else:
        state = registry.newest_state()
    if not state.active and state.run_id is None:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request, "dashboard.html", {"state": state.model_dump()}
    )


@app.get("/archive", response_class=HTMLResponse)
async def archive(request: Request, domain: str, run_id: str):
    """Read-only archive view of ANY past run.

    domain + run_id are QUERY params (a domain can be a full URL whose slashes
    break path-segment routing). Renders purely from the on-demand REST
    endpoints (model / tree / file) — no SSE, no supervisor, no live state.
    """
    return templates.TemplateResponse(
        request, "archive.html", {"domain": domain, "run_id": run_id}
    )


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ── noVNC reverse proxy (same-origin → only one SSH tunnel) ──────────
# Both routes proxy to websockify on 127.0.0.1:6080 (the display stack). Serving
# them under Helmsman's own origin makes the embedded iframe same-origin (no
# CORS) and means the operator forwards only port 8000.


@app.websocket("/vnc/{run_id}/websockify")
async def vnc_websockify(websocket: WebSocket, run_id: str):
    h = websocket.app.state.registry.get(run_id)
    upstream = f"127.0.0.1:{h._vnc_port}" if h is not None and h._vnc_port else None
    await websocket.app.state.vnc.relay_ws(websocket, upstream)


@app.get("/vnc/{run_id}/{path:path}")
async def vnc_assets(request: Request, run_id: str, path: str):
    h = request.app.state.registry.get(run_id)
    upstream = f"127.0.0.1:{h._vnc_port}" if h is not None and h._vnc_port else None
    return await request.app.state.vnc.proxy_http(request, path, upstream)
