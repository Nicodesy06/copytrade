"""App FastAPI della dashboard. Solo 127.0.0.1; azioni di scrittura protette da token + conferma."""

from __future__ import annotations

import hmac
import os
import secrets as pysecrets
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
    ("/impostazioni", "Impostazioni"), ("/log", "Log"), ("/live", "Live"),
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


DB_LABELS = {"paper": "PAPER (mainnet)", "demo": "DEMO (simulato)", "replay": "REPLAY"}


def create_app(db_paths: str | Path | dict[str, Path], cfg: AppConfig, secrets: Secrets) -> FastAPI:
    """`db_paths` può essere un solo DB o più DB selezionabili (es. paper + demo nell'app macOS)."""
    paths = db_paths if isinstance(db_paths, dict) else {"default": Path(db_paths)}
    dbs = {name: Database(p) for name, p in paths.items()}
    views_by_db = {name: Views(d, cfg) for name, d in dbs.items()}
    first = next(iter(dbs))
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

    def current(request: Request) -> str:
        name = request.cookies.get("m4db", first)
        return name if name in dbs else first

    def V(request: Request) -> Views:  # noqa: N802
        return views_by_db[current(request)]

    def render(request: Request, name: str, ctx: dict[str, Any], active: str) -> HTMLResponse:
        partial = request.query_params.get("partial") == "1"
        choices = [(n, DB_LABELS.get(n, n)) for n in dbs] if len(dbs) > 1 else []
        return tpl.TemplateResponse(request, name, {**ctx, "active": active, "partial": partial, "csrf": token,
                                                    "db_choices": choices, "db_current": current(request)})

    @app.get("/db/{name}")
    def choose_db(name: str):
        if name not in dbs:
            raise HTTPException(404, "database sconosciuto")
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("m4db", name, httponly=True, samesite="strict")
        return resp

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        return render(request, "overview.html", V(request).overview(), "/")

    @app.get("/agenti", response_class=HTMLResponse)
    def agents(request: Request):
        return render(request, "agents.html", V(request).agents(), "/agenti")

    @app.get("/trader", response_class=HTMLResponse)
    def traders(request: Request):
        return render(request, "traders.html", V(request).traders(), "/trader")

    @app.get("/token", response_class=HTMLResponse)
    def tokens(request: Request):
        return render(request, "tokens.html", V(request).tokens(), "/token")

    @app.get("/operazioni", response_class=HTMLResponse)
    def operations(request: Request, s: str | None = None):
        return render(request, "operations.html", V(request).operations(s), "/operazioni")

    @app.get("/portafoglio", response_class=HTMLResponse)
    def portfolio(request: Request, s: str | None = None):
        return render(request, "portfolio.html", V(request).portfolio(s), "/portafoglio")

    @app.get("/rischi", response_class=HTMLResponse)
    def risks(request: Request):
        return render(request, "risks.html", V(request).risks(), "/rischi")

    @app.get("/report", response_class=HTMLResponse)
    def reports(request: Request, id: int | None = None):
        return render(request, "reports.html", V(request).reports(id), "/report")

    @app.get("/impostazioni", response_class=HTMLResponse)
    def settings(request: Request):
        return render(request, "settings.html", V(request).settings(), "/impostazioni")

    @app.get("/log", response_class=HTMLResponse)
    def logs(request: Request, level: str | None = None):
        return render(request, "logs.html", V(request).logs(level), "/log")

    @app.get("/controllo", response_class=HTMLResponse)
    def check_page(request: Request):
        d = dbs.get("paper") or dbs.get("default") or dbs[first]
        ctx = {**V(request).common(), "last": d.get_meta("last_check")}
        return render(request, "check.html", ctx, "/controllo")

    @app.post("/api/check")
    async def run_check(request: Request):
        _check_write(request)
        from ..services.healthcheck import run_full_check

        d = dbs.get("paper") or dbs.get("default") or dbs[first]
        return await run_full_check(cfg, secrets, d, send_test_notification=True)

    @app.get("/live", response_class=HTMLResponse)
    def live_page(request: Request):
        return render(request, "live.html", V(request).live(), "/live")

    def _check_write(request: Request) -> None:
        if not hmac.compare_digest(request.headers.get("x-m4-token", ""), token):
            raise HTTPException(403, "token non valido")
        origin = request.headers.get("origin")
        if origin and (origin.split("//")[-1].split(":")[0].strip("[]") not in allowed):
            raise HTTPException(403, "origine non consentita")

    def _live_for_paper():
        if "paper" not in dbs and "default" not in dbs:
            raise HTTPException(400, "il live riguarda solo il database PAPER")
        if not cfg.live_enabled:
            raise HTTPException(400, "live_enabled è false in config.yaml")
        from ..clock import Clock
        from ..live import build_live

        d = dbs.get("paper") or dbs["default"]
        live = build_live(cfg, d, Clock(), secrets, None)  # usa la sessione registrata dal motore
        if not live.boot_id:
            raise HTTPException(409, "il motore PAPER non è in esecuzione")
        return live

    @app.post("/api/live/arm")
    async def live_arm(request: Request):
        _check_write(request)
        body = await request.json()
        ok, msg = await _live_for_paper().arm(str(body.get("confirm", "")), "human:dashboard")
        if not ok:
            raise HTTPException(400, msg)
        return {"ok": True, "messaggio": msg}

    @app.post("/api/live/disarm")
    async def live_disarm(request: Request):
        _check_write(request)
        _live_for_paper().disarm("dashboard", "human:dashboard")
        return {"ok": True}

    @app.get("/api/state")
    def state(request: Request):
        return V(request).state_json()

    @app.get("/health")
    def health():
        return {"ok": True, "app": "m4desk"}

    @app.post("/api/control/{action}")
    async def control(action: str, request: Request):
        db = dbs[current(request)]
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
    app.state.db = dbs[first]
    app.state.dbs = dbs
    return app
