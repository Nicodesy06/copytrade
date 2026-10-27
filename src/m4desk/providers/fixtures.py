"""Provider fixture/mock: dati deterministici da file JSON, nessuna rete, nessuna chiave.

Tutti i dati sono etichettati SIMULATO / DATI DI ESEMPIO e ogni interrogazione rispetta
l'orologio simulato: nessun dato futuro può trapelare nelle decisioni passate.
"""

from __future__ import annotations

import bisect
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..clock import Clock
from ..types import SOL_MINT, DataOrigin, ReasonCode, Side, SwapObservation
from .base import MarketSnapshot, MintInfo, ProviderError, Quote, RiskItem, RugReport

ORIGIN = DataOrigin.SIMULATED


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class _Series:
    """Serie a gradini: lista di punti [iso, valore] oppure {start, step_s, values, points}."""

    def __init__(self, spec: list[list[Any]] | dict[str, Any]):
        if isinstance(spec, dict):
            from datetime import timedelta

            t0, step = _dt(spec["start"]), int(spec["step_s"])
            raw = [(t0 + timedelta(seconds=i * step), v) for i, v in enumerate(spec["values"])]
            raw += [(_dt(t), v) for t, v in spec.get("points", [])]
        else:
            raw = [(_dt(t), v) for t, v in spec]
        pts = sorted((t, Decimal(str(v))) for t, v in raw)
        self.times = [p[0] for p in pts]
        self.values = [p[1] for p in pts]

    def at(self, t: datetime) -> tuple[datetime, Decimal] | None:
        i = bisect.bisect_right(self.times, t) - 1
        if i < 0:
            return None
        return self.times[i], self.values[i]


class FixtureWorld:
    def __init__(self, data: dict[str, Any], clock: Clock, origin: DataOrigin = ORIGIN):
        self.data = data
        self.clock = clock
        self.origin = origin
        self.meta = data["meta"]
        self.tokens: dict[str, dict[str, Any]] = data["tokens"]
        self.prices = {m: _Series(t.get("prices", [])) for m, t in self.tokens.items()}
        self.liquidity = {m: _Series(t.get("liquidity", [])) for m, t in self.tokens.items()}
        self.swaps = sorted(
            (self._swap(s) for s in data.get("swaps", [])), key=lambda s: (s.event_at, s.slot, s.signature)
        )
        self.retractions = [(_dt(r["event_at"]), r["signature"]) for r in data.get("retractions", [])]
        self.retracted: set[str] = set()

    @classmethod
    def load(cls, path: str | Path, clock: Clock, origin: DataOrigin = ORIGIN) -> FixtureWorld:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), clock, origin)

    @staticmethod
    def _swap(s: dict[str, Any]) -> SwapObservation:
        return SwapObservation(
            signature=s["signature"],
            slot=s["slot"],
            wallet=s["wallet"],
            mint=s["mint"],
            side=Side(s["side"]),
            token_amount_raw=int(s["token_amount_raw"]),
            decimals=s["decimals"],
            quote_mint=s["quote_mint"],
            quote_amount_raw=int(s["quote_amount_raw"]),
            quote_decimals=s["quote_decimals"],
            wallet_pre_amount_raw=None if s.get("wallet_pre_amount_raw") is None else int(s["wallet_pre_amount_raw"]),
            wallet_post_amount_raw=None
            if s.get("wallet_post_amount_raw") is None
            else int(s["wallet_post_amount_raw"]),
            fee_lamports=s.get("fee_lamports", 5000),
            event_at=_dt(s["event_at"]),
            commitment=s.get("commitment", "confirmed"),
            program_labels=tuple(s.get("program_labels", ["SIMULATO"])),
        )

    @property
    def stream_start(self) -> datetime:
        return _dt(self.meta["stream_start"])

    @property
    def end(self) -> datetime:
        return _dt(self.meta["end"])

    def visible_swaps(self) -> list[SwapObservation]:
        now = self.clock.now()
        return [s for s in self.swaps if s.event_at <= now and s.signature not in self.retracted]


class FixtureMarket:
    name = "fixture-market"

    def __init__(self, world: FixtureWorld):
        self.w = world

    async def get_market(self, mint: str) -> MarketSnapshot:
        now = self.w.clock.now()
        tok = self.w.tokens.get(mint)
        if tok is None:
            raise ProviderError(self.name, ReasonCode.PRICE_UNAVAILABLE, "token sconosciuto")
        p = self.w.prices[mint].at(now)
        liq = self.w.liquidity[mint].at(now)
        created = tok.get("pair_created_at")
        return MarketSnapshot(
            mint=mint,
            price_usd=p[1] if p else None,
            liquidity_usd=liq[1] if liq else None,
            pair_created_at=_dt(created) if created else None,
            price_observed_at=p[0] if p else None,
            fetched_at=now,
            provider=self.name,
            origin=self.w.origin,
            symbol=tok.get("symbol"),
            pair_address=f"PAIR-{tok.get('symbol')}",
            dex_id="simulato",
        )


