"""Ingestione swap dei wallet osservati: gap-fill da checkpoint + (opzionale) notifiche WebSocket.

Il gap-fill via HTTP è il meccanismo primario e robusto (riconnessioni, slot persi, riavvii);
`logsSubscribe` (un solo pubkey per sottoscrizione) serve solo ad anticipare il gap-fill.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime

from ..providers.base import ProviderError
from ..types import EventEnvelope, EventType, SwapObservation
from .events import make_event

log = logging.getLogger(__name__)


def swap_event(rt, s: SwapObservation, observed_at: datetime, provider: str) -> EventEnvelope:
    return make_event(
        EventType.LEADER_SWAP, "ingest", (s.signature, s.wallet), rt.origin, provider, s.event_at,
        observed_at=observed_at, wallet=s.wallet, token=s.mint, slot=s.slot, ref=s.signature,
        payload={"swap": s.model_dump(mode="json")},
    )


class GapFiller:
    def __init__(self, rt, max_per_fill: int = 25):
        self.rt = rt
        self.max_per_fill = max_per_fill
        self._locks: dict[str, asyncio.Lock] = {}

    async def fill(self, wallet: str) -> int:
        """Recupera gli swap dopo l'ultimo checkpoint e li pubblica in ordine cronologico (dedupe garantito)."""
        lock = self._locks.setdefault(wallet, asyncio.Lock())
        async with lock:
            key = f"wallet_cursor:{wallet}"
            cursor = self.rt.db.get_checkpoint(key)
            try:
                if cursor is None:
                    # primo avvio: lo storico serve solo all'analisi; non si copiano operazioni passate
                    head = await self.rt.providers.chain.head_signature(wallet)
                    self.rt.db.set_checkpoint(key, head or "", self.rt.clock.now())
                    return 0
                swaps = await self.rt.providers.chain.wallet_swaps(wallet, self.max_per_fill,
                                                                   until_signature=cursor or None)
            except ProviderError as e:
                self.rt.db.audit("ingest", "gapfill.error", f"{wallet[:8]}…: {e.reason.value}", None, "WARN")
                return 0
            published = 0
            for s in sorted(swaps, key=lambda x: (x.slot, x.event_at)):
                if await self.rt.bus.publish(swap_event(self.rt, s, self.rt.clock.now(), self.rt.providers.chain.name)):
                    published += 1
            if swaps:
                newest = max(swaps, key=lambda x: (x.slot, x.event_at))
                self.rt.db.set_checkpoint(key, newest.signature, self.rt.clock.now())
            return published


class LogsSubscriber:
    """WebSocket `logsSubscribe` con `mentions: [wallet]` (un pubkey per sottoscrizione)."""

    def __init__(self, ws_url: str, on_wallet: Callable[[str], asyncio.Future | None], max_subs: int = 10):
        self.ws_url = ws_url
        self.on_wallet = on_wallet
        self.max_subs = max_subs
        self.connected = False
        self.reconnects = 0
        self.last_error: str | None = None

    async def run(self, wallets_fn: Callable[[], set[str]]) -> None:
        import websockets

        backoff = 2.0
        while True:
            wallets = sorted(wallets_fn())[: self.max_subs]
            if not wallets:
                await asyncio.sleep(30)
                continue
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20, max_size=2**20) as ws:
                    sub_ids: dict[int, str] = {}
                    for i, w in enumerate(wallets):
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": i + 1, "method": "logsSubscribe",
                                                  "params": [{"mentions": [w]}, {"commitment": "confirmed"}]}))
                    self.connected = True
                    backoff = 2.0
                    req_to_wallet = {i + 1: w for i, w in enumerate(wallets)}
                    async for raw in ws:
                        msg = json.loads(raw)
                        if "id" in msg and "result" in msg and msg["id"] in req_to_wallet:
                            sub_ids[msg["result"]] = req_to_wallet[msg["id"]]
                        elif msg.get("method") == "logsNotification":
                            sid = msg["params"]["subscription"]
                            w = sub_ids.get(sid)
                            if w and msg["params"]["result"]["value"].get("err") is None:
                                fut = self.on_wallet(w)
                                if fut is not None:
                                    await fut
                        if set(wallets_fn()) != set(wallets) and len(wallets) < self.max_subs:
                            break  # watchlist cambiata: riconnessione con le nuove sottoscrizioni
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}"
                log.warning("WS disconnesso: %s", e)
            self.connected = False
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
