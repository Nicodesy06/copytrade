"""AuditAgent: riconciliazione periodica, invarianti, prezzi stantii, snapshot equity e report giornalieri."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from ..services.reporting import build_daily_report
from ..services.safety import check_contracts
from ..storage.db import Report
from ..types import EventEnvelope, EventType, ReasonCode
from .base import Agent


class AuditAgent(Agent):
    name = "audit"
    description = "Riconciliazione ledger/posizioni, invarianti contabili, prezzi stantii, report giornalieri"

    def __init__(self, rt):
        super().__init__(rt)
        self._stale_alerted: set[int] = set()
        rt.bus.subscribe(self.name, {EventType.DAY_END}, self.handle)

    async def cycle(self) -> list[str]:
        self.beat("RUNNING", "riconciliazione")
        now = self.rt.clock.now()
        problems: list[str] = []
        for sid in self.rt.strategies:
            v = self.rt.ledger.snapshot(sid, now)
            violations = check_contracts(self.rt.db, sid, self.rt.cfg.risk.starting_equity_usd,
                                         self.rt.cfg.risk.max_positions, v.cash)
            problems += [f"[{sid}] {x}" for x in violations]
            for row in v.by_position:
                if row["stale"] and row["id"] not in self._stale_alerted:
                    self._stale_alerted.add(row["id"])
                    self.rt.db.audit(self.name, "price.stale",
                                     f"[{sid}] posizione {row['id']}: prezzo stantio, esclusa dall'equity prudenziale",
                                     {"position_id": row["id"]}, "WARN", ts=now)
        if problems:
            self.rt.db.audit(self.name, "reconcile.failed", "; ".join(problems)[:1000], None, "ERROR", ts=now)
            await self.rt.enter_safe(self.name, f"{ReasonCode.POSITION_MISMATCH.value}: {problems[0]}")
        self.processed += 1
        self.beat("IDLE", "ok" if not problems else f"{len(problems)} problemi")
        return problems

    async def handle(self, env: EventEnvelope) -> None:
        await self.daily_report(env.payload["day"])

    async def daily_report(self, day: str) -> Report:
        await self.cycle()
        now = self.rt.clock.now()
        md, data = build_daily_report(
            self.rt.db, self.rt.ledger, {k: v.label for k, v in self.rt.strategies.items()},
            self.rt.cfg.strategy.primary, day, now, self.rt.origin.value, self.rt.strategy_version,
        )
        with self.rt.db.session() as s:
            for old in s.scalars(select(Report).where(Report.day == day)):
                s.delete(old)
            r = Report(day=day, created_at=now, title=f"Report {day}", body_md=md, data=data)
            s.add(r)
        out = Path(self.rt.cfg.data_dir) / "reports"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"report-{self.rt.mode.value}-{day}.md").write_text(md, encoding="utf-8")
        self.rt.db.audit(self.name, "report.daily", f"Report giornaliero {day} generato", ts=now)
        return r
