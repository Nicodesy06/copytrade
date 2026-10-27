"""Ledger virtuale riproducibile. Nessuna contabilità ottimistica: tutte le commissioni sono a carico.

Convenzioni:
- `cost_basis_usd` include commissioni d'acquisto e rent dell'account token.
- Il P&L realizzato di una vendita = incasso netto (dopo commissioni) − quota di costo residuo.
- Il rent viene restituito solo alla chiusura totale (l'account token viene chiuso).
- Le posizioni con prezzo stantio non contano nell'equity usata per dimensionare gli ordini.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..storage.db import Database, EquitySnapshot, LedgerEntry, Order, Position

ZERO = Decimal(0)


@dataclass(frozen=True)
class Fill:
    qty_raw: int
    decimals: int
    price_usd: Decimal  # prezzo effettivo per unità (impatto + slippage inclusi)
    quote_price_usd: Decimal  # prezzo di riferimento della quotazione ritardata
    notional_usd: Decimal  # qty * price_usd
    fee_network_usd: Decimal
    fee_priority_usd: Decimal
    fee_dex_usd: Decimal
    rent_usd: Decimal  # >0 al primo acquisto, rimborso alla chiusura
    price_impact_pct: Decimal
    est_slippage_bps: Decimal
    actual_slippage_bps: Decimal
    quoted_at: datetime
    filled_at: datetime

    @property
    def qty(self) -> Decimal:
        return Decimal(self.qty_raw) / (Decimal(10) ** self.decimals)

    def fees_dict(self) -> dict[str, str]:
        return {
            "network_usd": str(self.fee_network_usd),
            "priority_usd": str(self.fee_priority_usd),
            "dex_usd": str(self.fee_dex_usd),
            "rent_usd": str(self.rent_usd),
        }


@dataclass
class EquityView:
    cash: Decimal
    fresh_value: Decimal
    stale_value: Decimal
    stale_positions: int
    open_positions: int
    cost_basis_open: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal
    by_position: list[dict] = field(default_factory=list)

    @property
    def equity_conservative(self) -> Decimal:
        return self.cash + self.fresh_value

    @property
    def equity_estimate(self) -> Decimal:
        return self.cash + self.fresh_value + self.stale_value

    @property
    def unrealized(self) -> Decimal:
        return self.fresh_value + self.stale_value - self.cost_basis_open

    @property
    def gross_exposure(self) -> Decimal:
        return self.cost_basis_open


class Ledger:
    def __init__(self, db: Database, starting_equity: Decimal, max_price_age_s: int):
        self.db = db
        self.starting_equity = starting_equity
        self.max_price_age_s = max_price_age_s

    def init_strategy(self, strategy: str, now: datetime) -> None:
        with self.db.session() as s:
            n = s.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.strategy == strategy))
            if not n:
                s.add(
                    LedgerEntry(
                        strategy=strategy, ts=now, kind="DEPOSIT", amount_usd=self.starting_equity,
                        note="Capitale virtuale iniziale (non è un saldo reale)",
                    )
                )

    def cash(self, strategy: str, s: Session | None = None) -> Decimal:
        def q(sess: Session) -> Decimal:
            vals = sess.scalars(select(LedgerEntry.amount_usd).where(LedgerEntry.strategy == strategy))
            return sum(vals, ZERO)

        if s is not None:
            return q(s)
        with self.db.session() as sess:
            return q(sess)

    def open_positions(self, strategy: str | None = None, s: Session | None = None) -> list[Position]:
        stmt = select(Position).where(Position.status == "OPEN")
        if strategy:
            stmt = stmt.where(Position.strategy == strategy)
        if s is not None:
            return list(s.scalars(stmt))
        with self.db.session() as sess:
            return list(sess.scalars(stmt))

    def open_position(self, strategy: str, mint: str, s: Session) -> Position | None:
        return s.scalars(
            select(Position).where(Position.strategy == strategy, Position.mint == mint, Position.status == "OPEN")
        ).first()

    # --- scritture (sempre dentro la transazione dell'ordine) ---------------
    def _entry(self, s: Session, strategy: str, ts: datetime, kind: str, amount: Decimal, order: Order,
               pos_id: int | None) -> None:
        if amount == 0:
            return
        s.add(LedgerEntry(strategy=strategy, ts=ts, kind=kind, amount_usd=amount, order_id=order.id,
                          position_id=pos_id))

    def apply_buy(self, s: Session, order: Order, fill: Fill, symbol: str | None) -> Position:
        total = fill.notional_usd + fill.fee_network_usd + fill.fee_priority_usd + fill.fee_dex_usd + fill.rent_usd
        pos = Position(
            strategy=order.strategy,
            leader=order.leader or "",
            mint=order.mint,
            symbol=symbol,
            status="OPEN",
            opened_at=fill.filled_at,
            decimals=fill.decimals,
            qty_raw=fill.qty_raw,
            entry_qty_raw=fill.qty_raw,
            cost_basis_usd=total,
            total_cost_usd=total,
            proceeds_usd=ZERO,
            realized_pnl_usd=ZERO,
            fees_usd=fill.fee_network_usd + fill.fee_priority_usd + fill.fee_dex_usd,
            entry_price_usd=fill.price_usd,
            last_price_usd=fill.quote_price_usd,
            last_price_at=fill.quoted_at,
            partial_exits=0,
            flags={},
        )
        s.add(pos)
        s.flush()
        ts = fill.filled_at
        self._entry(s, order.strategy, ts, "BUY_NOTIONAL", -fill.notional_usd, order, pos.id)
        self._entry(s, order.strategy, ts, "FEE_NETWORK", -fill.fee_network_usd, order, pos.id)
        self._entry(s, order.strategy, ts, "FEE_PRIORITY", -fill.fee_priority_usd, order, pos.id)
        self._entry(s, order.strategy, ts, "FEE_DEX", -fill.fee_dex_usd, order, pos.id)
        self._entry(s, order.strategy, ts, "RENT", -fill.rent_usd, order, pos.id)
        order.position_id = pos.id
        return pos

    def apply_sell(self, s: Session, order: Order, pos: Position, fill: Fill) -> Decimal:
        """Ritorna il P&L realizzato di questa vendita (netto commissioni)."""
        if fill.qty_raw <= 0 or fill.qty_raw > pos.qty_raw:
            raise ValueError("quantità di vendita non valida")
        closing = fill.qty_raw == pos.qty_raw
        proceeds = fill.notional_usd - fill.fee_network_usd - fill.fee_priority_usd - fill.fee_dex_usd
        if closing:
            proceeds += fill.rent_usd  # rimborso rent alla chiusura dell'account
        cost_part = (pos.cost_basis_usd * Decimal(fill.qty_raw) / Decimal(pos.qty_raw)) if not closing else (
            pos.cost_basis_usd
        )
        realized = proceeds - cost_part
        ts = fill.filled_at
        self._entry(s, order.strategy, ts, "SELL_PROCEEDS", fill.notional_usd, order, pos.id)
        self._entry(s, order.strategy, ts, "FEE_NETWORK", -fill.fee_network_usd, order, pos.id)
        self._entry(s, order.strategy, ts, "FEE_PRIORITY", -fill.fee_priority_usd, order, pos.id)
        self._entry(s, order.strategy, ts, "FEE_DEX", -fill.fee_dex_usd, order, pos.id)
        if closing:
            self._entry(s, order.strategy, ts, "RENT_REFUND", fill.rent_usd, order, pos.id)
        pos.qty_raw -= fill.qty_raw
        pos.cost_basis_usd -= cost_part
        pos.proceeds_usd += proceeds
        pos.realized_pnl_usd += realized
        pos.fees_usd += fill.fee_network_usd + fill.fee_priority_usd + fill.fee_dex_usd
        pos.last_price_usd = fill.quote_price_usd
        pos.last_price_at = fill.quoted_at
        if closing:
            pos.status = "CLOSED"
            pos.closed_at = ts
            pos.close_reason = order.reason
            pos.cost_basis_usd = ZERO
        else:
            pos.partial_exits += 1
        order.position_id = pos.id
        return realized

    def mark(self, mint: str, price: Decimal, at: datetime) -> None:
        with self.db.session() as s:
            for p in s.scalars(select(Position).where(Position.mint == mint, Position.status == "OPEN")):
                if p.last_price_at is None or at >= p.last_price_at:
                    p.last_price_usd = price
                    p.last_price_at = at

    # --- letture ----------------------------------------------------------
    def view(self, strategy: str, now: datetime) -> EquityView:
        with self.db.session() as s:
            cash = self.cash(strategy, s)
            positions = self.open_positions(strategy, s)
            realized = sum(
                s.scalars(select(Position.realized_pnl_usd).where(Position.strategy == strategy)), ZERO
            )
            fees = sum(s.scalars(select(Position.fees_usd).where(Position.strategy == strategy)), ZERO)
        fresh = stale = basis = ZERO
        n_stale = 0
        rows = []
        for p in positions:
            qty = Decimal(p.qty_raw) / (Decimal(10) ** p.decimals)
            value = qty * p.last_price_usd if p.last_price_usd is not None else None
            is_stale = (
                p.last_price_at is None
                or value is None
                or (now - p.last_price_at).total_seconds() > self.max_price_age_s
            )
            basis += p.cost_basis_usd
            if is_stale:
                n_stale += 1
                stale += value or ZERO
            else:
                fresh += value or ZERO
            rows.append({"id": p.id, "mint": p.mint, "value": value, "stale": is_stale})
        return EquityView(cash, fresh, stale, n_stale, len(positions), basis, realized, fees, rows)

    def snapshot(self, strategy: str, now: datetime) -> EquityView:
        v = self.view(strategy, now)
        with self.db.session() as s:
            s.add(EquitySnapshot(strategy=strategy, ts=now, equity_usd=v.equity_estimate, cash_usd=v.cash,
                                 stale_marks=v.stale_positions))
        return v
