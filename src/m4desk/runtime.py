"""Contenitore delle dipendenze condivise dagli agenti (nessuna autorità propria sui limiti)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .clock import Clock, SimClock
from .config import AppConfig
from .providers.base import ChainProvider, MarketDataProvider, MintInfoProvider, QuoteProvider, TokenRiskProvider
from .providers.budget import ProviderGate
from .services.events import EventBus, make_event
from .services.ledger import Ledger
from .services.llm import LLMService
from .services.paper_broker import PaperBroker
from .services.risk import RiskEngine
from .services.safety import LagBreaker, ReservationBook
from .services.strategies import StrategyDef, build_strategies
from .storage.db import Database
from .types import DataOrigin, EventType, Mode, ReasonCode, SystemState


@dataclass
class Providers:
    market: MarketDataProvider
    quotes: QuoteProvider
    rug: TokenRiskProvider | None  # None = Rugcheck disabilitato => solo osservazione
    mint: MintInfoProvider
    chain: ChainProvider
    gates: dict[str, ProviderGate] = field(default_factory=dict)
    # provider obbligatori per i nuovi acquisti
    mandatory: tuple[str, ...] = ()


@dataclass
class Runtime:
    cfg: AppConfig
    mode: Mode
    origin: DataOrigin
    db: Database
    clock: Clock
    bus: EventBus
    providers: Providers
    ledger: Ledger
    risk: RiskEngine
    broker: PaperBroker
    llm: LLMService
    reservations: ReservationBook
    strategies: dict[str, StrategyDef]
    strategy_version: str
    feed_breaker: LagBreaker
    quote_breaker: LagBreaker
    alerts: list[str] = field(default_factory=list)

    # --- stato del sistema (letto dal DB: comandi da CLI/dashboard valgono subito) ---
    def system_state(self) -> SystemState:
        return SystemState(self.db.get_control("state", SystemState.RUNNING.value))

    def set_state(self, state: SystemState, actor: str, reason: str) -> None:
        cur = self.system_state()
        if cur == state:
            return
        if cur == SystemState.STOPPED and actor != "human":
            return  # solo un umano può riattivare dopo ARRESTA
        if cur == SystemState.SAFE and state == SystemState.RUNNING and actor != "human":
            return  # uscita da SAFE solo con RIPRENDI umano
        self.db.set_control("state", state.value, actor, reason)

    async def enter_safe(self, actor: str, reason: str) -> None:
        if self.system_state() in (SystemState.SAFE, SystemState.STOPPED):
            return
        self.set_state(SystemState.SAFE, actor, reason)
        self.alerts.append(reason)
        now = self.clock.now()
        await self.bus.publish(
            make_event(EventType.STATE_CHANGE, actor, ("SAFE", now.isoformat(), reason), self.origin, "system",
                       now, payload={"state": "SAFE", "reason": reason})
        )

    def provider_blockers(self) -> list[ReasonCode]:
        out: list[ReasonCode] = []
        if self.providers.rug is None:
            out.append(ReasonCode.RUGCHECK_MISSING)  # STOP acquisti: modalità solo osservazione
        for name in self.providers.mandatory:
            g = self.providers.gates.get(name)
            if g is None:
                continue
            ok, reason = g.available()
            if not ok and reason is not None:
                out.append(reason)
        if self.feed_breaker.is_open:
            out.append(ReasonCode.FEED_LAG_BREAKER)
        if self.quote_breaker.is_open:
            out.append(ReasonCode.QUOTE_LAG_BREAKER)
        return out

    def watch_only(self) -> bool:
        return self.providers.rug is None


def make_sleeper(clock: Clock):
    if isinstance(clock, SimClock):
        from datetime import timedelta

        async def sim_sleep(seconds: float) -> None:
            clock.set(clock.now() + timedelta(seconds=seconds))

        return sim_sleep
    return asyncio.sleep


def build_runtime(
    cfg: AppConfig,
    mode: Mode,
    origin: DataOrigin,
    db: Database,
    clock: Clock,
    providers: Providers,
    llm: LLMService,
) -> Runtime:
    ledger = Ledger(db, cfg.risk.starting_equity_usd, cfg.risk.max_price_age_s)
    broker = PaperBroker(db, ledger, providers.quotes, providers.market, cfg.simulation, cfg.risk, clock,
                         make_sleeper(clock))
    strategies = build_strategies(cfg)
    version = cfg.strategy_version()
    db.set_meta("mode", mode.value)
    db.set_meta("origin", origin.value)
    db.set_meta("strategy_version", version)
    db.set_meta("primary_strategy", cfg.strategy.primary)
    db.set_meta("strategies", {k: v.label for k, v in strategies.items()})
    db.set_meta("config_public", cfg.model_dump(mode="json", exclude={"seed_wallets"}))
    r = cfg.risk
    return Runtime(
        cfg=cfg, mode=mode, origin=origin, db=db, clock=clock, bus=EventBus(db), providers=providers,
        ledger=ledger, risk=RiskEngine(cfg.risk), broker=broker, llm=llm, reservations=ReservationBook(db),
        strategies=strategies, strategy_version=version,
        feed_breaker=LagBreaker("feed", ReasonCode.FEED_LAG_BREAKER, r.feed_lag_max_s, r.breaker_trip_count,
                                r.breaker_recover_count),
        quote_breaker=LagBreaker("quote", ReasonCode.QUOTE_LAG_BREAKER, r.quote_lag_max_s, r.breaker_trip_count,
                                 r.breaker_recover_count),
    )


def day_start_equity(rt: Runtime, strategy: str, now: datetime) -> Decimal:
    key = f"day_start_equity:{strategy}:{now.strftime('%Y-%m-%d')}"
    val = rt.db.get_checkpoint(key)
    if val is None:
        v = rt.ledger.view(strategy, now).equity_estimate
        rt.db.set_checkpoint(key, str(v), now)
        return v
    return Decimal(val)
