"""Accettazione: demo end-to-end offline deterministica, dashboard HTTP, CLI."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from m4desk.cli import main
from m4desk.config import AppConfig, Secrets
from m4desk.engine import DEFAULT_FIXTURE, build_fixture_engine, run_replay
from m4desk.services.reporting import strategy_stats
from m4desk.services.safety import check_contracts
from m4desk.storage.db import AuditLog, DecisionRow, LeaderSlot, Order, Position, Report
from m4desk.web.app import NAV, create_app


async def _demo(tmp_path: Path):
    cfg = AppConfig(data_dir=str(tmp_path / "data"))
    rt, world, sup = build_fixture_engine(cfg, DEFAULT_FIXTURE, tmp_path / "demo.db")
    await run_replay(rt, world, sup)
    return rt, cfg


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory):
    import asyncio

    tmp = tmp_path_factory.mktemp("demo")
    rt, cfg = asyncio.run(_demo(tmp))
    return rt, cfg, tmp


def test_demo_storyline_end_to_end(demo_db):
    rt, cfg, _ = demo_db
    with rt.db.session() as s:
        dec = list(s.scalars(select(DecisionRow).where(DecisionRow.strategy == "switch2")))
        reasons = {r for d in dec for r in d.reasons}
        orders = list(s.scalars(select(Order).where(Order.strategy == "switch2")))
        pos = list(s.scalars(select(Position).where(Position.strategy == "switch2")))
        slots = {r.wallet: r.status for r in s.scalars(select(LeaderSlot).where(LeaderSlot.strategy == "switch2"))}
        audit = {a.action for a in s.scalars(select(AuditLog))}
        reports = list(s.scalars(select(Report)))
    # discover -> score -> select
    assert len(rt.db.list_wallets()) >= 5 and len(slots) >= 3
    # rischio: pass e fail con motivi trasparenti
    for code in ("MINT_AUTHORITY_OPEN", "LIQUIDITY_TOO_LOW", "METADATA_SUSPICIOUS", "TOKEN2022_EXTENSION_UNSUPPORTED",
                 "PRICE_STALE", "TOKEN_TOO_NEW", "LEADER_BLOCKED_LOSSES", "OK", "LEADER_SELL_MIRRORED",
                 "LEADER_HOLDINGS_UNKNOWN"):
        assert code in reasons, code
    # buy simulato, vendita parziale, P&L realizzato, max chase all'esecuzione, stop-loss
    assert any(o.state == "REJECTED" and o.reason == "MAX_CHASE_EXCEEDED" for o in orders)
    assert any(p.partial_exits > 0 for p in pos)
    assert any(p.close_reason == "STOP_LOSS" for p in pos)
    assert any(p.realized_pnl_usd < 0 for p in pos) and any(p.realized_pnl_usd > 0 for p in pos)
    # cambio trader dopo 2 perdite
    assert "BLOCKED" in slots.values() and list(slots.values()).count("SELECTED") == 3
    assert {"leader.blocked", "reorg.after_copy", "report.daily"} <= audit
    assert len(reports) >= 2 and "SIMULATO" in reports[0].body_md
    assert rt.bus.duplicates_by_type["LEADER_SWAP"] >= 1
    for sid in rt.strategies:
        assert check_contracts(rt.db, sid, cfg.risk.starting_equity_usd, 3, rt.ledger.cash(sid)) == []


async def test_demo_is_deterministic(tmp_path, demo_db):
    rt1, _, _ = demo_db
    rt2, _ = await _demo(tmp_path)
    for sid in rt1.strategies:
        a = strategy_stats(rt1.db, rt1.ledger, sid, rt1.clock.now())
        b = strategy_stats(rt2.db, rt2.ledger, sid, rt2.clock.now())
        assert a["equity_estimate"] == b["equity_estimate"] and a["orders"] == b["orders"]


def test_dashboard_pages_and_labels(demo_db):
    rt, cfg, tmp = demo_db
    c = TestClient(create_app(rt.db.path, cfg, Secrets(M4DESK_DASHBOARD_TOKEN="tok-test")))
    for href, label in NAV:
        r = c.get(href)
        assert r.status_code == 200, href
        assert "SIMULATO / DATI DI ESEMPIO" in r.text and label in r.text
        assert "PAPER" in r.text and "PAUSA NUOVI ACQUISTI" in r.text
        assert "cdn" not in r.text.lower()  # nessun asset esterno
    assert c.get("/?partial=1").text.lstrip().startswith("<div id=\"content\"")
    assert c.get("/static/app.css").status_code == 200
    assert c.get("/api/state").json()["origin"] == "SIMULATO / DATI DI ESEMPIO"
    assert "Content-Security-Policy" in c.get("/").headers


def test_dashboard_control_actions_are_protected_and_audited(demo_db):
    rt, cfg, tmp = demo_db
    c = TestClient(create_app(rt.db.path, cfg, Secrets(M4DESK_DASHBOARD_TOKEN="tok-test")))
    assert c.post("/api/control/pause", json={"confirm": True}).status_code == 403
    assert c.post("/api/control/pause", json={}, headers={"X-M4-Token": "tok-test"}).status_code == 400
    r = c.post("/api/control/pause", json={"confirm": True}, headers={"X-M4-Token": "tok-test"})
    assert r.status_code == 200 and rt.db.get_control("state", "") == "PAUSED"
    r = c.post("/api/control/stop", json={"confirm": True}, headers={"X-M4-Token": "tok-test"})
    assert rt.db.get_control("state", "") == "STOPPED"
    c.post("/api/control/resume", json={"confirm": True}, headers={"X-M4-Token": "tok-test"})
    assert rt.db.get_control("state", "") == "RUNNING"
    r = c.post("/api/control/withdraw", json={"confirm": True}, headers={"X-M4-Token": "tok-test"})
    assert r.status_code == 404
    assert c.get("/", headers={"host": "evil.example"}).status_code == 403
    with rt.db.session() as s:
        acts = [a.actor for a in s.scalars(select(AuditLog).where(AuditLog.action == "control.state"))]
    assert acts.count("human") >= 3


def test_cli_demo_doctor_status_report(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["doctor"]) == 0
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "SIMULATO / DATI DI ESEMPIO" in out and "switch2" in out
    assert main(["status", "--db", "demo"]) == 0
    assert main(["report", "--db", "demo"]) == 0
    assert "Report giornaliero" in capsys.readouterr().out
    assert main(["pause", "--db", "demo"]) == 0
    assert main(["resume", "--db", "demo"]) == 0
    assert main(["budget", "--db", "demo"]) == 0


def test_cli_wallets_and_refusals(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    good = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    assert main(["wallets", "add", good, "--label", "test"]) == 0
    assert main(["wallets", "list"]) == 0
    assert good in capsys.readouterr().out
    for bad in ("0xABC", "word " * 12, "a" * 88):
        with pytest.raises(SystemExit):
            main(["wallets", "add", bad])
    for mode in ("live", "demo"):
        with pytest.raises(SystemExit):
            main(["run", "--mode", mode])


def test_cli_init_creates_private_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    env = tmp_path / ".env"
    assert env.exists() and (env.stat().st_mode & 0o077) == 0
    assert "M4DESK_DASHBOARD_TOKEN=" in env.read_text()
    assert (tmp_path / "config.yaml").exists()
    from m4desk.config import load_config

    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.mode.value == "paper" and cfg.live_enabled is False
    assert os.path.exists(tmp_path / "data" / "paper.db")
