"""Launcher dell'app desktop (macOS: "M4 CopyDesk.app").

- Doppio clic: prepara la cartella dati, esegue la demo al primo avvio, avvia la dashboard locale
  (127.0.0.1) e l'icona nella barra dei menu; il motore PAPER parte se ci sono wallet in watchlist.
- Con argomenti funziona come la CLI: `".../M4 CopyDesk.app/Contents/MacOS/M4 CopyDesk" doctor`.
- `--headless [--port N]`: solo server + motore, senza interfaccia (usato dai test di build).

Nessuna transazione reale: l'app è solo PAPER, come la CLI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "M4 CopyDesk"
PKG_DATA = Path(__file__).parent / "data"
BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
log = logging.getLogger("m4desk.app")


def app_home() -> Path:
    if os.environ.get("M4DESK_HOME"):
        return Path(os.environ["M4DESK_HOME"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".local" / "share" / "m4-copydesk"


def log_dir() -> Path:
    if sys.platform == "darwin" and not os.environ.get("M4DESK_HOME"):
        return Path.home() / "Library" / "Logs" / APP_NAME
    return app_home() / "logs"


def bootstrap() -> Path:
    """Crea cartella dati, config.yaml, .env (0600, token dashboard) e si sposta lì."""
    home = app_home()
    (home / "data").mkdir(parents=True, exist_ok=True)
    cfg = home / "config.yaml"
    if not cfg.exists():
        shutil.copy(PKG_DATA / "config.example.yaml", cfg)
    env = home / ".env"
    if not env.exists():
        shutil.copy(PKG_DATA / "env.example", env)
    from .config import ensure_env_secrets

    ensure_env_secrets(env)
    os.chdir(home)
    return home


def setup_logging() -> Path:
    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = d / "app.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(f, encoding="utf-8"), logging.StreamHandler(sys.stderr)])
    return f


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:  # noqa: S310 (solo locale)
            return json.loads(r.read()).get("app") == "m4desk"
    except Exception:
        return False


class Controller:
    """Stato dell'app: server dashboard e motore PAPER in thread separati, ognuno con il proprio loop."""

    def __init__(self, port: int | None = None):
        from .config import load_config, load_secrets

        self.cfg = load_config("config.yaml")
        self.secrets = load_secrets()
        self.port = port or self.cfg.dashboard.port
        self.data = Path(self.cfg.data_dir)
        self.paper_db = self.data / "paper.db"
        self.demo_db = self.data / "demo.db"
        self.server_thread: threading.Thread | None = None
        self.engine_thread: threading.Thread | None = None
        self.engine_error: str | None = None
        self._caffeinate: subprocess.Popen | None = None

    # --- dati ---------------------------------------------------------------------
    def _db(self, path: Path):
        from .storage.db import Database

        return Database(path)

    def wallets(self) -> list[str]:
        return [w.address for w in self._db(self.paper_db).list_wallets()] + list(self.cfg.seed_wallets)

    def add_wallet(self, address: str, label: str | None = None) -> str:
        a = address.strip()
        if not BASE58.match(a):
            return "Indirizzo non valido: serve un indirizzo PUBBLICO Solana. Mai chiavi private o seed phrase."
        db = self._db(self.paper_db)
        added = db.add_wallet(a, "manual", label)
        db.audit("human", "wallet.add", f"Wallet pubblico aggiunto dall'app: {a}")
        return "Wallet aggiunto." if added else "Wallet già presente."

    def ensure_demo(self, force: bool = False) -> None:
        if not force and self.demo_db.exists() and self._db(self.demo_db).get_meta("replay_completed_at"):
            return
        from .engine import DEFAULT_FIXTURE, build_fixture_engine, run_replay

        log.info("esecuzione demo (dati sintetici)")
        rt, world, sup = build_fixture_engine(self.cfg, DEFAULT_FIXTURE, self.demo_db)
        asyncio.run(run_replay(rt, world, sup))

    # --- dashboard ------------------------------------------------------------------
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start_server(self) -> None:
        import uvicorn

        from .web.app import create_app

        dbs = {"paper": self.paper_db, "demo": self.demo_db} if self.wallets() else \
            {"demo": self.demo_db, "paper": self.paper_db}
        app = create_app(dbs, self.cfg, self.secrets)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.server_thread = threading.Thread(target=server.run, name="dashboard", daemon=True)
        self.server_thread.start()
        for _ in range(100):
            if already_running(self.port):
                return
            time.sleep(0.1)
        raise RuntimeError(f"la dashboard non risponde sulla porta {self.port}")

    # --- motore PAPER ---------------------------------------------------------------
    def engine_running(self) -> bool:
        return self.engine_thread is not None and self.engine_thread.is_alive()

    def start_engine(self) -> str:
        if self.engine_running():
            return "Il motore è già attivo."
        if not self.wallets():
            return "Aggiungi prima almeno un wallet PUBBLICO (menu: Aggiungi wallet…)."
        from .engine import build_paper_engine, run_paper
        from .types import SystemState

        db = self._db(self.paper_db)
        if db.get_control("state", "RUNNING") == SystemState.STOPPED.value:
            db.set_control("state", SystemState.RUNNING.value, "human", "app: avvio motore")

        def run() -> None:
            try:
                rt, sup = build_paper_engine(self.cfg, self.secrets, self.paper_db)
                asyncio.run(run_paper(rt, sup))
            except BaseException as e:  # noqa: BLE001 - mostrato all'utente, mai trasformato in acquisto
                self.engine_error = f"{type(e).__name__}: {e}"
                log.exception("motore PAPER terminato con errore")

        self.engine_error = None
        self.engine_thread = threading.Thread(target=run, name="engine", daemon=True)
        self.engine_thread.start()
        self._keep_awake(True)
        return "Motore PAPER avviato (sola lettura, nessuna transazione reale)."

    def stop_engine(self, wait_s: float = 15) -> str:
        from .types import SystemState

        self._db(self.paper_db).set_control("state", SystemState.STOPPED.value, "human", "app: ARRESTA")
        if self.engine_thread is not None:
            self.engine_thread.join(timeout=wait_s)
        self._keep_awake(False)
        return "Motore arrestato."

    def set_state(self, state: str, label: str) -> str:
        self._db(self.paper_db).set_control("state", state, "human", f"app: {label}")
        return f"Stato: {label}"

    # --- live (soldi veri) ------------------------------------------------------------
    def reload_config(self) -> None:
        from .config import load_config

        self.cfg = load_config("config.yaml")

    def live_armed(self) -> bool:
        db = self._db(self.paper_db)
        boot = db.get_meta("engine_boot_id")
        return bool(boot) and db.get_control("live_armed", "") == boot

    def import_key(self, secret: str) -> tuple[bool, str]:
        """Salva la chiave nel Portachiavi e abilita il live in config.yaml (wallet atteso = chiave importata)."""
        from .config import set_yaml_value
        from .live import KeyStore, KeyStoreError

        try:
            pub = KeyStore().store(secret)
        except KeyStoreError as e:
            return False, f"Chiave non valida: {e}"
        cfg = Path("config.yaml")
        for k, v in (("live.wallet_pubkey", pub), ("live_enabled", "true"), ("network", "solana-mainnet")):
            set_yaml_value(cfg, k, v)
        self.reload_config()
        return True, (f"Chiave salvata nel Portachiavi.\nIndirizzo: {pub}\nVerifica che sia lo stesso indirizzo "
                      "Solana mostrato in Fomo. Riavvia il motore, poi usa ARMA LIVE.")

    def _live(self):
        from .cli import _live_executor

        self.reload_config()
        if not self.cfg.live_enabled:
            return None
        live, _ = _live_executor(self.cfg)
        return live

    def live_preflight(self) -> str:
        live = self._live()
        if live is None:
            return "Live non abilitato: importa prima la chiave."
        checks = asyncio.run(live.preflight())
        return "\n".join(f"{'OK' if ok else 'ERRORE'} · {n}: {d}" for n, ok, d in checks)

    def live_arm(self, confirm: str) -> str:
        live = self._live()
        if live is None:
            return "Live non abilitato: importa prima la chiave."
        if not self.engine_running():
            return "Avvia prima il motore PAPER (il live replica i suoi ordini)."
        ok, msg = asyncio.run(live.arm(confirm, "human:app"))
        return msg

    def live_disarm(self) -> str:
        live = self._live()
        if live is not None:
            live.disarm("menu dell'app", "human:app")
        return "Live disarmato: nessun nuovo acquisto reale."

    def use_key_address(self) -> str:
        from .config import set_yaml_value
        from .live import KeyStore

        pub = KeyStore().pubkey()
        if not pub:
            return "Nessuna chiave nel Portachiavi: importala prima."
        set_yaml_value(Path("config.yaml"), "live.wallet_pubkey", pub)
        self.reload_config()
        return f"Indirizzo del wallet impostato a {pub}. Riavvia il motore, poi ripeti la verifica."

    def forget_key(self) -> str:
        from .config import set_yaml_value
        from .live import KeyStore

        self.live_disarm()
        KeyStore().forget()
        set_yaml_value(Path("config.yaml"), "live_enabled", "false")
        set_yaml_value(Path("config.yaml"), "network", "solana-mainnet-readonly")
        self.reload_config()
        return "Chiave rimossa e live disabilitato. Riavvia il motore."

    def _keep_awake(self, on: bool) -> None:
        """Su macOS impedisce lo stop per inattività mentre il motore gira (caffeinate -i)."""
        if sys.platform != "darwin":
            return
        if on and self._caffeinate is None:
            self._caffeinate = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])  # noqa: S603
        elif not on and self._caffeinate is not None:
            self._caffeinate.terminate()
            self._caffeinate = None


