"""Notifiche push sul telefono via ntfy (https://ntfy.sh, gratuito, open source, senza account).

Il nome del topic è segreto (`NTFY_TOPIC` in .env): chi lo conosce legge le notifiche. I messaggi
contengono solo eventi e indirizzi pubblici, MAI chiavi. Un errore di invio non blocca mai il motore.
"""

from __future__ import annotations

import logging
import time
from collections import deque

import httpx

from ..config import NotifyConfig

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: NotifyConfig, topic: str | None, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.topic = topic
        self.transport = transport
        self.sent = 0
        self.failed = 0
        self._recent: deque[float] = deque()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.topic)

    def url(self) -> str:
        return f"{self.cfg.server.rstrip('/')}/{self.topic}"

    async def send(self, title: str, message: str, priority: str = "default", tags: str = "") -> bool:
        if not self.enabled:
            return False
        now = time.monotonic()
        while self._recent and now - self._recent[0] > 3600:
            self._recent.popleft()
        if len(self._recent) >= self.cfg.max_per_hour:
            return False  # limite locale anti-spam
        self._recent.append(now)
        headers = {"Title": title.encode("latin-1", "ignore").decode("latin-1"), "Priority": priority}
        if tags:
            headers["Tags"] = tags
        try:
            async with httpx.AsyncClient(timeout=10, transport=self.transport) as c:
                r = await c.post(self.url(), content=message.encode("utf-8"), headers=headers)
                r.raise_for_status()
            self.sent += 1
            return True
        except Exception as e:  # mai bloccante
            self.failed += 1
            log.warning("notifica non inviata: %s", type(e).__name__)
            return False
