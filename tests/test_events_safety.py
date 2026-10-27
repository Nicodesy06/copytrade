"""Event bus (at-least-once, dedupe, backpressure), gap-fill, breaker, invarianti, nessuna firma."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from helpers import LEADER, S, engine, feed, follow, mint, scenario, swap, token
from m4desk.services.events import EventBus, make_event
from m4desk.services.ingest import GapFiller
from m4desk.services.safety import LagBreaker, check_contracts
from m4desk.storage.db import Database, LedgerEntry
from m4desk.types import DataOrigin, EventType, ReasonCode, SystemState


def ev(i, t=EventType.PRICE_TICK):
    return make_event(t, "test", (i,), DataOrigin.SIMULATED, "x", S, token="M")


async def test_bus_dedupe_and_at_least_once_redelivery(tmp_path):
    db = Database(tmp_path / "b.db")
    bus = EventBus(db)
    seen, fail = [], {"on": True}

    async def h(e):
        if fail["on"]:
            raise RuntimeError("crash")
        seen.append(e.event_id)

    bus.subscribe("c", {EventType.PRICE_TICK}, h)
    bus.start()
    assert await bus.publish(ev(1)) is True
    assert await bus.publish(ev(1)) is False  # stessa idempotency key
    await bus.wait_idle()
    assert seen == [] and bus.subs[0].errors == 1
    await bus.stop()
    # "riavvio": l'evento non confermato viene riconsegnato
    bus2 = EventBus(db)
    bus2.subscribe("c", {EventType.PRICE_TICK}, h)
    fail["on"] = False
    bus2.start()
    assert await bus2.redeliver_pending() == 1
    await bus2.wait_idle()
    assert len(seen) == 1
    assert await bus2.redeliver_pending() == 0  # confermato: nessuna seconda consegna
    await bus2.stop()


async def test_bounded_queue_applies_backpressure(tmp_path):
    bus = EventBus(Database(tmp_path / "q.db"), max_queue=2)
    release = asyncio.Event()

    async def slow(e):
        await release.wait()

    bus.subscribe("slow", {EventType.PRICE_TICK}, slow)
    bus.start()
    for i in range(3):
        await bus.publish(ev(i))
    blocked = asyncio.create_task(bus.publish(ev(99)))
    await asyncio.sleep(0.05)
    assert not blocked.done()  # il produttore attende: nessuna crescita illimitata
    release.set()
    await asyncio.wait_for(blocked, 1)
    await bus.wait_idle()
    await bus.stop()


async def test_envelope_is_immutable():
    e = ev(1)
    with pytest.raises(Exception):
        e.wallet = "x"  # type: ignore[misc]
    assert e.idempotency_key and e.provenance.origin == DataOrigin.SIMULATED


async def test_gap_fill_after_outage(tmp_path):
    m = mint("GAP")
    hist = [swap(LEADER, m, "BUY", S - timedelta(hours=2), 500, 0.01, 0, 10**10)]
    rt, world, sup = await engine(tmp_path, scenario([token("GAP", 0.01)], swaps=hist))
    follow(rt)
    gap = GapFiller(rt)
    assert await gap.fill(LEADER) == 0  # primo avvio: solo checkpoint, lo storico non si copia
    # "outage": arrivano 3 swap senza notifiche WS
    for i in range(3):
        world.swaps.append(world._swap(swap(LEADER, m, "SELL" if i else "BUY", S + timedelta(minutes=i + 1), 100,
                                            0.01, 10**10, 10**10 - 10**8 * (i + 1))))
    rt.clock.set(S + timedelta(minutes=5))
    assert await gap.fill(LEADER) == 3
    assert await gap.fill(LEADER) == 0  # checkpoint aggiornato
    await rt.bus.wait_idle()


def test_lag_breaker_trips_and_recovers():
    b = LagBreaker("feed", ReasonCode.FEED_LAG_BREAKER, 10, trip_count=3, recover_count=2)
    assert not b.observe(30) and not b.observe(30)
    assert b.observe(30) and b.is_open
    assert not b.observe(1) and b.is_open
    assert b.observe(1) and not b.is_open


async def test_feed_lag_breaker_blocks_buys(tmp_path):
    rt, world, sup = await engine(tmp_path, scenario([token("LAG", 0.01)]))
    follow(rt)
    for i in range(3):
        await feed(rt, world, swap(LEADER, mint("LAG"), "BUY", S + timedelta(minutes=i), 100, 0.01, 0, 10**9),
                   latency_s=90)
    assert rt.feed_breaker.is_open and ReasonCode.FEED_LAG_BREAKER in rt.provider_blockers()


async def test_contract_violation_detected_and_safe_mode(tmp_path):
    rt, world, sup = await engine(tmp_path, scenario([token("C", 0.01)]))
    assert check_contracts(rt.db, "switch2", Decimal(1000), 3, rt.ledger.cash("switch2")) == []
    with rt.db.session() as s:  # manomissione: denaro comparso dal nulla
        s.add(LedgerEntry(strategy="switch2", ts=S, kind="BUG", amount_usd=Decimal(5)))
    problems = await sup.audit.cycle()
    assert problems and "identità contabile" in problems[0]
    assert rt.system_state() == SystemState.SAFE
    # SAFE non può essere tolto da un agente, solo da un umano
    rt.set_state(SystemState.RUNNING, "supervisor", "auto")
    assert rt.system_state() == SystemState.SAFE
    rt.set_state(SystemState.RUNNING, "human", "RIPRENDI")
    assert rt.system_state() == SystemState.RUNNING


def test_no_signing_code_anywhere():
    src = Path(__file__).parents[1] / "src" / "m4desk"
    text = "\n".join(p.read_text() for p in src.rglob("*.py"))
    for forbidden in ("Keypair", "solders", "sign_transaction", "signTransaction", "v2/execute", "private_key ="):
        assert forbidden not in text, forbidden
    from m4desk.live import LiveDisabledError, execute, preflight

    with pytest.raises(LiveDisabledError):
        execute()
    assert all(not x.ok for x in preflight())


def test_live_mode_rejected_by_config():
    from pydantic import ValidationError

    from m4desk.config import AppConfig

    with pytest.raises(ValidationError):
        AppConfig(live_enabled=True)
    with pytest.raises(ValidationError):
        AppConfig(mode="live")
    with pytest.raises(ValidationError):
        AppConfig(dashboard={"host": "0.0.0.0"})


def test_strategy_version_changes_with_thresholds():
    from m4desk.config import AppConfig, RiskConfig

    a = AppConfig().strategy_version()
    b = AppConfig(risk=RiskConfig(min_liquidity_usd=Decimal(60000))).strategy_version()
    assert a != b and a == AppConfig().strategy_version()


async def test_secrets_never_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "SUPERSEGRETO123")
    from m4desk.config import load_secrets

    s = load_secrets()
    assert "SUPERSEGRETO123" not in repr(s) and "SUPERSEGRETO123" not in str(s.model_dump())


async def test_audit_records_every_decision(tmp_path):
    rt, world, sup = await engine(tmp_path, scenario([token("AU", 0.01)]))
    follow(rt)
    await feed(rt, world, swap(LEADER, mint("AU"), "BUY", S, 2000, 0.01, 0, 10**11))
    from m4desk.storage.db import DecisionRow, EventRow

    with rt.db.session() as s:
        d = s.scalars(select(DecisionRow)).one()
        types = {e.type for e in s.scalars(select(EventRow))}
    assert d.facts["provenance"]["origin"] == "SIMULATO / DATI DI ESEMPIO" and d.facts["signature"]
    assert {"LEADER_SWAP", "TOKEN_ASSESSED", "DECISION", "ORDER_UPDATE"} <= types


async def test_events_during_stop_are_deferred_not_lost(tmp_path):
    rt, world, sup = await engine(tmp_path, scenario([token("ST", 0.01)]))
    follow(rt)
    rt.db.set_control("state", SystemState.STOPPED.value, "human", "test")
    await feed(rt, world, swap(LEADER, mint("ST"), "BUY", S, 2000, 0.01, 0, 10**11))
    from m4desk.storage.db import ConsumerCheckpoint, EventRow

    with rt.db.session() as s:
        ev = s.scalars(select(EventRow).where(EventRow.type == "LEADER_SWAP")).one()
        done = s.get(ConsumerCheckpoint, ("position_manager", ev.event_id))
    assert done is None  # non confermato => riconsegnato al riavvio
    await rt.bus.stop()
    rt.db.set_control("state", SystemState.RUNNING.value, "human", "test")
    rt.bus.start()
    assert await rt.bus.redeliver_pending() >= 1
    await rt.bus.wait_idle()
    from m4desk.storage.db import DecisionRow

    with rt.db.session() as s:
        assert s.scalars(select(DecisionRow)).first() is not None
