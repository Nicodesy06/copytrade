"""Query di sola lettura per le pagine della dashboard."""

from __future__ import annotations

import html
import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from ..config import AppConfig
from ..services.ledger import Ledger
from ..services.reporting import decision_stats, per_leader, strategy_stats
from ..services.safety import check_contracts
from ..storage.db import (
    AgentStatusRow,
    AuditLog,
    Checkpoint,
    Database,
    DecisionRow,
    EquitySnapshot,
    LeaderSlot,
    Order,
    Position,
    ProviderUsage,
    Report,
    Reservation,
    TokenCheck,
    TraderScore,
    Wallet,
)
from ..types import reason_it

AGENT_DESCRIPTIONS = {
    "discovery": "Scopre wallet candidati da seed pubblici ed esplorazione limitata degli swap",
    "trader_analyst": "Ricostruisce round-trip e calcola metriche e punteggio deterministici",
    "token_analyst": "Rugcheck, authority on-chain, Token-2022, liquidità, età, concentrazione",
    "strategy": "Seleziona fino a 3 leader; LLM solo consultivo; sostituzione dopo perdite",
    "position_manager": "Replica acquisti/vendite dei leader, stop/TP simulati, nessuna posizione orfana",
    "audit": "Riconciliazione, invarianti contabili, prezzi stantii, report giornalieri",
    "supervisor": "Orchestrazione, health check, recovery, transizioni di stato",
}


