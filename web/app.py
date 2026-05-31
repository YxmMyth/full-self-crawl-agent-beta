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

from fastapi import FastAPI, Request
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
from web.services.supervisor import RunSupervisor

logger = get_logger("helmsman.app")

templates = Jinja2Templates(directory=str(WebConfig.TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(level="INFO")
    Config.require("LLM_API_KEY", "LLM_BASE_URL", "DATABASE_URL")
    await db.connect()
    logger.info("Database connected (read-only console use)")

    bus = EventBus()
    arts = ArtifactService()
    sup = RunSupervisor(bus, DbReadService(), arts)
    app.state.bus = bus
    app.state.artifacts = arts
    app.state.supervisor = sup

    logger.info(f"Helmsman ready on http://{WebConfig.HOST}:{WebConfig.PORT}")
    try:
        yield
    finally:
        await sup.stop()
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
    sup: RunSupervisor = request.app.state.supervisor
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "recent_runs": arts.list_all_runs(limit=20),
            "active": sup.state().model_dump(),
        },
    )


@app.get("/run", response_class=HTMLResponse)
async def run_dashboard(request: Request):
    sup: RunSupervisor = request.app.state.supervisor
    state = sup.state()
    if not state.active and state.run_id is None:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request, "dashboard.html", {"state": state.model_dump()}
    )


@app.get("/healthz")
async def healthz():
    return {"ok": True}
