"""SSE firehose — one live channel per open dashboard.

A fresh connection (no Last-Event-ID) gets a whole-ring replay so opening the
dashboard mid-run shows everything buffered so far; a reconnect sends
Last-Event-ID and the bus replays only the gap. Event name = the model's `type`;
data = the model JSON; id = bus seq.

  - GET /api/stream            → the most-recent run's bus (legacy single-run alias)
  - GET /api/stream/{run_id}   → that specific run's bus (per-run, for N>1)

Each run owns its own EventBus, so the two never interleave events.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from web.services.supervisor import RunRegistry

router = APIRouter(prefix="/api", tags=["stream"])


def _registry(request: Request) -> RunRegistry:
    return request.app.state.registry


def _last_id(request: Request) -> int:
    raw = request.headers.get("last-event-id")
    return int(raw) if raw and raw.isdigit() else 0


def _sse(handle, last_id: int, request: Request) -> EventSourceResponse:
    async def gen():
        yield {"event": "hello", "data": "{}"}
        if handle is None:
            # No such run (or none active) — hold the SSE open until reconnect.
            while not await request.is_disconnected():
                await asyncio.sleep(15)
            return
        async for ev in handle.bus.subscribe(last_event_id=last_id):
            if await request.is_disconnected():
                break
            yield {"event": ev.name, "id": str(ev.seq), "data": ev.json}

    return EventSourceResponse(gen(), ping=15)


@router.get("/stream")
async def stream(request: Request):
    return _sse(_registry(request).newest(), _last_id(request), request)


@router.get("/stream/{run_id}")
async def stream_run(request: Request, run_id: str):
    return _sse(_registry(request).get(run_id), _last_id(request), request)
