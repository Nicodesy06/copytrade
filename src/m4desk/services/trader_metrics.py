"""Ricostruzione round-trip e metriche trader. Solo dati con event_at <= as_of (niente look-ahead).

Limiti dichiarati: lo storico pubblico è parziale (vendite senza acquisto noto = dati mancanti),
il campione è piccolo e i wallet trovati sono soggetti a survivorship bias.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..config import TraderConfig
from ..types import ReasonCode, Side, SwapObservation


@dataclass
class RoundTrip:
    mint: str
    quote_mint: str
    opened_at: datetime
    closed_at: datetime
    cost: Decimal
    proceeds: Decimal

    @property
    def ret(self) -> float:
        return float(self.proceeds / self.cost - 1) if self.cost > 0 else 0.0

    @property
    def hold_s(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds()


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    p = wins / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)


def reconstruct(swaps: list[SwapObservation], as_of: datetime) -> tuple[list[RoundTrip], int, int]:
    """Ritorna (round_trip chiusi, vendite orfane, posizioni ancora aperte)."""
    state: dict[tuple[str, str], dict[str, Any]] = {}
    trips: list[RoundTrip] = []
    orphans = 0
    for s in sorted((x for x in swaps if x.event_at <= as_of), key=lambda x: (x.event_at, x.slot)):
        key = (s.mint, s.quote_mint)
        st = state.get(key)
        if s.side == Side.BUY:
            if st is None:
                st = state[key] = {"qty": 0, "peak": 0, "cost": Decimal(0), "proceeds": Decimal(0),
                                   "total_cost": Decimal(0), "opened": s.event_at}
            st["qty"] += s.token_amount_raw
            st["peak"] = max(st["peak"], st["qty"])
            st["cost"] += s.quote_amount
            st["total_cost"] += s.quote_amount
        else:
            if st is None or st["qty"] <= 0:
                orphans += 1
                continue
            sold = min(s.token_amount_raw, st["qty"])
            st["qty"] -= sold
            st["proceeds"] += s.quote_amount
            if st["qty"] <= st["peak"] // 100:  # chiuso (polvere < 1%)
                trips.append(RoundTrip(s.mint, s.quote_mint, st["opened"], s.event_at, st["total_cost"],
                                       st["proceeds"]))
                del state[key]
    return trips, orphans, len(state)


def _q(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    i = min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))
    return round(v[i], 4)


def compute_metrics(swaps: list[SwapObservation], as_of: datetime, cfg: TraderConfig
                    ) -> tuple[str, Decimal | None, dict[str, Any], list[str]]:
    """Ritorna (status, score, metriche, motivi). Metriche ignote = None."""
    window = [s for s in swaps if as_of - timedelta(days=cfg.lookback_days) <= s.event_at <= as_of]
    trips, orphans, open_n = reconstruct(window, as_of)
    n = len(trips)
    rets = [t.ret for t in trips]
    wins = sum(1 for r in rets if r > 0)
    holds = [t.hold_s for t in trips]
    by_mint: dict[str, int] = defaultdict(int)
    for t in trips:
        by_mint[t.mint] += 1
    gains = sum(r for r in rets if r > 0)
    losses = -sum(r for r in rets if r < 0)
    r7 = [t.ret for t in trips if t.closed_at >= as_of - timedelta(days=7)]
    buys = sum(1 for s in window if s.side == Side.BUY)
    sells = len(window) - buys
    lo, hi = wilson(wins, n)
    m: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "swaps": len(window),
        "round_trips": n,
        "wins": wins,
        "win_rate": round(wins / n, 4) if n else None,
        "win_rate_ci95": [lo, hi],
        "median_return": round(statistics.median(rets), 4) if rets else None,
        "mean_return": round(statistics.fmean(rets), 4) if rets else None,
        "p25_return": _q(rets, 0.25),
        "p75_return": _q(rets, 0.75),
        "best_return": round(max(rets), 4) if rets else None,
        "worst_return": round(min(rets), 4) if rets else None,
        "profit_factor": round(gains / losses, 3) if losses > 0 else (None if not gains else 99.0),
        "hold_median_s": round(statistics.median(holds)) if holds else None,
        "hold_p25_s": _q(holds, 0.25),
        "hold_p75_s": _q(holds, 0.75),
        "top_token_share": round(max(by_mint.values()) / n, 4) if n else None,
        "distinct_tokens": len(by_mint),
        "buy_sell_ratio": round(buys / sells, 3) if sells else None,
        "orphan_sells": orphans,
        "open_positions": open_n,
        "data_completeness": round(n / (n + orphans), 4) if (n + orphans) else None,
        "momentum_7d_mean": round(statistics.fmean(r7), 4) if r7 else None,
        "momentum_30d_mean": round(statistics.fmean(rets), 4) if rets else None,
        "copiability": round(sum(1 for h in holds if h >= cfg.min_median_hold_s) / n, 4) if n else None,
        "crowding_proxy": None,  # NON DISPONIBILE: non esiste una fonte gratuita verificabile
        "sample_note": "Campione piccolo e soggetto a survivorship bias: non indica rendimenti futuri.",
    }
    reasons: list[str] = []
    if n < cfg.min_round_trips:
        reasons.append(ReasonCode.INSUFFICIENT_HISTORY.value)
        return "INSUFFICIENT_DATA", None, m, reasons
    wr_shrunk = (wins + 5) / (n + 10)  # shrinkage verso 50% per campioni piccoli
    pf = m["profit_factor"] or 0.0
    pf_c = min(pf, 3.0) / 3.0
    med = m["median_return"] or 0.0
    med_c = (min(max(med, -0.5), 1.0) + 0.5) / 1.5
    cop = m["copiability"] or 0.0
    conc = m["top_token_share"] or 1.0
    compl = m["data_completeness"] or 0.0
    score = 0.35 * wr_shrunk + 0.25 * pf_c + 0.15 * cop + 0.15 * med_c + 0.10 * (1 - conc)
    score *= 0.5 + 0.5 * compl  # dati incompleti => punteggio ridotto
    if (m["hold_median_s"] or 0) < cfg.min_median_hold_s:
        score *= 0.5
    sc = Decimal(str(round(score, 4)))
    if sc < cfg.min_score:
        reasons.append(ReasonCode.SCORE_TOO_LOW.value)
        return "BELOW_THRESHOLD", sc, m, reasons
    return "ELIGIBLE", sc, m, [ReasonCode.OK.value]
