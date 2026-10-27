"""Modulo LIVE: NON IMPLEMENTATO e DISABILITATO in MVP 0.1.

Questo pacchetto esiste solo come confine architetturale isolato. Non contiene chiavi, non importa
librerie di firma e non può inviare transazioni. Ogni chiamata solleva `LiveDisabledError`.
I prerequisiti obbligatori prima di qualsiasi implementazione sono in docs/LIVE_CHECKLIST.md.
"""

from __future__ import annotations

from dataclasses import dataclass


class LiveDisabledError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreflightItem:
    name: str
    ok: bool
    detail: str


def preflight() -> list[PreflightItem]:
    """Tutti i controlli falliscono finché il modulo non viene progettato, implementato e revisionato."""
    missing = "NON IMPLEMENTATO"
    return [
        PreflightItem("Wallet dedicato a basso saldo", False, missing),
        PreflightItem("Firma isolata (Keychain/Secure Enclave o processo separato)", False, missing),
        PreflightItem("Allowlist programmi di swap e controllo account/token", False, missing),
        PreflightItem("Limiti di spesa per ordine/giorno", False, missing),
        PreflightItem("Simulazione e ispezione transazione prima della firma", False, missing),
        PreflightItem("Conferma umana esplicita e configurazione runtime separata", False, missing),
        PreflightItem("Periodo forward-paper documentato", False, missing),
    ]


def execute(*_args, **_kwargs):
    raise LiveDisabledError("Esecuzione reale disabilitata: modulo live NON IMPLEMENTATO (vedi docs/LIVE_CHECKLIST.md)")
