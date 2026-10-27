"""Broker PAPER: quotazione ritardata, ordini a due fasi, nessuna firma, nessun invio di transazioni.

La copia non eredita mai il prezzo del leader: si attende `fill_delay_s`, si chiede una nuova
quotazione e si applica slippage aggiuntivo avverso. Ordini oltre i limiti di liquidità/impatto
vengono marcati UNFILLABLE.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..clock import Clock
from ..config import RiskConfig, SimulationConfig
from ..providers.base import MarketDataProvider, ProviderError, Quote, QuoteProvider
from ..storage.db import Database, Order, Position
from ..types import LAMPORTS_PER_SOL, SOL_MINT, OrderState, ReasonCode, Side
from .ledger import Fill, Ledger

ZERO = Decimal(0)
BPS = Decimal(10_000)
Sleeper = Callable[[float], Awaitable[None]]


class PaperBroker:
    PAPER_ONLY = True  # questo componente non possiede chiavi e non firma nulla

    def __init__(
        self,
        db: Database,
        ledger: Ledger,
        quotes: QuoteProvider,
        market: MarketDataProvider,
        sim: SimulationConfig,
        risk: RiskConfig,
        clock: Clock,
        sleeper: Sleeper,
    ):
        self.db = db
        self.ledger = ledger
        self.quotes = quotes
        self.market = market
        self.sim = sim
        self.risk = risk
        self.clock = clock
        self.sleep = sleeper

    # --- fase 1 ----------------------------------------------------------
    def _create_pending(self, **kw) -> tuple[Order, bool]:
        try:
            with self.db.session() as s:
                o = Order(state=OrderState.PENDING.value, created_at=self.clock.now(), **kw)
                s.add(o)
                s.flush()
                s.expunge(o)
                return o, True
        except IntegrityError:
            with self.db.session() as s:
                o = s.scalars(select(Order).where(Order.idempotency_key == kw["idempotency_key"])).one()
                s.expunge(o)
                return o, False

    def _finish(self, order_id: int, state: OrderState, reason: ReasonCode, **fields) -> Order:
        with self.db.session() as s:
            o = s.get(Order, order_id)
            assert o is not None
            o.state = state.value
            o.reason = reason.value
            for k, v in fields.items():
                setattr(o, k, v)
            s.flush()
            s.expunge(o)
            return o

    async def _sol_price(self) -> tuple[Decimal, datetime]:
        m = await self.market.get_market(SOL_MINT)
        if m.price_usd is None or m.price_observed_at is None:
            raise ProviderError(self.market.name, ReasonCode.PRICE_UNAVAILABLE, "SOL")
        if (self.clock.now() - m.price_observed_at).total_seconds() > self.risk.max_price_age_s:
            raise ProviderError(self.market.name, ReasonCode.PRICE_STALE, "SOL")
        return m.price_usd, m.price_observed_at

    def _check_quote(self, q: Quote, ref_price: Decimal | None, implied: Decimal, max_impact: Decimal
                     ) -> ReasonCode | None:
        if (self.clock.now() - q.quoted_at).total_seconds() > self.risk.max_quote_age_s:
            return ReasonCode.QUOTE_STALE
        if q.price_impact_pct is None:
            return ReasonCode.QUOTE_LOW_CONFIDENCE
        if q.price_impact_pct > max_impact:
            return ReasonCode.UNFILLABLE_LIQUIDITY
        if ref_price is not None and ref_price > 0:
            dev = abs(implied - ref_price) / ref_price * 100
            # deviazione oltre l'impatto atteso => quotazione incoerente col mercato
            if dev > self.sim.quote_deviation_max_pct + q.price_impact_pct:
                return ReasonCode.QUOTE_LOW_CONFIDENCE
        return None

    def _fees(self, sol_px: Decimal, notional: Decimal, quote_fee_bps: int | None) -> tuple[Decimal, ...]:
        net = Decimal(self.sim.base_fee_lamports) / LAMPORTS_PER_SOL * sol_px
        prio = Decimal(self.sim.priority_fee_lamports) / LAMPORTS_PER_SOL * sol_px
        bps = max(self.sim.dex_fee_bps, quote_fee_bps or 0)  # prudente: commissione sempre a carico
        dex = notional * Decimal(bps) / BPS
        return net, prio, dex

    # --- acquisto --------------------------------------------------------
    async def buy(
        self,
        *,
        strategy: str,
        leader: str,
        mint: str,
        decimals: int,
        symbol: str | None,
        size_usd: Decimal,
        idempotency_key: str,
        source_event_id: str,
        leader_event_at: datetime,
        leader_price_usd: Decimal | None,
        decision_price_usd: Decimal | None,
    ) -> Order:
        order, new = self._create_pending(
            idempotency_key=idempotency_key, strategy=strategy, leader=leader, mint=mint, side=Side.BUY.value,
            reason=ReasonCode.OK.value, requested_usd=size_usd, leader_event_at=leader_event_at,
            leader_price_usd=leader_price_usd, source_event_id=source_event_id, decimals=decimals,
        )
        if not new:
            return order
        t0 = self.clock.now()
        await self.sleep(self.sim.fill_delay_s)
        try:
            sol_px, _ = await self._sol_price()
            lamports = int((size_usd / sol_px * LAMPORTS_PER_SOL).to_integral_value(rounding=ROUND_DOWN))
            q = await self.quotes.quote(SOL_MINT, mint, lamports)
            mkt = await self.market.get_market(mint)
        except ProviderError as e:
            return self._finish(order.id, OrderState.FAILED, e.reason, detail={"error": str(e)})
        if q.out_amount_raw <= 0:
            return self._finish(order.id, OrderState.UNFILLABLE, ReasonCode.UNFILLABLE_LIQUIDITY)
        quote_qty = Decimal(q.out_amount_raw) / (Decimal(10) ** decimals)
        implied = size_usd / quote_qty
        bad = self._check_quote(q, mkt.price_usd, implied, self.risk.max_price_impact_pct)
        if bad is None and (mkt.liquidity_usd is None
                            or size_usd > mkt.liquidity_usd * self.risk.max_order_liquidity_fraction):
            bad = ReasonCode.UNFILLABLE_LIQUIDITY
        # max chase sull'esecuzione: prezzo eseguibile della copia vs fill osservato del leader
        exec_price = implied / (1 - Decimal(self.sim.extra_slippage_bps) / BPS)
        chase = None
        if leader_price_usd is None or leader_price_usd <= 0:
            bad = bad or ReasonCode.LEADER_FILL_UNKNOWN
        else:
            chase = (exec_price - leader_price_usd) / leader_price_usd * 100
            if bad is None and chase > self.risk.max_chase_pct:
                bad = ReasonCode.MAX_CHASE_EXCEEDED
        if bad is not None:
            state = OrderState.UNFILLABLE if bad == ReasonCode.UNFILLABLE_LIQUIDITY else OrderState.REJECTED
            return self._finish(order.id, state, bad, quote_at=q.quoted_at, price_impact_pct=q.price_impact_pct,
                                detail={"implied_price": str(implied), "market_price": str(mkt.price_usd),
                                        "chase_pct": str(chase) if chase is not None else None})
        qty_raw = int(Decimal(q.out_amount_raw) * (1 - Decimal(self.sim.extra_slippage_bps) / BPS))
        qty = Decimal(qty_raw) / (Decimal(10) ** decimals)
        fill_price = size_usd / qty
        net, prio, dex = self._fees(sol_px, size_usd, q.fee_bps)
        rent = Decimal(self.sim.account_rent_lamports) / LAMPORTS_PER_SOL * sol_px
        ref = mkt.price_usd or implied
        est_bps = (q.price_impact_pct or ZERO) * 100 + self.sim.extra_slippage_bps
        actual_bps = ((fill_price - decision_price_usd) / decision_price_usd * BPS) if decision_price_usd else None
        now = self.clock.now()
        fill = Fill(
            qty_raw=qty_raw, decimals=decimals, price_usd=fill_price, quote_price_usd=ref, notional_usd=size_usd,
            fee_network_usd=net, fee_priority_usd=prio, fee_dex_usd=dex, rent_usd=rent,
            price_impact_pct=q.price_impact_pct or ZERO, est_slippage_bps=est_bps,
            actual_slippage_bps=actual_bps if actual_bps is not None else est_bps, quoted_at=q.quoted_at, filled_at=now,
        )
        with self.db.session() as s:  # fase 2 atomica: ordine + ledger
            o = s.get(Order, order.id)
            assert o is not None and o.state == OrderState.PENDING.value
            pos = self.ledger.apply_buy(s, o, fill, symbol)
            pos.flags = {"rent_usd": str(rent), "leader_entry_event": source_event_id}
            self._fill_fields(o, fill, t0, leader_event_at)
            o.detail = {"chase_pct": str(chase.quantize(Decimal("0.01"))) if chase is not None else None}
            s.flush()
            s.expunge(o)
            return o

    def _fill_fields(self, o: Order, f: Fill, t0: datetime, leader_event_at: datetime | None) -> None:
        o.state = OrderState.FILLED.value
        o.quote_at = f.quoted_at
        o.filled_at = f.filled_at
        o.quote_price_usd = f.quote_price_usd
        o.fill_price_usd = f.price_usd
        o.qty_raw = f.qty_raw
        o.decimals = f.decimals
        o.notional_usd = f.notional_usd
        o.fees = f.fees_dict()
        o.price_impact_pct = f.price_impact_pct
        o.est_slippage_bps = f.est_slippage_bps.quantize(Decimal("0.01"))
        o.actual_slippage_bps = f.actual_slippage_bps.quantize(Decimal("0.01"))
        start = leader_event_at or t0
        o.delay_ms = int((f.filled_at - start).total_seconds() * 1000)

    # --- vendita --------------------------------------------------------
    async def sell(
        self,
        *,
        position_id: int,
        fraction: Decimal,
        reason: ReasonCode,
        idempotency_key: str,
        source_event_id: str | None,
        leader_event_at: datetime | None,
    ) -> Order:
        with self.db.session() as s:
            pos = s.get(Position, position_id)
            assert pos is not None
            strategy, mint, leader, decimals = pos.strategy, pos.mint, pos.leader, pos.decimals
            open_qty, entry_qty, status = pos.qty_raw, pos.entry_qty_raw, pos.status
            rent_usd = Decimal((pos.flags or {}).get("rent_usd", "0"))
            decision_price = pos.last_price_usd
        order, new = self._create_pending(
            idempotency_key=idempotency_key, strategy=strategy, leader=leader, mint=mint, side=Side.SELL.value,
            reason=reason.value, requested_fraction=fraction, leader_event_at=leader_event_at,
            source_event_id=source_event_id, position_id=position_id, decimals=decimals,
        )
        if not new:
            return order
        if status != "OPEN" or open_qty <= 0:
            return self._finish(order.id, OrderState.REJECTED, ReasonCode.NO_POSITION)
        fraction = min(max(fraction, ZERO), Decimal(1))
        qty_raw = int((Decimal(open_qty) * fraction).to_integral_value(rounding=ROUND_DOWN))
        if fraction >= Decimal("0.999") or open_qty - qty_raw <= entry_qty // 100:
            qty_raw = open_qty  # niente polvere residua: chiusura totale
        if qty_raw <= 0:
            return self._finish(order.id, OrderState.REJECTED, ReasonCode.ORDER_TOO_SMALL)
        t0 = self.clock.now()
        await self.sleep(self.sim.fill_delay_s)
        try:
            sol_px, _ = await self._sol_price()
            q = await self.quotes.quote(mint, SOL_MINT, qty_raw)
            mkt = await self.market.get_market(mint)
        except ProviderError as e:
            return self._finish(order.id, OrderState.FAILED, ReasonCode.EXIT_NOT_RELIABLE,
                                detail={"error": str(e), "cause": e.reason.value})
        qty = Decimal(qty_raw) / (Decimal(10) ** decimals)
        gross_quote = Decimal(q.out_amount_raw) / LAMPORTS_PER_SOL * sol_px
        implied = gross_quote / qty if qty > 0 else ZERO
        bad = self._check_quote(q, mkt.price_usd, implied, self.sim.max_exit_price_impact_pct)
        if bad is not None or q.out_amount_raw <= 0:
            return self._finish(order.id, OrderState.FAILED, ReasonCode.EXIT_NOT_RELIABLE,
                                quote_at=q.quoted_at, price_impact_pct=q.price_impact_pct,
                                detail={"cause": (bad or ReasonCode.UNFILLABLE_LIQUIDITY).value})
        gross = gross_quote * (1 - Decimal(self.sim.extra_slippage_bps) / BPS)
        fill_price = gross / qty
        net, prio, dex = self._fees(sol_px, gross, q.fee_bps)
        ref = mkt.price_usd or implied
        est_bps = (q.price_impact_pct or ZERO) * 100 + self.sim.extra_slippage_bps
        actual_bps = ((decision_price - fill_price) / decision_price * BPS) if decision_price else est_bps
        fill = Fill(
            qty_raw=qty_raw, decimals=decimals, price_usd=fill_price, quote_price_usd=ref, notional_usd=gross,
            fee_network_usd=net, fee_priority_usd=prio, fee_dex_usd=dex, rent_usd=rent_usd,
            price_impact_pct=q.price_impact_pct or ZERO, est_slippage_bps=est_bps, actual_slippage_bps=actual_bps,
            quoted_at=q.quoted_at, filled_at=self.clock.now(),
        )
        with self.db.session() as s:
            o = s.get(Order, order.id)
            p = s.get(Position, position_id)
            assert o is not None and p is not None
            if p.status != "OPEN" or p.qty_raw < qty_raw:  # stato cambiato durante l'attesa
                o.state = OrderState.REJECTED.value
                o.reason = ReasonCode.POSITION_MISMATCH.value
            else:
                realized = self.ledger.apply_sell(s, o, p, fill)
                self._fill_fields(o, fill, t0, leader_event_at)
                o.detail = {"realized_pnl_usd": str(realized), "closed": p.status == "CLOSED"}
            s.flush()
            s.expunge(o)
            return o

    def recover_pending(self) -> list[int]:
        """Riavvio: gli ordini rimasti PENDING non hanno toccato il ledger => FAILED, mai riprovati in silenzio."""
        ids = []
        with self.db.session() as s:
            for o in s.scalars(select(Order).where(Order.state == OrderState.PENDING.value)):
                o.state = OrderState.FAILED.value
                o.reason = ReasonCode.RECOVERED_INCOMPLETE_ORDER.value
                ids.append(o.id)
        if ids:
            self.db.audit("broker", "recovery", f"{len(ids)} ordini PENDING annullati al riavvio", {"ids": ids}, "WARN")
        return ids
