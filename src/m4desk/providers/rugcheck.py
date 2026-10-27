"""Rugcheck in sola lettura: `/v1/tokens/{mint}/report` + `/report/summary`.

`lpLockedPct` cambia significato fra endpoint (summary vs singoli mercati): si prende il valore
PIÙ PRUDENTE disponibile e si registra la fonte; se nessuno è disponibile resta `None` (=> rifiuto).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..types import DataOrigin, ReasonCode
from .base import ProviderError, RiskItem, RugReport
from .http import HttpJson


def _d(x: Any) -> Decimal | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = Decimal(str(x))
    except InvalidOperation:
        return None
    return v if v.is_finite() else None


def _dt(x: Any) -> datetime | None:
    if not isinstance(x, str):
        return None
    try:
        return datetime.fromisoformat(x[:26].rstrip("Z") + "+00:00") if "T" in x else None
    except ValueError:
        return None


def parse_report(mint: str, report: Any, summary: Any, origin: DataOrigin) -> RugReport:
    if not isinstance(report, dict) or "score_normalised" not in report or "risks" not in report:
        raise ProviderError("rugcheck", ReasonCode.RUGCHECK_SCHEMA_INVALID, "campi obbligatori assenti")
    tok = report.get("token") if isinstance(report.get("token"), dict) else None
    known = report.get("knownAccounts") or {}
    amm_owners = {k for k, v in known.items() if isinstance(v, dict) and v.get("type") == "AMM"}
    holders = report.get("topHolders")
    top10 = None
    if isinstance(holders, list) and holders:
        vals = [_d(h.get("pct")) for h in holders if isinstance(h, dict) and h.get("owner") not in amm_owners
                and h.get("address") not in amm_owners]
        if all(v is not None for v in vals):
            top10 = sum(sorted((v for v in vals if v is not None), reverse=True)[:10], Decimal(0))
    lp_candidates: list[tuple[Decimal, str]] = []
    if isinstance(summary, dict) and _d(summary.get("lpLockedPct")) is not None:
        lp_candidates.append((_d(summary["lpLockedPct"]), "summary"))  # type: ignore[arg-type]
    for m in report.get("markets") or []:
        lp = (m or {}).get("lp") if isinstance(m, dict) else None
        if isinstance(lp, dict) and _d(lp.get("lpLockedPct")) is not None:
            lp_candidates.append((_d(lp["lpLockedPct"]), "markets"))  # type: ignore[arg-type]
    lp_val, lp_src = min(lp_candidates, key=lambda x: x[0]) if lp_candidates else (None, None)
    exts = report.get("token_extensions")
    ext_names: tuple[str, ...] | None
    if isinstance(exts, dict):
        ext_names = tuple(exts.keys())
    elif isinstance(exts, list):
        ext_names = tuple(str(e.get("extension", e)) if isinstance(e, dict) else str(e) for e in exts)
    else:
        ext_names = None
    meta = report.get("tokenMeta") if isinstance(report.get("tokenMeta"), dict) else {}
    score = report.get("score_normalised")
    return RugReport(
        mint=mint,
        fetched_at=datetime.now(UTC),
        provider="rugcheck",
        origin=origin,
        score_normalised=int(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
        risks=tuple(
            RiskItem(name=str(r.get("name", "?")), level=str(r.get("level", "unknown")),
                     description=str(r.get("description", ""))[:200])
            for r in report.get("risks") or [] if isinstance(r, dict)
        ),
        rugged=report.get("rugged") if isinstance(report.get("rugged"), bool) else None,
        mint_authority=tok.get("mintAuthority") if tok else report.get("mintAuthority"),
        freeze_authority=tok.get("freezeAuthority") if tok else report.get("freezeAuthority"),
        authorities_known=tok is not None,
        lp_locked_pct=lp_val,
        lp_source=lp_src,
        top10_pct=top10,
        token_program=report.get("tokenProgram"),
        extensions=ext_names,
        name=meta.get("name"),
        symbol=meta.get("symbol"),
        description=None,
        detected_at=_dt(report.get("detectedAt")),
    )


class RugcheckClient:
    name = "rugcheck"

    def __init__(self, http: HttpJson, base_url: str, ttl_s: float = 300, origin=DataOrigin.MAINNET_READONLY):
        self.http = http
        self.base = base_url.rstrip("/")
        self.ttl_s = ttl_s
        self.origin = origin

    async def get_report(self, mint: str) -> RugReport:
        report = await self.http.get(f"{self.base}/v1/tokens/{mint}/report", cache_key=f"r:{mint}", ttl_s=self.ttl_s)
        try:
            summary = await self.http.get(f"{self.base}/v1/tokens/{mint}/report/summary", cache_key=f"s:{mint}",
                                          ttl_s=self.ttl_s)
        except ProviderError:
            summary = None  # il report completo resta valido; lp dai mercati
        return parse_report(mint, report, summary, self.origin)
