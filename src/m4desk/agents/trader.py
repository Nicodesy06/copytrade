"""TraderAnalystAgent: ricostruisce round-trip da swap pubblici e calcola metriche/punteggio deterministici."""

from __future__ import annotations

from ..providers.base import ProviderError
from ..services.events import make_event
from ..services.trader_metrics import compute_metrics
from ..storage.db import TraderScore
from ..types import EventEnvelope, EventType, SwapObservation
from .base import Agent


class TraderAnalystAgent(Agent):
    name = "trader_analyst"
    description = "Round-trip, P&L realizzato, win rate (IC95%), tempi di detenzione, concentrazione, copiabilità"

    def __init__(self, rt):
        super().__init__(rt)
        self.history: dict[str, dict[str, SwapObservation]] = {}
        rt.bus.subscribe(self.name, {EventType.WALLET_CANDIDATE, EventType.LEADER_SWAP}, self.handle)

    async def handle(self, env: EventEnvelope) -> None:
        if env.wallet is None:
            return
        if env.type == EventType.LEADER_SWAP:
            s = SwapObservation.model_validate(env.payload["swap"])
            self.history.setdefault(s.wallet, {})[s.signature] = s
            return
        await self.score(env.wallet)

    async def _load(self, wallet: str) -> bool:
        try:
            swaps = await self.rt.providers.chain.wallet_swaps(wallet, limit=self.rt.cfg.trader.max_history_tx)
        except ProviderError as e:
            self.fail(e)
            return False
        h = self.history.setdefault(wallet, {})
        for s in swaps:
            h[s.signature] = s
        return True

    def retract(self, signature: str) -> None:
        for h in self.history.values():
            h.pop(signature, None)

    async def score(self, wallet: str, reload: bool = True) -> TraderScore | None:
        """Ritorna None se lo storico non è recuperabile ora: nessun punteggio inventato, si riprova dopo."""
        self.beat("RUNNING", f"analisi {wallet[:6]}…")
        if (reload or wallet not in self.history) and not await self._load(wallet):
            self.rt.db.audit(self.name, "score.deferred", f"Storico di {wallet[:8]}… non disponibile: "
                             f"valutazione rinviata ({self.last_error})", None, "WARN", ts=self.rt.clock.now())
            self.beat("IDLE", "storico non disponibile")
            return None
        now = self.rt.clock.now()
        swaps = list(self.history.get(wallet, {}).values())
        status, score, metrics, reasons = compute_metrics(swaps, now, self.rt.cfg.trader)
        row = TraderScore(wallet=wallet, computed_at=now, as_of=now, status=status, score=score, metrics=metrics,
                          reasons=reasons)
        with self.rt.db.session() as s:
            s.add(row)
        await self.rt.bus.publish(
            make_event(EventType.TRADER_SCORED, self.name, (wallet, now.isoformat()), self.rt.origin, "local",
                       now, wallet=wallet,
                       payload={"status": status, "score": str(score) if score is not None else None,
                                "reasons": reasons})
        )
        self.processed += 1
        self.beat("IDLE", f"ultimo: {wallet[:6]}… {status}")
        return row
