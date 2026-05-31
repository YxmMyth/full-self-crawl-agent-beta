"""EventBus — in-process pub/sub fan-out for the SSE firehose.

A single active run produces a stream of typed events (web.models). Each SSE
client subscribes and receives every event from the moment it connects, plus a
replay of whatever it missed from a bounded ring buffer (Last-Event-ID resume,
and a whole-ring replay for a fresh connection so opening the dashboard mid-run
shows everything so far).

Single-process, single-event-loop, single active run: deliberately simple
(asyncio.Queue per subscriber + a ring buffer). No external broker.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator, Optional

from pydantic import BaseModel

from web.config import WebConfig


class BufferedEvent:
    __slots__ = ("seq", "name", "json")

    def __init__(self, seq: int, name: str, json_data: str) -> None:
        self.seq = seq
        self.name = name
        self.json = json_data


class EventBus:
    """Broadcast hub. Producers publish(); subscribers stream via subscribe()."""

    def __init__(self, ring_size: int | None = None) -> None:
        self._seq = 0
        self._ring: deque[BufferedEvent] = deque(
            maxlen=ring_size or WebConfig.EVENT_RING_SIZE
        )
        self._subscribers: set[asyncio.Queue[BufferedEvent]] = set()

    def publish(self, event: BaseModel) -> None:
        """Publish a web.models event to all subscribers + the ring (no await)."""
        self._seq += 1
        name = getattr(event, "type", "message")
        buffered = BufferedEvent(self._seq, name, event.model_dump_json())
        self._ring.append(buffered)
        for q in list(self._subscribers):
            try:
                q.put_nowait(buffered)
            except asyncio.QueueFull:
                # Slow consumer: drop its oldest, then enqueue the newest.
                try:
                    q.get_nowait()
                    q.put_nowait(buffered)
                except Exception:
                    pass

    def reset(self) -> None:
        """Clear ring + sequence for a fresh run (subscribers stay attached)."""
        self._ring.clear()
        self._seq = 0

    async def subscribe(
        self, last_event_id: Optional[int] = None
    ) -> AsyncIterator[BufferedEvent]:
        """Yield events forever. Replays events with seq > last_event_id first."""
        q: asyncio.Queue[BufferedEvent] = asyncio.Queue(maxsize=2000)
        self._subscribers.add(q)
        try:
            if last_event_id is not None:
                for ev in list(self._ring):
                    if ev.seq > last_event_id:
                        yield ev
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
