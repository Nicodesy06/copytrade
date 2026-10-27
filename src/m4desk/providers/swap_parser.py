"""Classificazione swap da `getTransaction` (jsonParsed) tramite variazioni nette di saldo.

Regole (fail-closed):
- transazione fallita => nessuno swap;
- serve almeno un programma DEX/aggregatore noto fra le istruzioni;
- esattamente UN token non-quote con variazione netta e una variazione quote (SOL+WSOL oppure USDC)
  di segno opposto; altrimenti `None` + motivo (trasferimenti, airdrop, swap token→token, LP...).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from ..types import SOL_MINT, USDC_MINT, ReasonCode, Side, SwapObservation

KNOWN_DEX_PROGRAMS: dict[str, str] = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter v6",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora DAMM v1",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY": "Phoenix",
}
QUOTE_DECIMALS = {SOL_MINT: 9, USDC_MINT: 6}


class ParseResult:
    def __init__(self, swap: SwapObservation | None, reason: ReasonCode | None, detail: str = ""):
        self.swap = swap
        self.reason = reason
        self.detail = detail


def _programs(tx: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    msg = tx.get("transaction", {}).get("message", {})
    for ix in msg.get("instructions", []) or []:
        if isinstance(ix, dict) and ix.get("programId"):
            ids.append(ix["programId"])
    for inner in (tx.get("meta") or {}).get("innerInstructions", []) or []:
        for ix in inner.get("instructions", []) or []:
            if isinstance(ix, dict) and ix.get("programId"):
                ids.append(ix["programId"])
    return ids


def _token_sums(balances: list[dict[str, Any]], wallet: str) -> tuple[dict[str, int], dict[str, int]]:
    sums: dict[str, int] = defaultdict(int)
    decs: dict[str, int] = {}
    for b in balances or []:
        if b.get("owner") != wallet:
            continue
        amt = b.get("uiTokenAmount", {})
        sums[b["mint"]] += int(amt.get("amount", "0"))
        decs[b["mint"]] = int(amt.get("decimals", 0))
    return sums, decs


def parse_swap(tx: dict[str, Any] | None, wallet: str, signature: str) -> ParseResult:
    if not tx:
        return ParseResult(None, ReasonCode.PROVIDER_ERROR, "transazione assente")
    meta = tx.get("meta")
    if not isinstance(meta, dict):
        return ParseResult(None, ReasonCode.UNKNOWN_TX_CLASSIFICATION, "meta mancante")
    if meta.get("err") is not None:
        return ParseResult(None, ReasonCode.UNKNOWN_TX_CLASSIFICATION, "transazione fallita")
    labels = tuple(sorted({KNOWN_DEX_PROGRAMS[p] for p in _programs(tx) if p in KNOWN_DEX_PROGRAMS}))
    if not labels:
        return ParseResult(None, ReasonCode.UNKNOWN_TX_CLASSIFICATION, "nessun programma DEX noto")

    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    pubkeys = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    if wallet not in pubkeys:
        return ParseResult(None, ReasonCode.UNKNOWN_TX_CLASSIFICATION, "wallet non presente")
    idx = pubkeys.index(wallet)
    fee = int(meta.get("fee", 0))
    pre_sol = int(meta["preBalances"][idx])
    post_sol = int(meta["postBalances"][idx])
    native_delta = post_sol - pre_sol + (fee if idx == 0 else 0)

    pre, pre_dec = _token_sums(meta.get("preTokenBalances", []), wallet)
    post, post_dec = _token_sums(meta.get("postTokenBalances", []), wallet)
    decs = {**pre_dec, **post_dec}
    deltas = {m: post.get(m, 0) - pre.get(m, 0) for m in set(pre) | set(post)}
    sol_delta = native_delta + deltas.pop(SOL_MINT, 0)
    usdc_delta = deltas.pop(USDC_MINT, 0)
    movers = {m: d for m, d in deltas.items() if d != 0}
    if len(movers) != 1:
        return ParseResult(None, ReasonCode.UNKNOWN_TX_CLASSIFICATION, f"{len(movers)} token non-quote variati")
    mint, d = next(iter(movers.items()))
    # quote: preferisci USDC se si muove in senso opposto, altrimenti SOL (al netto della fee)
    if usdc_delta != 0 and (usdc_delta > 0) != (d > 0):
        qmint, qd = USDC_MINT, usdc_delta
    elif sol_delta != 0 and (sol_delta > 0) != (d > 0):
        qmint, qd = SOL_MINT, sol_delta
    else:
        return ParseResult(None, ReasonCode.UNKNOWN_TX_CLASSIFICATION, "nessuna contropartita quote: trasferimento?")
    bt = tx.get("blockTime")
    if bt is None:
        return ParseResult(None, ReasonCode.UNKNOWN_TX_CLASSIFICATION, "blockTime mancante")
    swap = SwapObservation(
        signature=signature,
        slot=int(tx.get("slot", 0)),
        wallet=wallet,
        mint=mint,
        side=Side.BUY if d > 0 else Side.SELL,
        token_amount_raw=abs(d),
        decimals=decs.get(mint, 0),
        quote_mint=qmint,
        quote_amount_raw=abs(qd),
        quote_decimals=QUOTE_DECIMALS[qmint],
        # saldo noto solo per gli account token toccati dalla transazione
        wallet_pre_amount_raw=pre.get(mint, 0),
        wallet_post_amount_raw=post.get(mint, 0),
        fee_lamports=fee if idx == 0 else 0,
        event_at=datetime.fromtimestamp(int(bt), UTC),
        program_labels=labels,
    )
    return ParseResult(swap, None)
