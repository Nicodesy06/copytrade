"""Statistiche e report giornaliero in italiano (P&L simulato, mai presentato come saldo reale)."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from ..storage.db import Database, DecisionRow, EquitySnapshot, Order, Position, ProviderUsage
from ..types import reason_it
from .ledger import Ledger
from .trader_metrics import wilson

ZERO = Decimal(0)
C2 = Decimal("0.01")


def _max_drawdown(values: list[Decimal]) -> Decimal:
    peak, mdd = None, ZERO
    for v in values:
        peak = v if peak is None or v > peak else peak
        if peak and peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def strategy_stats(db: Database, ledger: Ledger, sid: str, now: datetime) -> dict[str, Any]:
    v = ledger.view(sid, now)
    with db.session() as s:
        closed = list(s.scalars(select(Position).where(Position.strategy == sid, Position.status == "CLOSED")))
        orders = list(s.scalars(select(Order).where(Order.strategy == sid)))
        eq = [e.equity_usd for e in s.scalars(
            select(EquitySnapshot).where(EquitySnapshot.strategy == sid).order_by(EquitySnapshot.ts))]
    pnls = [p.realized_pnl_usd for p in closed]
    wins = sum(1 for x in pnls if x > 0)
    lo, hi = wilson(wins, len(pnls))
    states = Counter(o.state for o in orders)
    filled = [o for o in orders if o.state == "FILLED"]
    delays = [o.delay_ms for o in filled if o.delay_ms is not None]
    slips = [float(o.actual_slippage_bps) for o in filled if o.actual_slippage_bps is not None]
    fees = ZERO
    for o in filled:
        f = o.fees or {}
        fees += sum((Decimal(f.get(k, "0")) for k in ("network_usd", "priority_usd", "dex_usd")), ZERO)
    start = ledger.starting_equity
    return {
        "strategy": sid,
        "start_equity": start,
        "cash": v.cash.quantize(C2),
        "equity_estimate": v.equity_estimate.quantize(C2),
        "equity_conservative": v.equity_conservative.quantize(C2),
        "return_pct": ((v.equity_estimate / start - 1) * 100).quantize(C2) if start else None,
        "realized": v.realized_pnl.quantize(C2),
        "unrealized": v.unrealized.quantize(C2),
        "open_positions": v.open_positions,
        "stale_positions": v.stale_positions,
        "closed": len(pnls),
        "wins": wins,
        "win_rate": round(wins / len(pnls), 3) if pnls else None,
        "win_rate_ci95": (lo, hi),
        "median_pnl": Decimal(statistics.median(pnls)).quantize(C2) if pnls else None,
        "best_pnl": max(pnls).quantize(C2) if pnls else None,
        "worst_pnl": min(pnls).quantize(C2) if pnls else None,
        "max_drawdown_pct": (_max_drawdown(eq + [v.equity_estimate]) * 100).quantize(C2),
        "orders": dict(states),
        "fees_usd": fees.quantize(C2),
        "median_delay_ms": int(statistics.median(delays)) if delays else None,
        "median_slippage_bps": round(statistics.median(slips), 1) if slips else None,
    }


def per_leader(db: Database, sid: str) -> list[dict[str, Any]]:
    with db.session() as s:
        rows = list(s.scalars(select(Position).where(Position.strategy == sid).order_by(Position.opened_at)))
    by: dict[str, list[Position]] = defaultdict(list)
    for p in rows:
        by[p.leader].append(p)
    out = []
    for leader, ps in by.items():
        closed = [p for p in ps if p.status == "CLOSED"]
        cum, curve = ZERO, []
        for p in sorted(closed, key=lambda x: x.closed_at):
            cum += p.realized_pnl_usd
            curve.append(cum)
        peak, dd = ZERO, ZERO
        for c in curve:
            peak = max(peak, c)
            dd = max(dd, peak - c)
        wins = sum(1 for p in closed if p.realized_pnl_usd > 0)
        out.append({
            "leader": leader,
            "positions": len(ps),
            "open": len(ps) - len(closed),
            "closed": len(closed),
            "realized": sum((p.realized_pnl_usd for p in ps), ZERO).quantize(C2),
            "win_rate": round(wins / len(closed), 3) if closed else None,
            "max_drawdown_usd": dd.quantize(C2),
            "fees": sum((p.fees_usd for p in ps), ZERO).quantize(C2),
        })
    return sorted(out, key=lambda x: -x["realized"])


def decision_stats(db: Database, sid: str | None = None) -> dict[str, Any]:
    with db.session() as s:
        stmt = select(DecisionRow)
        if sid:
            stmt = stmt.where(DecisionRow.strategy == sid)
        rows = list(s.scalars(stmt))
    dec = Counter(r.decision for r in rows)
    reasons = Counter(x for r in rows if r.decision == "SKIP" for x in r.reasons)
    lats = [r.latency_ms for r in rows if r.latency_ms is not None]
    return {"counts": dict(dec), "skip_reasons": reasons.most_common(10),
            "median_latency_ms": int(statistics.median(lats)) if lats else None}


def build_daily_report(db: Database, ledger: Ledger, strategies: dict[str, str], primary: str, day: str,
                       now: datetime, origin: str, version: str) -> tuple[str, dict[str, Any]]:
    stats = [strategy_stats(db, ledger, sid, now) for sid in strategies]
    leaders = per_leader(db, primary)
    ds = decision_stats(db, primary)
    with db.session() as s:
        usage = list(s.scalars(select(ProviderUsage).where(ProviderUsage.day == day)))
    lines = [
        f"# Report giornaliero M4 CopyDesk — {day}",
        "",
        f"**Origine dati:** {origin} · **Versione strategia:** `{version}`",
        "",
        "> P&L **simulato** su capitale virtuale: non è un saldo bancario né un risultato reale. "
        "Campioni piccoli; wallet selezionati a posteriori (survivorship bias). Solo il paper-trading "
        "in avanti può supportare affermazioni sulle prestazioni correnti.",
        "",
        "## Confronto strategie (stesso flusso di eventi)",
        "",
        "| Strategia | Equity stimata | Rend. % | Realizzato | Non realizzato | Chiuse | Win rate (IC95%) "
        "| P&L mediano | Max DD % | Commissioni |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for st in stats:
        ci = st["win_rate_ci95"]
        wr = f"{st['win_rate']:.0%} ({ci[0]:.0%}–{ci[1]:.0%})" if st["win_rate"] is not None else "n/d"
        lines.append(
            f"| {strategies[st['strategy']]} | {st['equity_estimate']} | {st['return_pct']} | {st['realized']} | "
            f"{st['unrealized']} | {st['closed']} | {wr} | {st['median_pnl'] or 'n/d'} | {st['max_drawdown_pct']} | "
            f"{st['fees_usd']} |"
        )
    lines += ["", f"## Leader (strategia primaria `{primary}`)", "",
              "| Leader | Posizioni | Aperte | Realizzato | Win rate | Max DD USD |", "|---|---|---|---|---|---|"]
    for lr in leaders:
        wr = f"{lr['win_rate']:.0%}" if lr["win_rate"] is not None else "n/d"
        lines.append(f"| `{lr['leader'][:10]}…` | {lr['positions']} | {lr['open']} | {lr['realized']} | {wr} | "
                     f"{lr['max_drawdown_usd']} |")
    lines += ["", "## Decisioni", "", f"- COPY: {ds['counts'].get('COPY', 0)} · SKIP: {ds['counts'].get('SKIP', 0)}",
              f"- Latenza mediana evento→decisione: {ds['median_latency_ms']} ms", "", "Motivi di SKIP più frequenti:"]
    for code, n in ds["skip_reasons"]:
        lines.append(f"- {reason_it(code)} (`{code}`): {n}")
    lines += ["", "## Provider (oggi)", ""]
    if usage:
        for u in usage:
            lines.append(f"- {u.provider}: {u.requests} richieste, {u.rate_limited} risposte 429, "
                         f"{u.errors} errori, {u.cache_hits} cache hit")
    else:
        lines.append("- Nessuna chiamata esterna (fixture/offline).")
    lines += ["", "## Limiti noti", "",
              "- Prezzi e liquidità possono essere stantii: le posizioni con prezzo vecchio sono escluse dall'equity "
              "prudenziale.",
              "- Il fill simulato usa una quotazione ritardata e slippage aggiuntivo; i risultati reali possono essere "
              "peggiori.",
              "- Nessuna soglia garantisce la sicurezza di un token."]
    data = {"day": day, "stats": stats, "leaders": leaders, "decisions": ds}
    return "\n".join(lines) + "\n", data
