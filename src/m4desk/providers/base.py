"""Interfacce provider e modelli dati normalizzati. Ogni dato porta provider, origine e orario."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from ..types import DataOrigin, ReasonCode, SwapObservation


class ProviderError(Exception):
    def __init__(self, provider: str, reason: ReasonCode, message: str = ""):
        super().__init__(f"{provider}: {reason.value} {message}".strip())
        self.provider = provider
        self.reason = reason


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class MarketSnapshot(_Frozen):
    mint: str
    price_usd: Decimal | None
    liquidity_usd: Decimal | None
    pair_created_at: datetime | None
    price_observed_at: datetime | None  # quando il prezzo è stato osservato
    fetched_at: datetime
    provider: str
    origin: DataOrigin
    pair_address: str | None = None
    dex_id: str | None = None
    symbol: str | None = None


class Quote(_Frozen):
    input_mint: str
    output_mint: str
    in_amount_raw: int
    out_amount_raw: int
    price_impact_pct: Decimal | None
    fee_bps: int | None
    quoted_at: datetime
    provider: str
    origin: DataOrigin
    route_labels: tuple[str, ...] = ()


class RiskItem(_Frozen):
    name: str
    level: str  # danger | warn | info | ...
    description: str = ""


class RugReport(_Frozen):
    mint: str
    fetched_at: datetime
    provider: str
    origin: DataOrigin
    score_normalised: int | None
    risks: tuple[RiskItem, ...]
    rugged: bool | None
    mint_authority: str | None
    freeze_authority: str | None
    authorities_known: bool
    lp_locked_pct: Decimal | None
    lp_source: str | None  # "summary" | "markets" | None
    top10_pct: Decimal | None
    token_program: str | None
    extensions: tuple[str, ...] | None
    name: str | None = None
    symbol: str | None = None
    description: str | None = None  # testo NON fidato, mai interpretato come istruzione
    detected_at: datetime | None = None


class MintInfo(_Frozen):
    mint: str
    program: str
    decimals: int
    mint_authority: str | None
    freeze_authority: str | None
    extensions: tuple[str, ...]
    fetched_at: datetime
    provider: str
    origin: DataOrigin


class MarketDataProvider(Protocol):
    name: str

    async def get_market(self, mint: str) -> MarketSnapshot: ...


class QuoteProvider(Protocol):
    name: str

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int) -> Quote: ...


class TokenRiskProvider(Protocol):
    name: str

    async def get_report(self, mint: str) -> RugReport: ...


class MintInfoProvider(Protocol):
    name: str

    async def get_mint_info(self, mint: str) -> MintInfo: ...


class ChainProvider(Protocol):
    name: str

    async def wallet_swaps(
        self, wallet: str, limit: int, until_signature: str | None = None
    ) -> list[SwapObservation]: ...

    async def token_swaps(self, mint: str, limit: int) -> list[SwapObservation]: ...

    async def is_confirmed(self, signature: str) -> bool | None: ...

    async def head_signature(self, wallet: str) -> str | None: ...
