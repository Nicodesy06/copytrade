"""Modulo LIVE (opt-in): unico punto del progetto che può leggere la chiave e firmare.

Isolato per costruzione: nessun altro modulo importa librerie di firma (verificato dai test).
Il motore PAPER chiama solo `on_paper_buy` / `on_paper_sell` dell'esecutore, se configurato.
"""

from __future__ import annotations

from .executor import CONFIRM_PHRASE, JupiterExec, LiveError, LiveExecutor, LiveRpc
from .keystore import KeyStore, KeyStoreError

__all__ = ["CONFIRM_PHRASE", "JupiterExec", "KeyStore", "KeyStoreError", "LiveError", "LiveExecutor", "LiveRpc",
           "build_live"]


def build_live(cfg, db, clock, secrets, notifier, keystore: KeyStore | None = None, transport=None,
               engine: bool = False):
    """Costruisce l'esecutore live dalla configurazione (None se live_enabled=false)."""
    if not cfg.live_enabled:
        return None
    pc = cfg.providers
    helius = secrets.helius_api_key.get_secret_value() if (pc.use_helius and secrets.helius_api_key) else None
    rpc_url = pc.helius_http_url if helius else pc.rpc_http_url
    jup_key = secrets.jupiter_api_key.get_secret_value() if secrets.jupiter_api_key else None
    return LiveExecutor(
        cfg, db, clock, keystore or KeyStore(), notifier,
        JupiterExec(pc.jupiter_url, jup_key, cfg.live.request_timeout_s, transport),
        LiveRpc(rpc_url, cfg.live.request_timeout_s, helius, transport),
        engine=engine,
    )
