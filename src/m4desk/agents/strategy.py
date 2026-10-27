"""StrategyAgent: seleziona fino a 3 leader per strategia; regola di sostituzione dopo N perdite.

Non opera direttamente. L'LLM (solo variante `ai_rank`) può solo riordinare candidati già idonei.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select

from ..services.events import make_event
from ..storage.db import LeaderSlot, Position, TraderScore
from ..types import EventEnvelope, EventType, LeaderStatus, ReasonCode
from .base import Agent


class StrategyAgent(Agent):
    name = "strategy"
    description = "Classifica deterministica + LLM consultivo; sostituzione dopo perdite chiuse nette"

    def __init__(self, rt):
        super().__init__(rt)
        self._last_empty: dict[str, int] = {}
        self.rescore = None  # callback: ricalcolo punteggi con dati fino ad ora (niente look-ahead)
        rt.bus.subscribe(self.name, {EventType.POSITION_CLOSED}, self.handle)

    # --- dati --------------------------------------------------------------
    def latest_scores(self) -> dict[str, TraderScore]:
        with self.rt.db.session() as s:
            sub = (select(TraderScore.wallet, func.max(TraderScore.id).label("mid"))
                   .group_by(TraderScore.wallet).subquery())
            rows = s.scalars(select(TraderScore).join(sub, TraderScore.id == sub.c.mid))
            return {r.wallet: r for r in rows}

    def slots(self, strategy: str) -> dict[str, LeaderSlot]:
        with self.rt.db.session() as s:
            return {r.wallet: r for r in s.scalars(select(LeaderSlot).where(LeaderSlot.strategy == strategy))}

    def leader_status(self, strategy: str, wallet: str) -> tuple[LeaderStatus | None, int]:
        with self.rt.db.session() as s:
            r = s.get(LeaderSlot, (strategy, wallet))
            return (LeaderStatus(r.status), r.consecutive_losses) if r else (None, 0)

    def is_watched(self, wallet: str) -> bool:
        with self.rt.db.session() as s:
            return s.scalar(select(func.count()).select_from(LeaderSlot).where(LeaderSlot.wallet == wallet)) > 0

    def watched_wallets(self) -> set[str]:
        with self.rt.db.session() as s:
            ws = set(s.scalars(select(LeaderSlot.wallet).where(LeaderSlot.status != LeaderStatus.BLOCKED.value)))
            ws |= set(s.scalars(select(Position.leader).where(Position.status == "OPEN")))
        return ws

    # --- selezione ----------------------------------------------------------
    async def selection_round(self) -> dict[str, list[str]]:
        self.beat("RUNNING", "selezione leader")
        scores = self.latest_scores()
        eligible = sorted(
            (w for w, r in scores.items() if r.status == "ELIGIBLE" and r.score is not None),
            key=lambda w: (-(scores[w].score or Decimal(0)), w),
        )
        out: dict[str, list[str]] = {}
        now = self.rt.clock.now()
        for sid, sdef in self.rt.strategies.items():
            slots = self.slots(sid)
            selected = [w for w, r in slots.items() if r.status == LeaderStatus.SELECTED.value]
            free = self.rt.cfg.risk.max_leaders - len(selected)
            pool = [w for w in eligible if w not in slots]  # bloccati o già usati non rientrano
            ranking = pool
            ai_note = None
            if sdef.use_ai and pool and free > 0:
                cands = [self._llm_view(w, scores[w]) for w in pool[:8]]
                res = await self.rt.llm.rank_leaders(cands)
                if res.ok and res.advice is not None:
                    ranked = list(res.advice.ranking)
                    ranking = ranked + [w for w in pool if w not in ranked]
                    ai_note = f"LLM ({res.backend}): {res.advice.rationale[:200]}"
                else:
                    ai_note = f"LLM non usato ({res.reason.value if res.reason else '?'}): baseline deterministica"
                self.rt.db.audit(self.name, "llm.advice", ai_note, {"strategy": sid}, ts=now)
            new = ranking[: max(free, 0)]
            with self.rt.db.session() as s:
                for i, w in enumerate(new):
                    s.add(LeaderSlot(strategy=sid, wallet=w, status=LeaderStatus.SELECTED.value,
                                     rank=len(selected) + i + 1, consecutive_losses=0, selected_at=now, updated_at=now,
                                     note=ai_note))
            selected += new
            out[sid] = selected
            if free > 0 and not new and self._last_empty.get(sid) != free:  # niente spam a ogni ciclo
                self._last_empty[sid] = free
                self.rt.db.audit(self.name, "selection.empty",
                                 f"[{sid}] {ReasonCode.NO_REPLACEMENT_CANDIDATE.value}: nessun candidato idoneo, "
                                 f"slot liberi {free}", {"strategy": sid}, "WARN", ts=now)
            if new:
                await self.rt.bus.publish(
                    make_event(EventType.LEADERS_SELECTED, self.name, (sid, tuple(selected), now.isoformat()),
                               self.rt.origin, "local", now,
                               payload={"strategy": sid, "selected": selected, "added": new, "note": ai_note})
                )
        self.processed += 1
        self.beat("IDLE", "; ".join(f"{k}:{len(v)}" for k, v in out.items()))
        return out

    @staticmethod
    def _llm_view(wallet: str, r: TraderScore) -> dict:
        m = r.metrics or {}
        keys = ("round_trips", "win_rate", "median_return", "profit_factor", "hold_median_s", "top_token_share",
                "copiability", "data_completeness", "momentum_7d_mean", "momentum_30d_mean")
        return {"id": wallet, "score": float(r.score or 0), **{k: m.get(k) for k in keys}}

    # --- perdite e sostituzione -------------------------------------------------
    async def handle(self, env: EventEnvelope) -> None:
        p = env.payload
        sid, leader = p["strategy"], p["leader"]
        sdef = self.rt.strategies.get(sid)
        if sdef is None:
            return
        pnl = Decimal(p["realized_pnl_usd"])
        now = self.rt.clock.now()
        blocked = False
        with self.rt.db.session() as s:
            slot = s.get(LeaderSlot, (sid, leader))
            if slot is None:
                return
            # perdita = posizione chiusa completamente con P&L netto < 0 (tutte le commissioni incluse)
            slot.consecutive_losses = slot.consecutive_losses + 1 if pnl < 0 else 0
            slot.updated_at = now
            if sdef.loss_limit is not None and slot.consecutive_losses >= sdef.loss_limit \
                    and slot.status == LeaderStatus.SELECTED.value:
                slot.status = LeaderStatus.BLOCKED.value
                slot.note = f"Bloccato dopo {slot.consecutive_losses} perdite consecutive chiuse"
                blocked = True
            losses = slot.consecutive_losses
        if blocked:
            self.rt.db.audit(self.name, "leader.blocked",
                             f"[{sid}] leader {leader[:8]}… bloccato ({losses} perdite nette consecutive); "
                             "le posizioni aperte restano in gestione", {"strategy": sid, "leader": leader},
                             "WARN", ts=now)
            if self.rescore is not None:
                await self.rescore()
            await self.selection_round()
