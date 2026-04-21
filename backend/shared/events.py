"""Per-job event bus — SSE-friendly queue of status events.

The agent and skills publish status messages via the job's bus; the API layer
drains the bus and streams events to the frontend over Server-Sent Events.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    event: str              # "status" | "skill_start" | "skill_end" | "components" | "done" | "error"
    data: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self) -> None:
        self._q: asyncio.Queue[Event] = asyncio.Queue()
        self._closed = False

    async def publish(self, event: str, **data: Any) -> None:
        if self._closed:
            return
        await self._q.put(Event(event=event, data=data))

    def publish_nowait(self, event: str, **data: Any) -> None:
        if self._closed:
            return
        self._q.put_nowait(Event(event=event, data=data))

    async def get(self) -> Event:
        return await self._q.get()

    def close(self) -> None:
        self._closed = True
