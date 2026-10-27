"""App FastAPI della dashboard. Solo 127.0.0.1; azioni di scrittura protette da token + conferma."""

from __future__ import annotations

import hmac
import os
import secrets as pysecrets
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from ..config import AppConfig, Secrets
from ..storage.db import Database
from ..types import SystemState, reason_it
from .views import Views

HERE = Path(__file__).parent
NAV = [
    ("/", "Panoramica"), ("/agenti", "Agenti"), ("/trader", "Trader"), ("/token", "Token"),
    ("/operazioni", "Operazioni"), ("/portafoglio", "Portafoglio"), ("/rischi", "Rischi"), ("/report", "Report"),
    ("/impostazioni", "Impostazioni"), ("/log", "Log"),
]
ACTIONS = {
    "pause": (SystemState.PAUSED, "PAUSA NUOVI ACQUISTI"),
    "resume": (SystemState.RUNNING, "RIPRENDI"),
    "stop": (SystemState.STOPPED, "ARRESTA"),
}


def usd(v: Any, digits: int = 2) -> str:
    if v is None:
        return "n/d"
    d = Decimal(str(v))
    s = f"{abs(d):,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("−" if d < 0 else "") + s + " $"


def num(v: Any, digits: int = 2) -> str:
    if v is None:
        return "n/d"
    s = f"{float(v):,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s


def pct(v: Any, digits: int = 1) -> str:
    if v is None:
        return "n/d"
    return num(float(v) * 100, digits) + "%"


def short(a: str | None, n: int = 6) -> str:
    if not a:
        return "—"
    return a if len(a) <= 2 * n else f"{a[:n]}…{a[-4:]}"


def _token(secrets: Secrets, data_dir: Path) -> str:
    if secrets.dashboard_token:
        return secrets.dashboard_token.get_secret_value()
    p = data_dir / "dashboard_token"
    if p.exists():
        return p.read_text().strip()
    tok = pysecrets.token_urlsafe(24)
    data_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(tok)
    os.chmod(p, 0o600)
    return tok


class LocalHostOnly(BaseHTTPMiddleware):
    """Blocca richieste con Host non locale (difesa da DNS rebinding)."""

    def __init__(self, app, allowed: set[str]):
        super().__init__(app)
        self.allowed = allowed

    async def dispatch(self, request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].strip("[]")
        if host not in self.allowed:
            return JSONResponse({"errore": "host non consentito"}, status_code=403)
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return resp


def create_app(db_path: str | Path, cfg: AppConfig, secrets: Secrets) -> FastAPI:
    db = Database(db_path)
    views = Views(db, cfg)
    token = _token(secrets, Path(cfg.data_dir))
    app = FastAPI(title="M4 CopyDesk", docs_url=None, redoc_url=None, openapi_url=None)
    allowed = {"127.0.0.1", "localhost", "::1", "testserver"}
    if cfg.dashboard.allow_non_local:
        allowed.add(cfg.dashboard.host)
    app.add_middleware(LocalHostOnly, allowed=allowed)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.filters.update(usd=usd, num=num, pct=pct, short=short, reason=reason_it)
    tpl.env.globals.update(nav=NAV)

    def render(request: Request, name: str, ctx: dict[str, Any], active: str) -> HTMLResponse:
        partial = request.query_params.get("partial") == "1"
        return tpl.TemplateResponse(request, name, {**ctx, "active": active, "partial": partial, "csrf": token})

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        return render(request, "overview.html", views.overview(), "/")

    @app.get("/agenti", response_class=HTMLResponse)
    def agents(request: Request):
        return render(request, "agents.html", views.agents(), "/agenti")

    @app.get("/trader", response_class=HTMLResponse)
    def traders(request: Request):
        return render(request, "traders.html", views.traders(), "/trader")

    @app.get("/token", response_class=HTMLResponse)
    def tokens(request: Request):
        return render(request, "tokens.html", views.tokens(), "/token")

    @app.get("/operazioni", response_class=HTMLResponse)
    def operations(request: Request, s: str | None = None):
        return render(request, "operations.html", views.operations(s), "/operazioni")

    @app.get("/portafoglio", response_class=HTMLResponse)
    def portfolio(request: Request, s: str | None = None):
        return render(request, "portfolio.html", views.portfolio(s), "/portafoglio")

    @app.get("/rischi", response_class=HTMLResponse)
    def risks(request: Request):
        return render(request, "risks.html", views.risks(), "/rischi")

    @app.get("/report", response_class=HTMLResponse)
    def reports(request: Request, id: int | None = None):
        return render(request, "reports.html", views.reports(id), "/report")

    @app.get("/impostazioni", response_class=HTMLResponse)
    def settings(request: Request):
        return render(request, "settings.html", views.settings(), "/impostazioni")

    @app.get("/log", response_class=HTMLResponse)
    def logs(request: Request, level: str | None = None):
        return render(request, "logs.html", views.logs(level), "/log")

    @app.get("/api/state")
    def state():
        return views.state_json()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/api/control/{action}")
    async def control(action: str, request: Request):
        if action not in ACTIONS:
            raise HTTPException(404, "azione sconosciuta")
        sent = request.headers.get("x-m4-token", "")
        if not hmac.compare_digest(sent, token):
            db.audit("dashboard", "control.denied", f"Tentativo {action} senza token valido", level="WARN")
            raise HTTPException(403, "token non valido")
        origin = request.headers.get("origin")
        if origin and (origin.split("//")[-1].split(":")[0].strip("[]") not in allowed):
            raise HTTPException(403, "origine non consentita")
        try:
            body = await request.json()
        except Exception:
            body = {}
        if body.get("confirm") is not True:
            raise HTTPException(400, "conferma esplicita richiesta")
        state_, label = ACTIONS[action]
        db.set_control("state", state_.value, "human", f"dashboard: {label}")
        return {"ok": True, "stato": state_.value, "azione": label}

    app.state.token = token
    app.state.db = db
    return app
