"""Event bus tipizzato: persistenza, dedupe deterministico, code limitate, consegna at-least-once."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..storage.db import ConsumerCheckpoint, Database, EventRow
from ..types import DataOrigin, EventEnvelope, EventType, Provenance, utcnow

log = logging.getLogger(__name__)

Handler = Callable[[EventEnvelope], Awaitable[None]]


@dataclass
class Subscription:
    name: str
    types: frozenset[EventType]
    handler: Handler
    queue: asyncio.Queue[EventEnvelope]
    processed: int = 0
    errors: int = 0
    last_error: str | None = None
    busy: bool = False
    task: asyncio.Task[None] | None = field(default=None, repr=False)


def row_to_envelope(r: EventRow) -> EventEnvelope:
    return EventEnvelope(
        event_id=r.event_id,
        type=EventType(r.type),
        source=r.source,
        slot=r.slot,
        observed_at=r.observed_at,
        event_at=r.event_at,
        wallet=r.wallet,
        token=r.token,
        idempotency_key=r.idempotency_key,
        provenance=Provenance.model_validate(r.provenance),
        payload=r.payload,
    )


class EventBus:
    def __init__(self, db: Database, max_queue: int = 1000):
        self.db = db
        self.max_queue = max_queue
        self.subs: list[Subscription] = []
        self.duplicates = 0
        self.duplicates_by_type: Counter[str] = Counter()
        self._running = False

    def subscribe(self, name: str, types: set[EventType], handler: Handler) -> Subscription:
        sub = Subscription(name, frozenset(types), handler, asyncio.Queue(maxsize=self.max_queue))
        self.subs.append(sub)
        return sub

    def _persist(self, env: EventEnvelope) -> bool:
        try:
            with self.db.session() as s:
                s.add(
                    EventRow(
                        event_id=env.event_id,
                        idempotency_key=env.idempotency_key,
                        type=env.type.value,
                        source=env.source,
                        slot=env.slot,
                        observed_at=env.observed_at,
                        event_at=env.event_at,
                        wallet=env.wallet,
                        token=env.token,
                        provenance=env.provenance.model_dump(mode="json"),
                        payload=env.payload,
                    )
                )
            return True
        except IntegrityError:
            return False

    async def publish(self, env: EventEnvelope) -> bool:
        """Ritorna False se l'evento è un duplicato (stessa idempotency key): non viene riconsegnato."""
        if not self._persist(env):
            self.duplicates += 1
            self.duplicates_by_type[env.type.value] += 1
            return False
        for sub in self.subs:
            if env.type in sub.types:
                await sub.queue.put(env)  # coda limitata => backpressure sul produttore
        return True

    def _mark_done(self, consumer: str, event_id: str) -> None:
        with self.db.session() as s:
            s.merge(ConsumerCheckpoint(consumer=consumer, event_id=event_id, done_at=utcnow()))

    async def _worker(self, sub: Subscription) -> None:
        while True:
            env = await sub.queue.get()
            sub.busy = True
            try:
                await sub.handler(env)
                self._mark_done(sub.name, env.event_id)
                sub.processed += 1
            except Exception as e:  # l'evento resta non confermato e verrà riconsegnato al riavvio
                sub.errors += 1
                sub.last_error = f"{type(e).__name__}: {e}"[:300]
                log.exception("handler %s fallito su %s", sub.name, env.type)
                self.db.audit(sub.name, "handler.error", sub.last_error, {"event_id": env.event_id}, "ERROR")
            finally:
                sub.busy = False
                sub.queue.task_done()

    def start(self) -> None:
        self._running = True
        for sub in self.subs:
            if sub.task is None or sub.task.done():
                sub.task = asyncio.create_task(self._worker(sub), name=f"bus-{sub.name}")

    async def stop(self) -> None:
        for sub in self.subs:
            if sub.task:
                sub.task.cancel()
        await asyncio.gather(*(s.task for s in self.subs if s.task), return_exceptions=True)
        for sub in self.subs:
            sub.task = None
        self._running = False

    async def wait_idle(self) -> None:
        """Attende che tutte le code siano vuote, inclusi gli eventi generati a catena."""
        while True:
            for sub in self.subs:
                await sub.queue.join()
            if all(s.queue.empty() and not s.busy for s in self.subs):
                return

    async def redeliver_pending(self) -> int:
        """Al riavvio: riconsegna gli eventi persistiti ma non confermati da ciascun consumer."""
        count = 0
        with self.db.session() as s:
            rows = list(s.scalars(select(EventRow).order_by(EventRow.seq)))
            done = {(c.consumer, c.event_id) for c in s.scalars(select(ConsumerCheckpoint))}
        for r in rows:
            env = row_to_envelope(r)
            for sub in self.subs:
                if env.type in sub.types and (sub.name, env.event_id) not in done:
                    await sub.queue.put(env)
                    count += 1
        return count

    def depth(self) -> dict[str, int]:
        return {s.name: s.queue.qsize() for s in self.subs}


def make_event(
    type: EventType,
    source: str,
    key_parts: tuple,
    origin: DataOrigin,
    provider: str,
    event_at,
    observed_at=None,
    wallet: str | None = None,
    token: str | None = None,
    slot: int | None = None,
    ref: str | None = None,
    payload: dict | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        type=type,
        source=source,
        slot=slot,
        observed_at=observed_at or event_at,
        event_at=event_at,
        wallet=wallet,
        token=token,
        idempotency_key=EventEnvelope.make_key(type.value, *key_parts),
        provenance=Provenance(provider=provider, origin=origin, ref=ref, fetched_at=observed_at or event_at),
        payload=payload or {},
    )
