"""Supervisor: orchestrazione, health check, recovery al riavvio, riavvio dei loop con backoff.

Non ha alcuna autorità sui limiti finanziari: può solo portare il sistema in SAFE o fermarlo.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ..types import SystemState
from .audit import AuditAgent
from .base import Agent
from .discovery import DiscoveryAgent
from .positions import PositionManagerAgent
from .strategy import StrategyAgent
from .token import TokenAnalystAgent
from .trader import TraderAnalystAgent

log = logging.getLogger(__name__)


class Supervisor(Agent):
    name = "supervisor"
    description = "Orchestrazione, health check, recovery, backpressure, transizioni di stato"

    def __init__(self, rt, seed_wallets: list[str], labels: dict[str, str] | None = None):
        super().__init__(rt)
        self.discovery = DiscoveryAgent(rt, seed_wallets, labels)
        self.trader = TraderAnalystAgent(rt)
        self.token = TokenAnalystAgent(rt)
        self.strategy = StrategyAgent(rt)
        self.positions = PositionManagerAgent(rt, self.token, self.strategy, self.trader)
        self.audit = AuditAgent(rt)
        self.strategy.rescore = self.rescore_candidates
        self.agents: list[Agent] = [self.discovery, self.trader, self.token, self.strategy, self.positions,
                                    self.audit, self]
        self._tasks: list[asyncio.Task] = []
        self.max_restarts = 5

    async def rescore_candidates(self) -> None:
        """Ricalcola i punteggi con i soli dati osservati fino ad ora."""
        for w in self.rt.db.list_wallets():
            await self.trader.score(w.address, reload=not self.rt.clock.simulated)

    def recover(self) -> dict[str, int]:
        now = self.rt.clock.now()
        for sid in self.rt.strategies:
            self.rt.ledger.init_strategy(sid, now)
        failed = self.rt.broker.recover_pending()
        cleared = self.rt.reservations.clear_all()
        self.rt.db.audit(self.name, "startup", f"Avvio {self.rt.mode.value}: {len(failed)} ordini PENDING annullati, "
                         f"{cleared} prenotazioni rilasciate", {"version": self.rt.strategy_version}, ts=now)
        return {"failed_orders": len(failed), "reservations": cleared}

    async def start(self) -> int:
        self.recover()
        self.rt.bus.start()
        n = await self.rt.bus.redeliver_pending()
        if n:
            self.rt.db.audit(self.name, "redelivery", f"{n} eventi non confermati riconsegnati", ts=self.rt.clock.now())
        self.heartbeat_all("RUNNING")
        return n

    def heartbeat_all(self, status: str | None = None) -> None:
        for a in self.agents:
            if a is self:
                self.beat(status or "RUNNING", f"stato sistema {self.rt.system_state().value}; "
                          f"code {sum(self.rt.bus.depth().values())}")
            else:
                a.beat()

    def supervise(self, name: str, fn: Callable[[], Awaitable[None]], interval_s: float) -> None:
        async def loop() -> None:
            restarts = 0
            while True:
                try:
                    await fn()
                    restarts = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    restarts += 1
                    self.restarts += 1
                    self.fail(f"{name}: {type(e).__name__}: {e}")
                    log.exception("loop %s fallito", name)
                    self.rt.db.audit(self.name, "loop.crash", f"{name} riavviato ({restarts})", None, "ERROR")
                    if restarts >= self.max_restarts:
                        await self.rt.enter_safe(self.name, f"loop {name} instabile")
                    await asyncio.sleep(min(60, 2 ** restarts))
                    continue
                await asyncio.sleep(interval_s)

        self._tasks.append(asyncio.create_task(loop(), name=name))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.rt.bus.stop()
        self.heartbeat_all("STOPPED")

    async def wait_until_stopped(self, poll_s: float = 2.0) -> None:
        while self.rt.system_state() != SystemState.STOPPED:
            self.heartbeat_all()
            await asyncio.sleep(poll_s)