# ------------------------------------------------------------------------------- avvio al login
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.m4copydesk.app.plist"


def app_bundle_path() -> Path | None:
    exe = Path(sys.executable).resolve()
    for p in exe.parents:
        if p.suffix == ".app":
            return p
    return None


def login_item_enabled() -> bool:
    return LAUNCH_AGENT.exists()


def set_login_item(enabled: bool) -> str:
    bundle = app_bundle_path()
    if enabled:
        if bundle is None:
            return "Disponibile solo dall'app installata."
        LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
        LAUNCH_AGENT.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict><key>Label</key><string>com.m4copydesk.app</string>'
            "<key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-a</string>"
            f"<string>{bundle}</string></array><key>RunAtLoad</key><true/></dict></plist>\n",
            encoding="utf-8",
        )
        return "Avvio automatico al login attivato."
    LAUNCH_AGENT.unlink(missing_ok=True)
    return "Avvio automatico al login disattivato."


# ------------------------------------------------------------------------------- interfaccia barra dei menu
def run_menubar(ctl: Controller) -> None:
    import rumps  # disponibile solo nella build macOS

    from .types import SystemState

    class MenuApp(rumps.App):
        def __init__(self) -> None:
            super().__init__(APP_NAME, title="M4", quit_button=None)
            self.engine_item = rumps.MenuItem("Avvia motore PAPER", callback=self.toggle_engine)
            self.login_item = rumps.MenuItem("Apri all'accesso", callback=self.toggle_login)
            live_menu = rumps.MenuItem("Live (soldi veri)")
            live_menu.update([
                rumps.MenuItem("Importa chiave wallet…", callback=self.import_key),
                rumps.MenuItem("Verifica (preflight)", callback=lambda _: rumps.alert(APP_NAME, ctl.live_preflight())),
                rumps.MenuItem("Usa l'indirizzo della chiave", callback=lambda _: rumps.alert(
                    APP_NAME, ctl.use_key_address())),
                rumps.MenuItem("ARMA LIVE…", callback=self.arm_live),
                rumps.MenuItem("Disarma live", callback=lambda _: self.notify(ctl.live_disarm())),
                None,
                rumps.MenuItem("Rimuovi chiave", callback=self.forget_key),
            ])
            self.login_item.state = int(login_item_enabled())
            self.menu = [
                rumps.MenuItem("Apri dashboard", callback=lambda _: webbrowser.open(ctl.url())),
                rumps.MenuItem("Controllo completo", callback=lambda _: webbrowser.open(ctl.url() + "controllo?run=1")),
                None,
                self.engine_item,
                rumps.MenuItem("Pausa nuovi acquisti", callback=lambda _: self.notify(
                    ctl.set_state(SystemState.PAUSED.value, "PAUSA NUOVI ACQUISTI"))),
                rumps.MenuItem("Riprendi", callback=lambda _: self.notify(
                    ctl.set_state(SystemState.RUNNING.value, "RIPRENDI"))),
                rumps.MenuItem("Aggiungi wallet pubblico…", callback=self.add_wallet),
                live_menu,
                None,
                rumps.MenuItem("Riesegui demo (dati sintetici)", callback=self.rerun_demo),
                rumps.MenuItem("Apri cartella dati", callback=lambda _: subprocess.run(  # noqa: S603
                    ["/usr/bin/open", str(app_home())], check=False)),
                rumps.MenuItem("Apri log", callback=lambda _: subprocess.run(  # noqa: S603
                    ["/usr/bin/open", str(log_dir())], check=False)),
                self.login_item,
                None,
                rumps.MenuItem("Esci", callback=self.quit),
            ]
            self.timer = rumps.Timer(self.refresh, 3)
            self.timer.start()

        def notify(self, msg: str) -> None:
            rumps.notification(APP_NAME, "", msg)

        def refresh(self, _=None) -> None:
            running = ctl.engine_running()
            self.engine_item.title = "Arresta motore PAPER" if running else "Avvia motore PAPER"
            self.title = ("M4 ● LIVE" if ctl.live_armed() else "M4 ●") if running else "M4"
            if ctl.engine_error:
                self.notify(f"Motore fermato: {ctl.engine_error[:120]}")
                ctl.engine_error = None

        def toggle_engine(self, _) -> None:
            self.notify(ctl.stop_engine() if ctl.engine_running() else ctl.start_engine())
            self.refresh()

        def add_wallet(self, _) -> None:
            w = rumps.Window("Indirizzo PUBBLICO Solana (mai chiavi private o seed phrase):",
                             "Aggiungi wallet", ok="Aggiungi", cancel="Annulla", dimensions=(360, 24))
            r = w.run()
            if r.clicked and r.text.strip():
                rumps.alert(APP_NAME, ctl.add_wallet(r.text))

        def import_key(self, _) -> None:
            rumps.alert(APP_NAME, "ATTENZIONE: con la chiave il bot potrà comprare e vendere con soldi veri dal "
                        "wallet, entro i limiti di config.yaml. La chiave resta solo nel Portachiavi del Mac. "
                        "Non incollarla mai altrove.")
            w = rumps.Window("Chiave PRIVATA del wallet Solana (base58 o array JSON). Non la seed phrase.",
                             "Importa chiave", ok="Salva nel Portachiavi", cancel="Annulla", dimensions=(360, 24),
                             secure=True)
            r = w.run()
            if r.clicked and r.text.strip():
                ok, msg = ctl.import_key(r.text)
                rumps.alert(APP_NAME, msg)

        def arm_live(self, _) -> None:
            w = rumps.Window('ESECUZIONE REALE: il bot comprerà e venderà da solo con soldi veri.\n'
                             'Per confermare digita: ARMA LIVE', "ARMA LIVE", ok="Arma", cancel="Annulla",
                             dimensions=(260, 24))
            r = w.run()
            if r.clicked:
                rumps.alert(APP_NAME, ctl.live_arm(r.text))
                self.refresh()

        def forget_key(self, _) -> None:
            if rumps.alert(APP_NAME, "Rimuovere la chiave dal Portachiavi e disabilitare il live?", ok="Rimuovi",
                           cancel="Annulla") == 1:
                rumps.alert(APP_NAME, ctl.forget_key())

        def rerun_demo(self, _) -> None:
            ctl.ensure_demo(force=True)
            webbrowser.open(ctl.url() + "db/demo")

        def toggle_login(self, sender) -> None:
            self.notify(set_login_item(not login_item_enabled()))
            sender.state = int(login_item_enabled())

        def quit(self, _) -> None:
            if ctl.engine_running():
                ctl.stop_engine()
            rumps.quit_application()

    MenuApp().run()


def main(argv: list[str] | None = None) -> int:
    args = [a for a in (sys.argv[1:] if argv is None else argv) if not a.startswith("-psn")]
    headless = "--headless" in args
    if args and not headless:
        # modalità CLI: stessi dati dell'app
        if args[0] == "replay" and len(args) > 1:
            args[1] = str(Path(args[1]).resolve())
        bootstrap()
        from .cli import main as cli_main

        return cli_main(args)
    port = None
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    bootstrap()
    logfile = setup_logging()
    ctl = Controller(port)
    if already_running(ctl.port):
        webbrowser.open(ctl.url())
        return 0
    log.info("avvio %s, dati in %s, log in %s", APP_NAME, app_home(), logfile)
    ctl.ensure_demo()
    ctl.start_server()
    if ctl.wallets():
        log.info(ctl.start_engine())
    if headless:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            ctl.stop_engine()
        return 0
    webbrowser.open(ctl.url())
    try:
        run_menubar(ctl)
    except ImportError:  # senza rumps (es. Linux): resta attivo finché non viene interrotto
        log.info("interfaccia barra dei menu non disponibile: dashboard su %s", ctl.url())
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            ctl.stop_engine()
    return 0


if __name__ == "__main__":
    sys.exit(main())
