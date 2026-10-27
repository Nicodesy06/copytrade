"""DiscoveryAgent: universo di wallet candidati da seed pubblici + esplorazione limitata degli swap.

Il risultato è un insieme di CANDIDATI, non una classifica di trader profittevoli.
"""

from __future__ import annotations

from ..providers.base import ProviderError
from ..services.events import make_event
from ..types import EventType
from .base import Agent


class DiscoveryAgent(Agent):
    name = "discovery"
    description = "Scopre wallet candidati (seed + co-trader sugli stessi token), con budget limitato"

    def __init__(self, rt, seed_wallets: list[str], labels: dict[str, str] | None = None):
        super().__init__(rt)
        self.seed_wallets = seed_wallets
        self.labels = labels or {}

    def funnel(self) -> dict[str, int]:
        return self.rt.db.get_checkpoint(
            "discovery_funnel",
            {"seed": 0, "token_scans": 0, "swaps_seen": 0, "wallets_seen": 0, "candidates": 0, "budget_stops": 0},
        )

    async def _add(self, wallet: str, source: str, via: str | None) -> bool:
        now = self.rt.clock.now()
        if not self.rt.db.add_wallet(wallet, source, self.labels.get(wallet), via, now):
            return False
        await self.rt.bus.publish(
            make_event(EventType.WALLET_CANDIDATE, self.name, (wallet,), self.rt.origin, self.rt.providers.chain.name,
                       now, wallet=wallet, payload={"source": source, "via": via})
        )
        return True

    async def cycle(self) -> dict[str, int]:
        cfg = self.rt.cfg.discovery
        f = self.funnel()
        self.beat("RUNNING", "ciclo di scoperta")
        for w in self.seed_wallets:
            if await self._add(w, "seed", None):
                f["seed"] += 1
                f["candidates"] += 1
        known = [w.address for w in self.rt.db.list_wallets()]
        tokens: list[str] = []
        try:
            for w in known[: cfg.max_candidates]:
                if len(tokens) >= cfg.max_token_scans_per_cycle:
                    break
                for s in await self.rt.providers.chain.wallet_swaps(w, limit=10):
                    if s.mint not in tokens:
                        tokens.append(s.mint)
            scanned = self.rt.db.get_checkpoint("discovery_scanned_tokens", [])
            todo = [t for t in tokens if t not in scanned][: cfg.max_token_scans_per_cycle]
            for mint in todo:
                swaps = await self.rt.providers.chain.token_swaps(mint, limit=cfg.swaps_per_token_scan)
                f["token_scans"] += 1
                f["swaps_seen"] += len(swaps)
                scanned.append(mint)
                for s in swaps:
                    f["wallets_seen"] += 1
                    if len(self.rt.db.list_wallets()) >= cfg.max_candidates:
                        break
                    if await self._add(s.wallet, "discovery", f"token:{mint}"):
                        f["candidates"] += 1
            self.rt.db.set_checkpoint("discovery_scanned_tokens", scanned[-500:], self.rt.clock.now())
        except ProviderError as e:
            f["budget_stops"] += 1
            self.fail(e)
            self.rt.db.audit(self.name, "discovery.stopped", f"Scoperta interrotta: {e.reason.value}",
                             {"provider": e.provider}, "WARN", ts=self.rt.clock.now())
        self.processed += 1
        self.rt.db.set_checkpoint("discovery_funnel", f, self.rt.clock.now())
        self.beat("IDLE", f"candidati totali {len(self.rt.db.list_wallets())}")
        return f
