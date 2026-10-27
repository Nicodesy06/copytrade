"""Varianti di strategia confrontate sullo stesso identico flusso di eventi (ledger separati)."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import AppConfig


@dataclass(frozen=True)
class StrategyDef:
    id: str
    label: str
    loss_limit: int | None  # None = nessuna sostituzione (baseline)
    use_ai: bool = False
    flat_ticket: bool = False


def build_strategies(cfg: AppConfig) -> dict[str, StrategyDef]:
    lim = cfg.risk.consecutive_loss_limit
    all_defs = {
        "switch2": StrategyDef("switch2", f"Sostituzione dopo {lim} perdite (primaria)", lim),
        "switch3": StrategyDef("switch3", "Sostituzione dopo 3 perdite", 3),
        "naive": StrategyDef("naive", "Copia ingenua (nessuna sostituzione)", None),
        "ai_rank": StrategyDef("ai_rank", f"Ranking LLM consultivo + sostituzione dopo {lim}", lim, use_ai=True),
        "flat20": StrategyDef(
            "flat20", f"Test: ticket fisso {cfg.risk.flat_ticket_usd} USD", lim, flat_ticket=True
        ),
    }
    out = {k: v for k, v in all_defs.items() if k in cfg.strategy.enabled}
    if cfg.strategy.primary not in out:
        raise ValueError("La strategia primaria deve essere abilitata")
    return out
