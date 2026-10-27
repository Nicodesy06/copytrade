"""Ispezione deterministica della transazione PRIMA della firma (fail-closed).

Una transazione viene firmata solo se TUTTE queste condizioni valgono:
- un solo firmatario richiesto, che è il nostro wallet (fee payer);
- ogni programma invocato al primo livello è in allowlist (ComputeBudget, System, Token, Token-2022,
  Associated Token, Jupiter v6);
- System: solo `Transfer` dal nostro wallet al NOSTRO account WSOL (wrap) e non oltre l'importo dello swap;
- Token: solo `SyncNative` e `CloseAccount` con destinazione il nostro wallet; nessun Transfer/Approve;
- Associated Token: creazione pagata da noi per un account nostro;
- priority fee entro il limite;
- nessun account di queste istruzioni proveniente da lookup table (non verificabile ⇒ rifiuto).
Gli effetti interni della route Jupiter sono poi verificati con la simulazione (`check_simulation`).
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass, field
from typing import Any

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
WSOL = "So11111111111111111111111111111111111111112"
ALLOWED_PROGRAMS = {COMPUTE_BUDGET, SYSTEM, TOKEN, TOKEN_2022, ATA, JUPITER_V6}


def ata_address(owner: str, mint: str, token_program: str = TOKEN) -> str:
    pda, _ = Pubkey.find_program_address(
        [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)), bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(ATA),
    )
    return str(pda)


@dataclass
class TxReport:
    ok: bool
    violations: list[str] = field(default_factory=list)
    programs: list[str] = field(default_factory=list)
    priority_fee_lamports: int = 0
    wrap_lamports: int = 0
    signers: int = 0


def decode(tx_b64: str) -> VersionedTransaction:
    return VersionedTransaction.from_bytes(base64.b64decode(tx_b64))


def inspect_transaction(tx_b64: str, owner: str, max_wrap_lamports: int, max_priority_fee_lamports: int) -> TxReport:
    v: list[str] = []
    try:
        tx = decode(tx_b64)
    except Exception as e:
        return TxReport(False, [f"transazione non decodificabile ({type(e).__name__})"])
    msg = tx.message
    keys = [str(k) for k in msg.account_keys]
    n_static = len(keys)
    signers = msg.header.num_required_signatures
    rep = TxReport(False, v, signers=signers)
    if signers != 1:
        v.append(f"firmatari richiesti {signers} (atteso 1: solo il nostro wallet)")
    if not keys or keys[0] != owner:
        v.append("il fee payer non è il nostro wallet")
    wsol_ata = ata_address(owner, WSOL)
    cu_limit, cu_price = 200_000, 0

    def acct(ix: Any, i: int) -> str | None:
        idx = ix.accounts[i] if i < len(ix.accounts) else None
        return keys[idx] if idx is not None and idx < n_static else None

    for ix in msg.instructions:
        if ix.program_id_index >= n_static:
            v.append("programma invocato da lookup table")
            continue
        pid = keys[ix.program_id_index]
        rep.programs.append(pid)
        data = bytes(ix.data)
        if pid not in ALLOWED_PROGRAMS:
            v.append(f"programma non in allowlist: {pid}")
        elif pid == COMPUTE_BUDGET:
            if data[:1] == b"\x02" and len(data) >= 5:
                cu_limit = struct.unpack_from("<I", data, 1)[0]
            elif data[:1] == b"\x03" and len(data) >= 9:
                cu_price = struct.unpack_from("<Q", data, 1)[0]
            elif data[:1] not in (b"\x01",):
                v.append("istruzione ComputeBudget non ammessa")
        elif pid == SYSTEM:
            kind = struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else -1
            if kind != 2 or len(data) < 12:
                v.append(f"istruzione System non ammessa (tipo {kind})")
                continue
            lamports = struct.unpack_from("<Q", data, 4)[0]
            src, dst = acct(ix, 0), acct(ix, 1)
            if src != owner or dst != wsol_ata:
                v.append(f"trasferimento SOL verso un account non nostro ({dst})")
            rep.wrap_lamports += lamports
        elif pid in (TOKEN, TOKEN_2022):
            kind = data[0] if data else -1
            if kind == 17:  # SyncNative
                continue
            if kind == 9:  # CloseAccount: [account, destination, owner]
                if acct(ix, 1) != owner or acct(ix, 2) != owner:
                    v.append("CloseAccount verso un destinatario non nostro")
                continue
            v.append(f"istruzione Token non ammessa (tipo {kind})")
        elif pid == ATA:
            if data[:1] not in (b"", b"\x00", b"\x01"):
                v.append("istruzione Associated Token non ammessa")
            elif acct(ix, 0) != owner or acct(ix, 2) != owner:
                v.append("creazione account token non pagata/posseduta dal nostro wallet")
    rep.priority_fee_lamports = cu_limit * cu_price // 1_000_000
    if rep.priority_fee_lamports > max_priority_fee_lamports:
        v.append(f"priority fee {rep.priority_fee_lamports} lamports oltre il limite {max_priority_fee_lamports}")
    if rep.wrap_lamports > max_wrap_lamports:
        v.append(f"SOL trasferiti {rep.wrap_lamports} oltre l'importo dello swap {max_wrap_lamports}")
    if JUPITER_V6 not in rep.programs:
        v.append("nessuna istruzione di swap Jupiter")
    rep.ok = not v
    return rep


def check_simulation(sim_value: dict[str, Any] | None, pre_lamports: int, max_spend_lamports: int,
                     owner_index: int = 0) -> list[str]:
    """Verifica il risultato di `simulateTransaction` (accounts = [nostro wallet, ...])."""
    if not isinstance(sim_value, dict):
        return ["simulazione non disponibile"]
    if sim_value.get("err") is not None:
        return [f"la simulazione fallisce: {str(sim_value['err'])[:120]}"]
    accts = sim_value.get("accounts") or []
    if len(accts) <= owner_index or not accts[owner_index]:
        return ["saldo simulato del wallet non disponibile"]
    post = int(accts[owner_index]["lamports"])
    spent = pre_lamports - post
    if spent > max_spend_lamports:
        return [f"la simulazione sottrae {spent} lamports, oltre il massimo consentito {max_spend_lamports}"]
    return []
