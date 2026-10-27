"""Motore di rischio: limiti di portafoglio e fail-closed (unit test puri)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from m4desk.config import RiskConfig
from m4desk.providers.base import MarketSnapshot
from m4desk.services.ledger import EquityView
from m4desk.services.risk import BuyContext, RiskEngine, TokenAssessment
from m4desk.types import CheckStatus, DataOrigin, LeaderStatus, ReasonCode, SystemState

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)
D = Decimal


def view(cash=1000, open_positions=0, basis=0, stale=0) -> EquityView:
    return EquityView(D(cash), D(basis), D(0), stale, open_positions, D(basis), D(0), D(0))


def market(price="0.01", liq="500000", age_s=5) -> MarketSnapshot:
    return MarketSnapshot(mint="M", price_usd=D(price) if price else None, liquidity_usd=D(liq) if liq else None,
                          pair_created_at=NOW - timedelta(days=10), price_observed_at=NOW - timedelta(seconds=age_s),
                          fetched_at=NOW, provider="t", origin=DataOrigin.SIMULATED)


def token(status=CheckStatus.PASS, reasons=()) -> TokenAssessment:
    return TokenAssessment("M", status, tuple(reasons), (), {}, NOW, "T", 6)


def ctx(**kw) -> BuyContext:
    base = dict(strategy="s", leader="L", mint="M", now=NOW, event_at=NOW - timedelta(seconds=2),
                system_state=SystemState.RUNNING, leader_status=LeaderStatus.SELECTED, consecutive_losses=0,
                loss_limit=2, token=token(), market=market(), view=view(), has_open_position_on_mint=False,
                last_exit_at_for_mint=None, day_start_equity=D(1000), leader_fill_price_usd=D("0.01"))
    base.update(kw)
    return BuyContext(**base)


ENGINE = RiskEngine(RiskConfig())


def test_happy_path_sizes_5pct():
    v = ENGINE.evaluate_buy(ctx())
    assert v.allowed and v.size_usd == D("50.00") and v.reasons == [ReasonCode.OK]


def test_flat_ticket_never_scales_up():
    v = ENGINE.evaluate_buy(ctx(flat_ticket=True, view=view(cash=5000)))
    assert v.allowed and v.size_usd == D("20.00")


def test_max_positions_cap():
    v = ENGINE.evaluate_buy(ctx(view=view(open_positions=3, basis=60)))
    assert not v.allowed and ReasonCode.MAX_POSITIONS in v.reasons


def test_reservations_count_towards_max_positions():
    v = ENGINE.evaluate_buy(ctx(view=view(open_positions=2, basis=0), reserved_count=1))
    assert ReasonCode.MAX_POSITIONS in v.reasons


def test_gross_exposure_cap():
    v = ENGINE.evaluate_buy(ctx(view=view(cash=855, basis=148, open_positions=2)))
    assert not v.allowed and ReasonCode.GROSS_EXPOSURE in v.reasons


def test_daily_loss_limit():
    v = ENGINE.evaluate_buy(ctx(view=view(cash=975), day_start_equity=D(1000)))
    assert not v.allowed and ReasonCode.DAILY_LOSS_LIMIT in v.reasons


def test_insufficient_cash():
    # posizioni molto in guadagno: equity alta, cassa bassa
    rich = EquityView(D(40), D(2000), D(0), 0, 1, D(100), D(0), D(0))
    v = ENGINE.evaluate_buy(ctx(view=rich, day_start_equity=D(2040)))
    assert not v.allowed and ReasonCode.INSUFFICIENT_CASH in v.reasons


def test_double_exposure_and_cooldown():
    v = ENGINE.evaluate_buy(ctx(has_open_position_on_mint=True, last_exit_at_for_mint=NOW - timedelta(seconds=60)))
    assert {ReasonCode.DOUBLE_EXPOSURE, ReasonCode.COOLDOWN} <= set(v.reasons)


def test_system_states_block_new_buys():
    for st, code in ((SystemState.PAUSED, ReasonCode.SYSTEM_PAUSED), (SystemState.SAFE, ReasonCode.SYSTEM_SAFE_MODE),
                     (SystemState.STOPPED, ReasonCode.SYSTEM_STOPPED)):
        v = ENGINE.evaluate_buy(ctx(system_state=st))
        assert not v.allowed and code in v.reasons


def test_fail_closed_on_missing_data():
    v = ENGINE.evaluate_buy(ctx(token=None, market=None, leader_fill_price_usd=None))
    assert not v.allowed
    assert {ReasonCode.RUGCHECK_MISSING, ReasonCode.PRICE_UNAVAILABLE, ReasonCode.LIQUIDITY_UNAVAILABLE,
            ReasonCode.LEADER_FILL_UNKNOWN} <= set(v.reasons)


def test_token_unavailable_is_never_a_buy():
    v = ENGINE.evaluate_buy(ctx(token=token(CheckStatus.UNAVAILABLE, [ReasonCode.PROVIDER_RATE_LIMITED])))
    assert not v.allowed and ReasonCode.PROVIDER_RATE_LIMITED in v.reasons


def test_stale_token_check_and_old_event():
    old = TokenAssessment("M", CheckStatus.PASS, (), (), {}, NOW - timedelta(hours=1))
    v = ENGINE.evaluate_buy(ctx(token=old, event_at=NOW - timedelta(minutes=10)))
    assert {ReasonCode.RUGCHECK_STALE, ReasonCode.EVENT_TOO_OLD} <= set(v.reasons)


def test_provider_blockers_and_breakers_propagate():
    v = ENGINE.evaluate_buy(ctx(provider_blockers=[ReasonCode.BUDGET_EXHAUSTED, ReasonCode.FEED_LAG_BREAKER]))
    assert not v.allowed and ReasonCode.BUDGET_EXHAUSTED in v.reasons and ReasonCode.FEED_LAG_BREAKER in v.reasons


def test_leader_blocked_or_not_selected():
    assert ReasonCode.LEADER_BLOCKED_LOSSES in ENGINE.evaluate_buy(ctx(leader_status=LeaderStatus.BLOCKED)).reasons
    assert ReasonCode.LEADER_NOT_SELECTED in ENGINE.evaluate_buy(ctx(leader_status=None)).reasons
    assert ReasonCode.LEADER_BLOCKED_LOSSES in ENGINE.evaluate_buy(ctx(consecutive_losses=2)).reasons
    # baseline senza regola di sostituzione: le perdite non bloccano
    assert ENGINE.evaluate_buy(ctx(consecutive_losses=5, loss_limit=None)).allowed


def test_max_chase_boundary():
    assert ENGINE.evaluate_buy(ctx(market=market("0.0115"))).allowed  # +15% esatto: ammesso
    v = ENGINE.evaluate_buy(ctx(market=market("0.01151")))
    assert ReasonCode.MAX_CHASE_EXCEEDED in v.reasons


def test_size_capped_by_liquidity_then_too_small():
    v = ENGINE.evaluate_buy(ctx(market=market(liq="60000")))  # 2% di 60k = 1200 > 50: ok
    assert v.allowed
    cfg = RiskConfig(min_liquidity_usd=D(100))
    v2 = RiskEngine(cfg).evaluate_buy(ctx(market=market(liq="200")))
    assert ReasonCode.ORDER_TOO_SMALL in v2.reasons


def test_hard_caps_cannot_be_loosened():
    import pytest
    from pydantic import ValidationError

    for kw in ({"max_position_pct": D("0.10")}, {"max_gross_exposure_pct": D("0.5")}, {"max_leaders": 5},
               {"max_daily_loss_pct": D("0.05")}, {"max_chase_pct": D("30")}):
        with pytest.raises(ValidationError):
            RiskConfig(**kw)
