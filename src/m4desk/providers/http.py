"""Client HTTP comune: mappa gli errori su reason code, non logga mai URL con chiavi."""

from __future__ import annotations

from typing import Any

import httpx

from ..types import ReasonCode
from .base import ProviderError
from .budget import ProviderGate

USER_AGENT = "m4-copydesk/0.1 (ricerca locale, sola lettura)"


class HttpJson:
    def __init__(self, name: str, gate: ProviderGate, timeout_s: float, headers: dict[str, str] | None = None):
        self.name = name
        self.gate = gate
        self.client = httpx.AsyncClient(timeout=timeout_s, headers={"user-agent": USER_AGENT, **(headers or {})})

    async def _do(self, method: str, url: str, **kw: Any) -> Any:
        try:
            r = await self.client.request(method, url, **kw)
        except httpx.TimeoutException as e:
            raise ProviderError(self.name, ReasonCode.PROVIDER_ERROR, "timeout") from e
        except httpx.HTTPError as e:
            raise ProviderError(self.name, ReasonCode.PROVIDER_ERROR, type(e).__name__) from e
        if r.status_code == 429:
            raise ProviderError(self.name, ReasonCode.PROVIDER_RATE_LIMITED, "HTTP 429")
        if r.status_code in (401, 402, 403):
            # autenticazione/piano cambiati: connettore disabilitato, mai upgrade automatico
            self.gate.disable(f"HTTP {r.status_code}: autenticazione o piano cambiati")
            raise ProviderError(self.name, ReasonCode.PROVIDER_DISABLED, f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise ProviderError(self.name, ReasonCode.PROVIDER_ERROR, f"HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise ProviderError(self.name, ReasonCode.PROVIDER_ERROR, "JSON non valido") from e

    async def get(self, url: str, cache_key: str | None = None, ttl_s: float = 0, **kw: Any) -> Any:
        return await self.gate.call(cache_key, ttl_s, lambda: self._do("GET", url, **kw))

    async def post(self, url: str, json: Any, cache_key: str | None = None, ttl_s: float = 0, **kw: Any) -> Any:
        return await self.gate.call(cache_key, ttl_s, lambda: self._do("POST", url, json=json, **kw))

    async def aclose(self) -> None:
        await self.client.aclose()
