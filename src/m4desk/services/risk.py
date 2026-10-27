"""Motore di rischio deterministico. Priorità più alta dell'AI; fail-closed su ogni dato mancante."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from ..config import RiskConfig
from ..providers.base import MarketSnapshot
from ..types import CheckStatus, LeaderStatus, ReasonCode, SystemState
from .ledger import EquityView

ZERO = Decimal(0)
CENT = Decimal("0.01")


@dataclass(frozen=True)
class TokenAssessment:
    mint: str
    status: CheckStatus
    reasons: tuple[ReasonCode, ...]
    warnings: tuple[ReasonCode, ...]
    facts: dict[str, Any]
    checked_at: datetime
    symbol: str | None = None
    decimals: int | None = None


@dataclass
class BuyContext:
    strategy: str
    leader: str
    mint: str
    now: datetime
    event_at: datetime
    system_state: SystemState
    leader_status: LeaderStatus | None
    consecutive_losses: int
    loss_limit: int | None  # None = nessuna regola di sostituzione (baseline naive)
    token: TokenAssessment | None
    market: MarketSnapshot | None
    view: EquityView
    has_open_position_on_mint: bool
    last_exit_at_for_mint: datetime | None
    day_start_equity: Decimal
    provider_blockers: list[ReasonCode] = field(default_factory=list)
    flat_ticket: bool = False
    leader_fill_price_usd: Decimal | None = None
    leader_open_cost_usd: Decimal = ZERO
    reserved_count: int = 0
    reserved_cost_usd: Decimal = ZERO


@dataclass
class RiskVerdict:
    allowed: bool
    reasons: list[ReasonCode]
    size_usd: Decimal
    facts: dict[str, Any]


class RiskEngine:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def evaluate_buy(self, c: BuyContext) -> RiskVerdict:
        cfg = self.cfg
        r: list[ReasonCode] = []
        facts: dict[str, Any] = {}

        # 1. stato del sistema (kill switch / pausa / safe)
        if c.system_state == SystemState.STOPPED:
            r.append(ReasonCode.SYSTEM_STOPPED)
        elif c.system_state == SystemState.SAFE:
            r.append(ReasonCode.SYSTEM_SAFE_MODE)
        elif c.system_state == SystemState.PAUSED:
            r.append(ReasonCode.SYSTEM_PAUSED)
        r.extend(c.provider_blockers)

        # 2. leader
        if c.leader_status != LeaderStatus.SELECTED:
            r.append(
                ReasonCode.LEADER_BLOCKED_LOSSES
                if c.leader_status == LeaderStatus.BLOCKED
                else ReasonCode.LEADER_NOT_SELECTED
            )
        if c.loss_limit is not None and c.consecutive_losses >= c.loss_limit:
            r.append(ReasonCode.LEADER_BLOCKED_LOSSES)
        age = (c.now - c.event_at).total_seconds()
        facts["event_age_s"] = round(age, 1)
        if age > cfg.max_event_age_s:
            r.append(ReasonCode.EVENT_TOO_OLD)

        # 3. token (controlli obbligatori)
        if c.token is None:
            r.append(ReasonCode.RUGCHECK_MISSING)
        else:
            facts["token_status"] = c.token.status.value
            if c.token.status != CheckStatus.PASS:
                r.extend(c.token.reasons or (ReasonCode.DATA_DESYNC,))
            elif (c.now - c.token.checked_at).total_seconds() > cfg.token_check_ttl_s:
                r.append(ReasonCode.RUGCHECK_STALE)

        # 4. mercato
        m = c.market
        if m is None or m.price_usd is None or m.price_usd <= 0:
            r.append(ReasonCode.PRICE_UNAVAILABLE)
        elif m.price_observed_at is None or (c.now - m.price_observed_at).total_seconds() > cfg.max_price_age_s:
            r.append(ReasonCode.PRICE_STALE)
        if m is None or m.liquidity_usd is None:
            r.append(ReasonCode.LIQUIDITY_UNAVAILABLE)
        elif m.liquidity_usd < cfg.min_liquidity_usd:
            r.append(ReasonCode.LIQUIDITY_TOO_LOW)
        if m is not None:
            facts["price_usd"] = str(m.price_usd) if m.price_usd is not None else None
            facts["liquidity_usd"] = str(m.liquidity_usd) if m.liquidity_usd is not None else None

        # 5. portafoglio
        v = c.view
        equity = v.equity_conservative
        facts["equity_conservative_usd"] = str(equity.quantize(CENT))
        if c.has_open_position_on_mint:
            r.append(ReasonCode.DOUBLE_EXPOSURE)
        if v.open_positions + c.reserved_count >= cfg.max_positions:
            r.append(ReasonCode.MAX_POSITIONS)
        if c.last_exit_at_for_mint is not None and (c.now - c.last_exit_at_for_mint).total_seconds() < cfg.cooldown_s:
            r.append(ReasonCode.COOLDOWN)
        if c.day_start_equity > 0:
            dd = (c.day_start_equity - v.equity_estimate) / c.day_start_equity
            facts["daily_drawdown_pct"] = str((dd * 100).quantize(CENT))
            if dd >= cfg.max_daily_loss_pct:
                r.append(ReasonCode.DAILY_LOSS_LIMIT)
        if v.stale_positions > 0:
            facts["stale_positions"] = v.stale_positions

        # strategia di test: ticket fisso, mai aumentato dopo le vincite
        size = cfg.flat_ticket_usd if c.flat_ticket else equity * cfg.max_position_pct
        room = equity * cfg.max_gross_exposure_pct - v.gross_exposure - c.reserved_cost_usd
        if room < size:
            size = max(room, ZERO)
            if size < cfg.min_order_usd:
                r.append(ReasonCode.GROSS_EXPOSURE)
        # libro a frazione fissa per leader (addendum): nessun leader può assorbire tutto il rischio
        frac = cfg.leader_book_fraction or (Decimal(1) / Decimal(max(cfg.max_leaders, 1)))
        book = equity * cfg.max_gross_exposure_pct * frac
        book_room = book - c.leader_open_cost_usd
        facts["leader_book_room_usd"] = str(book_room.quantize(CENT))
        if book_room < size:
            size = max(book_room, ZERO)
            if size < cfg.min_order_usd:
                r.append(ReasonCode.LEADER_BOOK_FULL)
        # max chase pre-trade: prezzo di mercato attuale vs fill osservato del leader
        if c.leader_fill_price_usd is None or c.leader_fill_price_usd <= 0:
            r.append(ReasonCode.LEADER_FILL_UNKNOWN)
        elif m is not None and m.price_usd is not None:
            chase = (m.price_usd - c.leader_fill_price_usd) / c.leader_fill_price_usd * 100
            facts["chase_pct_pre"] = str(chase.quantize(CENT))
            if chase > cfg.max_chase_pct:
                r.append(ReasonCode.MAX_CHASE_EXCEEDED)
        if m is not None and m.liquidity_usd is not None and m.liquidity_usd > 0:
            liq_cap = m.liquidity_usd * cfg.max_order_liquidity_fraction
            if liq_cap < size:
                facts["size_capped_by_liquidity"] = True
                size = liq_cap
        # riserva per commissioni/rent stimata 1 USD + 2%
        fee_reserve = Decimal(1) + size * Decimal("0.02")
        if v.cash < size + fee_reserve:
            r.append(ReasonCode.INSUFFICIENT_CASH)
        size = size.quantize(CENT, rounding=ROUND_DOWN)
        if size < cfg.min_order_usd and not {ReasonCode.GROSS_EXPOSURE, ReasonCode.LEADER_BOOK_FULL} & set(r):
            r.append(ReasonCode.ORDER_TOO_SMALL)
        facts["size_usd"] = str(size)

        # dedupe mantenendo l'ordine
        seen: list[ReasonCode] = []
        for x in r:
            if x not in seen:
                seen.append(x)
        return RiskVerdict(allowed=not seen, reasons=seen or [ReasonCode.OK], size_usd=size, facts=facts)
