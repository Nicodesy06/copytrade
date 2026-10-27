"""Persistenza SQLite (WAL) con SQLAlchemy 2. Gli importi sono salvati come stringhe Decimal."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from ..types import canonical_json, utcnow

SCHEMA_VERSION = 1


class DecimalStr(TypeDecorator):
    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return str(Decimal(value))

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        return None if value is None else Decimal(value)


class BigIntStr(TypeDecorator):
    """Interi arbitrari (quantità raw dei token possono superare 2^63)."""

    impl = String(80)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        return None if value is None else str(int(value))

    def process_result_value(self, value: Any, dialect: Any) -> int | None:
        return None if value is None else int(value)


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime senza timezone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class JSONText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        return None if value is None else canonical_json(value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        return None if value is None else json.loads(value)


class Base(DeclarativeBase):
    pass


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONText)


class EventRow(Base):
    __tablename__ = "events"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32))
    slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    event_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    wallet: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance: Mapped[Any] = mapped_column(JSONText)
    payload: Mapped[Any] = mapped_column(JSONText)


class ConsumerCheckpoint(Base):
    __tablename__ = "consumer_done"
    consumer: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    done_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Checkpoint(Base):
    __tablename__ = "checkpoints"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONText)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Control(Base):
    __tablename__ = "control"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    actor: Mapped[str] = mapped_column(String(64))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    level: Mapped[str] = mapped_column(String(8), default="INFO")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[Any] = mapped_column(JSONText, nullable=True)


class Wallet(Base):
    __tablename__ = "wallets"
    address: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32))  # seed | discovery | manual
    discovered_via: Mapped[str | None] = mapped_column(String(128), nullable=True)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class TraderScore(Base):
    __tablename__ = "trader_scores"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(String(64), index=True)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(32))
    score: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    metrics: Mapped[Any] = mapped_column(JSONText)
    reasons: Mapped[Any] = mapped_column(JSONText)


class LeaderSlot(Base):
    __tablename__ = "leaders"
    strategy: Mapped[str] = mapped_column(String(16), primary_key=True)
    wallet: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    rank: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    selected_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class TokenCheck(Base):
    __tablename__ = "token_checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reasons: Mapped[Any] = mapped_column(JSONText)
    facts: Mapped[Any] = mapped_column(JSONText)


class DecisionRow(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    strategy: Mapped[str] = mapped_column(String(16), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32))
    source_event_id: Mapped[str] = mapped_column(String(64))
    wallet: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    side: Mapped[str] = mapped_column(String(8))
    decision: Mapped[str] = mapped_column(String(8))
    reasons: Mapped[Any] = mapped_column(JSONText)
    facts: Mapped[Any] = mapped_column(JSONText)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __table_args__ = (UniqueConstraint("strategy", "source_event_id", "side", name="uq_decision"),)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    strategy: Mapped[str] = mapped_column(String(16), index=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leader: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mint: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[str] = mapped_column(String(48))
    requested_usd: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    requested_fraction: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    leader_event_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    leader_price_usd: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    quote_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    quote_price_usd: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    fill_price_usd: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    qty_raw: Mapped[int | None] = mapped_column(BigIntStr, nullable=True)
    decimals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notional_usd: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    fees: Mapped[Any] = mapped_column(JSONText, nullable=True)
    price_impact_pct: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    est_slippage_bps: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    actual_slippage_bps: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    delay_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[Any] = mapped_column(JSONText, nullable=True)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(16), index=True)
    leader: Mapped[str] = mapped_column(String(64), index=True)
    mint: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(8), index=True)  # OPEN | CLOSED
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decimals: Mapped[int] = mapped_column(Integer)
    qty_raw: Mapped[int] = mapped_column(BigIntStr)
    entry_qty_raw: Mapped[int] = mapped_column(BigIntStr)
    cost_basis_usd: Mapped[Decimal] = mapped_column(DecimalStr)  # costo residuo (incl. commissioni)
    total_cost_usd: Mapped[Decimal] = mapped_column(DecimalStr)
    proceeds_usd: Mapped[Decimal] = mapped_column(DecimalStr)
    realized_pnl_usd: Mapped[Decimal] = mapped_column(DecimalStr)
    fees_usd: Mapped[Decimal] = mapped_column(DecimalStr)
    entry_price_usd: Mapped[Decimal] = mapped_column(DecimalStr)
    last_price_usd: Mapped[Decimal | None] = mapped_column(DecimalStr, nullable=True)
    last_price_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    partial_exits: Mapped[int] = mapped_column(Integer, default=0)
    flags: Mapped[Any] = mapped_column(JSONText, nullable=True)


class LedgerEntry(Base):
    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(16), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    kind: Mapped[str] = mapped_column(String(24))
    amount_usd: Mapped[Decimal] = mapped_column(DecimalStr)  # variazione della cassa virtuale
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class EquitySnapshot(Base):
    __tablename__ = "equity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(16), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    equity_usd: Mapped[Decimal] = mapped_column(DecimalStr)
    cash_usd: Mapped[Decimal] = mapped_column(DecimalStr)
    stale_marks: Mapped[int] = mapped_column(Integer, default=0)


class ProviderUsage(Base):
    __tablename__ = "provider_usage"
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    rate_limited: Mapped[int] = mapped_column(Integer, default=0)
    cache_hits: Mapped[int] = mapped_column(Integer, default=0)
    near_quota_events: Mapped[int] = mapped_column(Integer, default=0)


class AgentStatusRow(Base):
    __tablename__ = "agent_status"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    restarts: Mapped[int] = mapped_column(Integer, default=0)
    queue_depth: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class Reservation(Base):
    """Prenotazione atomica di una posizione: conta nei limiti di portafoglio prima del fill."""

    __tablename__ = "reservations"
    strategy: Mapped[str] = mapped_column(String(16), primary_key=True)
    mint: Mapped[str] = mapped_column(String(64), primary_key=True)
    leader: Mapped[str] = mapped_column(String(64))
    cost_usd: Mapped[Decimal] = mapped_column(DecimalStr)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    title: Mapped[str] = mapped_column(String(128))
    body_md: Mapped[str] = mapped_column(Text)
    data: Mapped[Any] = mapped_column(JSONText)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{self.path}", future=True)

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn: Any, _rec: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        Base.metadata.create_all(self.engine)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        with self.session() as s:
            if s.get(Meta, "schema_version") is None:
                s.add(Meta(key="schema_version", value=SCHEMA_VERSION))

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._sessions()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # --- meta / checkpoint -------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.session() as s:
            row = s.get(Meta, key)
            return default if row is None else row.value

    def set_meta(self, key: str, value: Any) -> None:
        with self.session() as s:
            s.merge(Meta(key=key, value=value))

    def get_checkpoint(self, key: str, default: Any = None) -> Any:
        with self.session() as s:
            row = s.get(Checkpoint, key)
            return default if row is None else row.value

    def set_checkpoint(self, key: str, value: Any, now: datetime | None = None) -> None:
        with self.session() as s:
            s.merge(Checkpoint(key=key, value=value, updated_at=now or utcnow()))

    # --- audit -----------------------------------------------------------
    def audit(
        self,
        actor: str,
        action: str,
        message: str,
        data: Any = None,
        level: str = "INFO",
        ts: datetime | None = None,
        session: Session | None = None,
    ) -> None:
        row = AuditLog(ts=ts or utcnow(), actor=actor, action=action, level=level, message=message, data=data)
        if session is not None:
            session.add(row)
            return
        with self.session() as s:
            s.add(row)

    # --- controllo --------------------------------------------------------
    def get_control(self, key: str, default: str) -> str:
        with self.session() as s:
            row = s.get(Control, key)
            return default if row is None else row.value

    def set_control(self, key: str, value: str, actor: str, reason: str | None = None) -> None:
        with self.session() as s:
            old = s.get(Control, key)
            s.merge(Control(key=key, value=value, reason=reason, updated_at=utcnow(), actor=actor))
            s.add(
                AuditLog(
                    ts=utcnow(),
                    actor=actor,
                    action=f"control.{key}",
                    level="WARN",
                    message=f"{key}: {old.value if old else '-'} -> {value}" + (f" ({reason})" if reason else ""),
                    data={"key": key, "value": value, "reason": reason},
                )
            )

    # --- wallet ------------------------------------------------------------
    def add_wallet(
        self,
        address: str,
        source: str,
        label: str | None = None,
        via: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        with self.session() as s:
            if s.get(Wallet, address) is not None:
                return False
            s.add(Wallet(address=address, source=source, label=label, discovered_via=via, added_at=now or utcnow()))
            return True

    def list_wallets(self) -> list[Wallet]:
        with self.session() as s:
            return list(s.scalars(select(Wallet).order_by(Wallet.added_at)))

    # --- stato agenti -------------------------------------------------------
    def heartbeat(self, name: str, status: str, **fields: Any) -> None:
        with self.session() as s:
            row = s.get(AgentStatusRow, name)
            if row is None:
                row = AgentStatusRow(name=name, status=status, heartbeat_at=utcnow())
                s.add(row)
            row.status = status
            row.heartbeat_at = utcnow()
            for k, v in fields.items():
                setattr(row, k, v)


def today_str(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%d")
