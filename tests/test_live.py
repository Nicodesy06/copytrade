"""Modulo LIVE: chiave, ispezione, simulazione, limiti, armamento, vendite protettive. Tutto offline.

Jupiter e RPC sono finti (httpx.MockTransport); la chiave è generata al volo e non ha fondi.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from sqlalchemy import select

from helpers import LEADER, S, engine, feed, follow, mint, scenario, swap, token
from m4desk.clock import SimClock
from m4desk.config import AppConfig, NotifyConfig
from m4desk.live import CONFIRM_PHRASE, KeyStore, KeyStoreError, build_live
from m4desk.live.keystore import MemoryBackend, _b58encode, parse_secret
from m4desk.live.txcheck import (
    ATA,
    COMPUTE_BUDGET,
    JUPITER_V6,
    TOKEN,
    WSOL,
    ata_address,
    check_simulation,
    inspect_transaction,
)
from m4desk.services.notify import Notifier
from m4desk.storage.db import Database, LiveOrder, LivePosition
from m4desk.types import SystemState

CAP = Path(__file__).parent / "fixtures" / "captured"
MINT = mint("LIVE")
SOL = 1_000_000_000


# ------------------------------------------------------------------------------- transazioni sintetiche
def build_tx(owner: Pubkey, ixs: list[Instruction]) -> str:
    msg = MessageV0.try_compile(owner, ixs, [], Hash.default())
    tx = VersionedTransaction.populate(msg, [Signature.default()] * msg.header.num_required_signatures)
    return base64.b64encode(bytes(tx)).decode()


def cu_price(micro: int) -> Instruction:
    return Instruction(Pubkey.from_string(COMPUTE_BUDGET), bytes([3]) + micro.to_bytes(8, "little"), [])


def jup_ix(owner: Pubkey, extra_signer: Pubkey | None = None) -> Instruction:
    metas = [AccountMeta(owner, True, True)]
    if extra_signer is not None:
        metas.append(AccountMeta(extra_signer, True, False))
    return Instruction(Pubkey.from_string(JUPITER_V6), b"\xd1", metas)


def honest_buy(owner: Pubkey, lamports: int) -> str:
    wsol = Pubkey.from_string(ata_address(str(owner), WSOL))
    return build_tx(owner, [
        cu_price(5000),
        Instruction(Pubkey.from_string(ATA), b"\x01", [AccountMeta(owner, True, True), AccountMeta(wsol, False, True),
                                                       AccountMeta(owner, False, False)]),
        transfer(TransferParams(from_pubkey=owner, to_pubkey=wsol, lamports=lamports)),
        Instruction(Pubkey.from_string(TOKEN), bytes([17]), [AccountMeta(wsol, False, True)]),
        jup_ix(owner),
        Instruction(Pubkey.from_string(TOKEN), bytes([9]), [AccountMeta(wsol, False, True),
                                                            AccountMeta(owner, False, True),
                                                            AccountMeta(owner, True, False)]),
    ])


def honest_sell(owner: Pubkey) -> str:
    return build_tx(owner, [cu_price(5000), jup_ix(owner)])


# ------------------------------------------------------------------------------- Jupiter + RPC finti
class FakeChain:
    def __init__(self, owner: Pubkey):
        self.owner = owner
        self.balance = 2 * SOL
        self.tokens: dict[str, int] = {}  # saldo per mint nel wallet
        self.calls: list[str] = []
        self.executed: list[str] = []
        self.order_tx = None  # override per scenari malevoli
        self.sim_extra_spend = 0
        self.execute_status = "Success"
        self.last_order: dict | None = None
        self.notifications: list[tuple[str, str]] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "ntfy" in url:
            self.notifications.append((req.headers.get("title", ""), req.content.decode()))
            return httpx.Response(200, json={})
        if "/swap/v2/order" in url:
            q = dict(req.url.params)
            self.calls.append("order")
            assert q["taker"] == str(self.owner) and q.get("excludeRouters") == "jupiterz"
            amt = int(q["amount"])
            buy = q["inputMint"] == WSOL
            tx = self.order_tx or (honest_buy(self.owner, amt) if buy else honest_sell(self.owner))
            self.last_order = {"amount": amt, "buy": buy, "mint": q["outputMint"] if buy else q["inputMint"]}
            return httpx.Response(200, json={"transaction": tx, "requestId": "req-1", "router": "metis"})
        if "/swap/v2/execute" in url:
            body = json.loads(req.content)
            self.calls.append("execute")
            self.executed.append(body["signedTransaction"])
            if self.execute_status != "Success":
                return httpx.Response(200, json={"status": "Failed", "code": -1000})
            amt, buy, m = self.last_order["amount"], self.last_order["buy"], self.last_order["mint"]
            if buy:
                out = amt * 1000
                self.balance -= amt + 10_000
                self.tokens[m] = self.tokens.get(m, 0) + out
                return httpx.Response(200, json={"status": "Success", "signature": "SimLiveSig", "code": 0,
                                                 "totalInputAmount": str(amt), "totalOutputAmount": str(out)})
            got = amt // 1000
            self.tokens[m] -= amt
            self.balance += got - 10_000
            return httpx.Response(200, json={"status": "Success", "signature": "SimLiveSig2", "code": 0,
                                             "totalInputAmount": str(amt), "totalOutputAmount": str(got)})
        body = json.loads(req.content)
        m, p = body["method"], body["params"]
        self.calls.append(m)
        if m == "getBalance":
            return httpx.Response(200, json={"result": {"value": self.balance}})
        if m == "getTokenAccountsByOwner":
            held = self.tokens.get(p[1]["mint"], 0)
            vals = [] if held == 0 else [{"pubkey": "SimTokenAcct", "account": {"data": {"parsed": {"info": {
                "tokenAmount": {"amount": str(held)}}}}}}]
            return httpx.Response(200, json={"result": {"value": vals}})
        if m == "simulateTransaction":
            amt, buy = self.last_order["amount"], self.last_order["buy"]
            spend = (amt + 10_000 if buy else 10_000 - amt // 1000) + self.sim_extra_spend
            accts = [{"lamports": self.balance - spend}]
            if len(p[1]["accounts"]["addresses"]) > 1:
                raw = bytes(64) + (self.tokens[self.last_order["mint"]] - amt).to_bytes(8, "little") + bytes(93)
                accts.append({"lamports": 1, "data": [base64.b64encode(raw).decode(), "base64"]})
            return httpx.Response(200, json={"result": {"value": {"err": None, "accounts": accts}}})
        if m == "getSignatureStatuses":
            return httpx.Response(200, json={"result": {"value": [None]}})
        return httpx.Response(400, json={"error": {"code": -1}})


def make_live(tmp_path, **live_kw):
    kp = Keypair()
    chain = FakeChain(kp.pubkey())
    transport = httpx.MockTransport(chain.handler)
    cfg = AppConfig(data_dir=str(tmp_path / "data"), live_enabled=True, network="solana-mainnet",
                    live={"wallet_pubkey": str(kp.pubkey()), **live_kw},
                    notify=NotifyConfig(enabled=True, server="https://ntfy.example"))
    db = Database(tmp_path / "live.db")
    ks = KeyStore(MemoryBackend())
    ks.store(_b58encode(bytes(kp)))
    from m4desk.config import Secrets

    notifier = Notifier(cfg.notify, "m4desk-test-topic", transport)
    clock = SimClock(S)
    live = build_live(cfg, db, clock, Secrets(), notifier, ks, transport)
    return live, chain, kp, db


async def armed(tmp_path, **kw):
    live, chain, kp, db = make_live(tmp_path, **kw)
    ok, msg = await live.arm(CONFIRM_PHRASE)
    assert ok, msg
    return live, chain, kp, db


def live_orders(db):
    with db.session() as s:
        return list(s.scalars(select(LiveOrder).order_by(LiveOrder.id)))


# ------------------------------------------------------------------------------- chiave
def test_keystore_formats_and_rejections():
    kp = Keypair()
    full = _b58encode(bytes(kp))
    assert parse_secret(full).pubkey() == kp.pubkey()
    assert parse_secret(json.dumps(list(bytes(kp)))).pubkey() == kp.pubkey()
    assert parse_secret(_b58encode(bytes(kp)[:32])).pubkey() == kp.pubkey()
    for bad in ("", "abandon " * 12, "0xdeadbeef", _b58encode(bytes(kp)[:32] + bytes(Keypair().pubkey()))):
        with pytest.raises(KeyStoreError):
            parse_secret(bad)
    ks = KeyStore(MemoryBackend())
    assert ks.store(full) == str(kp.pubkey()) and ks.has_key()
    ks.forget()
    assert not ks.has_key()


# ------------------------------------------------------------------------------- ispezione
def test_real_jupiter_transaction_passes_inspection_and_simulation():
    d = json.loads((CAP / "jup_order_taker.json").read_text())
    owner = d["taker"]
    rep = inspect_transaction(d["transaction"], owner, 10_000_000, 200_000)
    assert rep.ok and rep.signers == 1 and JUPITER_V6 in rep.programs and rep.wrap_lamports == 10_000_000
    sim = json.loads((CAP / "rpc_simulate_buy.json").read_text())["result"]["value"]
    assert check_simulation(sim, 25_465_115, 10_000_000 + 3_000_000) == []
    assert check_simulation(sim, 25_465_115, 9_000_000)  # spenderebbe più del consentito
    assert not inspect_transaction(d["transaction"], str(Keypair().pubkey()), 10**9, 200_000).ok  # altro wallet


@pytest.mark.parametrize("attack", ["drain", "token_transfer", "extra_signer", "unknown_program", "priority",
                                    "close_to_other", "no_jupiter"])
def test_malicious_transactions_are_rejected(attack):
    owner, thief = Keypair().pubkey(), Keypair().pubkey()
    ixs = {
        "drain": [transfer(TransferParams(from_pubkey=owner, to_pubkey=thief, lamports=10)), jup_ix(owner)],
        "token_transfer": [Instruction(Pubkey.from_string(TOKEN), bytes([3]) + (5).to_bytes(8, "little"),
                                       [AccountMeta(owner, False, True), AccountMeta(thief, False, True),
                                        AccountMeta(owner, True, False)]), jup_ix(owner)],
        "extra_signer": [jup_ix(owner, extra_signer=thief)],
        "unknown_program": [Instruction(thief, b"\x00", [AccountMeta(owner, True, True)]), jup_ix(owner)],
        "priority": [cu_price(10**9), jup_ix(owner)],
        "close_to_other": [Instruction(Pubkey.from_string(TOKEN), bytes([9]), [AccountMeta(thief, False, True),
                                                                               AccountMeta(thief, False, True),
                                                                               AccountMeta(owner, True, False)]),
                           jup_ix(owner)],
        "no_jupiter": [cu_price(1)],
    }[attack]
    rep = inspect_transaction(build_tx(owner, ixs), str(owner), 10**9, 200_000)
    assert not rep.ok and rep.violations


# ------------------------------------------------------------------------------- armamento
async def test_preflight_and_arming(tmp_path):
    live, chain, kp, db = make_live(tmp_path)
    checks = await live.preflight()
    assert all(ok for _, ok, _ in checks), checks
    assert "execute" not in chain.calls  # il preflight non firma né invia nulla
    ok, msg = await live.arm("arma")
    assert not ok and not live.is_armed()
    ok, _ = await live.arm(CONFIRM_PHRASE)
    assert ok and live.is_armed()
    assert any("armato" in t.lower() for t, _ in chain.notifications)
    # riavvio del motore = nuovo boot id ⇒ disarmato
    from m4desk.config import Secrets

    live2 = build_live(live.cfg, db, live.clock, Secrets(), None, live.keys, httpx.MockTransport(chain.handler))
    assert not live2.is_armed()


async def test_preflight_fails_on_wrong_key_or_low_balance(tmp_path):
    live, chain, kp, db = make_live(tmp_path)
    live.keys.store(_b58encode(bytes(Keypair())))  # chiave diversa dal wallet dichiarato
    chain.balance = 1000
    failed = [n for n, ok, _ in await live.preflight(dry_run_swap=False) if not ok]
    assert "La chiave corrisponde a live.wallet_pubkey" in failed and "Saldo SOL sufficiente" in failed
    ok, msg = await live.arm(CONFIRM_PHRASE)
    assert not ok and "Preflight" in msg


# ------------------------------------------------------------------------------- acquisti
async def test_not_armed_never_signs(tmp_path):
    live, chain, kp, db = make_live(tmp_path)
    assert await live.buy(MINT, LEADER, "k1") == "LIVE_NOT_ARMED"
    assert "order" not in chain.calls and "execute" not in chain.calls


async def test_armed_buy_signs_with_our_key_and_records_real_amounts(tmp_path):
    live, chain, kp, db = await armed(tmp_path)
    assert await live.buy(MINT, LEADER, "k1") == "SUCCESS"
    signed = VersionedTransaction.from_bytes(base64.b64decode(chain.executed[0]))
    assert signed.signatures[0] != Signature.default()
    assert signed.verify_with_results() == [True]
    [o] = live_orders(db)
    assert o.state == "SUCCESS" and o.in_amount_raw == int(0.05 * SOL) and o.out_amount_raw == int(0.05 * SOL) * 1000
    assert o.sol_spent_lamports == int(0.05 * SOL) + 10_000  # importo reale dal saldo, commissioni incluse
    [p] = live.open_positions()
    assert p.qty_raw == o.out_amount_raw and p.cost_lamports == o.sol_spent_lamports
    assert await live.buy(MINT, LEADER, "k1") == "DUPLICATE"  # idempotente


async def test_daily_cap_and_max_positions(tmp_path):
    # il tetto conta la spesa reale, commissioni incluse
    live, chain, kp, db = await armed(tmp_path, max_sol_per_day="0.11")
    assert await live.buy(mint("A"), LEADER, "a") == "SUCCESS"
    assert await live.buy(mint("B"), LEADER, "b") == "SUCCESS"
    assert await live.buy(mint("C"), LEADER, "c") == "LIVE_DAILY_CAP"
    assert any("tetto" in t.lower() for t, _ in chain.notifications)
    live.clock.set(S + timedelta(days=1))
    assert await live.buy(mint("C"), LEADER, "c2") == "SUCCESS"
    assert await live.buy(mint("D"), LEADER, "d") == "LIVE_MAX_POSITIONS"


async def test_never_mixes_with_user_holdings_and_keeps_reserve(tmp_path):
    live, chain, kp, db = await armed(tmp_path)
    chain.tokens[MINT] = 123  # l'utente detiene già questo token
    assert await live.buy(MINT, LEADER, "k1") == "LIVE_USER_HOLDING"
    chain.tokens[MINT] = 0
    chain.balance = int(0.07 * SOL)  # resterebbe meno della riserva minima
    assert await live.buy(MINT, LEADER, "k2") == "LIVE_INSUFFICIENT_SOL"
    assert "execute" not in chain.calls


async def test_malicious_order_is_never_signed(tmp_path):
    live, chain, kp, db = await armed(tmp_path)
    thief = Keypair().pubkey()
    chain.order_tx = build_tx(kp.pubkey(), [transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=thief,
                                                                     lamports=10**9)), jup_ix(kp.pubkey())])
    assert await live.buy(MINT, LEADER, "k1") == "INSPECT_FAIL"
    assert "execute" not in chain.calls and live_orders(db)[0].state == "INSPECT_FAIL"
    assert any("NON eseguito" in t for t, _ in chain.notifications)


async def test_simulation_overspend_is_never_signed(tmp_path):
    live, chain, kp, db = await armed(tmp_path)
    chain.sim_extra_spend = 50_000_000
    assert await live.buy(MINT, LEADER, "k1") == "SIM_FAIL"
    assert "execute" not in chain.calls


async def test_consecutive_failures_disarm(tmp_path):
    live, chain, kp, db = await armed(tmp_path)
    chain.execute_status = "Failed"
    for i in range(3):
        assert await live.buy(mint(f"F{i}"), LEADER, f"f{i}") == "EXECUTE_FAILED"
    assert not live.is_armed()


# ------------------------------------------------------------------------------- vendite
async def test_sells_only_bot_quantity_and_close(tmp_path):
    live, chain, kp, db = await armed(tmp_path)
    await live.buy(MINT, LEADER, "b1")
    bot_qty = live.open_positions()[0].qty_raw
    chain.tokens[MINT] += 7_777  # l'utente aggiunge token propri dello stesso mint
    assert await live.sell(MINT, Decimal("0.5"), "s1", "LEADER_SELL_MIRRORED") == "SUCCESS"
    assert live.open_positions()[0].qty_raw == bot_qty - bot_qty // 2
    assert await live.sell(MINT, Decimal(1), "s2", "STOP_LOSS") == "SUCCESS"
    assert live.open_positions() == [] and chain.tokens[MINT] == 7_777  # i token dell'utente restano suoi
    with db.session() as s:
        p = s.scalars(select(LivePosition)).one()
    assert p.status == "CLOSED" and p.proceeds_lamports > 0


async def test_protective_sell_works_when_disarmed_and_retries(tmp_path):
    live, chain, kp, db = await armed(tmp_path)
    await live.buy(MINT, LEADER, "b1")
    live.disarm("test")
    chain.execute_status = "Failed"
    assert await live.sell(MINT, Decimal(1), "s1", "STOP_LOSS") == "EXECUTE_FAILED"
    assert live.open_positions()  # nessuna uscita inventata
    assert any(t.startswith("LIVE vendita") for t, _ in chain.notifications)
    chain.execute_status = "Success"
    assert await live.retry_pending_exits() == 1
    assert live.open_positions() == []


async def test_recovery_marks_interrupted_orders_unknown(tmp_path):
    live, chain, kp, db = await armed(tmp_path)
    o = live._new_order("x", "BUY", MINT, LEADER, None)
    live._update(o.id, state="SUBMITTED", signature="SimSigX")
    assert await live.recover() == [o.id]
    assert live_orders(db)[0].state == "UNKNOWN"


# ------------------------------------------------------------------------------- integrazione col motore
async def test_engine_mirrors_primary_paper_fills_and_safe_disarms(tmp_path):
    rt, world, sup = await engine(tmp_path, scenario([token("LIVE", 0.01)]))
    live, chain, kp, db = make_live(tmp_path / "l")
    live.db = rt.db  # stesso DB del motore
    live.db.set_meta("engine_boot_id", live.boot_id)
    rt.db.set_control("live_armed", live.boot_id, "human", "test")
    rt.live = live
    follow(rt)
    amt = 2 * 10**11
    await feed(rt, world, swap(LEADER, MINT, "BUY", S, 2000, 0.01, 0, amt))
    assert [o.state for o in live_orders(rt.db)] == ["SUCCESS"]
    await feed(rt, world, swap(LEADER, MINT, "SELL", S + timedelta(minutes=5), 0, 0.01, amt, amt // 2))
    assert [o.side for o in live_orders(rt.db)] == ["BUY", "SELL"]
    await rt.enter_safe("test", "prova")
    assert not live.is_armed() and rt.system_state() == SystemState.SAFE


async def test_notifier_disabled_without_topic():
    n = Notifier(NotifyConfig(enabled=True), None)
    assert not n.enabled and await n.send("t", "m") is False


def test_dashboard_live_page_and_protected_arm(tmp_path):
    from fastapi.testclient import TestClient

    from m4desk.config import Secrets
    from m4desk.web.app import create_app

    live, chain, kp, db = make_live(tmp_path)
    c = TestClient(create_app({"paper": db.path}, live.cfg, Secrets(M4DESK_DASHBOARD_TOKEN="tok")))
    r = c.get("/live")
    assert r.status_code == 200 and "ARMA LIVE" in r.text and "LIVE ARMATO" not in r.text
    assert c.post("/api/live/arm", json={"confirm": CONFIRM_PHRASE}).status_code == 403  # senza token
    db.set_control("live_armed", live.boot_id, "human", "test")
    assert "LIVE ARMATO" in c.get("/").text  # banner su tutte le pagine
    assert c.post("/api/live/disarm", json={}, headers={"X-M4-Token": "tok"}).status_code == 200
    assert db.get_control("live_armed", "") == ""


def test_cli_import_key_never_echoes_secret(tmp_path, monkeypatch, capsys):
    from m4desk import cli
    from m4desk.live import keystore

    monkeypatch.chdir(tmp_path)
    mem = MemoryBackend()
    monkeypatch.setattr(keystore, "default_backend", lambda: mem)
    kp = Keypair()
    secret = _b58encode(bytes(kp))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": secret)
    monkeypatch.setattr("builtins.input", lambda prompt="": "s")
    assert cli.main(["init"]) == 0
    assert cli.main(["live", "import-key"]) == 0
    out = capsys.readouterr().out
    assert str(kp.pubkey()) in out and secret not in out
    from m4desk.config import load_config

    assert load_config(tmp_path / "config.yaml").live.wallet_pubkey == str(kp.pubkey())
    assert secret not in (tmp_path / "config.yaml").read_text() and secret not in (tmp_path / ".env").read_text()
    assert "NTFY_TOPIC=m4desk-" in (tmp_path / ".env").read_text()


async def test_preflight_explains_mismatch_and_missing_sol(tmp_path):
    live, chain, kp, db = make_live(tmp_path)
    other = Keypair()
    live.keys.store(_b58encode(bytes(other)))
    chain.balance = 0
    res = {n: d for n, ok, d in await live.preflight(dry_run_swap=False)}
    assert str(other.pubkey()) in res["La chiave corrisponde a live.wallet_pubkey"]
    assert "use-key-address" in res["La chiave corrisponde a live.wallet_pubkey"]
    assert "commissioni in SOL" in res["Saldo SOL sufficiente"]


def test_cli_use_key_address(tmp_path, monkeypatch, capsys):
    from m4desk import cli
    from m4desk.config import load_config
    from m4desk.live import keystore

    monkeypatch.chdir(tmp_path)
    mem = MemoryBackend()
    monkeypatch.setattr(keystore, "default_backend", lambda: mem)
    assert cli.main(["init"]) == 0
    kp = Keypair()
    KeyStore(mem).store(_b58encode(bytes(kp)))
    assert cli.main(["live", "use-key-address"]) == 0
    assert load_config(tmp_path / "config.yaml").live.wallet_pubkey == str(kp.pubkey())