class Views:
    def __init__(self, db: Database, cfg: AppConfig):
        self.db = db
        self.cfg = cfg
        self.ledger = Ledger(db, cfg.risk.starting_equity_usd, cfg.risk.max_price_age_s)

    # --- contesto comune -------------------------------------------------------
    def now(self) -> datetime:
        """In DEMO/REPLAY l'"adesso" è la fine del replay (orologio simulato)."""
        done = self.db.get_meta("replay_completed_at")
        if done:
            return datetime.fromisoformat(done)
        if self.db.get_meta("mode") in ("demo", "replay"):
            with self.db.session() as s:
                t = s.scalar(select(func.max(AuditLog.ts)))
            return t or datetime.now(UTC)
        return datetime.now(UTC)

    def common(self) -> dict[str, Any]:
        mode = self.db.get_meta("mode", "paper")
        origin = self.db.get_meta("origin", "MAINNET READ-ONLY")
        return {
            "mode": mode,
            "origin": origin,
            "simulated": origin != "MAINNET READ-ONLY",
            "state": self.db.get_control("state", "RUNNING"),
            "version": self.db.get_meta("strategy_version", "-"),
            "strategies": _ordered(self.db.get_meta("strategies") or {}, self.db.get_meta("primary_strategy")),
            "primary": self.db.get_meta("primary_strategy", "switch2"),
            "fixture": self.db.get_meta("fixture"),
            "now": self.now(),
            "llm_backend": self.db.get_meta("llm_backend", "-"),
        }

    def labels(self) -> dict[str, str]:
        return {w.address: (w.label or "") for w in self.db.list_wallets()}

    # --- panoramica ----------------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        c = self.common()
        now = c["now"]
        stats = [strategy_stats(self.db, self.ledger, sid, now) for sid in c["strategies"]]
        primary = next((s for s in stats if s["strategy"] == c["primary"]), None)
        with self.db.session() as s:
            eq = [(e.ts, e.equity_usd) for e in s.scalars(
                select(EquitySnapshot).where(EquitySnapshot.strategy == c["primary"]).order_by(EquitySnapshot.ts))]
            recent = list(s.scalars(select(DecisionRow).where(DecisionRow.strategy == c["primary"])
                                    .order_by(DecisionRow.id.desc()).limit(8)))
            alerts = list(s.scalars(select(AuditLog).where(AuditLog.level.in_(["WARN", "ERROR"]))
                                    .order_by(AuditLog.id.desc()).limit(6)))
            n_wallets = s.scalar(select(func.count()).select_from(Wallet))
            scores = self._latest_scores(s)
        funnel = self.db.get_checkpoint("discovery_funnel") or {}
        elig = sum(1 for r in scores.values() if r.status == "ELIGIBLE")
        selected = self.leaders().get(c["primary"], [])
        return {**c, "stats": stats, "p": primary, "spark": sparkline([float(v) for _, v in eq]),
                "recent": recent, "alerts": alerts, "labels": self.labels(),
                "funnel": [("Seed", funnel.get("seed", 0)), ("Token scansionati", funnel.get("token_scans", 0)),
                           ("Swap osservati", funnel.get("swaps_seen", 0)), ("Candidati", n_wallets or 0),
                           ("Valutati", len(scores)), ("Idonei", elig),
                           ("Seguiti", sum(1 for x in selected if x.status == "SELECTED"))],
                "providers": self.providers(), "ds": decision_stats(self.db, c["primary"])}

    def _latest_scores(self, s) -> dict[str, TraderScore]:
        sub = select(TraderScore.wallet, func.max(TraderScore.id).label("mid")).group_by(TraderScore.wallet).subquery()
        return {r.wallet: r for r in s.scalars(select(TraderScore).join(sub, TraderScore.id == sub.c.mid))}

    def leaders(self) -> dict[str, list[LeaderSlot]]:
        with self.db.session() as s:
            rows = list(s.scalars(select(LeaderSlot).order_by(LeaderSlot.strategy, LeaderSlot.rank)))
        out: dict[str, list[LeaderSlot]] = {}
        for r in rows:
            out.setdefault(r.strategy, []).append(r)
        return out

    def providers(self) -> list[dict[str, Any]]:
        day = self.now().strftime("%Y-%m-%d")
        with self.db.session() as s:
            usage = {u.provider: u for u in s.scalars(select(ProviderUsage).where(ProviderUsage.day == day))}
            disabled = {a.message for a in s.scalars(select(AuditLog).where(AuditLog.action == "provider.disabled"))}
        out = []
        if self.db.get_meta("mode") in ("demo", "replay"):
            for name in ("fixture-market", "fixture-quotes", "fixture-rugcheck", "fixture-chain"):
                out.append({"name": name, "status": "FIXTURE", "requests": 0, "cap": "∞ (offline)", "r429": 0,
                            "errors": 0, "note": "dati sintetici locali, nessuna rete"})
            out.append({"name": "LLM", "status": "MOCK", "requests": 0, "cap": "-", "r429": 0, "errors": 0,
                        "note": self.db.get_meta("llm_backend", "")})
            return out
        for name, b in self.cfg.providers.budget.items():
            u = usage.get(name)
            dis = any(m.startswith(f"Provider {name} disabilitato") for m in disabled)
            req = u.requests if u else 0
            status = "DISABILITATO" if dis else ("BUDGET_ESAURITO" if req >= b.daily_requests else "OK")
            out.append({"name": name, "status": status, "requests": req, "cap": b.daily_requests,
                        "r429": u.rate_limited if u else 0, "errors": u.errors if u else 0,
                        "note": f"cache hit {u.cache_hits if u else 0}"})
        out.append({"name": "LLM", "status": "OK" if self.cfg.llm.enabled else "DISABILITATO", "requests": "-",
                    "cap": "locale", "r429": 0, "errors": 0, "note": self.db.get_meta("llm_backend", "")})
        return out

    # --- agenti --------------------------------------------------------------------
    def agents(self) -> dict[str, Any]:
        c = self.common()
        with self.db.session() as s:
            rows = list(s.scalars(select(AgentStatusRow).order_by(AgentStatusRow.name)))
        return {**c, "agents": rows, "desc": AGENT_DESCRIPTIONS}

    # --- trader --------------------------------------------------------------------
    def traders(self) -> dict[str, Any]:
        c = self.common()
        with self.db.session() as s:
            wallets = list(s.scalars(select(Wallet).order_by(Wallet.added_at)))
            scores = self._latest_scores(s)
        rows = sorted(wallets, key=lambda w: -(scores[w.address].score or Decimal(-1)) if w.address in scores else 1)
        return {**c, "wallets": rows, "scores": scores, "leaders": self.leaders(), "labels": self.labels()}

    # --- token ---------------------------------------------------------------------
    def tokens(self) -> dict[str, Any]:
        c = self.common()
        with self.db.session() as s:
            sub = select(TokenCheck.mint, func.max(TokenCheck.id).label("mid")).group_by(TokenCheck.mint).subquery()
            rows = list(s.scalars(select(TokenCheck).join(sub, TokenCheck.id == sub.c.mid)
                                  .order_by(TokenCheck.checked_at.desc())))
        return {**c, "checks": rows}

    # --- operazioni ----------------------------------------------------------------
    def operations(self, strategy: str | None) -> dict[str, Any]:
        c = self.common()
        sid = strategy if strategy in c["strategies"] else c["primary"]
        with self.db.session() as s:
            decisions = list(s.scalars(select(DecisionRow).where(DecisionRow.strategy == sid)
                                       .order_by(DecisionRow.id.desc()).limit(200)))
            orders = list(s.scalars(select(Order).where(Order.strategy == sid).order_by(Order.id.desc()).limit(200)))
            symbols = {p.mint: p.symbol for p in s.scalars(select(Position))}
            symbols.update({t.mint: t.symbol for t in s.scalars(select(TokenCheck)) if t.symbol})
        return {**c, "sid": sid, "decisions": decisions, "orders": orders, "labels": self.labels(),
                "symbols": symbols}

    # --- portafoglio ------------------------------------------------------------------
    def portfolio(self, strategy: str | None) -> dict[str, Any]:
        c = self.common()
        sid = strategy if strategy in c["strategies"] else c["primary"]
        now = c["now"]
        with self.db.session() as s:
            open_p = list(s.scalars(select(Position).where(Position.strategy == sid, Position.status == "OPEN")))
            closed = list(s.scalars(select(Position).where(Position.strategy == sid, Position.status == "CLOSED")
                                    .order_by(Position.closed_at.desc())))
        rows = []
        for p in open_p:
            qty = Decimal(p.qty_raw) / (Decimal(10) ** p.decimals)
            value = qty * p.last_price_usd if p.last_price_usd is not None else None
            stale = p.last_price_at is None or (now - p.last_price_at).total_seconds() > self.cfg.risk.max_price_age_s
            rows.append({"p": p, "qty": qty, "value": value, "stale": stale,
                         "unreal": (value - p.cost_basis_usd) if value is not None else None})
        return {**c, "sid": sid, "open": rows, "closed": closed, "st": strategy_stats(self.db, self.ledger, sid, now),
                "per_leader": per_leader(self.db, sid), "labels": self.labels()}

    # --- rischi --------------------------------------------------------------------
    def risks(self) -> dict[str, Any]:
        c = self.common()
        now = c["now"]
        contracts = {}
        for sid in c["strategies"]:
            contracts[sid] = check_contracts(self.db, sid, self.cfg.risk.starting_equity_usd,
                                             self.cfg.risk.max_positions, self.ledger.cash(sid))
        with self.db.session() as s:
            breakers = {k.key: k.value for k in s.scalars(select(Checkpoint).where(Checkpoint.key.like("breaker:%")))}
            reservations = list(s.scalars(select(Reservation)))
            skip = Counter(x for r in s.scalars(select(DecisionRow).where(DecisionRow.decision == "SKIP"))
                           for x in r.reasons)
            blocked = list(s.scalars(select(LeaderSlot).where(LeaderSlot.status == "BLOCKED")))
            risk_events = list(s.scalars(select(AuditLog).where(
                AuditLog.action.in_(["exit.failed", "contract.violation", "reconcile.failed", "price.stale",
                                     "reorg.after_copy", "breaker.feed", "breaker.quote", "control.state",
                                     "provider.circuit_open", "provider.disabled", "recovery"]))
                .order_by(AuditLog.id.desc()).limit(40)))
        dd = {}
        for sid in c["strategies"]:
            key = f"day_start_equity:{sid}:{now.strftime('%Y-%m-%d')}"
            start = self.db.get_checkpoint(key)
            cur = self.ledger.view(sid, now).equity_estimate
            dd[sid] = (max(Decimal(start) - cur, Decimal(0)) / Decimal(start) * 100).quantize(Decimal("0.01")) \
                if start else None
        return {**c, "risk": self.cfg.risk, "sim": self.cfg.simulation, "contracts": contracts, "breakers": breakers,
                "reservations": reservations, "skip": skip.most_common(20), "blocked": blocked,
                "events": risk_events, "dd": dd, "labels": self.labels()}

    # --- report / log / impostazioni ---------------------------------------------------
    def reports(self, rid: int | None) -> dict[str, Any]:
        c = self.common()
        with self.db.session() as s:
            rows = list(s.scalars(select(Report).order_by(Report.id.desc())))
        cur = next((r for r in rows if r.id == rid), rows[0] if rows else None)
        return {**c, "reports": rows, "cur": cur, "html": md_to_html(cur.body_md) if cur else ""}

    def logs(self, level: str | None) -> dict[str, Any]:
        c = self.common()
        with self.db.session() as s:
            stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(400)
            if level in ("INFO", "WARN", "ERROR"):
                stmt = select(AuditLog).where(AuditLog.level == level).order_by(AuditLog.id.desc()).limit(400)
            rows = list(s.scalars(stmt))
        return {**c, "rows": rows, "level": level or ""}

    def settings(self) -> dict[str, Any]:
        c = self.common()
        return {**c, "cfg": self.cfg, "public": self.db.get_meta("config_public") or {},
                "rpc": self.db.get_meta("rpc", "fixture (offline)")}

    def state_json(self) -> dict[str, Any]:
        c = self.common()
        now = c["now"]
        return {
            "mode": c["mode"], "origin": c["origin"], "state": c["state"], "version": c["version"],
            "strategies": {sid: {"equity_estimate": str(self.ledger.view(sid, now).equity_estimate.quantize(
                Decimal("0.01")))} for sid in c["strategies"]},
            "note": "P&L simulato su capitale virtuale",
        }


