"""TokenAnalystAgent: Rugcheck + controlli on-chain diretti + mercato. Politica rigida, fail-closed."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from ..providers.base import MarketSnapshot, ProviderError
from ..services.events import make_event
from ..services.llm import sanitize_untrusted
from ..services.risk import TokenAssessment
from ..storage.db import TokenCheck
from ..types import CheckStatus, EventType, ReasonCode
from .base import Agent

UNAVAILABLE_REASONS = {
    ReasonCode.RUGCHECK_MISSING, ReasonCode.PROVIDER_ERROR, ReasonCode.PROVIDER_RATE_LIMITED,
    ReasonCode.BUDGET_EXHAUSTED, ReasonCode.CIRCUIT_OPEN, ReasonCode.PROVIDER_DISABLED,
    ReasonCode.AUTHORITY_UNKNOWN, ReasonCode.PRICE_UNAVAILABLE, ReasonCode.LIQUIDITY_UNAVAILABLE,
}
SUPPORTED_PROGRAMS = {"spl-token", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}
T22_PROGRAMS = {"spl-token-2022", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}


def _s(v: Any) -> Any:
    return str(v) if isinstance(v, (Decimal, datetime)) else v


class TokenAnalystAgent(Agent):
    name = "token_analyst"
    description = "Authority mint/freeze, estensioni Token-2022, Rugcheck, liquidità, età, concentrazione"

    def __init__(self, rt):
        super().__init__(rt)
        self.cache: dict[str, TokenAssessment] = {}

    async def assess(self, mint: str, force: bool = False) -> TokenAssessment:
        now = self.rt.clock.now()
        cached = self.cache.get(mint)
        if cached and not force and (now - cached.checked_at).total_seconds() <= self.rt.cfg.risk.token_check_ttl_s:
            return cached
        self.beat("RUNNING", f"controllo {mint[:6]}…")
        cfg = self.rt.cfg.risk
        reasons: list[ReasonCode] = []
        warnings: list[ReasonCode] = []
        facts: dict[str, Any] = {"mint": mint}
        symbol = None
        decimals = None

        # 1. Rugcheck (obbligatorio quando configurato; se disabilitato => solo osservazione)
        rug = None
        if self.rt.providers.rug is None:
            reasons.append(ReasonCode.RUGCHECK_MISSING)
            facts["rugcheck"] = "DISABILITATO (modalità solo osservazione)"
        else:
            try:
                rug = await self.rt.providers.rug.get_report(mint)
            except ProviderError as e:
                reasons.extend([ReasonCode.RUGCHECK_MISSING, e.reason])
                facts["rugcheck_error"] = e.reason.value
        if rug is not None:
            facts["rugcheck"] = {
                "provider": rug.provider, "fetched_at": rug.fetched_at.isoformat(),
                "score_normalised": rug.score_normalised, "lp_locked_pct": _s(rug.lp_locked_pct),
                "lp_source": rug.lp_source, "top10_pct": _s(rug.top10_pct), "rugged": rug.rugged,
                "risks": [f"{x.level}:{x.name}" for x in rug.risks],
            }
            if (now - rug.fetched_at).total_seconds() > cfg.token_check_ttl_s:
                reasons.append(ReasonCode.RUGCHECK_STALE)
            if rug.rugged:
                reasons.append(ReasonCode.RUGGED)
            if rug.score_normalised is None:
                reasons.append(ReasonCode.RUGCHECK_SCHEMA_INVALID)
            elif rug.score_normalised > cfg.max_rugcheck_score_normalised:
                reasons.append(ReasonCode.RUGCHECK_SCORE_HIGH)
            if any(x.level.lower() == "danger" for x in rug.risks):
                reasons.append(ReasonCode.RUGCHECK_DANGER)
            if rug.lp_locked_pct is None:
                reasons.append(ReasonCode.LP_LOCK_UNKNOWN)
            elif rug.lp_locked_pct < cfg.min_lp_locked_pct:
                reasons.append(ReasonCode.LP_LOCK_TOO_LOW)
            if rug.top10_pct is None:
                reasons.append(ReasonCode.HOLDERS_UNKNOWN)
            elif rug.top10_pct > cfg.max_top10_holder_pct:
                reasons.append(ReasonCode.TOP_HOLDERS_CONCENTRATED)
            # metadati: testo NON fidato, sanificato e mai passato all'LLM
            for fld in ("name", "symbol", "description"):
                clean, suspicious = sanitize_untrusted(getattr(rug, fld), 32 if fld != "description" else 80)
                facts[f"meta_{fld}"] = clean
                if suspicious:
                    reasons.append(ReasonCode.METADATA_SUSPICIOUS)
            symbol = facts.get("meta_symbol") or None

        # 2. controllo on-chain diretto della mint
        mi = None
        try:
            mi = await self.rt.providers.mint.get_mint_info(mint)
        except ProviderError as e:
            reasons.extend([ReasonCode.AUTHORITY_UNKNOWN, e.reason])
        if mi is not None:
            decimals = mi.decimals
            facts["onchain"] = {"program": mi.program, "decimals": mi.decimals, "mint_authority": mi.mint_authority,
                                "freeze_authority": mi.freeze_authority, "extensions": list(mi.extensions),
                                "provider": mi.provider}
            if mi.mint_authority:
                reasons.append(ReasonCode.MINT_AUTHORITY_OPEN)
            if mi.freeze_authority:
                reasons.append(ReasonCode.FREEZE_AUTHORITY_OPEN)
            if rug is not None and rug.authorities_known and (
                rug.mint_authority != mi.mint_authority or rug.freeze_authority != mi.freeze_authority
            ):
                reasons.append(ReasonCode.AUTHORITY_MISMATCH)
            if mi.program in T22_PROGRAMS:
                bad = [x for x in mi.extensions if x not in cfg.allowed_token2022_extensions]
                if bad:
                    reasons.append(ReasonCode.TOKEN2022_EXTENSION_UNSUPPORTED)
                    facts["unsupported_extensions"] = bad
            elif mi.program not in SUPPORTED_PROGRAMS:
                reasons.append(ReasonCode.TOKEN_PROGRAM_UNKNOWN)

        # 3. mercato
        m: MarketSnapshot | None = None
        try:
            m = await self.rt.providers.market.get_market(mint)
        except ProviderError as e:
            reasons.extend([ReasonCode.LIQUIDITY_UNAVAILABLE, e.reason])
        if m is not None:
            symbol = symbol or m.symbol
            facts["market"] = {"provider": m.provider, "liquidity_usd": _s(m.liquidity_usd),
                               "price_usd": _s(m.price_usd), "pair_created_at": _s(m.pair_created_at),
                               "dex": m.dex_id}
            if m.liquidity_usd is None:
                reasons.append(ReasonCode.LIQUIDITY_UNAVAILABLE)
            elif m.liquidity_usd < cfg.min_liquidity_usd:
                reasons.append(ReasonCode.LIQUIDITY_TOO_LOW)
            if m.pair_created_at is None:
                reasons.append(ReasonCode.TOKEN_AGE_UNKNOWN)
            else:
                age_min = (now - m.pair_created_at).total_seconds() / 60
                facts["age_minutes"] = round(age_min)
                if age_min < cfg.min_token_age_minutes:
                    reasons.append(ReasonCode.TOKEN_TOO_NEW)

        uniq: list[ReasonCode] = []
        for r in reasons:
            if r not in uniq:
                uniq.append(r)
        if not uniq:
            status = CheckStatus.PASS
        elif set(uniq) <= UNAVAILABLE_REASONS:
            status = CheckStatus.UNAVAILABLE
        else:
            status = CheckStatus.FAIL
        a = TokenAssessment(mint, status, tuple(uniq), tuple(warnings), facts, now, symbol, decimals)
        self.cache[mint] = a
        with self.rt.db.session() as s:
            s.add(TokenCheck(mint=mint, checked_at=now, status=status.value, symbol=symbol,
                             reasons=[r.value for r in uniq], facts=facts))
        await self.rt.bus.publish(
            make_event(EventType.TOKEN_ASSESSED, self.name, (mint, now.isoformat()), self.rt.origin, "local", now,
                       token=mint, payload={"status": status.value, "reasons": [r.value for r in uniq]})
        )
        self.processed += 1
        self.beat("IDLE", f"ultimo: {symbol or mint[:6]} {status.value}")
        return a
