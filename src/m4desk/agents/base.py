"""Base comune degli agenti logici: stato, heartbeat e contatori visibili in dashboard."""

from __future__ import annotations

from ..runtime import Runtime


class Agent:
    name = "agent"
    description = ""

    def __init__(self, rt: Runtime):
        self.rt = rt
        self.processed = 0
        self.errors = 0
        self.restarts = 0
        self.status = "IDLE"
        self.detail = ""
        self.last_error: str | None = None

    def beat(self, status: str | None = None, detail: str | None = None) -> None:
        if status:
            self.status = status
        if detail is not None:
            self.detail = detail
        sub = next((s for s in self.rt.bus.subs if s.name == self.name), None)
        self.rt.db.heartbeat(
            self.name, self.status, processed=self.processed + (sub.processed if sub else 0),
            errors=self.errors + (sub.errors if sub else 0), restarts=self.restarts,
            queue_depth=sub.queue.qsize() if sub else 0,
            last_error=self.last_error or (sub.last_error if sub else None), detail=self.detail[:300],
        )

    def fail(self, err: Exception | str) -> None:
        self.errors += 1
        self.last_error = str(err)[:300]
