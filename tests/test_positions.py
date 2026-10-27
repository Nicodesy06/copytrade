"""Integrazione PositionManager + broker + ledger su scenari minimi deterministici."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from helpers import LEADER, LEADER2, D, S, engine, feed, follow, iso, mint, scenario, swap, token
from m4desk.services.events import make_event
from m4desk.storage.db import AuditLog, DecisionRow, Order, Position
from m4desk.types import EventType, SystemState

GOOD = mint("GOOD")


def orders(rt, **flt):
    with rt.db.session() as s:
        stmt = select(Order)
        for k, v in flt.items():
            stmt = stmt.where(getattr(Order, k) == v)
        return list(s.scalars(stmt.order_by(Order.id)))


def positions(rt):
    with rt.db.session() as s:
        return list(s.scalars(select(Position).order_by(Position.id)))


def decisions(rt):
    with rt.db.session() as s:
        return list(s.scalars(select(DecisionRow).order_by(DecisionRow.id)))


async def _buy(tmp_path, price=0.01, **risk):
    rt, world, sup = await engine(tmp_path, scenario([token("GOOD", price)]), **risk)
    follow(rt)
    amt = round(2000 / price * 1e6)
    await feed(rt, world, swap(LEADER, GOOD, "BUY", S, 2000, price, 0, amt))
    return rt, world, sup, amt


async def test_copy_buy_never_inherits_leader_price_and_pays_all_fees(tmp_path):
    rt, world, sup, _ = await _buy(tmp_path)
    [o] = orders(rt, side="BUY")
    assert o.state == "FILLED"
    assert o.notional_usd == D("50.00")  # 5% di 1000
    assert o.fill_price_usd > D("0.01")  # impatto + slippage avverso, mai il prezzo del leader
    assert o.delay_ms >= 4000  # quotazione ritardata
    fees = {k: D(v) for k, v in o.fees.items()}
    assert all(v > 0 for v in fees.values())
    [p] = positions(rt)
    expected_cost = o.notional_usd + sum(fees.values())
    assert p.cost_basis_usd == expected_cost
    assert rt.ledger.cash("switch2") == D(1000) - expected_cost


async def test_partial_exit_mirrors_leader_fraction_after_fees(tmp_path):
    rt, world, sup, amt = await _buy(tmp_path)
    [p0] = positions(rt)
    rt.clock.set(S + timedelta(minutes=30))
    await feed(rt, world, swap(LEADER, GOOD, "SELL", S + timedelta(minutes=30), 0, 0.01, amt, amt // 2))
    [p] = positions(rt)
    assert p.status == "OPEN" and p.partial_exits == 1
    assert abs(p.qty_raw - p0.entry_qty_raw // 2) <= 1
    sell = orders(rt, side="SELL")[0]
    fees = sum(D(v) for k, v in sell.fees.items() if k != "rent_usd")
    proceeds = sell.notional_usd - fees  # rent non rimborsato in chiusura parziale
    half_cost = p0.cost_basis_usd * Decimal(sell.qty_raw) / Decimal(p0.qty_raw)
    assert p.realized_pnl_usd == proceeds - half_cost
    assert p.realized_pnl_usd < 0  # stesso prezzo: commissioni e slippage => perdita netta


async def test_full_exit_same_price_loses_exactly_costs(tmp_path):
    rt, world, sup, amt = await _buy(tmp_path)
    await feed(rt, world, swap(LEADER, GOOD, "SELL", S + timedelta(minutes=20), 0, 0.01, amt, 0))
    [p] = positions(rt)
    assert p.status == "CLOSED" and p.qty_raw == 0
    assert p.realized_pnl_usd == p.proceeds_usd - p.total_cost_usd
    assert p.realized_pnl_usd < 0
    # identità contabile: cassa = iniziale + realizzato (nessuna posizione aperta)
    assert rt.ledger.cash("switch2") == D(1000) + p.realized_pnl_usd


async def test_unknown_leader_holdings_triggers_full_exit(tmp_path):
    rt, world, sup, amt = await _buy(tmp_path)
    await feed(rt, world, swap(LEADER, GOOD, "SELL", S + timedelta(minutes=20), 500, 0.01, None, None))
    [p] = positions(rt)
    assert p.status == "CLOSED" and p.close_reason == "LEADER_HOLDINGS_UNKNOWN"


async def test_duplicate_signature_is_processed_once(tmp_path):
    rt, world, sup = await engine(tmp_path, scenario([token("GOOD", 0.01)]))
    follow(rt)
    sw = swap(LEADER, GOOD, "BUY", S, 2000, 0.01, 0, 200_000_000_000, sig="SimDupSig" + "1" * 30)
    assert await feed(rt, world, sw) is True
    assert await feed(rt, world, dict(sw)) is False
    assert len(orders(rt)) == 1 and len(decisions(rt)) == 1
    assert rt.bus.duplicates_by_type["LEADER_SWAP"] == 1


async def test_skipped_buy_when_wallet_not_followed(tmp_path):
    rt, world, sup = await engine(tmp_path, scenario([token("GOOD", 0.01)]))
    rt.db.add_wallet(LEADER, "seed")  # osservato ma non selezionato
    await feed(rt, world, swap(LEADER, GOOD, "BUY", S, 2000, 0.01, 0, 10**11))
    assert orders(rt) == [] and decisions(rt) == []


async def test_risk_rejection_records_skip_without_order(tmp_path):
    rt, world, sup = await engine(tmp_path, scenario([token("TINY", 0.01, liq=5_000)]))
    follow(rt)
    await feed(rt, world, swap(LEADER, mint("TINY"), "BUY", S, 2000, 0.01, 0, 10**11))
    [d] = decisions(rt)
    assert d.decision == "SKIP" and "LIQUIDITY_TOO_LOW" in d.reasons
    assert orders(rt) == []


async def test_stale_price_rejects_buy(tmp_path):
    stale = {"start": iso(S - timedelta(hours=1)), "step_s": 60, "values": [0.01] * 30}  # fermo da 30 min
    rt, world, sup = await engine(tmp_path, scenario([token("OLD", 0.01, prices=stale)]))
    follow(rt)
    await feed(rt, world, swap(LEADER, mint("OLD"), "BUY", S, 2000, 0.01, 0, 10**11))
    [d] = decisions(rt)
    assert d.decision == "SKIP" and "PRICE_STALE" in d.reasons


async def test_max_chase_pre_trade(tmp_path):
    rt, world, sup = await engine(tmp_path, scenario([token("JUMP", 0.013)]))
    follow(rt)
    # il leader ha comprato a 0.010 ma il mercato è già a 0.013 (+30%)
    await feed(rt, world, swap(LEADER, mint("JUMP"), "BUY", S, 2000, 0.010, 0, 200_000_000_000))
    [d] = decisions(rt)
    assert d.decision == "SKIP" and "MAX_CHASE_EXCEEDED" in d.reasons


async def test_max_chase_at_execution_after_delay(tmp_path):
    vals = [0.010] * 61 + [0.0125] * 539  # salto a S+60s, subito dopo la decisione
    prices = {"start": iso(S - timedelta(hours=1)), "step_s": 60, "values": vals}
    rt, world, sup = await engine(tmp_path, scenario([token("JUMP", 0.010, prices=prices)]))
    follow(rt)
    await feed(rt, world, swap(LEADER, mint("JUMP"), "BUY", S + timedelta(seconds=55), 2000, 0.010, 0, 2 * 10**11),
               latency_s=2)
    [o] = orders(rt)
    assert o.state == "REJECTED" and o.reason == "MAX_CHASE_EXCEEDED"
    assert positions(rt) == []
    assert rt.ledger.cash("switch2") == D(1000)  # nessun costo per ordine rifiutato


async def test_paused_blocks_new_buys_but_not_protective_sells(tmp_path):
    rt, world, sup, amt = await _buy(tmp_path)
    rt.db.set_control("state", SystemState.PAUSED.value, "human", "test")
    await feed(rt, world, swap(LEADER, mint("GOOD"), "SELL", S + timedelta(minutes=5), 0, 0.01, amt, 0))
    other = mint("GOOD")
    assert positions(rt)[0].status == "CLOSED"
    await feed(rt, world, swap(LEADER, other, "BUY", S + timedelta(minutes=30), 2000, 0.01, 0, amt))
    last = decisions(rt)[-1]
    assert last.decision == "SKIP" and "SYSTEM_PAUSED" in last.reasons


async def test_stop_loss_fills_at_delayed_observed_price_not_stop_level(tmp_path):
    vals = [0.010] * 70 + [0.0070] * 5 + [0.0060] * 525  # crollo con gap
    prices = {"start": iso(S - timedelta(hours=1)), "step_s": 60, "values": vals}
    rt, world, sup = await engine(tmp_path, scenario([token("DROP", 0.010, prices=prices)]))
    follow(rt)
    await feed(rt, world, swap(LEADER, mint("DROP"), "BUY", S, 2000, 0.010, 0, 2 * 10**11))
    t = S + timedelta(minutes=10, seconds=58)
    rt.clock.set(t)
    await rt.bus.publish(make_event(EventType.PRICE_TICK, "pricer", (mint("DROP"), "t1"), rt.origin, "x", t,
                                    token=mint("DROP")))
    await rt.bus.wait_idle()
    [p] = positions(rt)
    sell = orders(rt, side="SELL")[0]
    assert p.status == "CLOSED" and p.close_reason == "STOP_LOSS"
    stop_level = p.entry_price_usd * (1 - rt.cfg.risk.stop_loss_pct)
    assert sell.fill_price_usd < stop_level  # eseguito dopo il ritardo, al prezzo peggiore osservato


async def test_exit_not_reliable_enters_safe_mode(tmp_path):
    rt, world, sup, amt = await _buy(tmp_path)
    world.tokens[GOOD]["quote_available"] = False  # quotazione impossibile in uscita
    await feed(rt, world, swap(LEADER, GOOD, "SELL", S + timedelta(minutes=5), 0, 0.01, amt, 0))
    [p] = positions(rt)
    assert p.status == "OPEN"  # nessuna uscita inventata
    assert orders(rt, side="SELL")[0].reason == "EXIT_NOT_RELIABLE"
    assert rt.system_state() == SystemState.SAFE


async def test_reorg_after_copy_keeps_position_managed(tmp_path):
    rt, world, sup, amt = await _buy(tmp_path)
    sig = world.swaps[-1].signature
    await rt.bus.publish(make_event(EventType.TX_RETRACTED, "ingest", (sig,), rt.origin, "x", S + timedelta(minutes=1),
                                    payload={"signature": sig, "wallet": LEADER}))
    await rt.bus.wait_idle()
    with rt.db.session() as s:
        acts = [a.action for a in s.scalars(select(AuditLog))]
    assert "reorg.after_copy" in acts
    assert positions(rt)[0].status == "OPEN"


async def test_restart_mid_swap_cancels_pending_and_never_rebuys(tmp_path):
    rt, world, sup, amt = await _buy(tmp_path)
    # simula un crash fra fase 1 e fase 2: ordine PENDING senza effetti sul ledger
    o, new = rt.broker._create_pending(idempotency_key="crash-1", strategy="switch2", leader=LEADER, mint=GOOD,
                                       side="BUY", reason="OK", requested_usd=D(50))
    cash_before = rt.ledger.cash("switch2")
    await rt.bus.stop()
    rt2_failed = rt.broker.recover_pending()
    assert o.id in rt2_failed
    assert orders(rt, idempotency_key="crash-1")[0].state == "FAILED"
    assert orders(rt, idempotency_key="crash-1")[0].reason == "RECOVERED_INCOMPLETE_ORDER"
    assert rt.ledger.cash("switch2") == cash_before
    # riconsegna dello stesso evento: la decisione esiste già => nessun nuovo ordine
    n_orders = len(orders(rt))
    rt.bus.start()
    await rt.bus.publish(make_event(EventType.PRICE_TICK, "pricer", ("noop",), rt.origin, "x", rt.clock.now(),
                                    token=GOOD))
    await rt.bus.wait_idle()
    from m4desk.services.events import row_to_envelope
    from m4desk.storage.db import EventRow

    with rt.db.session() as s:
        ev = s.scalars(select(EventRow).where(EventRow.type == "LEADER_SWAP")).first()
    await sup.positions.handle(row_to_envelope(ev))
    assert len(orders(rt)) == n_orders


async def test_double_exposure_and_reservations_atomic(tmp_path):
    a, b = mint("AAA"), mint("BBB")
    rt, world, sup = await engine(tmp_path, scenario([token("AAA", 0.01), token("BBB", 0.01)]), max_positions=1)
    follow(rt)
    follow(rt, LEADER2)
    import asyncio

    s1 = world._swap(swap(LEADER, a, "BUY", S, 2000, 0.01, 0, 2 * 10**11))
    s2 = world._swap(swap(LEADER2, b, "BUY", S, 2000, 0.01, 0, 2 * 10**11))
    world.swaps += [s1, s2]
    rt.clock.set(S + timedelta(seconds=2))
    from m4desk.services.ingest import swap_event

    e1, e2 = (swap_event(rt, x, rt.clock.now(), "t") for x in (s1, s2))
    await asyncio.gather(sup.positions.on_leader_buy(e1, s1), sup.positions.on_leader_buy(e2, s2))
    filled = [o for o in orders(rt) if o.state == "FILLED"]
    assert len(filled) == 1  # la seconda è bloccata da MAX_POSITIONS grazie alla prenotazione atomica
    skips = [d for d in decisions(rt) if d.decision == "SKIP"]
    assert skips and "MAX_POSITIONS" in skips[0].reasons


async def test_leader_book_limits_per_leader_exposure(tmp_path):
    rt, world, sup = await engine(tmp_path, scenario([token("AAA", 0.01), token("BBB", 0.01)]))
    follow(rt)
    await feed(rt, world, swap(LEADER, mint("AAA"), "BUY", S, 2000, 0.01, 0, 2 * 10**11))
    await feed(rt, world, swap(LEADER, mint("BBB"), "BUY", S + timedelta(minutes=1), 2000, 0.01, 0, 2 * 10**11))
    last = decisions(rt)[-1]
    assert last.decision == "SKIP" and "LEADER_BOOK_FULL" in last.reasons
