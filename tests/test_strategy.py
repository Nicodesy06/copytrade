"""Metriche trader, regola delle perdite consecutive, sostituzione e LLM consultivo."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from helpers import LEADER, S, engine, feed, follow, mint, scenario, swap, token
from m4desk.config import TraderConfig
from m4desk.services.events import make_event
from m4desk.services.llm import LLMService, MockLLMBackend
from m4desk.services.trader_metrics import compute_metrics, reconstruct, wilson
from m4desk.storage.db import AuditLog, LeaderSlot, TraderScore
from m4desk.types import EventType, Side, SwapObservation


def obs(side, t, tok, quote, m="M", pre=None, post=None, sig=None):
    return SwapObservation(signature=sig or f"s{t.timestamp()}{side}", slot=1, wallet="W", mint=m, side=Side(side),
                           token_amount_raw=tok, decimals=6, quote_mint="Q", quote_amount_raw=quote, quote_decimals=9,
                           wallet_pre_amount_raw=pre, wallet_post_amount_raw=post, event_at=t)


def test_round_trips_partial_and_orphans():
    t = S - timedelta(days=1)
    sw = [obs("BUY", t, 1000, 10**9), obs("SELL", t + timedelta(hours=1), 500, 6 * 10**8),
          obs("SELL", t + timedelta(hours=2), 500, 7 * 10**8), obs("SELL", t + timedelta(hours=3), 10, 10**7, m="X")]
    trips, orphans, open_n = reconstruct(sw, S)
    assert len(trips) == 1 and abs(trips[0].ret - 0.3) < 1e-9 and orphans == 1 and open_n == 0


def test_no_lookahead_in_metrics():
    t = S - timedelta(days=1)
    sw = [obs("BUY", t, 1000, 10**9), obs("SELL", S + timedelta(hours=1), 1000, 2 * 10**9)]
    trips, _, open_n = reconstruct(sw, S)
    assert trips == [] and open_n == 1  # la vendita futura non è visibile


def test_metrics_insufficient_data_and_buy_sell_ratio():
    t = S - timedelta(days=2)
    sw = [obs("BUY", t + timedelta(hours=i), 100, 10**8, m=f"M{i}") for i in range(3)]
    sw.append(obs("SELL", t + timedelta(hours=5), 100, 2 * 10**8, m="M0"))
    status, score, m, reasons = compute_metrics(sw, S, TraderConfig())
    assert status == "INSUFFICIENT_DATA" and score is None and reasons == ["INSUFFICIENT_HISTORY"]
    assert m["buy_sell_ratio"] == 3.0 and m["crowding_proxy"] is None


def test_wilson_interval():
    lo, hi = wilson(8, 10)
    assert 0.4 < lo < 0.8 < hi <= 1.0 and wilson(0, 0) == (None, None)


def _hist(wallet, rets, start, m_prefix="H"):
    out = []
    for i, r in enumerate(rets):
        t = start + timedelta(hours=6 * i)
        out.append(swap(wallet, f"{m_prefix}{i}" + "1" * 30, "BUY", t, 1000, 0.01, 0, 10**11))
        out.append(swap(wallet, f"{m_prefix}{i}" + "1" * 30, "SELL", t + timedelta(hours=2), 1000 * (1 + r),
                        0.01 * (1 + r), 10**11, 0))
    return out


async def _two_loss_setup(tmp_path, replacement=True):
    other = ("SimReplace" + "1" * 40)[:44]
    hist = _hist(other, [0.3, 0.2, -0.1, 0.25, 0.4, 0.2, -0.05, 0.3, 0.2, 0.1], S - timedelta(days=5)) if replacement \
        else []
    a, b = mint("LA"), mint("LB")
    rt, world, sup = await engine(tmp_path, scenario([token("LA", 0.01), token("LB", 0.01)], swaps=hist))
    follow(rt)
    if replacement:
        rt.db.add_wallet(other, "discovery")
        await sup.trader.score(other)
    for i, m in enumerate((a, b)):
        t = S + timedelta(minutes=30 * i)
        await feed(rt, world, swap(LEADER, m, "BUY", t, 2000, 0.01, 0, 2 * 10**11))
        await feed(rt, world, swap(LEADER, m, "SELL", t + timedelta(minutes=10), 0, 0.009, 2 * 10**11, 0))
    return rt, world, sup, other


async def test_two_consecutive_losses_block_leader_and_replace(tmp_path):
    rt, world, sup, other = await _two_loss_setup(tmp_path)
    with rt.db.session() as s:
        slots = {r.wallet: r for r in s.scalars(select(LeaderSlot).where(LeaderSlot.strategy == "switch2"))}
    assert slots[LEADER].status == "BLOCKED" and slots[LEADER].consecutive_losses == 2
    assert slots[other].status == "SELECTED"


async def test_no_defensible_replacement_follows_nobody(tmp_path):
    rt, world, sup, _ = await _two_loss_setup(tmp_path, replacement=False)
    with rt.db.session() as s:
        selected = [r for r in s.scalars(select(LeaderSlot)) if r.status == "SELECTED"]
        acts = [a.message for a in s.scalars(select(AuditLog).where(AuditLog.action == "selection.empty"))]
    assert selected == [] and any("NO_REPLACEMENT_CANDIDATE" in m for m in acts)


async def test_blocked_leader_open_positions_still_managed(tmp_path):
    rt, world, sup, _ = await _two_loss_setup(tmp_path, replacement=False)
    c = mint("LC")
    world.tokens[c] = dict(world.tokens[mint("LA")], symbol="LC")
    world.prices[c] = world.prices[mint("LA")]
    world.liquidity[c] = world.liquidity[mint("LA")]
    from m4desk.storage.db import Position

    # posizione aperta prima del blocco (simulata come ereditata) => le vendite del leader restano replicate
    with rt.db.session() as s:
        pos = s.scalars(select(Position)).first()
        pos.status, pos.qty_raw, pos.cost_basis_usd, pos.closed_at = "OPEN", pos.entry_qty_raw, Decimal(40), None
        pos.mint = c
        mid = pos.id
    await feed(rt, world, swap(LEADER, c, "SELL", S + timedelta(hours=3), 0, 0.01, 10**11, 0))
    with rt.db.session() as s:
        assert s.get(Position, mid).status == "CLOSED"


async def test_win_resets_loss_counter(tmp_path):
    rt, world, sup = await engine(tmp_path, scenario([token("WA", 0.01)]))
    follow(rt)
    for pnl in ("-1", "2"):
        await rt.bus.publish(make_event(EventType.POSITION_CLOSED, "t", (pnl,), rt.origin, "x", S,
                                        payload={"strategy": "switch2", "leader": LEADER, "position_id": 1,
                                                 "realized_pnl_usd": pnl, "reason": "x"}))
        await rt.bus.wait_idle()
    assert sup.strategy.leader_status("switch2", LEADER)[1] == 0


async def test_llm_only_reorders_eligible_and_falls_back_on_invalid(tmp_path):
    seen = {}

    class Spy(MockLLMBackend):
        async def chat_json(self, system, user, schema, timeout_s):
            seen["user"] = user
            return await super().chat_json(system, user, schema, timeout_s)

    from m4desk.config import LLMConfig

    svc = LLMService(LLMConfig(), Spy())
    res = await svc.rank_leaders([{"id": "W1", "profit_factor": 1.0}, {"id": "W2", "profit_factor": 3.0}])
    assert res.ok and res.advice.ranking == ["W2", "W1"]
    assert "description" not in seen["user"] and "name" not in json.loads(seen["user"][5:])["candidates"][0]
    for bad in ('{"ranking": ["EVIL"], "rationale": "x"}', '{"ranking": ["W1"], "rationale": "x", "action": "BUY"}',
                "BUY NOW", '{"ranking": ["W1","W1"], "rationale": ""}'):
        r = await LLMService(LLMConfig(), MockLLMBackend(bad)).rank_leaders([{"id": "W1"}, {"id": "W2"}])
        assert not r.ok and r.reason.value == "LLM_INVALID_OUTPUT"


async def test_llm_timeout_and_disabled_fallback():
    import asyncio

    from m4desk.config import LLMConfig

    class Slow(MockLLMBackend):
        async def chat_json(self, *a, **k):
            await asyncio.sleep(5)

    r = await LLMService(LLMConfig(timeout_s=0.05), Slow()).rank_leaders([{"id": "W1"}])
    assert not r.ok and r.reason.value == "LLM_TIMEOUT"
    r2 = await LLMService(LLMConfig(enabled=False), MockLLMBackend()).rank_leaders([{"id": "W1"}])
    assert r2.reason.value == "LLM_DISABLED"


def test_llm_must_be_local():
    import pytest
    from pydantic import ValidationError

    from m4desk.config import LLMConfig

    with pytest.raises(ValidationError):
        LLMConfig(base_url="https://api.example.com")


async def test_trader_score_is_persisted_with_provenance(tmp_path):
    hist = _hist(LEADER, [0.2] * 9, S - timedelta(days=4))
    rt, world, sup = await engine(tmp_path, scenario([], swaps=hist))
    row = await sup.trader.score(LEADER)
    with rt.db.session() as s:
        assert s.scalars(select(TraderScore)).first().wallet == LEADER
    assert row.status in ("ELIGIBLE", "BELOW_THRESHOLD") and row.metrics["round_trips"] == 9
