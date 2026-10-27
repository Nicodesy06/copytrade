"""Solana JSON-RPC standard (pubblico o Helius free). Solo lettura: nessun metodo di invio esiste qui."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..types import DataOrigin, ReasonCode, SwapObservation
from .base import MintInfo, ProviderError
from .http import HttpJson
from .swap_parser import parse_swap

PROGRAM_NAMES = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "spl-token",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "spl-token-2022",
}
FORBIDDEN_METHODS = {"sendTransaction", "requestAirdrop", "simulateTransaction"}


class SolanaRpc:
    name = "rpc"

    def __init__(self, http: HttpJson, url: str, api_key: str | None = None, origin=DataOrigin.MAINNET_READONLY):
        self.http = http
        self.url = url
        self._params = {"api-key": api_key} if api_key else None
        self.origin = origin
        self._tx_cache: dict[str, dict[str, Any] | None] = {}
        self.unsupported_versions = 0
        self.unclassified = 0

    async def call(self, method: str, params: list[Any], cache_key: str | None = None, ttl_s: float = 0) -> Any:
        if method in FORBIDDEN_METHODS:
            raise PermissionError(f"metodo {method} vietato in modalità read-only")
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        res = await self.http.post(self.url, body, cache_key=cache_key, ttl_s=ttl_s, params=self._params)
        if not isinstance(res, dict):
            raise ProviderError(self.name, ReasonCode.PROVIDER_ERROR, "risposta non valida")
        if "error" in res:
            code = res["error"].get("code")
            if code == 429:
                raise ProviderError(self.name, ReasonCode.PROVIDER_RATE_LIMITED, "RPC 429")
            if code == -32015:
                self.unsupported_versions += 1
                raise ProviderError(self.name, ReasonCode.UNKNOWN_TX_CLASSIFICATION, "versione tx non supportata")
            raise ProviderError(self.name, ReasonCode.PROVIDER_ERROR, f"RPC {code}")
        return res.get("result")

    async def get_mint_info(self, mint: str) -> MintInfo:
        res = await self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
                              cache_key=f"mint:{mint}", ttl_s=300)
        try:
            v = res["value"]
            info = v["data"]["parsed"]["info"]
            if v["data"]["parsed"]["type"] != "mint":
                raise KeyError("non è una mint")
            return MintInfo(
                mint=mint,
                program=PROGRAM_NAMES.get(v["owner"], v["owner"]),
                decimals=int(info["decimals"]),
                mint_authority=info.get("mintAuthority"),
                freeze_authority=info.get("freezeAuthority"),
                extensions=tuple(e["extension"] for e in info.get("extensions", []) or []),
                fetched_at=datetime.now(UTC),
                provider=self.name,
                origin=self.origin,
            )
        except (KeyError, TypeError) as e:
            raise ProviderError(self.name, ReasonCode.AUTHORITY_UNKNOWN, "schema getAccountInfo") from e

    async def _signatures(self, address: str, limit: int, until: str | None = None) -> list[dict[str, Any]]:
        opts: dict[str, Any] = {"limit": limit, "commitment": "confirmed"}
        if until:
            opts["until"] = until
        res = await self.call("getSignaturesForAddress", [address, opts])
        return res if isinstance(res, list) else []

    async def get_transaction(self, sig: str) -> dict[str, Any] | None:
        if sig in self._tx_cache:
            return self._tx_cache[sig]
        tx = await self.call("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                      "maxSupportedTransactionVersion": 1}])
        self._tx_cache[sig] = tx
        if len(self._tx_cache) > 5000:
            self._tx_cache.pop(next(iter(self._tx_cache)))
        return tx

    async def _parse_many(self, sigs: list[dict[str, Any]], wallet: str | None) -> list[SwapObservation]:
        out: list[SwapObservation] = []
        for s in sigs:
            if s.get("err") is not None:
                continue
            try:
                tx = await self.get_transaction(s["signature"])
            except ProviderError as e:
                if e.reason == ReasonCode.UNKNOWN_TX_CLASSIFICATION:
                    continue
                raise
            if tx is None:
                continue
            signer = wallet
            if signer is None:
                keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                signer = keys[0]["pubkey"] if keys and isinstance(keys[0], dict) else (keys[0] if keys else None)
            if signer is None:
                continue
            r = parse_swap(tx, signer, s["signature"])
            if r.swap is None:
                self.unclassified += 1
                continue
            out.append(r.swap)
        return out

    async def wallet_swaps(self, wallet: str, limit: int, until_signature: str | None = None
                           ) -> list[SwapObservation]:
        return await self._parse_many(await self._signatures(wallet, limit, until_signature), wallet)

    async def token_swaps(self, mint: str, limit: int) -> list[SwapObservation]:
        return await self._parse_many(await self._signatures(mint, limit), None)

    async def head_signature(self, wallet: str) -> str | None:
        sigs = await self._signatures(wallet, 1)
        return sigs[0]["signature"] if sigs else None

    async def is_confirmed(self, signature: str) -> bool | None:
        res = await self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        v = (res or {}).get("value", [None])[0]
        if v is None:
            return False
        return v.get("err") is None and v.get("confirmationStatus") in ("confirmed", "finalized")
