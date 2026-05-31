"""SSE firehose — the single live channel from backend to browser.

One EventSource per open dashboard. A fresh connection (no Last-Event-ID) gets a
whole-ring replay so opening the dashboard mid-run shows everything buffered so
far; a reconnect sends Last-Event-ID and the bus replays only the gap. Event
name = the model's `type`; data = the model JSON; id = bus seq.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from web.services.eventbus import EventBus

router = APIRouter(prefix="/api", tags=["stream"])


def _bus(request: Request) -> EventBus:
    return request.app.state.bus


@router.get("/stream")
async def stream(request: Request):
    bus = _bus(request)
    last_id_raw = request.headers.get("last-event-id")
    # Reconnect → resume after client's last id. Fresh → replay whole ring (>0).
    last_id = int(last_id_raw) if last_id_raw and last_id_raw.isdigit() else 0

    async def gen():
        yield {"event": "hello", "data": "{}"}
        async for ev in bus.subscribe(last_event_id=last_id):
            if await request.is_disconnected():
                break
            yield {"event": ev.name, "id": str(ev.seq), "data": ev.json}

    return EventSourceResponse(gen(), ping=15)
