"""SSE firehose — one live channel per open dashboard.

A fresh connection (no Last-Event-ID) gets a whole-ring replay so opening the
dashboard mid-run shows everything buffered so far; a reconnect sends
Last-Event-ID and the bus replays only the gap. Event name = the model's `type`;
data = the model JSON; id = bus seq.

The bus is resolved per-connection from RunRegistry.newest() (the most-recent
run's own EventBus), so the legacy /api/stream keeps working unchanged. Per-run
/api/stream/{run_id} is a later step.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from web.services.supervisor import RunRegistry

router = APIRouter(prefix="/api", tags=["stream"])


def _registry(request: Request) -> RunRegistry:
    return request.app.state.registry


@router.get("/stream")
async def stream(request: Request):
    handle = _registry(request).newest()
    last_id_raw = request.headers.get("last-event-id")
    # Reconnect → resume after client's last id. Fresh → replay whole ring (>0).
    last_id = int(last_id_raw) if last_id_raw and last_id_raw.isdigit() else 0

    async def gen():
        yield {"event": "hello", "data": "{}"}
        if handle is None:
            # No run yet — hold the SSE open (pings) until the client reconnects.
            while not await request.is_disconnected():
                await asyncio.sleep(15)
            return
        async for ev in handle.bus.subscribe(last_event_id=last_id):
            if await request.is_disconnected():
                break
            yield {"event": ev.name, "id": str(ev.seq), "data": ev.json}

    return EventSourceResponse(gen(), ping=15)
