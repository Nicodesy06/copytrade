"""Budget gratuito, rate limit, backoff, circuit breaker e cache TTL per ogni provider esterno."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import func, select

from ..clock import Clock
from ..config import BudgetConfig
from ..storage.db import Database, ProviderUsage, today_str
from ..types import ReasonCode
from .base import ProviderError

T = TypeVar("T")


class ProviderGate:
    """Ogni chiamata esterna passa da qui: niente chiamate fuori budget, niente upgrade automatici."""

    def __init__(
        self,
        name: str,
        budget: BudgetConfig,
        db: Database,
        clock: Clock,
        failure_threshold: int = 3,
        base_backoff_s: float = 30.0,
        max_backoff_s: float = 900.0,
        enabled: bool = True,
    ):
        self.name = name
        self.budget = budget
        self.db = db
        self.clock = clock
        self.failure_threshold = failure_threshold
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        self.enabled = enabled
        self.disabled_reason: str | None = None
        self._minute: deque[datetime] = deque()
        self._consecutive_failures = 0
        self._open_until: datetime | None = None
        self._backoff_s = base_backoff_s
        self._cache: dict[str, tuple[datetime, Any]] = {}
        self._lock = asyncio.Lock()
        self.last_error: str | None = None
        self.last_ok_at: datetime | None = None

    # --- stato -------------------------------------------------------------
    def usage(self) -> dict[str, int]:
        now = self.clock.now()
        day = today_str(now)
        month = day[:7]
        with self.db.session() as s:
            row = s.get(ProviderUsage, (self.name, day))
            monthly = s.scalar(
                select(func.coalesce(func.sum(ProviderUsage.requests), 0)).where(
                    ProviderUsage.provider == self.name, ProviderUsage.day.like(f"{month}%")
                )
            )
        return {
            "day_requests": row.requests if row else 0,
            "day_errors": row.errors if row else 0,
            "day_429": row.rate_limited if row else 0,
            "day_cache_hits": row.cache_hits if row else 0,
            "month_requests": int(monthly or 0),
            "daily_cap": self.budget.daily_requests,
            "monthly_cap": self.budget.monthly_requests,
        }

    def circuit_open(self) -> bool:
        return self._open_until is not None and self.clock.now() < self._open_until

    def status(self) -> str:
        if not self.enabled:
            return "DISABILITATO"
        if self.circuit_open():
            return "CIRCUIT_OPEN"
        u = self.usage()
        if u["day_requests"] >= u["daily_cap"] or u["month_requests"] >= u["monthly_cap"]:
            return "BUDGET_ESAURITO"
        return "OK"

    def available(self) -> tuple[bool, ReasonCode | None]:
        if not self.enabled:
            return False, ReasonCode.PROVIDER_DISABLED
        if self.circuit_open():
            return False, ReasonCode.CIRCUIT_OPEN
        u = self.usage()
        if u["day_requests"] >= u["daily_cap"] or u["month_requests"] >= u["monthly_cap"]:
            return False, ReasonCode.BUDGET_EXHAUSTED
        return True, None

    def disable(self, reason: str) -> None:
        self.enabled = False
        self.disabled_reason = reason
        self.db.audit("gate", "provider.disabled", f"Provider {self.name} disabilitato: {reason}", level="WARN")

    # --- contabilità -----------------------------------------------------
    def _bump(self, **inc: int) -> None:
        day = today_str(self.clock.now())
        with self.db.session() as s:
            row = s.get(ProviderUsage, (self.name, day))
            if row is None:
                row = ProviderUsage(
                    provider=self.name, day=day, requests=0, errors=0, rate_limited=0, cache_hits=0,
                    near_quota_events=0,
                )
                s.add(row)
            for k, v in inc.items():
                setattr(row, k, (getattr(row, k) or 0) + v)
            if "requests" in inc and row.requests == int(self.budget.daily_requests * 0.8):
                row.near_quota_events = (row.near_quota_events or 0) + 1

    def _trip(self, reason: str) -> None:
        now = self.clock.now()
        self._open_until = now + timedelta(seconds=self._backoff_s)
        self.db.audit(
            "gate",
            "provider.circuit_open",
            f"{self.name}: circuito aperto per {int(self._backoff_s)}s ({reason})",
            level="WARN",
        )
        self._backoff_s = min(self._backoff_s * 2, self.max_backoff_s)

    async def call(self, cache_key: str | None, ttl_s: float, fn: Callable[[], Awaitable[T]]) -> T:
        now = self.clock.now()
        if cache_key is not None and cache_key in self._cache:
            at, val = self._cache[cache_key]
            if (now - at).total_seconds() <= ttl_s:
                self._bump(cache_hits=1)
                return val
        ok, reason = self.available()
        if not ok:
            assert reason is not None
            raise ProviderError(self.name, reason, self.disabled_reason or "")
        async with self._lock:
            while self._minute and (now - self._minute[0]).total_seconds() > 60:
                self._minute.popleft()
            if len(self._minute) >= self.budget.per_minute:
                raise ProviderError(self.name, ReasonCode.PROVIDER_RATE_LIMITED, "limite locale per minuto")
            self._minute.append(now)
        self._bump(requests=1)
        try:
            val = await fn()
        except ProviderError as e:
            self.last_error = str(e)
            if e.reason == ReasonCode.PROVIDER_RATE_LIMITED:
                self._bump(rate_limited=1, errors=1)
                self._trip("HTTP 429")
            else:
                self._bump(errors=1)
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.failure_threshold:
                    self._trip(e.reason.value)
            raise
        except Exception as e:  # errori di rete/parsing: mai convertiti in dati
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            self._bump(errors=1)
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._trip(type(e).__name__)
            raise ProviderError(self.name, ReasonCode.PROVIDER_ERROR, type(e).__name__) from e
        self._consecutive_failures = 0
        self._backoff_s = self.base_backoff_s
        self.last_ok_at = self.clock.now()
        if cache_key is not None:
            self._cache[cache_key] = (self.clock.now(), val)
            if len(self._cache) > 2000:
                self._cache.pop(next(iter(self._cache)))
        return val
