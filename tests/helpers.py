"""Costruttori di scenari minimi (SIMULATO) per i test."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from m4desk.config import AppConfig, RiskConfig, StrategyConfig
from m4desk.engine import build_fixture_engine
from m4desk.services.ingest import swap_event
from m4desk.storage.db import LeaderSlot
from m4desk.types import SOL_MINT, LeaderStatus

S = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
LEADER = ("SimLeader" + "1" * 40)[:44]
LEADER2 = ("SimLeaderTwo" + "1" * 40)[:44]
_n = [0]


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def mint(sym: str) -> str:
    return ("SimTkn" + sym + "1" * 40)[:44]


def token(sym: str, price: float, liq: float = 500_000, prices: Any = None, **kw: Any) -> tuple[str, dict]:
    d = {
        "symbol": sym, "name": f"{sym} test", "decimals": 6, "program": "spl-token", "extensions": [],
        "mint_authority": None, "freeze_authority": None, "pair_created_at": iso(S - timedelta(days=30)),
        "description": "token di test", "quote_available": True,
        "rugcheck": {"score_normalised": 5, "risks": [], "rugged": False, "lp_locked_pct": 90, "top10_pct": 20},
        "liquidity": [[iso(S - timedelta(days=30)), liq]],
        "prices": prices if prices is not None else {"start": iso(S - timedelta(hours=1)), "step_s": 60,
                                                     "values": [price] * 600},
    }
    d.update(kw)
    return mint(sym), d


def sol() -> tuple[str, dict]:
    return SOL_MINT, {"symbol": "SOL", "name": "SOL", "decimals": 9, "program": "spl-token", "extensions": [],
                      "mint_authority": None, "freeze_authority": None, "pair_created_at": "2021-01-01T00:00:00Z",
                      "rugcheck": None, "liquidity": [["2020-01-01T00:00:00Z", 50_000_000]],
                      "prices": {"start": iso(S - timedelta(hours=1)), "step_s": 60, "values": [150] * 600}}


def swap(wallet: str, m: str, side: str, t: datetime, usd: float, price: float, pre: int | None, post: int | None,
         sig: str | None = None) -> dict:
    _n[0] += 1
    tok = abs((post or 0) - (pre or 0)) if pre is not None and post is not None else round(usd / price * 1e6)
    return {
        "signature": sig or f"SimTestSig{_n[0]:06d}" + "1" * 20, "slot": 1_000_000 + _n[0], "wallet": wallet,
        "mint": m, "side": side, "token_amount_raw": tok, "decimals": 6, "quote_mint": SOL_MINT,
        "quote_amount_raw": round(tok / 1e6 * price / 150 * 1e9), "quote_decimals": 9,
        "wallet_pre_amount_raw": pre, "wallet_post_amount_raw": post, "fee_lamports": 5000, "event_at": iso(t),
    }


def scenario(tokens: list[tuple[str, dict]], swaps: list[dict] | None = None, retractions: list | None = None,
             seeds: list[str] | None = None) -> dict:
    toks = dict([sol(), *tokens])
    return {"meta": {"label": "SIMULATO / DATI DI ESEMPIO", "name": "test", "stream_start": iso(S),
                     "end": iso(S + timedelta(hours=6))},
            "seed_wallets": seeds or [], "wallet_labels": {}, "tokens": toks, "swaps": swaps or [],
            "retractions": retractions or []}


def make_cfg(tmp_path: Path, strategies=("switch2",), **risk: Any) -> AppConfig:
    return AppConfig(data_dir=str(tmp_path / "data"), risk=RiskConfig(**risk),
                     strategy=StrategyConfig(primary=strategies[0], enabled=list(strategies)))


async def engine(tmp_path: Path, data: dict, cfg: AppConfig | None = None, strategies=("switch2",), **risk: Any):
    cfg = cfg or make_cfg(tmp_path, strategies, **risk)
    f = tmp_path / "scenario.json"
    f.write_text(json.dumps(data))
    rt, world, sup = build_fixture_engine(cfg, f, tmp_path / "test.db")
    await sup.start()
    return rt, world, sup


def follow(rt, wallet: str = LEADER, strategy: str | None = None, status: LeaderStatus = LeaderStatus.SELECTED):
    with rt.db.session() as s:
        for sid in [strategy] if strategy else list(rt.strategies):
            s.merge(LeaderSlot(strategy=sid, wallet=wallet, status=status.value, rank=1, consecutive_losses=0,
                               selected_at=rt.clock.now(), updated_at=rt.clock.now()))
    rt.db.add_wallet(wallet, "seed")


async def feed(rt, world, sw: dict, latency_s: int = 2) -> bool:
    """Pubblica uno swap come farebbe l'ingestione e attende che tutti gli agenti abbiano finito."""
    obs = world._swap(sw)
    world.swaps.append(obs)
    rt.clock.set(obs.event_at + timedelta(seconds=latency_s))
    ok = await rt.bus.publish(swap_event(rt, obs, rt.clock.now(), "fixture-chain"))
    await rt.bus.wait_idle()
    return ok


def D(x: Any) -> Decimal:
    return Decimal(str(x))
