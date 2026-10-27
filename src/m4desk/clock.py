"""Orologio iniettabile: reale in PAPER, simulato (deterministico) in DEMO/REPLAY."""

from __future__ import annotations

from datetime import UTC, datetime


class Clock:
    simulated = False

    def now(self) -> datetime:
        return datetime.now(UTC)


class SimClock(Clock):
    simulated = True

    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, t: datetime) -> None:
        if t > self._now:
            self._now = t