def _ordered(strategies: dict[str, str], primary: str | None) -> dict[str, str]:
    order = ["switch2", "switch3", "naive", "ai_rank", "flat20"]
    keys = sorted(strategies, key=lambda k: (k != primary, order.index(k) if k in order else 99, k))
    return {k: strategies[k] for k in keys}


def sparkline(values: list[float], w: int = 560, h: int = 90) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pts = " ".join(f"{i * w / (len(values) - 1):.1f},{h - 6 - (v - lo) / span * (h - 12):.1f}"
                   for i, v in enumerate(values))
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" '
            f'aria-label="Andamento equity simulata da {lo:.2f} a {hi:.2f} USD">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="2" points="{pts}"/></svg>')


def md_to_html(md: str) -> str:
    """Markdown minimale (titoli, tabelle, elenchi, citazioni, grassetto, codice). Tutto viene prima escapato."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0

    def inline(t: str) -> str:
        t = html.escape(t)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)

    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [x.strip() for x in lines[i].strip("|").split("|")]
                if not all(re.fullmatch(r"-+", x or "-") for x in cells):
                    rows.append(cells)
                i += 1
            out.append('<div class="tablewrap"><table><thead><tr>' + "".join(f"<th>{inline(x)}</th>" for x in rows[0])
                       + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{inline(x)}</td>" for x in r)
                                                         + "</tr>" for r in rows[1:]) + "</tbody></table></div>")
            continue
        if ln.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(f"<li>{inline(lines[i][2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        m = re.match(r"^(#{1,3}) (.*)", ln)
        if m:
            n = len(m.group(1)) + 1
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
        elif ln.startswith("> "):
            out.append(f'<p class="callout">{inline(ln[2:])}</p>')
        elif ln.strip():
            out.append(f"<p>{inline(ln)}</p>")
        i += 1
    return "\n".join(out)


__all__ = ["Views", "reason_it"]
