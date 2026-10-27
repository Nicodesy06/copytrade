"""Launcher dell'app desktop: bootstrap, modalità CLI, demo al primo avvio, dashboard multi-DB."""

from __future__ import annotations

import json
import socket
import urllib.request
from pathlib import Path

import pytest

from m4desk import macapp

ROOT = Path(__file__).parents[1]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("M4DESK_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return tmp_path / "home"


def test_packaged_config_copies_are_in_sync():
    pkg = ROOT / "src" / "m4desk" / "data"
    assert (pkg / "config.example.yaml").read_text() == (ROOT / "config.example.yaml").read_text()
    assert (pkg / "env.example").read_text() == (ROOT / ".env.example").read_text()


def test_bootstrap_creates_private_home(home):
    h = macapp.bootstrap()
    assert h == home and Path.cwd() == home
    env = home / ".env"
    assert (env.stat().st_mode & 0o077) == 0 and "M4DESK_DASHBOARD_TOKEN=" in env.read_text()
    assert len(env.read_text().split("M4DESK_DASHBOARD_TOKEN=")[1].strip()) > 20
    from m4desk.config import load_config

    cfg = load_config(home / "config.yaml")
    assert cfg.mode.value == "paper" and not cfg.live_enabled
    tok = env.read_text()
    macapp.bootstrap()
    assert env.read_text() == tok  # non sovrascrive


def test_cli_mode_uses_app_home(home, capsys):
    assert macapp.main(["doctor"]) == 0
    assert macapp.main(["wallets", "add", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"]) == 0
    assert (home / "data" / "paper.db").exists()
    assert "TUTTO OK" in capsys.readouterr().out


def test_controller_demo_server_and_wallets(home):
    macapp.bootstrap()
    ctl = macapp.Controller(port=free_port())
    assert ctl.wallets() == []
    assert "Aggiungi prima" in ctl.start_engine()  # senza wallet il motore non parte
    assert "non valido" in ctl.add_wallet("seed phrase segreta")
    ctl.ensure_demo()
    assert ctl._db(ctl.demo_db).get_meta("replay_completed_at")
    ctl.start_server()
    assert macapp.already_running(ctl.port)
    with urllib.request.urlopen(ctl.url()) as r:
        page = r.read().decode()
    assert "SIMULATO / DATI DI ESEMPIO" in page and "PAPER (mainnet)" in page
    with urllib.request.urlopen(ctl.url() + "api/state") as r:
        assert json.loads(r.read())["mode"] == "demo"
    assert ctl.add_wallet("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263") == "Wallet aggiunto."
    assert ctl.wallets()


def test_dashboard_db_switch(home):
    from fastapi.testclient import TestClient

    from m4desk.config import AppConfig, Secrets
    from m4desk.web.app import create_app

    macapp.bootstrap()
    ctl = macapp.Controller()
    ctl.ensure_demo()
    c = TestClient(create_app({"paper": ctl.paper_db, "demo": ctl.demo_db}, AppConfig(), Secrets()))
    assert "SIMULATO / DATI DI ESEMPIO" not in c.get("/").text
    r = c.get("/db/demo", follow_redirects=False)
    assert r.status_code == 303
    c.cookies.set("m4db", "demo")
    assert "SIMULATO / DATI DI ESEMPIO" in c.get("/").text
    assert c.get("/db/altro").status_code == 404


def test_login_item_requires_installed_app(home):
    assert "solo dall'app installata" in macapp.set_login_item(True)


def test_app_import_key_enables_live_and_forget(home, monkeypatch):
    from solders.keypair import Keypair

    from m4desk.live import keystore
    from m4desk.live.keystore import MemoryBackend, _b58encode

    mem = MemoryBackend()
    monkeypatch.setattr(keystore, "default_backend", lambda: mem)
    macapp.bootstrap()
    ctl = macapp.Controller()
    ok, msg = ctl.import_key("non una chiave")
    assert not ok
    kp = Keypair()
    ok, msg = ctl.import_key(_b58encode(bytes(kp)))
    assert ok and str(kp.pubkey()) in msg
    assert ctl.cfg.live_enabled and ctl.cfg.network == "solana-mainnet"
    assert ctl.cfg.live.wallet_pubkey == str(kp.pubkey())
    assert "Avvia prima il motore" in ctl.live_arm("ARMA LIVE")
    assert not ctl.live_armed()
    assert "rimossa" in ctl.forget_key()
    assert not ctl.cfg.live_enabled and mem.data == {}
