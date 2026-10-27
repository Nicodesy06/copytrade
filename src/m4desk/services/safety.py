"""Controlli dell'addendum di sicurezza: prenotazioni atomiche, breaker di ritardo, invarianti (contratti)."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from ..storage.db import Database, Order, Position, Reservation
from ..types import OrderState, ReasonCode

ZERO = Decimal(0)
TOL = Decimal("0.000001")


class ReservationBook:
    """Valutazione rischio + prenotazione sono atomiche per strategia (lock asyncio + vincolo unico DB)."""

    def __init__(self, db: Database):
        self.db = db
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock(self, strategy: str) -> asyncio.Lock:
        return self._locks[strategy]

    def totals(self, strategy: str, leader: str | None = None) -> tuple[int, Decimal]:
        with self.db.session() as s:
            stmt = select(func.count(), func.group_concat(Reservation.cost_usd, "|")).where(
                Reservation.strategy == strategy
            )
            if leader is not None:
                stmt = stmt.where(Reservation.leader == leader)
            n, costs = s.execute(stmt).one()
        total = sum((Decimal(x) for x in (costs or "").split("|") if x), ZERO)
        return int(n or 0), total

    def reserve(self, strategy: str, mint: str, leader: str, cost: Decimal, now: datetime) -> bool:
        try:
            with self.db.session() as s:
                s.add(Reservation(strategy=strategy, mint=mint, leader=leader, cost_usd=cost, created_at=now))
            return True
        except IntegrityError:
            return False

    def release(self, strategy: str, mint: str) -> None:
        with self.db.session() as s:
            s.execute(delete(Reservation).where(Reservation.strategy == strategy, Reservation.mint == mint))

    def clear_all(self) -> int:
        """Al riavvio nessun ordine è in volo: le prenotazioni residue vengono rilasciate."""
        with self.db.session() as s:
            return s.execute(delete(Reservation)).rowcount or 0


@dataclass
class LagBreaker:
    """Si apre dopo `trip_count` misure consecutive oltre soglia; si richiude dopo `recover_count` sane."""

    name: str
    reason: ReasonCode
    max_lag_s: float
    trip_count: int = 3
    recover_count: int = 3
    _bad: int = 0
    _good: int = 0
    is_open: bool = False
    last_lag_s: float | None = None

    def observe(self, lag_s: float) -> bool:
        """Ritorna True se lo stato del breaker è cambiato."""
        self.last_lag_s = lag_s
        before = self.is_open
        if lag_s > self.max_lag_s:
            self._bad += 1
            self._good = 0
            if self._bad >= self.trip_count:
                self.is_open = True
        else:
            self._good += 1
            self._bad = 0
            if self.is_open and self._good >= self.recover_count:
                self.is_open = False
        return before != self.is_open


def check_contracts(db: Database, strategy: str, starting_equity: Decimal, max_positions: int,
                    cash: Decimal) -> list[str]:
    """Invarianti contabili. Qualsiasi violazione => modalità SAFE (nessun nuovo acquisto)."""
    v: list[str] = []
    with db.session() as s:
        pos = list(s.scalars(select(Position).where(Position.strategy == strategy)))
        filled_no_pos = s.scalar(
            select(func.count()).select_from(Order).where(
                Order.strategy == strategy, Order.state == OrderState.FILLED.value, Order.position_id.is_(None)
            )
        )
    open_pos = [p for p in pos if p.status == "OPEN"]
    basis = sum((p.cost_basis_usd for p in open_pos), ZERO)
    realized = sum((p.realized_pnl_usd for p in pos), ZERO)
    if cash < -TOL:
        v.append(f"cassa negativa: {cash}")
    if len(open_pos) > max_positions:
        v.append(f"posizioni aperte {len(open_pos)} > massimo {max_positions}")
    # identità contabile: cassa + costo residuo − P&L realizzato = capitale iniziale
    drift = cash + basis - realized - starting_equity
    if abs(drift) > Decimal("0.0001"):
        v.append(f"identità contabile violata (scarto {drift})")
    for p in pos:
        if p.qty_raw < 0 or p.qty_raw > p.entry_qty_raw:
            v.append(f"quantità non valida posizione {p.id}")
        if p.status == "OPEN" and (p.qty_raw == 0 or p.cost_basis_usd < -TOL):
            v.append(f"posizione aperta incoerente {p.id}")
        if p.status == "CLOSED" and p.qty_raw != 0:
            v.append(f"posizione chiusa con quantità residua {p.id}")
    mints = [p.mint for p in open_pos]
    if len(mints) != len(set(mints)):
        v.append("doppia esposizione sullo stesso token")
    if filled_no_pos:
        v.append(f"{filled_no_pos} ordini FILLED senza posizione")
    return v
