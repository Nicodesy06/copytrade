"""DEX Screener API gratuita: `GET /tokens/v1/solana/{mint}`. Schema verificato, cache TTL.

La risposta non contiene l'orario del prezzo: si usa l'orario del fetch (dichiarato in provenienza).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, ValidationError

from ..types import DataOrigin, ReasonCode
from .base import MarketSnapshot, ProviderError
from .http import HttpJson


class _Tok(BaseModel):
    model_config = ConfigDict(extra="ignore")
    address: str
    symbol: str | None = None


class _Liq(BaseModel):
    model_config = ConfigDict(extra="ignore")
    usd: float | None = None


class _Pair(BaseModel):
    model_config = ConfigDict(extra="ignore")
    chainId: str
    dexId: str | None = None
    pairAddress: str
    baseToken: _Tok
    quoteToken: _Tok
    priceNative: str | None = None
    priceUsd: str | None = None
    liquidity: _Liq | None = None
    pairCreatedAt: int | None = None


def _dec(x: str | float | None) -> Decimal | None:
    if x is None:
        return None
    try:
        d = Decimal(str(x))
    except InvalidOperation:
        return None
    return d if d.is_finite() and d > 0 else None


def pick_market(raw: object, mint: str, now: datetime, origin: DataOrigin) -> MarketSnapshot:
    if not isinstance(raw, list):
        raise ProviderError("dexscreener", ReasonCode.PROVIDER_ERROR, "schema inatteso")
    pairs: list[_Pair] = []
    for item in raw:
        try:
            p = _Pair.model_validate(item)
        except ValidationError:
            continue
        if p.chainId == "solana" and mint in (p.baseToken.address, p.quoteToken.address):
            pairs.append(p)
    if not pairs:
        raise ProviderError("dexscreener", ReasonCode.LIQUIDITY_UNAVAILABLE, "nessuna coppia")
    best = max(pairs, key=lambda p: (p.liquidity.usd or 0) if p.liquidity else 0)
    price = None
    base_usd = _dec(best.priceUsd)
    if best.baseToken.address == mint:
        price = base_usd
    else:
        native = _dec(best.priceNative)  # prezzo base espresso in token quote
        price = base_usd / native if base_usd and native else None
    liq = _dec(best.liquidity.usd) if best.liquidity else None
    return MarketSnapshot(
        mint=mint,
        price_usd=price,
        liquidity_usd=liq,
        pair_created_at=datetime.fromtimestamp(best.pairCreatedAt / 1000, UTC) if best.pairCreatedAt else None,
        price_observed_at=now if price is not None else None,
        fetched_at=now,
        provider="dexscreener",
        origin=origin,
        pair_address=best.pairAddress,
        dex_id=best.dexId,
        symbol=best.baseToken.symbol if best.baseToken.address == mint else best.quoteToken.symbol,
    )


class DexScreener:
    name = "dexscreener"

    def __init__(self, http: HttpJson, base_url: str, ttl_s: float = 20, origin=DataOrigin.MAINNET_READONLY):
        self.http = http
        self.base = base_url.rstrip("/")
        self.ttl_s = ttl_s
        self.origin = origin

    async def get_market(self, mint: str) -> MarketSnapshot:
        raw = await self.http.get(f"{self.base}/tokens/v1/solana/{mint}", cache_key=f"tok:{mint}", ttl_s=self.ttl_s)
        return pick_market(raw, mint, datetime.now(UTC), self.origin)
