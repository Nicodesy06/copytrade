"""PositionManager: replica acquisti/vendite dei leader, stop/TP simulati, nessuna posizione orfana.

- Acquisto: solo se il leader è SELECTED nella strategia e il motore di rischio approva.
- Vendita: frazione = venduto / saldo del leader prima della vendita (dalla transazione originale).
  Le vendite protettive valgono anche in PAUSA/SAFE e anche se il leader non è più seguito.
- Uscite non affidabili => allarme + modalità SAFE.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..providers.base import MarketSnapshot, ProviderError
from ..runtime import day_start_equity
from ..services.events import make_event
from ..services.risk import BuyContext
from ..storage.db import DecisionRow, EventRow, Order, Position
from ..types import (
    SOL_MINT,
    USDC_MINT,
    Decision,
    EventEnvelope,
    EventType,
    OrderState,
    ReasonCode,
    Side,
    SwapObservation,
    SystemState,
)
from .base import Agent

ZERO = Decimal(0)


class PositionManagerAgent(Agent):
    name = "position_manager"
    description = "Copia proporzionale degli swap dei leader, stop/TP simulati, gestione posizioni ereditate"

    def __init__(self, rt, token_agent, strategy_agent, trader_agent):
        super().__init__(rt)
        self.tokens = token_agent
        self.strategy = strategy_agent
        self.trader = trader_agent
        self.latencies_ms: list[int] = []
        rt.bus.subscribe(self.name, {EventType.LEADER_SWAP, EventType.PRICE_TICK, EventType.TX_RETRACTED},
                         self.handle)

    async def handle(self, env: EventEnvelope) -> None:
        self.beat("RUNNING", env.type.value)
        if self.rt.system_state() == SystemState.STOPPED:
            self.beat("STOPPED", "sistema arrestato")
            return
        if env.type == EventType.LEADER_SWAP:
            swap = SwapObservation.model_validate(env.payload["swap"])
            lag = (env.observed_at - env.event_at).total_seconds()
            if self.rt.feed_breaker.observe(lag):
                self._breaker_audit(self.rt.feed_breaker, lag)
            if swap.side == Side.BUY:
                await self.on_leader_buy(env, swap)
            else:
                await self.on_leader_sell(env, swap)
        elif env.type == EventType.PRICE_TICK:
            await self.on_price_tick(env.token or "")
        elif env.type == EventType.TX_RETRACTED:
            await self.on_retracted(env)
        self.processed += 1
        self.beat("IDLE", "")

    def _breaker_audit(self, br, lag: float) -> None:
        state = "APERTO" if br.is_open else "CHIUSO"
        self.rt.db.set_checkpoint(f"breaker:{br.name}", {"open": br.is_open, "lag_s": lag}, self.rt.clock.now())
        self.rt.db.audit(self.name, f"breaker.{br.name}", f"Breaker {br.name} {state} (ritardo {lag:.1f}s)",
                         {"lag_s": lag}, "WARN" if br.is_open else "INFO", ts=self.rt.clock.now())

    # --- helper --------------------------------------------------------------
    async def _market(self, mint: str) -> MarketSnapshot | None:
        try:
            return await self.rt.providers.market.get_market(mint)
        except ProviderError:
            return None

    async def _leader_price_usd(self, swap: SwapObservation) -> tuple[Decimal | None, str]:
        px = swap.price_in_quote
        if px is None:
            return None, "quantità nulla"
        if swap.quote_mint == USDC_MINT:
            return px, "USDC"
        if swap.quote_mint == SOL_MINT:
            sol = await self._market(SOL_MINT)
            if sol is None or sol.price_usd is None:
                return None, "prezzo SOL non disponibile"
            return px * sol.price_usd, "SOL×prezzo SOL corrente"
        return None, "quote non supportata"

    def _decision(self, sid: str, env: EventEnvelope, swap: SwapObservation, decision: Decision,
                  reasons: list[ReasonCode], facts: dict[str, Any]) -> bool:
        now = self.rt.clock.now()
        lat = int((now - swap.event_at).total_seconds() * 1000)
        try:
            with self.rt.db.session() as s:
                s.add(DecisionRow(ts=now, strategy=sid, strategy_version=self.rt.strategy_version,
                                  source_event_id=env.event_id, wallet=swap.wallet, mint=swap.mint,
                                  side=swap.side.value, decision=decision.value, reasons=[r.value for r in reasons],
                                  facts=facts, latency_ms=lat))
            self.latencies_ms.append(lat)
            return True
        except IntegrityError:
            return False  # decisione già presa per questo evento (riconsegna at-least-once)

    def _decided(self, sid: str, env: EventEnvelope, side: Side) -> bool:
        with self.rt.db.session() as s:
            return s.scalar(select(func.count()).select_from(DecisionRow).where(
                DecisionRow.strategy == sid, DecisionRow.source_event_id == env.event_id,
                DecisionRow.side == side.value)) > 0

    async def _publish_order(self, o: Order, extra: dict[str, Any] | None = None) -> None:
        now = self.rt.clock.now()
        await self.rt.bus.publish(make_event(
            EventType.ORDER_UPDATE, self.name, (o.idempotency_key, o.state), self.rt.origin, "paper-broker", now,
            wallet=o.leader, token=o.mint,
            payload={"order_id": o.id, "strategy": o.strategy, "side": o.side, "state": o.state, "reason": o.reason,
                     **(extra or {})}))

    # --- acquisto del leader -------------------------------------------------------
    async def on_leader_buy(self, env: EventEnvelope, swap: SwapObservation) -> None:
        targets = []
        for sid in self.rt.strategies:
            status, losses = self.strategy.leader_status(sid, swap.wallet)
            if status is not None and not self._decided(sid, env, Side.BUY):
                targets.append((sid, status, losses))
        if not targets:
            return
        leader_px, px_src = await self._leader_price_usd(swap)
        token = await self.tokens.assess(swap.mint)
        market = await self._market(swap.mint)
        blockers = self.rt.provider_blockers()
        for sid, status, losses in targets:
            sdef = self.rt.strategies[sid]
            now = self.rt.clock.now()
            async with self.rt.reservations.lock(sid):
                view = self.rt.ledger.view(sid, now)
                with self.rt.db.session() as s:
                    open_on_mint = self.rt.ledger.open_position(sid, swap.mint, s) is not None
                    last_exit = s.scalar(select(func.max(Position.closed_at)).where(
                        Position.strategy == sid, Position.mint == swap.mint))
                    leader_cost = sum(s.scalars(select(Position.cost_basis_usd).where(
                        Position.strategy == sid, Position.leader == swap.wallet, Position.status == "OPEN")), ZERO)
                n_res, res_cost = self.rt.reservations.totals(sid)
                _, leader_res = self.rt.reservations.totals(sid, swap.wallet)
                ctx = BuyContext(
                    strategy=sid, leader=swap.wallet, mint=swap.mint, now=now, event_at=swap.event_at,
                    system_state=self.rt.system_state(), leader_status=status, consecutive_losses=losses,
                    loss_limit=sdef.loss_limit, token=token, market=market, view=view,
                    has_open_position_on_mint=open_on_mint, last_exit_at_for_mint=last_exit,
                    day_start_equity=day_start_equity(self.rt, sid, now), provider_blockers=blockers,
                    flat_ticket=sdef.flat_ticket, leader_fill_price_usd=leader_px,
                    leader_open_cost_usd=leader_cost + leader_res, reserved_count=n_res, reserved_cost_usd=res_cost,
                )
                verdict = self.rt.risk.evaluate_buy(ctx)
                facts = {
                    **verdict.facts,
                    "leader_fill_price_usd": str(leader_px) if leader_px is not None else None,
                    "leader_price_source": px_src,
                    "leader_token_amount": str(swap.token_amount),
                    "leader_quote": f"{swap.quote_amount} {'SOL' if swap.quote_mint == SOL_MINT else 'USDC'}",
                    "signature": swap.signature,
                    "slot": swap.slot,
                    "token_check": token.status.value,
                    "token_reasons": [r.value for r in token.reasons],
                    "symbol": token.symbol,
                    "provenance": env.provenance.model_dump(mode="json"),
                }
                reasons = list(verdict.reasons)
                allowed = verdict.allowed
                if allowed and not self.rt.reservations.reserve(sid, swap.mint, swap.wallet, verdict.size_usd, now):
                    allowed, reasons = False, [ReasonCode.RESERVATION_CONFLICT]
                if not self._decision(sid, env, swap, Decision.COPY if allowed else Decision.SKIP, reasons, facts):
                    if allowed:
                        self.rt.reservations.release(sid, swap.mint)
                    continue
            await self.rt.bus.publish(make_event(
                EventType.DECISION, self.name, (sid, env.event_id, "BUY"), self.rt.origin, "local", now,
                wallet=swap.wallet, token=swap.mint,
                payload={"strategy": sid, "decision": "COPY" if allowed else "SKIP",
                         "reasons": [r.value for r in reasons]}))
            if not allowed:
                continue
            try:
                decimals = token.decimals if token.decimals is not None else swap.decimals
                o = await self.rt.broker.buy(
                    strategy=sid, leader=swap.wallet, mint=swap.mint, decimals=decimals, symbol=token.symbol,
                    size_usd=verdict.size_usd, idempotency_key=f"{sid}:{env.idempotency_key}:BUY",
                    source_event_id=env.event_id, leader_event_at=swap.event_at, leader_price_usd=leader_px,
                    decision_price_usd=market.price_usd if market else None,
                )
            finally:
                self.rt.reservations.release(sid, swap.mint)
            if o.quote_at is not None and o.filled_at is not None:
                if self.rt.quote_breaker.observe((o.filled_at - o.quote_at).total_seconds()):
                    self._breaker_audit(self.rt.quote_breaker, (o.filled_at - o.quote_at).total_seconds())
            await self._publish_order(o)
            await self._after_fill_checks(sid)

    async def _after_fill_checks(self, sid: str) -> None:
        from ..services.safety import check_contracts

        now = self.rt.clock.now()
        cash = self.rt.ledger.cash(sid)
        v = check_contracts(self.rt.db, sid, self.rt.cfg.risk.starting_equity_usd, self.rt.cfg.risk.max_positions,
                            cash)
        if v:
            self.rt.db.audit(self.name, "contract.violation", f"[{sid}] " + "; ".join(v), {"strategy": sid}, "ERROR",
                             ts=now)
            await self.rt.enter_safe(self.name, f"{ReasonCode.CONTRACT_VIOLATION.value}: {v[0]}")

    # --- vendita del leader --------------------------------------------------------
    async def on_leader_sell(self, env: EventEnvelope, swap: SwapObservation) -> None:
        with self.rt.db.session() as s:
            positions = list(s.scalars(select(Position).where(
                Position.leader == swap.wallet, Position.mint == swap.mint, Position.status == "OPEN")))
        for pos in positions:
            if pos.strategy not in self.rt.strategies or self._decided(pos.strategy, env, Side.SELL):
                continue
            pre, post = swap.wallet_pre_amount_raw, swap.wallet_post_amount_raw
            if pre is None or pre <= 0:
                fraction, reason = Decimal(1), ReasonCode.LEADER_HOLDINGS_UNKNOWN
            elif post is not None and post == 0:
                fraction, reason = Decimal(1), ReasonCode.LEADER_SELL_MIRRORED
            else:
                fraction = min(Decimal(swap.token_amount_raw) / Decimal(pre), Decimal(1))
                reason = ReasonCode.LEADER_SELL_MIRRORED
            facts = {"leader_pre_raw": pre, "leader_post_raw": post, "leader_sold_raw": swap.token_amount_raw,
                     "fraction": str(fraction.quantize(Decimal("0.0001"))), "position_id": pos.id,
                     "signature": swap.signature}
            if not self._decision(pos.strategy, env, swap, Decision.COPY, [reason], facts):
                continue
            await self._sell(pos.strategy, pos.id, fraction, reason, f"{pos.strategy}:{env.idempotency_key}:SELL",
                             env.event_id, swap.event_at)

    async def _sell(self, sid: str, pos_id: int, fraction: Decimal, reason: ReasonCode, key: str,
                    source_event_id: str | None, leader_event_at) -> Order:
        o = await self.rt.broker.sell(position_id=pos_id, fraction=fraction, reason=reason, idempotency_key=key,
                                      source_event_id=source_event_id, leader_event_at=leader_event_at)
        await self._publish_order(o)
        now = self.rt.clock.now()
        if o.state == OrderState.FILLED.value:
            with self.rt.db.session() as s:
                p = s.get(Position, pos_id)
                assert p is not None
                closed, realized, leader = p.status == "CLOSED", p.realized_pnl_usd, p.leader
            if closed:
                await self.rt.bus.publish(make_event(
                    EventType.POSITION_CLOSED, self.name, (sid, pos_id), self.rt.origin, "paper-broker", now,
                    wallet=leader, payload={"strategy": sid, "leader": leader, "position_id": pos_id,
                                            "realized_pnl_usd": str(realized), "reason": reason.value}))
            await self._after_fill_checks(sid)
        elif o.state == OrderState.FAILED.value and o.reason == ReasonCode.EXIT_NOT_RELIABLE.value:
            self.rt.db.audit(self.name, "exit.failed",
                             f"[{sid}] uscita non affidabile per posizione {pos_id} ({reason.value}): ALLARME",
                             {"order_id": o.id, "detail": o.detail}, "ERROR", ts=now)
            await self.rt.enter_safe(self.name, f"Uscita non affidabile posizione {pos_id}")
        return o

    # --- prezzi: mark-to-market, stop, take-profit, tempo massimo ---------------------
    async def on_price_tick(self, mint: str) -> None:
        now = self.rt.clock.now()
        m = await self._market(mint)
        if m is not None and m.price_usd is not None and m.price_observed_at is not None:
            self.rt.ledger.mark(mint, m.price_usd, m.price_observed_at)
        cfg = self.rt.cfg.risk
        with self.rt.db.session() as s:
            positions = list(s.scalars(select(Position).where(Position.mint == mint, Position.status == "OPEN")))
        for p in positions:
            if p.strategy not in self.rt.strategies:
                continue
            fresh = p.last_price_at is not None and (now - p.last_price_at).total_seconds() <= cfg.max_price_age_s
            reason = None
            if (now - p.opened_at).total_seconds() > cfg.max_hold_hours * 3600:
                reason = ReasonCode.MAX_HOLD
            elif fresh and p.last_price_usd is not None:
                change = p.last_price_usd / p.entry_price_usd - 1
                if change <= -cfg.stop_loss_pct:
                    reason = ReasonCode.STOP_LOSS
                elif change >= cfg.take_profit_pct:
                    reason = ReasonCode.TAKE_PROFIT
            if reason is None:
                continue
            with self.rt.db.session() as s:
                attempts = s.scalar(select(func.count()).select_from(Order).where(
                    Order.position_id == p.id, Order.reason == ReasonCode.EXIT_NOT_RELIABLE.value))
            # lo stop viene eseguito al prezzo ottenuto DOPO il ritardo, mai al prezzo dello stop
            await self._sell(p.strategy, p.id, Decimal(1), reason, f"{p.strategy}:pos{p.id}:{reason.value}:{attempts}",
                             None, None)

    # --- reorg ------------------------------------------------------------------
    async def on_retracted(self, env: EventEnvelope) -> None:
        sig, wallet = env.payload["signature"], env.payload["wallet"]
        self.trader.retract(sig)
        key = make_event(EventType.LEADER_SWAP, "x", (sig, wallet), self.rt.origin, "x", env.event_at).idempotency_key
        with self.rt.db.session() as s:
            ev = s.scalars(select(EventRow).where(EventRow.idempotency_key == key)).first()
            orders = list(s.scalars(select(Order).where(Order.source_event_id == ev.event_id))) if ev else []
        now = self.rt.clock.now()
        if not orders:
            self.rt.db.audit(self.name, "reorg.retracted", f"Tx {sig[:10]}… ritirata: nessuna copia effettuata",
                             {"signature": sig}, ts=now)
            return
        for o in orders:
            self.rt.db.audit(self.name, "reorg.after_copy",
                             f"[{o.strategy}] {ReasonCode.REORG_AFTER_COPY.value}: tx {sig[:10]}… ritirata dopo "
                             f"ordine {o.id} ({o.state}); posizione mantenuta in gestione",
                             {"signature": sig, "order_id": o.id}, "WARN", ts=now)
