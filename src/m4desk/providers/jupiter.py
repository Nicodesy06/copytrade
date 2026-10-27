"""Jupiter Swap API v2 (`GET https://api.jup.ag/swap/v2/order`) usata SOLO come quotazione.

Non si passa mai `taker`: senza taker la risposta non contiene alcuna transazione (verificato).
L'endpoint di esecuzione di Jupiter non viene mai chiamato; questo modulo non può firmare né inviare nulla.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from ..types import DataOrigin, ReasonCode
from .base import ProviderError, Quote
from .http import HttpJson


def parse_order(raw: object, input_mint: str, output_mint: str, amount_raw: int, origin: DataOrigin) -> Quote:
    if not isinstance(raw, dict) or "outAmount" not in raw:
        raise ProviderError("jupiter", ReasonCode.QUOTE_UNAVAILABLE, "schema inatteso")
    if raw.get("transaction"):
        # difesa in profondità: non dovrebbe mai accadere senza taker; la transazione viene scartata
        raw = {k: v for k, v in raw.items() if k != "transaction"}
    if raw.get("inputMint") not in (None, input_mint) or raw.get("outputMint") not in (None, output_mint):
        raise ProviderError("jupiter", ReasonCode.QUOTE_LOW_CONFIDENCE, "mint incoerenti")
    try:
        out_amt = int(raw["outAmount"])
        if raw.get("priceImpact") is not None:  # già in percentuale (verificato: = priceImpactPct × 100)
            impact = Decimal(str(raw["priceImpact"]))
        elif raw.get("priceImpactPct") is not None:
            impact = Decimal(str(raw["priceImpactPct"])) * 100
        else:
            impact = None
    except (ValueError, InvalidOperation) as e:
        raise ProviderError("jupiter", ReasonCode.QUOTE_UNAVAILABLE, "valori non numerici") from e
    labels = tuple(
        str(r.get("swapInfo", {}).get("label", "?")) for r in raw.get("routePlan", []) or [] if isinstance(r, dict)
    )
    return Quote(
        input_mint=input_mint,
        output_mint=output_mint,
        in_amount_raw=amount_raw,
        out_amount_raw=out_amt,
        price_impact_pct=max(impact, Decimal(0)) if impact is not None else None,
        fee_bps=int(raw.get("feeBps") or 0),
        quoted_at=datetime.now(UTC),
        provider="jupiter",
        origin=origin,
        route_labels=labels,
    )


class JupiterQuotes:
    name = "jupiter"

    def __init__(self, http: HttpJson, base_url: str, origin=DataOrigin.MAINNET_READONLY):
        self.http = http
        self.base = base_url.rstrip("/")
        self.origin = origin

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int) -> Quote:
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw)}
        raw = await self.http.get(f"{self.base}/swap/v2/order", params=params)
        return parse_order(raw, input_mint, output_mint, amount_raw, self.origin)
