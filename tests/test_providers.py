"""Adapter reali testati OFFLINE su risposte catturate + errori HTTP simulati (nessuna rete)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from m4desk.clock import Clock, SimClock
from m4desk.config import BudgetConfig
from m4desk.providers.base import ProviderError
from m4desk.providers.budget import ProviderGate
from m4desk.providers.dexscreener import pick_market
from m4desk.providers.http import HttpJson
from m4desk.providers.jupiter import parse_order
from m4desk.providers.rugcheck import parse_report
from m4desk.providers.solana_rpc import SolanaRpc
from m4desk.providers.swap_parser import parse_swap
from m4desk.storage.db import Database
from m4desk.types import SOL_MINT, USDC_MINT, DataOrigin, ReasonCode, Side

CAP = Path(__file__).parent / "fixtures" / "captured"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
ORIG = DataOrigin.MAINNET_READONLY


def load(name):
    return json.loads((CAP / name).read_text())


# --- Rugcheck ----------------------------------------------------------------------------
def test_rugcheck_real_report_and_lp_source_is_conservative():
    r = parse_report(BONK, load("rug_report.json"), load("rug_summary.json"), ORIG)
    assert r.authorities_known and r.mint_authority is None and r.freeze_authority is None
    assert r.score_normalised == 7
    # summary dice ~15.96, i mercati 0: si prende il valore più prudente e si registra la fonte
    assert r.lp_locked_pct == 0 and r.lp_source == "markets"
    assert r.top10_pct is not None and r.top10_pct > 0
    assert r.description is None  # mai usata come istruzione


def test_rugcheck_summary_only_lp():
    rep = load("rug_report.json")
    rep["markets"] = []
    r = parse_report(BONK, rep, load("rug_summary.json"), ORIG)
    assert r.lp_source == "summary" and r.lp_locked_pct > Decimal(15)
    r2 = parse_report(BONK, rep, None, ORIG)
    assert r2.lp_locked_pct is None and r2.lp_source is None  # nessuna fonte => None, mai assunto


@pytest.mark.parametrize("bad", [None, [], {}, {"foo": 1}, {"score_normalised": 1}, "html"])
def test_rugcheck_unrelated_or_null_schema(bad):
    with pytest.raises(ProviderError) as e:
        parse_report(BONK, bad, None, ORIG)
    assert e.value.reason == ReasonCode.RUGCHECK_SCHEMA_INVALID


def test_rugcheck_null_fields_stay_unknown():
    r = parse_report(BONK, {"score_normalised": None, "risks": [], "topHolders": None, "markets": None}, None, ORIG)
    assert r.score_normalised is None and r.top10_pct is None and r.lp_locked_pct is None
    assert not r.authorities_known


# --- DEX Screener ----------------------------------------------------------------------
def test_dexscreener_real_response():
    m = pick_market(load("dexscreener_tokens_v1.json"), BONK, datetime.now(UTC), ORIG)
    assert m.price_usd > 0 and m.liquidity_usd > 0 and m.pair_created_at is not None


def test_dexscreener_quote_side_price_and_missing_liquidity():
    raw = [{"chainId": "solana", "pairAddress": "P", "baseToken": {"address": "BASE"}, "quoteToken": {"address": "Q"},
            "priceNative": "2", "priceUsd": "10"}]
    m = pick_market(raw, "Q", datetime.now(UTC), ORIG)
    assert m.price_usd == Decimal(5) and m.liquidity_usd is None
    with pytest.raises(ProviderError) as e:
        pick_market([], "Q", datetime.now(UTC), ORIG)
    assert e.value.reason == ReasonCode.LIQUIDITY_UNAVAILABLE
    with pytest.raises(ProviderError):
        pick_market({"pairs": None}, "Q", datetime.now(UTC), ORIG)


# --- Jupiter -----------------------------------------------------------------------------
def test_jupiter_v2_order_parse_real():
    q = parse_order(load("jup_order.json"), SOL_MINT, USDC_MINT, 100_000_000, ORIG)
    assert q.out_amount_raw > 0 and q.price_impact_pct is not None and q.price_impact_pct >= 0
    assert q.fee_bps == 2


def test_jupiter_drops_any_transaction_and_validates():
    raw = load("jup_order.json") | {"transaction": "BASE64TX"}
    q = parse_order(raw, SOL_MINT, USDC_MINT, 1, ORIG)
    assert not hasattr(q, "transaction")
    with pytest.raises(ProviderError):
        parse_order({"error": "x"}, SOL_MINT, USDC_MINT, 1, ORIG)
    with pytest.raises(ProviderError) as e:
        parse_order(load("jup_order.json"), USDC_MINT, SOL_MINT, 1, ORIG)
    assert e.value.reason == ReasonCode.QUOTE_LOW_CONFIDENCE


# --- Swap parser -------------------------------------------------------------------------
def _tx():
    return load("tx.json")["result"]


def test_swap_parser_real_jupiter_sell():
    tx = _tx()
    signer = tx["transaction"]["message"]["accountKeys"][0]["pubkey"]
    r = parse_swap(tx, signer, "sig")
    assert r.swap is not None and r.swap.side == Side.SELL and r.swap.quote_mint == SOL_MINT
    assert r.swap.wallet_pre_amount_raw == r.swap.token_amount_raw and r.swap.wallet_post_amount_raw == 0
    assert "Jupiter v6" in r.swap.program_labels


def test_swap_parser_rejects_failed_transfer_and_unknown():
    tx = _tx()
    signer = tx["transaction"]["message"]["accountKeys"][0]["pubkey"]
    failed = json.loads(json.dumps(tx))
    failed["meta"]["err"] = {"InstructionError": [0, "x"]}
    assert parse_swap(failed, signer, "s").swap is None
    nodex = json.loads(json.dumps(tx))
    nodex["transaction"]["message"]["instructions"] = []
    nodex["meta"]["innerInstructions"] = []
    r = parse_swap(nodex, signer, "s")
    assert r.swap is None and r.reason == ReasonCode.UNKNOWN_TX_CLASSIFICATION
    # trasferimento puro: il token esce ma nessuna contropartita SOL/USDC entra
    transfer = json.loads(json.dumps(tx))
    idx = 0
    transfer["meta"]["postBalances"][idx] = transfer["meta"]["preBalances"][idx] - transfer["meta"]["fee"]
    transfer["meta"]["postTokenBalances"] = [b for b in transfer["meta"]["postTokenBalances"]
                                             if b["mint"] != SOL_MINT or b.get("owner") != signer]
    transfer["meta"]["preTokenBalances"] = [b for b in transfer["meta"]["preTokenBalances"]
                                            if b["mint"] != SOL_MINT or b.get("owner") != signer]
    assert parse_swap(transfer, signer, "s").swap is None
    assert parse_swap(tx, "SimNotInTx1111111111111111111111", "s").swap is None
    assert parse_swap(None, signer, "s").reason == ReasonCode.PROVIDER_ERROR


def test_rpc_mint_info_token2022_extensions():
    class FakeHttp:
        async def post(self, url, body, **kw):
            return load("rpc_mint_account_t22.json")

    import asyncio

    mi = asyncio.run(SolanaRpc(FakeHttp(), "u").get_mint_info("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"))
    assert mi.program == "spl-token-2022" and "transferHook" in mi.extensions and "permanentDelegate" in mi.extensions


async def test_rpc_unsupported_version_and_429_and_forbidden():
    class FakeHttp:
        def __init__(self, payload):
            self.payload = payload

        async def post(self, url, body, **kw):
            return self.payload

    rpc = SolanaRpc(FakeHttp({"error": {"code": -32015, "message": "Transaction version (2)"}}), "u")
    with pytest.raises(ProviderError) as e:
        await rpc.get_transaction("x")
    assert e.value.reason == ReasonCode.UNKNOWN_TX_CLASSIFICATION and rpc.unsupported_versions == 1
    rpc2 = SolanaRpc(FakeHttp({"error": {"code": 429, "message": "Too many"}}), "u")
    with pytest.raises(ProviderError) as e2:
        await rpc2.get_transaction("y")
    assert e2.value.reason == ReasonCode.PROVIDER_RATE_LIMITED
    with pytest.raises(PermissionError):
        await rpc.call("sendTransaction", [])


# --- Gate: budget, rate limit, circuit breaker, cache --------------------------------------
def _gate(tmp_path, clock=None, **b):
    budget = BudgetConfig(**({"daily_requests": 100, "monthly_requests": 1000, "per_minute": 50} | b))
    return ProviderGate("p", budget, Database(tmp_path / "g.db"), clock or SimClock(datetime(2026, 1, 1, tzinfo=UTC)))


async def test_rate_limit_429_opens_circuit_then_recovers(tmp_path):
    clock = SimClock(datetime(2026, 1, 1, tzinfo=UTC))
    g = _gate(tmp_path, clock)

    async def r429():
        raise ProviderError("p", ReasonCode.PROVIDER_RATE_LIMITED)

    with pytest.raises(ProviderError):
        await g.call(None, 0, r429)
    assert g.circuit_open() and g.available() == (False, ReasonCode.CIRCUIT_OPEN)

    async def ok():
        return 1

    with pytest.raises(ProviderError) as e:
        await g.call(None, 0, ok)
    assert e.value.reason == ReasonCode.CIRCUIT_OPEN
    from datetime import timedelta

    clock.set(clock.now() + timedelta(seconds=31))
    assert await g.call(None, 0, ok) == 1
    assert g.usage()["day_429"] == 1


async def test_budget_exhausted_stops_calls(tmp_path):
    g = _gate(tmp_path, daily_requests=2)

    async def ok():
        return 1

    await g.call(None, 0, ok)
    await g.call(None, 0, ok)
    with pytest.raises(ProviderError) as e:
        await g.call(None, 0, ok)
    assert e.value.reason == ReasonCode.BUDGET_EXHAUSTED and g.status() == "BUDGET_ESAURITO"


async def test_cache_hits_do_not_consume_budget(tmp_path):
    g = _gate(tmp_path, daily_requests=1)
    calls = []

    async def ok():
        calls.append(1)
        return 7

    assert await g.call("k", 60, ok) == 7
    assert await g.call("k", 60, ok) == 7
    assert len(calls) == 1 and g.usage()["day_cache_hits"] == 1


async def test_http_429_and_auth_change_disable_connector(tmp_path):
    g = _gate(tmp_path, Clock())

    def handler(req):
        return httpx.Response(429 if "rl" in str(req.url) else 401, json={})

    h = HttpJson("p", g, 5)
    h.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as e:
        await h.get("http://x/rl")
    assert e.value.reason == ReasonCode.PROVIDER_RATE_LIMITED
    g._open_until = None
    with pytest.raises(ProviderError) as e2:
        await h.get("http://x/auth")
    assert e2.value.reason == ReasonCode.PROVIDER_DISABLED and not g.enabled
    assert g.available() == (False, ReasonCode.PROVIDER_DISABLED)
