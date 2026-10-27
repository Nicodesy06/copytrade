"""Costruzione del motore: DEMO/REPLAY (fixture, offline) e PAPER (mainnet read-only, provider gratuiti)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from .agents.supervisor import Supervisor
from .clock import Clock, SimClock
from .config import AppConfig, Secrets
from .providers.budget import ProviderGate
from .providers.fixtures import (
    FixtureChain,
    FixtureMarket,
    FixtureMintInfo,
    FixtureQuotes,
    FixtureRugcheck,
    FixtureWorld,
)
from .runtime import Providers, Runtime, build_runtime
from .services.events import make_event
from .services.ingest import GapFiller, LogsSubscriber, swap_event
from .services.llm import LLMService, MockLLMBackend, OllamaBackend
from .storage.db import Database, Position
from .types import DataOrigin, EventType, Mode, SystemState

log = logging.getLogger(__name__)

DEFAULT_FIXTURE = Path(__file__).parent / "data" / "demo_scenario.json"


# --------------------------------------------------------------------------- DEMO / REPLAY
def build_fixture_engine(cfg: AppConfig, fixture: str | Path, db_path: str | Path, mode: Mode = Mode.DEMO,
                         fresh: bool = True) -> tuple[Runtime, FixtureWorld, Supervisor]:
    p = Path(db_path)
    if fresh:
        for suffix in ("", "-wal", "-shm"):
            Path(str(p) + suffix).unlink(missing_ok=True)
    db = Database(p)
    import json

    data = json.loads(Path(fixture).read_text(encoding="utf-8"))
    clock = SimClock(FixtureWorld(data, Clock()).stream_start)
    origin = DataOrigin.SIMULATED if mode == Mode.DEMO else DataOrigin.REPLAY
    if data["meta"].get("label") == DataOrigin.SIMULATED.value:
        origin = DataOrigin.SIMULATED  # dati sintetici restano sempre etichettati come tali
    world = FixtureWorld(data, clock, origin)
    providers = Providers(
        market=FixtureMarket(world), quotes=FixtureQuotes(world, cfg.simulation.dex_fee_bps),
        rug=FixtureRugcheck(world), mint=FixtureMintInfo(world), chain=FixtureChain(world),
    )
    llm = LLMService(cfg.llm, MockLLMBackend())  # in demo nessuna chiamata a Ollama: backend MOCK etichettato
    rt = build_runtime(cfg, mode, origin, db, clock, providers, llm)
    db.set_meta("fixture", {"path": str(fixture), "name": data["meta"].get("name"), "label": data["meta"]["label"]})
    db.set_meta("llm_backend", "mock-demo (MOCK, nessun modello reale)")
    sup = Supervisor(rt, list(data.get("seed_wallets", [])), data.get("wallet_labels", {}))
    return rt, world, sup


async def run_replay(rt: Runtime, world: FixtureWorld, sup: Supervisor, pace_s: float = 0.0,
                     detection_latency_s: int = 2, tick_minutes: int = 10) -> dict[str, Any]:
    assert isinstance(rt.clock, SimClock)
    clock = rt.clock
    await sup.start()
    await sup.discovery.cycle()
    await rt.bus.wait_idle()
    await sup.strategy.selection_round()
    await rt.bus.wait_idle()
    sup.heartbeat_all()

    start, end = world.stream_start, world.end
    timeline: list[tuple[Any, int, str, Any]] = []
    for s in world.swaps:
        if s.event_at > start:
            timeline.append((s.event_at, 1, "swap", s))
    for t, sig in world.retractions:
        timeline.append((t, 2, "retract", sig))
    t = start
    while t <= end:
        timeline.append((t, 3, "tick", None))
        if t.minute == 0 and t.hour % 6 == 0 and t > start:
            timeline.append((t, 4, "discover", None))
        if t.minute == 0 and t.hour % 1 == 0:
            timeline.append((t, 5, "audit", None))
        if t.hour == 0 and t.minute == 0 and t > start:
            timeline.append((t, 6, "day_end", (t - timedelta(days=1)).strftime("%Y-%m-%d")))
        t += timedelta(minutes=tick_minutes)
    timeline.sort(key=lambda x: (x[0], x[1]))
    swaps_by_sig = {s.signature: s for s in world.swaps}
    counts = {"swaps": 0, "ticks": 0}
    for when, _, kind, obj in timeline:
        if rt.system_state() == SystemState.STOPPED:
            break
        clock.set(when)
        if kind == "swap":
            watched = {w.address for w in rt.db.list_wallets()}
            if obj.wallet not in watched or obj.signature in world.retracted:
                continue
            clock.set(when + timedelta(seconds=detection_latency_s))
            await rt.bus.publish(swap_event(rt, obj, clock.now(), rt.providers.chain.name))
            counts["swaps"] += 1
        elif kind == "retract":
            world.retracted.add(obj)
            s = swaps_by_sig.get(obj)
            await rt.bus.publish(make_event(EventType.TX_RETRACTED, "ingest", (obj,), rt.origin,
                                            rt.providers.chain.name, when, wallet=s.wallet if s else None,
                                            payload={"signature": obj, "wallet": s.wallet if s else None}))
        elif kind == "tick":
            for mint in sorted({p.mint for p in rt.ledger.open_positions()}):
                await rt.bus.publish(make_event(EventType.PRICE_TICK, "pricer", (mint, when.isoformat()), rt.origin,
                                                rt.providers.market.name, when, token=mint))
                counts["ticks"] += 1
        elif kind == "discover":
            await sup.discovery.cycle()
            await rt.bus.wait_idle()
            await sup.strategy.selection_round()
        elif kind == "audit":
            await sup.audit.cycle()
        elif kind == "day_end":
            await rt.bus.publish(make_event(EventType.DAY_END, "supervisor", (obj,), rt.origin, "local", when,
                                            payload={"day": obj}))
        await rt.bus.wait_idle()
        if pace_s and kind in ("swap", "day_end", "discover"):
            sup.heartbeat_all()
            await asyncio.sleep(pace_s)
    clock.set(end)
    await sup.audit.daily_report(end.strftime("%Y-%m-%d"))
    await rt.bus.wait_idle()
    sup.heartbeat_all("IDLE")
    await rt.bus.stop()
    rt.db.set_meta("replay_completed_at", clock.now().isoformat())
    return counts


# --------------------------------------------------------------------------- PAPER (mainnet read-only)
def build_paper_engine(cfg: AppConfig, secrets: Secrets, db_path: str | Path) -> tuple[Runtime, Supervisor]:
    from .providers.dexscreener import DexScreener
    from .providers.http import HttpJson
    from .providers.jupiter import JupiterQuotes
    from .providers.rugcheck import RugcheckClient
    from .providers.solana_rpc import SolanaRpc

    db = Database(db_path)
    clock = Clock()
    pc = cfg.providers
    gates = {n: ProviderGate(n, b, db, clock) for n, b in pc.budget.items()}
    t = pc.http_timeout_s
    if pc.use_helius:
        key = secrets.helius_api_key.get_secret_value() if secrets.helius_api_key else None
        if not key:
            raise SystemExit("use_helius=true ma HELIUS_API_KEY non è impostata in .env")
        rpc = SolanaRpc(HttpJson("rpc", gates["rpc"], t), pc.helius_http_url, api_key=key)
    else:
        rpc = SolanaRpc(HttpJson("rpc", gates["rpc"], t), pc.rpc_http_url)
    jup_headers = {"x-api-key": secrets.jupiter_api_key.get_secret_value()} if secrets.jupiter_api_key else None
    rug = None
    if pc.rugcheck_enabled:
        rug = RugcheckClient(HttpJson("rugcheck", gates["rugcheck"], t), pc.rugcheck_url, cfg.risk.token_check_ttl_s)
    else:
        gates["rugcheck"].disable("disabilitato in configurazione: modalità SOLO OSSERVAZIONE")
    providers = Providers(
        market=DexScreener(HttpJson("dexscreener", gates["dexscreener"], t), pc.dexscreener_url),
        quotes=JupiterQuotes(HttpJson("jupiter", gates["jupiter"], t, jup_headers), pc.jupiter_url),
        rug=rug, mint=rpc, chain=rpc, gates=gates,
        mandatory=("rpc", "dexscreener", "jupiter", "rugcheck"),
    )
    backend = OllamaBackend(cfg.llm) if cfg.llm.enabled else None
    llm = LLMService(cfg.llm, backend)
    rt = build_runtime(cfg, Mode.PAPER, DataOrigin.MAINNET_READONLY, db, clock, providers, llm)
    db.set_meta("llm_backend", f"ollama {cfg.llm.model}" if backend else "DISABILITATO")
    db.set_meta("rpc", "helius (free)" if pc.use_helius else "RPC pubblico Solana (non production-grade)")
    from .live import build_live
    from .services.notify import Notifier

    topic = secrets.ntfy_topic.get_secret_value() if secrets.ntfy_topic else None
    rt.notifier = Notifier(cfg.notify, topic)
    rt.live = build_live(cfg, db, clock, secrets, rt.notifier, engine=True)
    db.set_meta("live", {"enabled": cfg.live_enabled, "wallet": cfg.live.wallet_pubkey})
    seeds = list(cfg.seed_wallets) + [w.address for w in db.list_wallets() if w.source == "manual"]
    return rt, Supervisor(rt, seeds)


async def run_paper(rt: Runtime, sup: Supervisor) -> None:
    cfg = rt.cfg
    await sup.start()
    gap = GapFiller(rt)
    if rt.live is not None:
        rt.live.disarm("riavvio del motore: serve un nuovo ARMA LIVE", "supervisor")
        await rt.live.recover()

    async def live_guard() -> None:
        """Vendite live in sospeso ritentate; disarmo se il sistema non è più RUNNING."""
        if rt.live is None:
            await asyncio.sleep(3600)
            return
        if rt.live.is_armed() and rt.system_state().value != "RUNNING":
            rt.live.disarm(f"stato sistema {rt.system_state().value}")
        await rt.live.retry_pending_exits()

    async def discovery() -> None:
        await sup.discovery.cycle()  # i nuovi candidati vengono valutati dal TraderAnalyst via evento
        await rt.bus.wait_idle()
        await sup.strategy.selection_round()
        # se qualche wallet non è ancora valutato (429/budget) si riprova presto: la cache delle
        # transazioni già scaricate fa avanzare ogni tentativo senza ripetere le richieste
        pending = sup.discovery.unscored([w.address for w in rt.db.list_wallets()])
        await asyncio.sleep(120 if pending else cfg.discovery.interval_s)

    async def rescore() -> None:
        await asyncio.sleep(cfg.trader.rescore_interval_s)
        await sup.rescore_candidates()
        await sup.strategy.selection_round()

    async def ingest() -> None:
        for w in sorted(sup.strategy.watched_wallets()):
            await gap.fill(w)

    async def prices() -> None:
        with rt.db.session() as s:
            mints = sorted({p.mint for p in s.query(Position).filter(Position.status == "OPEN")})
        now = rt.clock.now()
        for mint in mints:
            await rt.bus.publish(make_event(EventType.PRICE_TICK, "pricer", (mint, now.isoformat()), rt.origin,
                                            rt.providers.market.name, now, token=mint))

    async def audit() -> None:
        await sup.audit.cycle()
        day = rt.clock.now().strftime("%Y-%m-%d")
        last = rt.db.get_checkpoint("last_report_day")
        if last is not None and last != day:
            await rt.bus.publish(make_event(EventType.DAY_END, "supervisor", (last,), rt.origin, "local",
                                            rt.clock.now(), payload={"day": last}))
        rt.db.set_checkpoint("last_report_day", day)

    sup.supervise("discovery", discovery, 1)
    sup.supervise("rescore", rescore, 1)
    sup.supervise("ingest", ingest, cfg.providers.poll_interval_s)
    sup.supervise("prices", prices, cfg.providers.price_refresh_s)
    sup.supervise("audit", audit, 60)
    sup.supervise("live", live_guard, 60)

    ws_url = cfg.providers.rpc_ws_url
    if cfg.providers.use_helius and rt.providers.chain is not None:
        log.info("WebSocket Helius: la chiave non viene mai loggata")
        from .config import load_secrets

        key = load_secrets().helius_api_key
        ws_url = cfg.providers.helius_ws_url + (f"?api-key={key.get_secret_value()}" if key else "")
    subscriber = LogsSubscriber(ws_url, lambda w: asyncio.ensure_future(gap.fill(w)))
    sup._tasks.append(asyncio.create_task(subscriber.run(sup.strategy.watched_wallets), name="ws"))
    try:
        await sup.wait_until_stopped()
    finally:
        await sup.stop()