class FixtureQuotes:
    """Quotazione simulata dal prezzo corrente e da un modello d'impatto a prodotto costante."""

    name = "fixture-quotes"

    def __init__(self, world: FixtureWorld, fee_bps: int = 30):
        self.w = world
        self.fee_bps = fee_bps

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int) -> Quote:
        now = self.w.clock.now()
        token = output_mint if input_mint == SOL_MINT else input_mint
        tok = self.w.tokens.get(token)
        if tok is None or not tok.get("quote_available", True):
            raise ProviderError(self.name, ReasonCode.QUOTE_UNAVAILABLE)
        p = self.w.prices[token].at(now)
        sol = self.w.prices[SOL_MINT].at(now)
        liq = self.w.liquidity[token].at(now)
        if not p or not sol or not liq or liq[1] <= 0:
            raise ProviderError(self.name, ReasonCode.QUOTE_UNAVAILABLE)
        price, sol_px, liq_usd = p[1], sol[1], liq[1]
        dec = int(tok["decimals"])
        if input_mint == SOL_MINT:
            in_usd = Decimal(amount_raw) / Decimal(10**9) * sol_px
        else:
            in_usd = Decimal(amount_raw) / Decimal(10**dec) * price
        half = liq_usd / 2
        # prodotto costante: out = y * dx / (x + dx) => impatto = dx / (x + dx)
        impact = in_usd / (half + in_usd)
        out_usd = in_usd * (1 - impact)
        if input_mint == SOL_MINT:
            out_raw = int(out_usd / price * Decimal(10**dec))
        else:
            out_raw = int(out_usd / sol_px * Decimal(10**9))
        return Quote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount_raw=amount_raw,
            out_amount_raw=out_raw,
            price_impact_pct=(impact * 100).quantize(Decimal("0.0001")),
            fee_bps=self.fee_bps,
            quoted_at=now,
            provider=self.name,
            origin=self.w.origin,
            route_labels=("SIMULATO",),
        )


class FixtureRugcheck:
    name = "fixture-rugcheck"

    def __init__(self, world: FixtureWorld):
        self.w = world

    async def get_report(self, mint: str) -> RugReport:
        tok = self.w.tokens.get(mint)
        if tok is None or tok.get("rugcheck") is None:
            raise ProviderError(self.name, ReasonCode.RUGCHECK_MISSING)
        r = tok["rugcheck"]
        lp = r.get("lp_locked_pct")
        top = r.get("top10_pct")
        return RugReport(
            mint=mint,
            fetched_at=self.w.clock.now(),
            provider=self.name,
            origin=self.w.origin,
            score_normalised=r.get("score_normalised"),
            risks=tuple(RiskItem(name=x["name"], level=x["level"]) for x in r.get("risks", [])),
            rugged=r.get("rugged"),
            mint_authority=tok.get("mint_authority"),
            freeze_authority=tok.get("freeze_authority"),
            authorities_known=True,
            lp_locked_pct=None if lp is None else Decimal(str(lp)),
            lp_source="summary" if lp is not None else None,
            top10_pct=None if top is None else Decimal(str(top)),
            token_program=tok.get("program"),
            extensions=tuple(tok.get("extensions", [])),
            name=tok.get("name"),
            symbol=tok.get("symbol"),
            description=tok.get("description"),
        )


class FixtureMintInfo:
    name = "fixture-chain"

    def __init__(self, world: FixtureWorld):
        self.w = world

    async def get_mint_info(self, mint: str) -> MintInfo:
        tok = self.w.tokens.get(mint)
        if tok is None:
            raise ProviderError(self.name, ReasonCode.AUTHORITY_UNKNOWN)
        prog = tok.get("program", "spl-token")
        return MintInfo(
            mint=mint,
            program=prog,
            decimals=int(tok["decimals"]),
            mint_authority=tok.get("mint_authority"),
            freeze_authority=tok.get("freeze_authority"),
            extensions=tuple(tok.get("extensions", [])),
            fetched_at=self.w.clock.now(),
            provider=self.name,
            origin=self.w.origin,
        )


class FixtureChain:
    name = "fixture-chain"

    def __init__(self, world: FixtureWorld):
        self.w = world

    async def wallet_swaps(
        self, wallet: str, limit: int, until_signature: str | None = None
    ) -> list[SwapObservation]:
        out: list[SwapObservation] = []
        for s in reversed(self.w.visible_swaps()):
            if s.wallet != wallet:
                continue
            if until_signature is not None and s.signature == until_signature:
                break
            out.append(s)
            if len(out) >= limit:
                break
        return out

    async def token_swaps(self, mint: str, limit: int) -> list[SwapObservation]:
        out = [s for s in reversed(self.w.visible_swaps()) if s.mint == mint]
        return out[:limit]

    async def head_signature(self, wallet: str) -> str | None:
        mine = [s for s in self.w.visible_swaps() if s.wallet == wallet]
        return mine[-1].signature if mine else None

    async def is_confirmed(self, signature: str) -> bool | None:
        return signature not in self.w.retracted
