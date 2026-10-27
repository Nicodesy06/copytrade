"""Controllo completo: struttura, esiti, salvataggio e pulsante protetto in dashboard."""

from __future__ import annotations

from fastapi.testclient import TestClient

from m4desk.config import AppConfig, Secrets
from m4desk.services import healthcheck as hc
from m4desk.storage.db import Database
from m4desk.web.app import create_app


async def test_check_reports_stopped_engine_and_unreachable_providers(tmp_path):
    cfg = AppConfig(data_dir=str(tmp_path / "data"))
    db = Database(tmp_path / "paper.db")
    res = await hc.run_full_check(cfg, Secrets(), db, send_test_notification=False)
    items = {i["name"]: i for i in res["items"]}
    assert res["status"] == "ERRORE" and res["verdict"] == "PROBLEMI DA RISOLVERE"
    assert items["Motore PAPER in esecuzione"]["status"] == "ERRORE" and items["Motore PAPER in esecuzione"]["fix"]
    assert items["Wallet in watchlist"]["status"] == "ERRORE"
    assert items["Database integro e scrivibile"]["status"] == "OK"
    assert items["RPC Solana (pubblico)"]["status"] == "ERRORE"  # rete bloccata nei test: errore onesto, non OK
    assert items["Esecuzione reale"]["status"] == "INFO"
    assert db.get_meta("last_check")["status"] == "ERRORE"


async def test_check_all_green_when_everything_runs(tmp_path, monkeypatch):
    cfg = AppConfig(data_dir=str(tmp_path / "data"))
    db = Database(tmp_path / "paper.db")
    for name in ("supervisor", "discovery", "trader_analyst", "token_analyst", "strategy", "position_manager", "audit"):
        db.heartbeat(name, "RUNNING" if name == "supervisor" else "IDLE")
    db.add_wallet("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "manual")

    async def fake_providers(*a):
        return [hc.CheckItem("Provider", "finto", hc.OK)]

    monkeypatch.setattr(hc, "check_providers", fake_providers)
    monkeypatch.setattr(hc, "check_activity", lambda db, cfg, now: [hc.CheckItem("Trader", "ok", hc.OK)])
    res = await hc.run_full_check(cfg, Secrets(), db, send_test_notification=False)
    assert res["status"] == "OK", [i for i in res["items"] if i["status"] != "OK"]
    assert res["verdict"].startswith("TUTTO OK")


def test_dashboard_check_button_is_protected(tmp_path):
    cfg = AppConfig(data_dir=str(tmp_path / "data"))
    p = tmp_path / "paper.db"
    Database(p)
    c = TestClient(create_app({"paper": p}, cfg, Secrets(M4DESK_DASHBOARD_TOKEN="tok")))
    page = c.get("/controllo")
    assert page.status_code == 200 and "ESEGUI CONTROLLO COMPLETO" in page.text
    assert 'href="/controllo"' in c.get("/").text  # pulsante CONTROLLO in ogni pagina
    assert c.post("/api/check", json={}).status_code == 403
    r = c.post("/api/check", json={}, headers={"X-M4-Token": "tok"})
    assert r.status_code == 200 and r.json()["items"]
    assert "PROBLEMI DA RISOLVERE" in c.get("/controllo").text


def test_cli_check(tmp_path, monkeypatch, capsys):
    from m4desk.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["check", "--no-notify"]) == 1  # motore spento => errore, esito chiaro
    out = capsys.readouterr().out
    assert "Motore PAPER in esecuzione" in out and "Esito:" in out and "→" in out
