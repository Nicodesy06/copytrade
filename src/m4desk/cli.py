"""CLI `m4desk`. `m4desk --help` è la fonte definitiva dei comandi disponibili."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .config import AppConfig, ensure_env_secrets, load_config, load_secrets, set_yaml_value
from .types import Mode, SystemState

BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
ROOT_FILES = Path(__file__).resolve().parents[2]


def _cfg(args) -> AppConfig:
    try:
        return load_config(getattr(args, "config", None))
    except Exception as e:
        raise SystemExit(f"Configurazione non valida: {e}") from e


def _db_path(cfg: AppConfig, which: str | None) -> Path:
    which = which or "paper"
    if which in ("paper", "demo", "replay"):
        return Path(cfg.data_dir) / f"{which}.db"
    return Path(which)


def _open_db(cfg: AppConfig, which: str | None, must_exist: bool = True):
    from .storage.db import Database

    p = _db_path(cfg, which)
    if must_exist and not p.exists():
        raise SystemExit(f"Database {p} inesistente. Esegui prima `m4desk demo` o `m4desk run --mode paper`.")
    return Database(p)


def _ledger(cfg: AppConfig, db):
    from .services.ledger import Ledger

    return Ledger(db, cfg.risk.starting_equity_usd, cfg.risk.max_price_age_s)


# ------------------------------------------------------------------------------------------ doctor
def cmd_doctor(args) -> int:
    ok = True

    def line(status: str, msg: str) -> None:
        nonlocal ok
        if status == "ERRORE":
            ok = False
        print(f"[{status:^7}] {msg}")

    v = sys.version_info
    line("OK" if v >= (3, 12) else "ERRORE", f"Python {v.major}.{v.minor}.{v.micro} (richiesto ≥ 3.12)")
    for mod in ("fastapi", "uvicorn", "httpx", "websockets", "pydantic", "pydantic_settings", "sqlalchemy", "jinja2",
                "yaml"):
        try:
            __import__(mod)
            line("OK", f"dipendenza {mod}")
        except ImportError:
            line("ERRORE", f"dipendenza mancante: {mod} (esegui pip install -e '.[dev]')")
    try:
        cfg = load_config(args.config)
        default_src = "config.yaml" if Path("config.yaml").exists() else "valori predefiniti, nessun config.yaml"
        src = args.config or default_src
        line("OK", f"configurazione valida ({src}; versione strategia {cfg.strategy_version()})")
    except Exception as e:
        line("ERRORE", f"configurazione non valida: {e}")
        return 1
    line("OK" if not cfg.live_enabled else "ERRORE", "live_enabled=false (esecuzione reale disabilitata)")
    line("OK" if cfg.dashboard.host in ("127.0.0.1", "localhost", "::1") else "ATTENZ.",
         f"dashboard su {cfg.dashboard.host}:{cfg.dashboard.port}")
    data = Path(cfg.data_dir)
    try:
        data.mkdir(parents=True, exist_ok=True)
        (data / ".w").write_text("x")
        (data / ".w").unlink()
        line("OK", f"cartella dati scrivibile: {data.resolve()}")
    except OSError as e:
        line("ERRORE", f"cartella dati non scrivibile: {e}")
    env = Path(".env")
    if env.exists():
        mode = env.stat().st_mode & 0o777
        line("OK" if mode & 0o077 == 0 else "ATTENZ.", f".env presente (permessi {oct(mode)}; consigliato 0o600)")
    else:
        line("INFO", ".env assente: ok per DEMO; per PAPER esegui `m4desk init`")
    gi = Path(".gitignore")
    if gi.exists():
        txt = gi.read_text()
        line("OK" if ".env" in txt and "config.yaml" in txt else "ATTENZ.", ".gitignore esclude .env e config.yaml")
    s = load_secrets()
    line("INFO", f"HELIUS_API_KEY {'impostata' if s.helius_api_key else 'non impostata (opzionale)'}; "
                 f"JUPITER_API_KEY {'impostata' if s.jupiter_api_key else 'non impostata (keyless)'}")
    if args.online or cfg.llm.enabled:
        from .services.llm import OllamaBackend

        good, msg = asyncio.run(OllamaBackend(cfg.llm).ping())
        line("OK" if good else "INFO", f"LLM locale {cfg.llm.model}: {msg} (opzionale: senza LLM si usa la baseline)")
    if shutil.which("caffeinate"):
        line("INFO", "macOS: per evitare lo stop in sospensione usa `caffeinate -dims m4desk run --mode paper`")
    from .engine import DEFAULT_FIXTURE

    line("OK" if DEFAULT_FIXTURE.exists() else "ERRORE", f"fixture demo: {DEFAULT_FIXTURE.name}")
    print("\nEsito:", "TUTTO OK" if ok else "CI SONO ERRORI")
    return 0 if ok else 1


# ------------------------------------------------------------------------------------------ init
def cmd_init(args) -> int:
    cwd = Path.cwd()
    for src, dst in (("config.example.yaml", "config.yaml"), (".env.example", ".env")):
        s, d = cwd / src, cwd / dst
        if not s.exists():
            s = ROOT_FILES / src
        if not s.exists():  # installazione da pacchetto/app: copie incluse nel pacchetto
            s = Path(__file__).parent / "data" / ("env.example" if src == ".env.example" else src)
        if d.exists():
            print(f"{dst} esiste già: non sovrascritto")
        elif s.exists():
            shutil.copy(s, d)
            print(f"creato {dst} da {src}")
        else:
            print(f"ATTENZIONE: {src} non trovato")
    env = cwd / ".env"
    if env.exists():
        for name in ensure_env_secrets(env):
            print(f"generato {name} in .env")
    cfg = _cfg(args)
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    _open_db(cfg, "paper", must_exist=False)
    print(f"database PAPER pronto in {_db_path(cfg, 'paper')}")
    print("Prossimi passi: `m4desk doctor`, `m4desk demo`, poi `m4desk wallets add <INDIRIZZO_PUBBLICO>`.")
    return 0


# ------------------------------------------------------------------------------------------ demo / replay
def _run_fixture(args, fixture: Path, which: str, mode: Mode) -> int:
    from .engine import build_fixture_engine, run_replay
    from .services.reporting import strategy_stats

    cfg = _cfg(args)
    db_path = _db_path(cfg, which)
    rt, world, sup = build_fixture_engine(cfg, fixture, db_path, mode)
    print(f"▶ {mode.value.upper()} — {world.meta['label']} — {world.meta.get('name', fixture.name)}")
    print("  Nessuna rete, nessuna chiave, nessuna firma: provider fixture deterministici.\n")
    counts = asyncio.run(run_replay(rt, world, sup, pace_s=args.pace))
    now = rt.clock.now()
    funnel = sup.discovery.funnel()
    print(f"Scoperta: seed {funnel['seed']} → token scansionati {funnel['token_scans']} → "
          f"candidati {funnel['candidates']}")
    dups = rt.bus.duplicates_by_type["LEADER_SWAP"]
    print(f"Eventi swap osservati: {counts['swaps']} · firme duplicate scartate: {dups}\n")
    print(f"{'Strategia':<10} {'Equity':>10} {'Realizz.':>9} {'Chiuse':>6} {'Win%':>6} {'Ordini':<32}")
    for sid in rt.strategies:
        st = strategy_stats(rt.db, rt.ledger, sid, now)
        wr = f"{st['win_rate']:.0%}" if st["win_rate"] is not None else "n/d"
        print(f"{sid:<10} {st['equity_estimate']:>10} {st['realized']:>9} {st['closed']:>6} {wr:>6} {st['orders']}")
    print(f"\nP&L SIMULATO su capitale virtuale ({world.meta['label']}): non è un risultato reale.")
    print(f"Report: {Path(cfg.data_dir) / 'reports'} · DB: {db_path}")
    print(f"Dashboard: m4desk dashboard --db {which}  (http://{cfg.dashboard.host}:{cfg.dashboard.port})")
    if args.serve:
        return _serve(cfg, db_path, cfg.dashboard.host, cfg.dashboard.port)
    return 0


def cmd_demo(args) -> int:
    from .engine import DEFAULT_FIXTURE

    return _run_fixture(args, DEFAULT_FIXTURE, "demo", Mode.DEMO)


def cmd_replay(args) -> int:
    p = Path(args.fixture)
    if not p.exists():
        raise SystemExit(f"Fixture {p} inesistente")
    return _run_fixture(args, p, "replay", Mode.REPLAY)


# ------------------------------------------------------------------------------------------ wallets
def cmd_wallets(args) -> int:
    cfg = _cfg(args)
    db = _open_db(cfg, args.db, must_exist=False)
    if args.wallets_cmd == "add":
        a = args.address.strip()
        if not BASE58.match(a):
            raise SystemExit("Indirizzo non valido: serve un indirizzo PUBBLICO Solana (base58, 32-44 caratteri). "
                             "Non inserire MAI chiavi private o seed phrase.")
        added = db.add_wallet(a, "manual", args.label)
        db.audit("human", "wallet.add", f"Wallet pubblico aggiunto: {a}" if added else f"Già presente: {a}")
        print("aggiunto" if added else "già presente", a)
        return 0
    rows = db.list_wallets()
    if not rows:
        print("Nessun wallet. Aggiungine uno con `m4desk wallets add <INDIRIZZO_PUBBLICO>`.")
    for w in rows:
        print(f"{w.address}  {w.source:<9} {w.label or '':<28} {w.added_at:%Y-%m-%d %H:%M}")
    return 0


# ------------------------------------------------------------------------------------------ run paper
def cmd_run(args) -> int:
    if args.mode != "paper":
        raise SystemExit("Solo `--mode paper` è supportato. Per la simulazione offline usa `m4desk demo`. "
                         "La modalità live NON è implementata (vedi docs/LIVE_CHECKLIST.md).")
    cfg = _cfg(args)
    from .engine import build_paper_engine, run_paper

    rt, sup = build_paper_engine(cfg, load_secrets(), _db_path(cfg, "paper"))
    if rt.system_state() == SystemState.STOPPED:
        rt.set_state(SystemState.RUNNING, "human", "avvio da CLI `m4desk run`")
    print("▶ PAPER mainnet READ-ONLY: nessuna transazione, nessuna firma, P&L virtuale.")
    print(f"  RPC: {rt.db.get_meta('rpc')} · LLM: {rt.db.get_meta('llm_backend')}")
    if rt.watch_only():
        print("  ATTENZIONE: Rugcheck disabilitato → modalità SOLO OSSERVAZIONE (nessun nuovo acquisto).")
    if not rt.db.list_wallets() and not cfg.seed_wallets:
        print("  Nessun wallet seed: aggiungi indirizzi pubblici con `m4desk wallets add` "
              "(la scoperta parte dai seed).")

    async def main() -> None:
        tasks = [asyncio.create_task(run_paper(rt, sup))]
        if args.with_dashboard:
            tasks.append(asyncio.create_task(_serve_async(cfg, _db_path(cfg, "paper"), cfg.dashboard.host,
                                                          cfg.dashboard.port)))
        await asyncio.gather(*tasks)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrotto (Ctrl-C). Lo stato è persistito; al riavvio gli ordini incompleti vengono annullati.")
    return 0


# ------------------------------------------------------------------------------------------ dashboard
async def _serve_async(cfg: AppConfig, db_path: Path, host: str, port: int) -> None:
    import uvicorn

    from .web.app import create_app

    app = create_app(db_path, cfg, load_secrets())
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    await server.serve()


def _serve(cfg: AppConfig, db_path: Path, host: str, port: int) -> int:
    if host not in ("127.0.0.1", "localhost", "::1") and not cfg.dashboard.allow_non_local:
        raise SystemExit("Per sicurezza la dashboard ascolta solo su 127.0.0.1 (vedi dashboard.allow_non_local).")
    print(f"Dashboard M4 CopyDesk su http://{host}:{port} (DB {db_path}) — Ctrl-C per uscire")
    try:
        asyncio.run(_serve_async(cfg, db_path, host, port))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_dashboard(args) -> int:
    cfg = _cfg(args)
    p = _db_path(cfg, args.db)
    if not p.exists():
        _open_db(cfg, args.db, must_exist=False)
    return _serve(cfg, p, args.host or cfg.dashboard.host, args.port or cfg.dashboard.port)


# ------------------------------------------------------------------------------------------ controllo
def _control(args, state: SystemState, label: str) -> int:
    cfg = _cfg(args)
    db = _open_db(cfg, args.db, must_exist=False)
    db.set_control("state", state.value, "human", f"CLI: {label}")
    print(f"Stato impostato a {state.value} ({label}). Registrato nell'audit log.")
    return 0


def cmd_pause(args) -> int:
    return _control(args, SystemState.PAUSED, "PAUSA NUOVI ACQUISTI")


def cmd_resume(args) -> int:
    return _control(args, SystemState.RUNNING, "RIPRENDI")


def cmd_stop(args) -> int:
    return _control(args, SystemState.STOPPED, "ARRESTA")


def cmd_status(args) -> int:
    from sqlalchemy import select

    from .storage.db import AgentStatusRow

    cfg = _cfg(args)
    db = _open_db(cfg, args.db)
    ledger = _ledger(cfg, db)
    now = datetime.now(UTC)
    print(f"Modalità: {db.get_meta('mode', '?')} · Origine dati: {db.get_meta('origin', '?')}")
    print(f"Stato sistema: {db.get_control('state', 'RUNNING')} · "
          f"Versione strategia: {db.get_meta('strategy_version')}")
    with db.session() as s:
        agents = list(s.scalars(select(AgentStatusRow).order_by(AgentStatusRow.name)))
    print("\nAgenti:")
    for a in agents:
        age = int((now - a.heartbeat_at).total_seconds())
        print(f"  {a.name:<17} {a.status:<8} heartbeat {age}s fa · elaborati {a.processed} · errori {a.errors}")
    print("\nStrategie (P&L simulato):")
    for sid in (db.get_meta("strategies") or {}):
        v = ledger.view(sid, now)
        print(f"  {sid:<8} equity stimata {v.equity_estimate:.2f} USD · cassa {v.cash:.2f} · posizioni aperte "
              f"{v.open_positions} (prezzi stantii {v.stale_positions})")
    return 0


def cmd_report(args) -> int:
    from sqlalchemy import select

    from .services.reporting import build_daily_report
    from .storage.db import Report

    cfg = _cfg(args)
    db = _open_db(cfg, args.db)
    if args.generate:
        now = datetime.now(UTC)
        md, _ = build_daily_report(db, _ledger(cfg, db), db.get_meta("strategies") or {},
                                   db.get_meta("primary_strategy", "switch2"), args.day or now.strftime("%Y-%m-%d"),
                                   now, db.get_meta("origin", "?"), db.get_meta("strategy_version", "?"))
        print(md)
        return 0
    with db.session() as s:
        stmt = select(Report).order_by(Report.id.desc())
        if args.day:
            stmt = select(Report).where(Report.day == args.day).order_by(Report.id.desc())
        r = s.scalars(stmt).first()
    if r is None:
        print("Nessun report salvato. Usa `m4desk report --generate`.")
        return 1
    print(r.body_md)
    return 0


def cmd_budget(args) -> int:
    from sqlalchemy import select

    from .storage.db import ProviderUsage

    cfg = _cfg(args)
    db = _open_db(cfg, args.db, must_exist=False)
    with db.session() as s:
        rows = list(s.scalars(select(ProviderUsage).order_by(ProviderUsage.day.desc(), ProviderUsage.provider)))
    print("Budget gratuiti configurati (richieste giorno / mese / minuto):")
    for name, b in cfg.providers.budget.items():
        print(f"  {name:<12} {b.daily_requests:>7} / {b.monthly_requests:>8} / {b.per_minute:>4}")
    print("\nConsumo registrato:")
    if not rows:
        print("  nessuna chiamata esterna registrata")
    for r in rows[:40]:
        print(f"  {r.day} {r.provider:<12} richieste {r.requests:>6} · 429 {r.rate_limited:>3} · errori {r.errors:>3} "
              f"· cache {r.cache_hits:>5} · vicino quota {r.near_quota_events}")
    print("\nA budget esaurito si fermano scoperta e nuovi acquisti: nessun upgrade a pagamento.")
    return 0


def cmd_test_providers(args) -> int:
    """Probe di rete esplicito (unico comando, insieme a `run`, che contatta Internet)."""
    from .engine import build_paper_engine

    cfg = _cfg(args)
    tmp = Path(cfg.data_dir) / "provider_probe.db"
    rt, _ = build_paper_engine(cfg, load_secrets(), tmp)
    bonk = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    from .types import SOL_MINT, USDC_MINT

    async def probe() -> int:
        bad = 0
        checks = [
            ("Solana RPC getAccountInfo", lambda: rt.providers.mint.get_mint_info(bonk)),
            ("DEX Screener /tokens/v1", lambda: rt.providers.market.get_market(SOL_MINT)),
            ("Jupiter /swap/v2/order (senza taker)", lambda: rt.providers.quotes.quote(SOL_MINT, USDC_MINT, 10**8)),
        ]
        if rt.providers.rug is not None:
            checks.append(("Rugcheck report+summary", lambda: rt.providers.rug.get_report(bonk)))
        else:
            print("[DISABIL.] Rugcheck: disabilitato in config → SOLO OSSERVAZIONE")
        for label, fn in checks:
            try:
                res = await fn()
                summary = {k: getattr(res, k) for k in ("price_usd", "liquidity_usd", "out_amount_raw",
                                                         "price_impact_pct", "score_normalised", "lp_locked_pct",
                                                         "mint_authority", "program") if hasattr(res, k)}
                print(f"[  OK   ] {label}: {summary}")
            except Exception as e:
                bad += 1
                print(f"[ERRORE ] {label}: {e} → connettore da considerare NON disponibile")
        from .services.llm import OllamaBackend

        good, msg = await OllamaBackend(cfg.llm).ping()
        print(f"[{'  OK   ' if good else ' INFO  '}] Ollama locale: {msg}")
        return bad

    bad = asyncio.run(probe())
    print("\nNessuna firma, nessuna transazione: solo letture pubbliche gratuite.")
    return 1 if bad else 0


# ------------------------------------------------------------------------------------------ check
def cmd_check(args) -> int:
    from .services.healthcheck import run_full_check

    cfg = _cfg(args)
    db = _open_db(cfg, args.db, must_exist=False)
    print("Controllo completo in corso (prove reali sui provider, nessuna firma, nessun ordine)…\n")
    res = asyncio.run(run_full_check(cfg, load_secrets(), db, send_test_notification=not args.no_notify))
    area = None
    for i in res["items"]:
        if i["area"] != area:
            area = i["area"]
            print(f"\n{area}")
        print(f"  [{i['status']:^10}] {i['name']}: {i['detail']}")
        if i["fix"]:
            print(f"               → {i['fix']}")
    c = res["counts"]
    print(f"\nEsito: {res['verdict']} (OK {c['OK']} · attenzione {c['ATTENZIONE']} · errori {c['ERRORE']})")
    return 1 if res["status"] == "ERRORE" else 0


# ------------------------------------------------------------------------------------------ live
LIVE_WARNING = """
ATTENZIONE — ESECUZIONE REALE
Una volta armato, il bot compra e vende DA SOLO con soldi veri dal wallet indicato, entro i limiti
di config.yaml (live.max_sol_per_trade, live.max_sol_per_day). Puoi perdere l'intero importo
impiegato. Nessun rendimento è garantito. Si disarma da solo a ogni riavvio del motore, in modalità
SICURA o dopo errori ripetuti; le vendite protettive restano attive.
"""


def _live_executor(cfg: AppConfig, db_which: str = "paper"):
    from .clock import Clock
    from .live import build_live
    from .services.notify import Notifier

    secrets = load_secrets()
    db = _open_db(cfg, db_which, must_exist=False)
    topic = secrets.ntfy_topic.get_secret_value() if secrets.ntfy_topic else None
    # engine=False: si agisce sulla sessione registrata dal motore in esecuzione
    live = build_live(cfg, db, Clock(), secrets, Notifier(cfg.notify, topic))
    return live, db


def cmd_live(args) -> int:
    from .live import CONFIRM_PHRASE, KeyStore, KeyStoreError

    cfg = _cfg(args)
    c = args.live_cmd
    if c == "import-key":
        import getpass

        print(LIVE_WARNING)
        print("La chiave privata viene letta SENZA mostrarla, salvata SOLO nel Portachiavi di macOS e mai in file,")
        print("log o chat. Non incollarla mai altrove. Una seed phrase NON è accettata: serve la chiave privata.")
        ks = KeyStore()
        ok, info = ks.available()
        if not ok:
            raise SystemExit(f"Portachiavi non disponibile: {info}")
        secret = getpass.getpass("Chiave privata (base58 o array JSON, input nascosto): ")
        try:
            pub = ks.store(secret)
        except KeyStoreError as e:
            raise SystemExit(f"Chiave non valida: {e}") from e
        print(f"\nChiave salvata. Indirizzo PUBBLICO: {pub}")
        print("Verifica che coincida con l'indirizzo Solana mostrato nell'app Fomo (Ricevi/Deposita).")
        cfg_path = Path(args.config or "config.yaml")
        question = "Scrivo questo indirizzo in config.yaml (live.wallet_pubkey)? [s/N] "
        if cfg_path.exists() and input(question).strip().lower() == "s":
            set_yaml_value(cfg_path, "live.wallet_pubkey", pub)
            print("Fatto. Per abilitare il live imposta anche live_enabled: true e network: solana-mainnet.")
        return 0
    if c == "use-key-address":
        pub = KeyStore().pubkey()
        if not pub:
            raise SystemExit("Nessuna chiave nel Portachiavi: esegui prima `m4desk live import-key`.")
        cfg_path = Path(args.config or "config.yaml")
        if not set_yaml_value(cfg_path, "live.wallet_pubkey", pub):
            raise SystemExit(f"Impossibile aggiornare {cfg_path}: imposta a mano live.wallet_pubkey: {pub}")
        print(f"live.wallet_pubkey = {pub} (indirizzo della chiave importata). Riavvia il motore.")
        return 0
    if c == "forget-key":
        KeyStore().forget()
        print("Chiave rimossa dal Portachiavi. Il live non può più firmare.")
        return 0
    if not cfg.live_enabled:
        print("Live DISABILITATO (live_enabled: false in config.yaml).")
        if c != "status":
            return 1
    live, db = _live_executor(cfg) if cfg.live_enabled else (None, _open_db(cfg, "paper", must_exist=False))
    if c == "status":
        from sqlalchemy import select

        from .storage.db import LiveOrder

        if live is not None:
            s = live.summary()
            print(f"Live: {'ARMATO' if s['armed'] else 'disarmato'} · wallet {s['wallet']}")
            print(f"Speso oggi: {s['spent_today_sol']:.4f} / {s['cap_day_sol']} SOL · posizioni aperte "
                  f"{s['open_positions']}")
        with db.session() as sess:
            for o in sess.scalars(select(LiveOrder).order_by(LiveOrder.id.desc()).limit(15)):
                print(f"  {o.created_at:%d/%m %H:%M} {o.side:<4} {o.mint[:8]}… {o.state:<12} {o.reason:<22} "
                      f"{o.signature or ''}")
        return 0
    assert live is not None
    if c == "preflight":
        checks = asyncio.run(live.preflight())
        for name, ok, detail in checks:
            print(f"[{'  OK  ' if ok else 'ERRORE'}] {name}: {detail}")
        return 0 if all(ok for _, ok, _ in checks) else 1
    if c == "arm":
        print(LIVE_WARNING)
        hb = db.get_meta("engine_boot_id")
        if not hb:
            raise SystemExit("Il motore PAPER non è mai stato avviato: esegui prima `m4desk run --mode paper`.")
        confirm = args.confirm if args.confirm is not None else input(f'Digita "{CONFIRM_PHRASE}" per confermare: ')
        ok, msg = asyncio.run(live.arm(confirm))
        print(msg)
        return 0 if ok else 1
    if c == "disarm":
        live.disarm("comando manuale", "human")
        print("Live disarmato: nessun nuovo acquisto reale. Le vendite protettive restano attive.")
        return 0
    return 1


# ------------------------------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="m4desk", description="M4 CopyDesk — ricerca copy-trading Solana, solo PAPER "
                                "(nessuna transazione reale).")
    p.add_argument("--version", action="version", version=f"m4desk {__version__}")
    p.add_argument("--config", default=None, help="percorso config.yaml (default ./config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def db_arg(sp, default="paper"):
        sp.add_argument("--db", default=default, help="paper | demo | replay | percorso file .db")

    sp = sub.add_parser("doctor", help="verifica ambiente e configurazione (offline)")
    sp.add_argument("--online", action="store_true", help="prova anche Ollama locale")
    sp.set_defaults(fn=cmd_doctor)
    sub.add_parser("init", help="crea config.yaml, .env (0600), cartella dati e DB").set_defaults(fn=cmd_init)
    for name, fn, hlp in (("demo", cmd_demo, "demo offline deterministica (SIMULATO / DATI DI ESEMPIO)"),
                          ("replay", cmd_replay, "replay di una fixture JSON")):
        sp = sub.add_parser(name, help=hlp)
        if name == "replay":
            sp.add_argument("fixture", help="file JSON di scenario")
        sp.add_argument("--pace", type=float, default=0.0, help="secondi di pausa fra eventi (per vederli in UI)")
        sp.add_argument("--serve", action="store_true", help="avvia la dashboard al termine")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("wallets", help="gestione watchlist di indirizzi PUBBLICI")
    wsub = sp.add_subparsers(dest="wallets_cmd", required=True)
    wa = wsub.add_parser("add", help="aggiungi un indirizzo pubblico")
    wa.add_argument("address")
    wa.add_argument("--label", default=None)
    db_arg(wa)
    wl = wsub.add_parser("list", help="elenca i wallet")
    db_arg(wl)
    sp.set_defaults(fn=cmd_wallets)
    sp = sub.add_parser("run", help="avvia il motore (solo --mode paper)")
    sp.add_argument("--mode", required=True, choices=["paper", "demo", "live"])
    sp.add_argument("--with-dashboard", action="store_true", help="avvia anche la dashboard nello stesso processo")
    sp.set_defaults(fn=cmd_run)
    sp = sub.add_parser("dashboard", help="dashboard web locale (solo 127.0.0.1)")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    db_arg(sp)
    sp.set_defaults(fn=cmd_dashboard)
    for name, fn, hlp in (("pause", cmd_pause, "PAUSA NUOVI ACQUISTI (le uscite protettive restano attive)"),
                          ("resume", cmd_resume, "RIPRENDI (esce anche da SAFE)"),
                          ("stop", cmd_stop, "ARRESTA il motore"),
                          ("status", cmd_status, "stato sistema, agenti e strategie"),
                          ("budget", cmd_budget, "budget e consumo dei provider gratuiti")):
        sp = sub.add_parser(name, help=hlp)
        db_arg(sp)
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("report", help="mostra l'ultimo report giornaliero (italiano)")
    db_arg(sp)
    sp.add_argument("--day", default=None, help="AAAA-MM-GG")
    sp.add_argument("--generate", action="store_true", help="genera ora dal DB")
    sp.set_defaults(fn=cmd_report)
    sub.add_parser("test-providers", help="prova i provider gratuiti (USA LA RETE, solo letture)").set_defaults(
        fn=cmd_test_providers)
    sp = sub.add_parser("check", help="CONTROLLO COMPLETO: motore, dati, provider, IA, live, notifiche, Mac")
    db_arg(sp)
    sp.add_argument("--no-notify", action="store_true", help="non inviare la notifica di prova")
    sp.set_defaults(fn=cmd_check)
    sp = sub.add_parser("live", help="esecuzione REALE opt-in: chiave, preflight, arma/disarma, stato")
    lsub = sp.add_subparsers(dest="live_cmd", required=True)
    lsub.add_parser("import-key", help="importa la chiave privata nel Portachiavi (input nascosto)")
    lsub.add_parser("forget-key", help="rimuove la chiave dal Portachiavi")
    lsub.add_parser("use-key-address", help="imposta live.wallet_pubkey all'indirizzo della chiave importata")
    lsub.add_parser("status", help="stato del live e ultimi ordini reali")
    lsub.add_parser("preflight", help="controlli completi + prova di swap SIMULATA (nessuna firma)")
    la = lsub.add_parser("arm", help='arma il live nel motore in esecuzione (richiede "ARMA LIVE")')
    la.add_argument("--confirm", default=None, help=argparse.SUPPRESS)
    lsub.add_parser("disarm", help="disarma: nessun nuovo acquisto reale")
    sp.set_defaults(fn=cmd_live)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())

