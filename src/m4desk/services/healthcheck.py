"""Controllo completo: verifica che tutto sia attivo e funzionante. Sola lettura: nessuna firma, nessun ordine.

Unica azione esterna: se le notifiche sono attive, invia una notifica di PROVA al telefono.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

from ..config import AppConfig, Secrets
from ..storage.db import AgentStatusRow, Database, EventRow, LeaderSlot, LiveOrder, ProviderUsage, TraderScore
from ..types import SOL_MINT, USDC_MINT

OK, WARN, ERR, INFO = "OK", "ATTENZIONE", "ERRORE", "INFO"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


@dataclass
class CheckItem:
    area: str
    name: str
    status: str
    detail: str = ""
    fix: str = ""


def _age(ts: datetime | None, now: datetime) -> float | None:
    return None if ts is None else (now - ts).total_seconds()


def _fmt_age(s: float | None) -> str:
    if s is None:
        return "mai"
    if s < 120:
        return f"{int(s)} s fa"
    if s < 7200:
        return f"{int(s // 60)} min fa"
    return f"{s / 3600:.1f} h fa"


def check_engine(db: Database, now: datetime) -> list[CheckItem]:
    out: list[CheckItem] = []
    with db.session() as s:
        agents = {a.name: a for a in s.scalars(select(AgentStatusRow))}
    sup = agents.get("supervisor")
    age = _age(sup.heartbeat_at, now) if sup else None
    running = sup is not None and sup.status == "RUNNING" and age is not None and age <= 60
    out.append(CheckItem("Motore", "Motore PAPER in esecuzione", OK if running else ERR,
                         f"ultimo battito {_fmt_age(age)}",
                         "" if running else "Avvia il motore: menu M4 → Avvia motore PAPER "
                                            "(o `m4desk run --mode paper`)"))
    if running:
        for name in ("discovery", "trader_analyst", "token_analyst", "strategy", "position_manager", "audit"):
            a = agents.get(name)
            if a is None:
                out.append(CheckItem("Motore", f"Agente {name}", ERR, "mai avviato", "Riavvia il motore"))
                continue
            st = WARN if a.errors else OK
            out.append(CheckItem("Motore", f"Agente {name}", st,
                                 f"{a.status}, elaborati {a.processed}, errori {a.errors}"
                                 + (f" — ultimo: {a.last_error[:120]}" if a.last_error else ""),
                                 "Errori occasionali di rete sono normali; se crescono vedi la pagina Log" if a.errors
                                 else ""))
    state = db.get_control("state", "RUNNING")
    st = {"RUNNING": OK, "PAUSED": WARN, "SAFE": ERR, "STOPPED": ERR}.get(state, WARN)
    fixes = {"PAUSED": "Premi RIPRENDI quando vuoi riattivare i nuovi acquisti",
             "SAFE": "Leggi la causa in Rischi/Log, risolvila e premi RIPRENDI",
             "STOPPED": "Premi RIPRENDI e avvia il motore"}
    out.append(CheckItem("Motore", "Stato del sistema", st, state, fixes.get(state, "")))
    return out


def check_database(db: Database, strategies: dict[str, str], starting_equity, max_positions: int) -> list[CheckItem]:
    from .ledger import Ledger
    from .safety import check_contracts

    out = []
    try:
        with db.engine.connect() as c:
            res = c.execute(text("PRAGMA quick_check")).scalar()
        db.set_meta("last_check_write", datetime.now(UTC).isoformat())
        out.append(CheckItem("Dati", "Database integro e scrivibile", OK if res == "ok" else ERR, str(res),
                             "" if res == "ok" else "Ferma il motore e ripristina un backup (docs/INCIDENTI)"))
    except Exception as e:
        out.append(CheckItem("Dati", "Database integro e scrivibile", ERR, type(e).__name__,
                             "Verifica spazio e permessi"))
        return out
    ledger = Ledger(db, starting_equity, 90)
    bad = []
    for sid in strategies:
        bad += [f"{sid}: {v}" for v in check_contracts(db, sid, starting_equity, max_positions, ledger.cash(sid))]
    out.append(CheckItem("Dati", "Invarianti contabili (tutte le strategie)", ERR if bad else OK,
                         "; ".join(bad)[:300] or "cassa, posizioni e P&L coerenti",
                         "Il sistema entra in modalità SICURA: vedi pagina Rischi" if bad else ""))
    return out


def check_activity(db: Database, cfg: AppConfig, now: datetime) -> list[CheckItem]:
    out = []
    with db.session() as s:
        n_w = len(db.list_wallets())
        n_scored = s.scalar(select(func.count(func.distinct(TraderScore.wallet))))
        n_elig = s.scalar(select(func.count(func.distinct(TraderScore.wallet))).where(TraderScore.status == "ELIGIBLE"))
        leaders = list(s.scalars(select(LeaderSlot).where(LeaderSlot.strategy == cfg.strategy.primary,
                                                          LeaderSlot.status == "SELECTED")))
        last_swap = s.scalar(select(func.max(EventRow.observed_at)).where(EventRow.type == "LEADER_SWAP"))
    seeds = n_w + len(cfg.seed_wallets)
    out.append(CheckItem("Trader", "Wallet in watchlist", OK if seeds else ERR, f"{n_w} wallet",
                         "" if seeds else "Aggiungi un indirizzo pubblico: menu M4 → Aggiungi wallet pubblico…"))
    out.append(CheckItem("Trader", "Trader valutati", OK if n_scored else WARN, f"{n_scored} valutati, {n_elig} idonei",
                         "" if n_scored else "Normale nelle prime ore: lo storico arriva a piccoli passi "
                                             "(RPC gratuito)"))
    out.append(CheckItem("Trader", f"Leader seguiti ({cfg.strategy.primary})", OK if leaders else WARN,
                         f"{len(leaders)} su {cfg.risk.max_leaders}",
                         "" if leaders else "Nessun trader supera ancora le soglie: niente da copiare finché "
                                            "non ce n'è uno idoneo (è un comportamento voluto)"))
    out.append(CheckItem("Trader", "Ultimo swap di un wallet seguito", INFO,
                         _fmt_age(_age(last_swap, now)), ""))
    return out


async def check_providers(cfg: AppConfig, secrets: Secrets, db: Database) -> list[CheckItem]:
    from ..clock import Clock
    from ..providers.budget import ProviderGate
    from ..providers.dexscreener import DexScreener
    from ..providers.http import HttpJson
    from ..providers.jupiter import JupiterQuotes
    from ..providers.rugcheck import RugcheckClient
    from ..providers.solana_rpc import SolanaRpc
    from .llm import OllamaBackend

    out = []
    pc, clock, t = cfg.providers, Clock(), cfg.providers.http_timeout_s
    gates = {n: ProviderGate(n, b, db, clock) for n, b in pc.budget.items()}
    helius = secrets.helius_api_key.get_secret_value() if (pc.use_helius and secrets.helius_api_key) else None
    rpc = SolanaRpc(HttpJson("rpc", gates["rpc"], t), pc.helius_http_url if helius else pc.rpc_http_url, helius)
    jkey = secrets.jupiter_api_key.get_secret_value() if secrets.jupiter_api_key else None
    probes = [
        ("RPC Solana" + (" (Helius)" if helius else " (pubblico)"), lambda: rpc.get_mint_info(USDC_MINT),
         "Con l'RPC pubblico i 429 sono frequenti: usa una chiave gratuita Helius (HELIUS_API_KEY)"),
        ("DEX Screener (prezzi e liquidità)",
         lambda: DexScreener(HttpJson("dexscreener", gates["dexscreener"], t), pc.dexscreener_url).get_market(SOL_MINT),
         "Riprova tra qualche minuto; se persiste il servizio potrebbe aver cambiato API"),
        ("Jupiter (quotazioni)",
         lambda: JupiterQuotes(HttpJson("jupiter", gates["jupiter"], t, {"x-api-key": jkey} if jkey else None),
                               pc.jupiter_url).quote(SOL_MINT, USDC_MINT, 10**7),
         "Riprova; una chiave gratuita Jupiter (JUPITER_API_KEY) alza i limiti"),
    ]
    if pc.rugcheck_enabled:
        probes.append(("Rugcheck (sicurezza token)",
                       lambda: RugcheckClient(HttpJson("rugcheck", gates["rugcheck"], t),
                                              pc.rugcheck_url).get_report(BONK),
                       "Senza Rugcheck i nuovi acquisti restano bloccati (scelta di sicurezza)"))
    else:
        out.append(CheckItem("Provider", "Rugcheck", WARN, "disabilitato: SOLO OSSERVAZIONE, nessun acquisto",
                             "providers.rugcheck_enabled: true in config.yaml"))
    for name, fn, fix in probes:
        try:
            await fn()
            out.append(CheckItem("Provider", name, OK, "risponde correttamente (prova reale)"))
        except Exception as e:
            out.append(CheckItem("Provider", name, ERR, str(e)[:160], fix))
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    with db.session() as s:
        usage = {u.provider: u for u in s.scalars(select(ProviderUsage).where(ProviderUsage.day == day))}
    for name, b in pc.budget.items():
        u = usage.get(name)
        req, r429 = (u.requests, u.rate_limited) if u else (0, 0)
        st = ERR if req >= b.daily_requests else WARN if (r429 > 20 or req > 0.8 * b.daily_requests) else OK
        out.append(CheckItem("Provider", f"Budget {name} oggi", st, f"{req}/{b.daily_requests} richieste, 429: {r429}",
                             "" if st == OK else "Riduci discovery.max_candidates o usa Helius free"))
    if cfg.llm.enabled:
        good, msg = await OllamaBackend(cfg.llm).ping()
        out.append(CheckItem("Provider", f"IA locale Ollama ({cfg.llm.model})", OK if good else WARN, msg,
                             "" if good else "Facoltativa: apri Ollama ed esegui `ollama pull qwen3.5:9b`; senza IA si "
                                             "usa la classifica deterministica"))
    else:
        out.append(CheckItem("Provider", "IA locale Ollama", INFO, "disabilitata in config.yaml"))
    return out


async def check_live(cfg: AppConfig, secrets: Secrets, db: Database) -> list[CheckItem]:
    if not cfg.live_enabled:
        return [CheckItem("Live", "Esecuzione reale", INFO, "spenta (solo PAPER)")]
    from ..clock import Clock
    from ..live import build_live

    out = []
    live = build_live(cfg, db, Clock(), secrets, None)  # sessione del motore, nessuna firma
    for name, ok, detail in await live.preflight():
        out.append(CheckItem("Live", name, OK if ok else ERR, detail[:300], "" if ok else "Vedi il dettaglio"))
    armed = live.is_armed()
    out.append(CheckItem("Live", "Live armato nel motore", OK if armed else WARN,
                         "ARMATO: opera con soldi veri" if armed else "disarmato: nessun acquisto reale",
                         "" if armed else "Se vuoi operare: ARMA LIVE (dopo ogni avvio del motore)"))
    with db.session() as s:
        unknown = s.scalar(select(func.count()).select_from(LiveOrder).where(LiveOrder.state == "UNKNOWN"))
    pending = [p.id for p in live.open_positions() if db.get_checkpoint(f"live_pending_exit:{p.id}")]
    out.append(CheckItem("Live", "Vendite reali in sospeso", ERR if pending else OK,
                         f"{len(pending)} posizioni da chiudere" if pending else "nessuna",
                         "Il motore ritenta ogni minuto; controlla saldo SOL e pagina Live" if pending else ""))
    out.append(CheckItem("Live", "Ordini reali con esito da verificare", WARN if unknown else OK, f"{unknown}",
                         "Controlla le firme su Solscan dalla pagina Live" if unknown else ""))
    return out


async def check_notify(cfg: AppConfig, secrets: Secrets, send_test: bool) -> list[CheckItem]:
    from .notify import Notifier

    topic = secrets.ntfy_topic.get_secret_value() if secrets.ntfy_topic else None
    n = Notifier(cfg.notify, topic)
    if not n.enabled:
        st = WARN if cfg.live_enabled else INFO
        return [CheckItem("Notifiche", "Notifiche sul telefono (ntfy)", st, "spente",
                          "notify.enabled: true in config.yaml e iscriviti al topic NTFY_TOPIC nell'app ntfy")]
    if not send_test:
        return [CheckItem("Notifiche", "Notifiche sul telefono (ntfy)", OK, "attive (prova non inviata)")]
    ok = await n.send("M4 CopyDesk: notifica di prova", "Controllo completo: se leggi questo messaggio le notifiche "
                      "funzionano.", "low", "white_check_mark")
    return [CheckItem("Notifiche", "Notifica di prova inviata", OK if ok else ERR,
                      "controlla che sia arrivata sul telefono" if ok else "invio fallito",
                      "" if ok else "Verifica la connessione e il server ntfy in config.yaml")]


def check_system(cfg: AppConfig, engine_running: bool, data_path: Path) -> list[CheckItem]:
    out = []
    free = shutil.disk_usage(data_path.resolve()).free / 1e9
    out.append(CheckItem("Mac", "Spazio libero su disco", OK if free > 2 else WARN if free > 0.5 else ERR,
                         f"{free:.1f} GB", "" if free > 2 else "Libera spazio: il database cresce nel tempo"))
    if sys.platform == "darwin":
        try:
            res = subprocess.run(["/usr/bin/pmset", "-g", "assertions"], capture_output=True, text=True, timeout=5,
                                 check=False).stdout
            awake = "caffeinate" in res and "PreventUserIdleSystemSleep" in res
        except Exception:
            awake = False
        st = OK if awake or not engine_running else WARN
        out.append(CheckItem("Mac", "Sospensione per inattività bloccata", st,
                             "attivo (caffeinate)" if awake else "non attivo",
                             "" if st == OK else "Avvia il motore dall'app (attiva caffeinate) o imposta Energia → "
                                                 "Impedisci lo stop automatico"))
    return out


async def run_full_check(cfg: AppConfig, secrets: Secrets, db: Database, send_test_notification: bool = True
                         ) -> dict[str, Any]:
    now = datetime.now(UTC)
    items = check_engine(db, now)
    engine_running = items[0].status == OK
    items += check_database(db, db.get_meta("strategies") or {}, cfg.risk.starting_equity_usd, cfg.risk.max_positions)
    items += check_activity(db, cfg, now)
    items += await check_providers(cfg, secrets, db)
    items += await check_live(cfg, secrets, db)
    items += await check_notify(cfg, secrets, send_test_notification)
    items += check_system(cfg, engine_running, db.path.parent)
    worst = ERR if any(i.status == ERR for i in items) else WARN if any(i.status == WARN for i in items) else OK
    verdict = {OK: "TUTTO OK: sistema attivo e funzionante", WARN: "FUNZIONANTE, con avvisi da guardare",
               ERR: "PROBLEMI DA RISOLVERE"}[worst]
    result = {"at": now.isoformat(), "status": worst, "verdict": verdict, "items": [asdict(i) for i in items],
              "counts": {s: sum(1 for i in items if i.status == s) for s in (OK, WARN, ERR, INFO)}}
    db.set_meta("last_check", result)
    db.audit("check", "check.full", f"Controllo completo: {verdict}", result["counts"],
             "WARN" if worst == ERR else "INFO")
    return result
