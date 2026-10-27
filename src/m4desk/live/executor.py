"""Esecuzione REALE (opt-in) che replica gli ordini PAPER della strategia primaria.

Flusso per ogni ordine: limiti → quotazione Jupiter `/swap/v2/order` con `taker` → ispezione della
transazione → simulazione RPC con controllo del saldo → firma locale (chiave nel Portachiavi) →
`/swap/v2/execute` → contabilità con gli importi REALI restituiti. Ogni passo è persistito
(`live_orders`) e notificato. Qualsiasi dubbio ⇒ nessuna firma.

Regole:
- acquisti solo se ARMATO nel processo motore corrente (riavvio ⇒ disarmato) e sistema RUNNING;
- importo fisso `max_sol_per_trade`, tetto giornaliero `max_sol_per_day`, riserva minima di SOL;
- mai toccare token che l'utente detiene per conto suo (si vende solo ciò che il bot ha comprato);
- vendite protettive consentite anche da disarmato; se falliscono restano in coda e si ritentano.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

import httpx
from solders.transaction import VersionedTransaction
from sqlalchemy import func, select

from ..clock import Clock
from ..config import AppConfig
from ..storage.db import Database, LiveOrder, LivePosition, Order
from ..types import LAMPORTS_PER_SOL, SOL_MINT, USDC_MINT, SystemState
from .keystore import KeyStore, KeyStoreError
from .txcheck import check_simulation, inspect_transaction

CONFIRM_PHRASE = "ARMA LIVE"
FEE_RESERVE_LAMPORTS = 10_000_000  # 0,01 SOL per commissioni e rent


class LiveError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason} {detail}".strip())
        self.reason = reason
        self.detail = detail


class JupiterExec:
    def __init__(self, base_url: str, api_key: str | None, timeout_s: float,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base = base_url.rstrip("/")
        self.headers = {"x-api-key": api_key} if api_key else {}
        self.timeout = timeout_s
        self.transport = transport

    async def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport, headers=self.headers) as c:
                r = await c.request(method, self.base + path, **kw)
        except httpx.HTTPError as e:
            raise LiveError("JUPITER_ERROR", type(e).__name__) from e
        if r.status_code == 429:
            raise LiveError("JUPITER_RATE_LIMITED")
        if r.status_code >= 400:
            raise LiveError("JUPITER_ERROR", f"HTTP {r.status_code}")
        data = r.json()
        if not isinstance(data, dict):
            raise LiveError("JUPITER_ERROR", "schema inatteso")
        return data

    async def order(self, input_mint: str, output_mint: str, amount: int, taker: str,
                    exclude_routers: list[str]) -> dict[str, Any]:
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount), "taker": taker}
        if exclude_routers:
            params["excludeRouters"] = ",".join(exclude_routers)
        return await self._req("GET", "/swap/v2/order", params=params)

    async def execute(self, signed_b64: str, request_id: str) -> dict[str, Any]:
        return await self._req("POST", "/swap/v2/execute", json={"signedTransaction": signed_b64,
                                                                 "requestId": request_id})


class LiveRpc:
    """RPC minimo per il live: saldi, saldi token, simulazione, stato firme. Nessun invio."""

    def __init__(self, url: str, timeout_s: float, api_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.url = url
        self.params = {"api-key": api_key} if api_key else None
        self.timeout = timeout_s
        self.transport = transport

    async def call(self, method: str, params: list[Any]) -> Any:
        if method == "sendTransaction":
            raise PermissionError("l'invio avviene solo tramite Jupiter /execute")
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as c:
                r = await c.post(self.url, params=self.params,
                                 json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except httpx.HTTPError as e:
            raise LiveError("RPC_ERROR", type(e).__name__) from e
        if r.status_code == 429:
            raise LiveError("RPC_RATE_LIMITED")
        data = r.json()
        if "error" in data:
            code = data["error"].get("code")
            raise LiveError("RPC_RATE_LIMITED" if code == 429 else "RPC_ERROR", str(code))
        return data.get("result")

    async def balance(self, owner: str) -> int:
        res = await self.call("getBalance", [owner, {"commitment": "confirmed"}])
        return int(res["value"])

    async def token_holdings(self, owner: str, mint: str) -> tuple[int, list[str]]:
        res = await self.call("getTokenAccountsByOwner",
                              [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        total, accts = 0, []
        for a in (res or {}).get("value", []):
            info = a["account"]["data"]["parsed"]["info"]
            total += int(info["tokenAmount"]["amount"])
            accts.append(a["pubkey"])
        return total, accts

    async def simulate(self, tx_b64: str, addresses: list[str]) -> dict[str, Any]:
        res = await self.call("simulateTransaction", [tx_b64, {
            "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "confirmed",
            "accounts": {"encoding": "base64", "addresses": addresses}}])
        return (res or {}).get("value") or {}

    async def signature_ok(self, sig: str) -> bool | None:
        res = await self.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
        st = (res or {}).get("value", [None])[0]
        if st is None:
            return None
        return st.get("err") is None and st.get("confirmationStatus") in ("confirmed", "finalized")


def token_account_amount(sim_acct: dict[str, Any] | None) -> int | None:
    """Quantità da un account token (SPL/Token-2022) restituito in base64 dalla simulazione."""
    if not sim_acct:
        return 0
    try:
        raw = base64.b64decode(sim_acct["data"][0])
        return int.from_bytes(raw[64:72], "little")
    except Exception:
        return None


class LiveExecutor:
    def __init__(self, cfg: AppConfig, db: Database, clock: Clock, keystore: KeyStore, notifier: Any,
                 jupiter: JupiterExec, rpc: LiveRpc, boot_id: str | None = None):
        self.cfg = cfg
        self.lc = cfg.live
        self.db = db
        self.clock = clock
        self.keys = keystore
        self.notifier = notifier
        self.jup = jupiter
        self.rpc = rpc
        self.boot_id = boot_id or uuid.uuid4().hex
        self.consecutive_failures = 0
        self._cap_notified_day: str | None = None
        db.set_meta("engine_boot_id", self.boot_id)

    # --- stato -----------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.live_enabled)

    @property
    def owner(self) -> str:
        assert self.lc.wallet_pubkey
        return self.lc.wallet_pubkey

    def is_armed(self) -> bool:
        return self.enabled and self.db.get_control("live_armed", "") == self.boot_id

    def disarm(self, reason: str, actor: str = "live") -> None:
        if self.db.get_control("live_armed", "") != "":
            self.db.set_control("live_armed", "", actor, f"LIVE DISARMATO: {reason}")
            self._notify_sync("LIVE disarmato", reason, "high", "warning")

    def _notify_sync(self, title: str, msg: str, prio: str = "default", tags: str = "") -> None:
        import asyncio

        if self.notifier is None or not getattr(self.notifier, "enabled", False):
            return
        try:
            asyncio.get_running_loop().create_task(self.notifier.send(title, msg, prio, tags))
        except RuntimeError:
            asyncio.run(self.notifier.send(title, msg, prio, tags))

    async def notify(self, title: str, msg: str, prio: str = "default", tags: str = "") -> None:
        if self.notifier is not None:
            await self.notifier.send(title, msg, prio, tags)

    def spent_today_lamports(self, now: datetime | None = None) -> int:
        day = (now or self.clock.now()).strftime("%Y-%m-%d")
        with self.db.session() as s:
            rows = s.scalars(select(LiveOrder).where(LiveOrder.side == "BUY", LiveOrder.state.in_(
                ["SUBMITTED", "SUCCESS", "UNKNOWN"])))
            return sum(int(r.sol_spent_lamports or r.in_amount_raw or 0) for r in rows
                       if r.created_at.strftime("%Y-%m-%d") == day)

    def open_positions(self) -> list[LivePosition]:
        with self.db.session() as s:
            return list(s.scalars(select(LivePosition).where(LivePosition.status == "OPEN")))

    # --- preflight -----------------------------------------------------------------------
    async def preflight(self, dry_run_swap: bool = True) -> list[tuple[str, bool, str]]:
        out: list[tuple[str, bool, str]] = []
        out.append(("live_enabled e network solana-mainnet", self.enabled, "config.yaml"))
        ok_kc, kc = self.keys.available()
        out.append(("Portachiavi sicuro disponibile", ok_kc, kc))
        pub = None
        try:
            pub = self.keys.pubkey()
        except Exception as e:
            out.append(("Chiave leggibile dal Portachiavi", False, type(e).__name__))
        out.append(("Chiave importata", pub is not None, "m4desk live import-key"))
        match = pub is not None and pub == self.lc.wallet_pubkey
        detail = f"chiave {pub or '-'} / atteso {self.lc.wallet_pubkey or '-'}"
        if pub and not match:
            detail += (f". La chiave importata appartiene a {pub}: se è il tuo wallet (es. quello di Fomo) usa "
                       "`m4desk live use-key-address` o il menu Live → \"Usa l'indirizzo della chiave\"; "
                       "altrimenti importa la chiave giusta.")
        out.append(("La chiave corrisponde a live.wallet_pubkey", match, detail))
        state = self.db.get_control("state", "RUNNING")
        out.append(("Sistema in stato RUNNING", state == SystemState.RUNNING.value, state))
        if not self.lc.wallet_pubkey:
            return out
        try:
            bal = await self.rpc.balance(self.owner)
            need = int((self.lc.min_sol_reserve + self.lc.max_sol_per_trade) * LAMPORTS_PER_SOL) + FEE_RESERVE_LAMPORTS
            detail = f"{bal / LAMPORTS_PER_SOL:.4f} SOL (minimo {need / LAMPORTS_PER_SOL:.4f})"
            if bal < need:
                usdc = 0
                try:
                    usdc, _ = await self.rpc.token_holdings(self.owner, USDC_MINT)
                except LiveError:
                    pass
                detail += (". Il bot compra con SOL e ogni transazione Solana paga le commissioni in SOL: "
                           f"trasferisci almeno {(need - bal) / LAMPORTS_PER_SOL:.4f} SOL su {self.owner}")
                if usdc:
                    detail += f" (il wallet ha {usdc / 1e6:.2f} USDC: puoi convertirne una parte in SOL da Fomo)"
            out.append(("Saldo SOL sufficiente", bal >= need, detail))
        except LiveError as e:
            out.append(("Saldo SOL leggibile via RPC", False, e.reason))
        if dry_run_swap:
            try:
                amount = 1_000_000  # 0,001 SOL → USDC: prova completa SENZA firma né invio
                o = await self.jup.order(SOL_MINT, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", amount, self.owner,
                                         self.lc.exclude_routers)
                tx = o.get("transaction") or ""
                rep = inspect_transaction(tx, self.owner, amount, self.lc.max_priority_fee_lamports) if tx else None
                why = "; ".join(rep.violations) if rep else str(o.get("errorMessage", "nessuna transazione"))
                if not tx and o.get("errorCode") in (1, 2):
                    why += " — servono SOL sul wallet per lo swap di prova (0,001 SOL) e le commissioni"
                out.append(("Jupiter: transazione ricevuta e ispezionata", bool(rep and rep.ok), why))
                if rep and rep.ok:
                    pre = await self.rpc.balance(self.owner)
                    sim = await self.rpc.simulate(tx, [self.owner])
                    v = check_simulation(sim, pre, amount + self.lc.max_fee_margin_lamports)
                    out.append(("Simulazione on-chain superata", not v, "; ".join(v) or "ok"))
            except LiveError as e:
                out.append(("Prova di swap simulata", False, e.reason))
        return out

    async def arm(self, confirm: str, actor: str = "human") -> tuple[bool, str]:
        if confirm.strip().upper() != CONFIRM_PHRASE:
            return False, f'Conferma errata: digita esattamente "{CONFIRM_PHRASE}"'
        checks = await self.preflight()
        failed = [f"{n}: {d}" for n, ok, d in checks if not ok]
        if failed:
            return False, "Preflight non superato: " + " | ".join(failed)
        self.db.set_control("live_armed", self.boot_id, actor, "LIVE ARMATO")
        await self.notify("LIVE armato", f"Wallet {self.owner[:6]}…{self.owner[-4:]} · max "
                          f"{self.lc.max_sol_per_trade} SOL/op, {self.lc.max_sol_per_day} SOL/giorno", "high", "rocket")
        return True, "LIVE armato fino al prossimo riavvio del motore."

    # --- persistenza ----------------------------------------------------------------------
    def _new_order(self, key: str, side: str, mint: str, leader: str | None, paper_id: int | None) -> LiveOrder | None:
        now = self.clock.now()
        with self.db.session() as s:
            if s.scalars(select(LiveOrder).where(LiveOrder.idempotency_key == key)).first():
                return None  # già gestito (riconsegna)
            o = LiveOrder(idempotency_key=key, created_at=now, updated_at=now, side=side, mint=mint, leader=leader,
                          paper_order_id=paper_id, state="PREPARED", reason="OK")
            s.add(o)
            s.flush()
            s.expunge(o)
            return o

    def _exists(self, key: str) -> bool:
        with self.db.session() as s:
            return s.scalars(select(LiveOrder.id).where(LiveOrder.idempotency_key == key)).first() is not None

    def _update(self, oid: int, **f: Any) -> None:
        with self.db.session() as s:
            o = s.get(LiveOrder, oid)
            assert o is not None
            for k, v in f.items():
                setattr(o, k, v)
            o.updated_at = self.clock.now()

    def _skip(self, key: str, side: str, mint: str, leader: str | None, paper_id: int | None, reason: str,
              detail: str = "") -> str:
        o = self._new_order(key, side, mint, leader, paper_id)
        if o is not None:
            self._update(o.id, state="SKIPPED", reason=reason, detail={"note": detail} if detail else None)
        return reason

    # --- pipeline comune: ordine → ispezione → simulazione → firma → esecuzione ---------------
    async def _swap(self, oid: int, input_mint: str, output_mint: str, amount: int, max_wrap: int,
                    max_spend: int, token_check: tuple[str, int] | None) -> dict[str, Any]:
        o = await self.jup.order(input_mint, output_mint, amount, self.owner, self.lc.exclude_routers)
        tx_b64 = o.get("transaction") or ""
        if not tx_b64:
            self._update(oid, state="FAILED", reason="NO_TRANSACTION",
                         detail={"errorCode": o.get("errorCode"), "errorMessage": o.get("errorMessage")})
            raise LiveError("NO_TRANSACTION", str(o.get("errorMessage", "")))
        rep = inspect_transaction(tx_b64, self.owner, max_wrap, self.lc.max_priority_fee_lamports)
        if not rep.ok:
            self._update(oid, state="INSPECT_FAIL", reason="INSPECT_FAIL", request_id=o.get("requestId"),
                         detail={"violations": rep.violations, "programs": rep.programs})
            raise LiveError("INSPECT_FAIL", "; ".join(rep.violations))
        pre = await self.rpc.balance(self.owner)
        addrs = [self.owner] + ([token_check[0]] if token_check else [])
        sim = await self.rpc.simulate(tx_b64, addrs)
        problems = check_simulation(sim, pre, max_spend)
        if token_check and not problems:
            post_tok = token_account_amount((sim.get("accounts") or [None, None])[1])
            if post_tok is None or token_check[1] - post_tok > amount:
                problems.append("la simulazione sottrae più token del previsto")
        if problems:
            self._update(oid, state="SIM_FAIL", reason="SIM_FAIL", request_id=o.get("requestId"),
                         detail={"problems": problems})
            raise LiveError("SIM_FAIL", "; ".join(problems))
        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        try:
            kp = self.keys.signer()
        except KeyStoreError as e:
            self._update(oid, state="FAILED", reason="NO_KEY")
            raise LiveError("NO_KEY", str(e)) from e
        if str(kp.pubkey()) != self.owner:
            self._update(oid, state="FAILED", reason="KEY_MISMATCH")
            raise LiveError("KEY_MISMATCH")
        signed = VersionedTransaction(tx.message, [kp])
        sig = str(signed.signatures[0])
        self._update(oid, state="SUBMITTED", reason="OK", signature=sig, request_id=o.get("requestId"),
                     in_amount_raw=amount)
        res = await self.jup.execute(base64.b64encode(bytes(signed)).decode(), str(o.get("requestId")))
        if res.get("status") != "Success":
            self._update(oid, state="FAILED", reason=f"EXECUTE_{res.get('code')}", detail={"response": {
                k: res.get(k) for k in ("status", "code", "error", "signature")}})
            raise LiveError("EXECUTE_FAILED", f"codice {res.get('code')}")
        post = None
        try:
            post = await self.rpc.balance(self.owner)
        except LiveError:
            pass
        return {"res": res, "pre": pre, "post": post, "sig": res.get("signature") or sig}

    # --- acquisto -----------------------------------------------------------------------------
    async def buy(self, mint: str, leader: str | None, key: str, paper_order_id: int | None = None) -> str:
        if not self.enabled:
            return "LIVE_DISABLED"
        if self._exists(key):
            return "DUPLICATE"
        if not self.is_armed():
            return self._skip(key, "BUY", mint, leader, paper_order_id, "LIVE_NOT_ARMED")
        if self.db.get_control("state", "RUNNING") != SystemState.RUNNING.value:
            return self._skip(key, "BUY", mint, leader, paper_order_id, "SYSTEM_NOT_RUNNING")
        lamports = int((self.lc.max_sol_per_trade * LAMPORTS_PER_SOL).to_integral_value(rounding=ROUND_DOWN))
        now = self.clock.now()
        if self.spent_today_lamports(now) + lamports > int(self.lc.max_sol_per_day * LAMPORTS_PER_SOL):
            day = now.strftime("%Y-%m-%d")
            if self._cap_notified_day != day:
                self._cap_notified_day = day
                await self.notify("LIVE: tetto giornaliero raggiunto", f"Nessun altro acquisto reale oggi "
                                  f"(max {self.lc.max_sol_per_day} SOL).", "default", "no_entry")
            return self._skip(key, "BUY", mint, leader, paper_order_id, "LIVE_DAILY_CAP")
        opens = self.open_positions()
        if len(opens) >= self.lc.max_open_positions:
            return self._skip(key, "BUY", mint, leader, paper_order_id, "LIVE_MAX_POSITIONS")
        if any(p.mint == mint for p in opens):
            return self._skip(key, "BUY", mint, leader, paper_order_id, "DOUBLE_EXPOSURE")
        o = self._new_order(key, "BUY", mint, leader, paper_order_id)
        if o is None:
            return "DUPLICATE"
        try:
            bal = await self.rpc.balance(self.owner)
            if bal - lamports - FEE_RESERVE_LAMPORTS < int(self.lc.min_sol_reserve * LAMPORTS_PER_SOL):
                self._update(o.id, state="SKIPPED", reason="LIVE_INSUFFICIENT_SOL")
                return "LIVE_INSUFFICIENT_SOL"
            held, _ = await self.rpc.token_holdings(self.owner, mint)
            if held > 0:
                # il wallet detiene già questo token per conto dell'utente: non si mescolano le posizioni
                self._update(o.id, state="SKIPPED", reason="LIVE_USER_HOLDING")
                return "LIVE_USER_HOLDING"
            r = await self._swap(o.id, SOL_MINT, mint, lamports, max_wrap=lamports,
                                 max_spend=lamports + self.lc.max_fee_margin_lamports, token_check=None)
        except LiveError as e:
            await self._failure("acquisto", mint, e)
            return e.reason
        res = r["res"]
        got = int(res.get("totalOutputAmount") or res.get("outputAmountResult") or 0)
        spent = (r["pre"] - r["post"]) if r["post"] is not None else int(res.get("totalInputAmount") or lamports)
        self._update(o.id, state="SUCCESS", reason="OK", out_amount_raw=got, sol_spent_lamports=spent,
                     signature=r["sig"])
        with self.db.session() as s:
            s.add(LivePosition(mint=mint, leader=leader, status="OPEN", opened_at=self.clock.now(), qty_raw=got,
                               cost_lamports=spent, proceeds_lamports=0))
        self.consecutive_failures = 0
        await self.notify("LIVE acquisto eseguito", f"{spent / LAMPORTS_PER_SOL:.4f} SOL → {mint[:6]}…\n"
                          f"https://solscan.io/tx/{r['sig']}", "default", "green_circle")
        return "SUCCESS"

    # --- vendita ----------------------------------------------------------------------------
    async def sell(self, mint: str, fraction: Decimal, key: str, reason: str,
                   paper_order_id: int | None = None) -> str:
        if not self.enabled:
            return "LIVE_DISABLED"
        with self.db.session() as s:
            pos = s.scalars(select(LivePosition).where(LivePosition.mint == mint,
                                                       LivePosition.status == "OPEN")).first()
            if pos is not None:
                s.expunge(pos)
        if pos is None:
            return "NO_POSITION"
        if not self.is_armed() and not self.lc.allow_exits_when_disarmed:
            return self._skip(key, "SELL", mint, pos.leader, paper_order_id, "LIVE_NOT_ARMED")
        o = self._new_order(key, "SELL", mint, pos.leader, paper_order_id)
        if o is None:
            return "DUPLICATE"
        try:
            held, accts = await self.rpc.token_holdings(self.owner, mint)
            qty = pos.qty_raw if fraction >= Decimal("0.999") else int(Decimal(pos.qty_raw) * fraction)
            qty = min(qty, held)  # mai più di quanto c'è, mai più di quanto ha comprato il bot
            if qty <= 0:
                self._update(o.id, state="SKIPPED", reason="NOTHING_TO_SELL")
                self._close_if_empty(pos.id, held)
                return "NOTHING_TO_SELL"
            check = (accts[0], held) if len(accts) == 1 else None
            r = await self._swap(o.id, mint, SOL_MINT, qty, max_wrap=0, max_spend=self.lc.max_fee_margin_lamports,
                                 token_check=check)
        except LiveError as e:
            self._mark_pending_exit(pos.id)
            await self._failure(f"vendita ({reason})", mint, e, urgent=True)
            return e.reason
        res = r["res"]
        sold = int(res.get("totalInputAmount") or res.get("inputAmountResult") or qty)
        got = (r["post"] - r["pre"]) if r["post"] is not None else int(res.get("totalOutputAmount") or 0)
        self._update(o.id, state="SUCCESS", reason="OK", in_amount_raw=sold, out_amount_raw=got,
                     sol_spent_lamports=-got, signature=r["sig"], detail={"reason": reason})
        with self.db.session() as s:
            p = s.get(LivePosition, pos.id)
            assert p is not None
            p.qty_raw = max(p.qty_raw - sold, 0)
            p.proceeds_lamports += max(got, 0)
            if p.qty_raw <= pos.qty_raw // 100 or fraction >= Decimal("0.999"):
                p.status, p.closed_at = "CLOSED", self.clock.now()
            pnl = p.proceeds_lamports - p.cost_lamports if p.status == "CLOSED" else None
        self.consecutive_failures = 0
        msg = f"{mint[:6]}… → {got / LAMPORTS_PER_SOL:.4f} SOL ({reason})"
        if pnl is not None:
            msg += f"\nP&L reale posizione: {pnl / LAMPORTS_PER_SOL:+.4f} SOL"
        await self.notify("LIVE vendita eseguita", msg + f"\nhttps://solscan.io/tx/{r['sig']}", "default", "red_circle")
        return "SUCCESS"

    def _mark_pending_exit(self, pos_id: int) -> None:
        self.db.set_checkpoint(f"live_pending_exit:{pos_id}", True, self.clock.now())

    def _close_if_empty(self, pos_id: int, held: int) -> None:
        if held == 0:
            with self.db.session() as s:
                p = s.get(LivePosition, pos_id)
                if p is not None and p.status == "OPEN":
                    p.status, p.closed_at = "CLOSED", self.clock.now()
            self.db.audit("live", "live.position_gone", f"Posizione live {pos_id}: nessun token nel wallet "
                          "(venduti manualmente?) → chiusa", level="WARN")

    async def _failure(self, what: str, mint: str, e: LiveError, urgent: bool = False) -> None:
        self.consecutive_failures += 1
        self.db.audit("live", "live.failure", f"LIVE {what} {mint[:8]}… fallito: {e.reason} {e.detail}"[:500],
                      level="ERROR")
        await self.notify(f"LIVE {what} NON eseguito", f"{mint[:6]}…: {e.reason} {e.detail}"[:300],
                          "urgent" if urgent else "high", "warning")
        if self.consecutive_failures >= self.lc.max_consecutive_failures:
            self.disarm(f"{self.consecutive_failures} errori consecutivi")

    async def retry_pending_exits(self) -> int:
        n = 0
        for p in self.open_positions():
            if self.db.get_checkpoint(f"live_pending_exit:{p.id}"):
                attempt = int(self.db.get_checkpoint(f"live_exit_attempt:{p.id}", 0)) + 1
                self.db.set_checkpoint(f"live_exit_attempt:{p.id}", attempt, self.clock.now())
                r = await self.sell(p.mint, Decimal(1), f"retry-exit:{p.id}:{attempt}", "RETRY_EXIT")
                if r in ("SUCCESS", "NOTHING_TO_SELL"):
                    self.db.set_checkpoint(f"live_pending_exit:{p.id}", False, self.clock.now())
                    n += 1
        return n

    # --- aggancio al motore PAPER --------------------------------------------------------------
    async def on_paper_buy(self, order: Order) -> str:
        return await self.buy(order.mint, order.leader, f"live:{order.id}:BUY", order.id)

    async def on_paper_sell(self, order: Order, fraction: Decimal) -> str:
        return await self.sell(order.mint, fraction, f"live:{order.id}:SELL", order.reason, order.id)

    async def recover(self) -> list[int]:
        """Al riavvio: ordini SUBMITTED senza esito ⇒ verifica on-chain; esito ignoto ⇒ UNKNOWN + avviso."""
        with self.db.session() as s:
            pending = list(s.scalars(select(LiveOrder).where(LiveOrder.state.in_(["SUBMITTED", "PREPARED"]))))
            for o in pending:
                s.expunge(o)
        ids = []
        for o in pending:
            st = None
            if o.signature:
                try:
                    st = await self.rpc.signature_ok(o.signature)
                except LiveError:
                    st = None
            state = "FAILED" if o.state == "PREPARED" or st is False else "UNKNOWN"
            self._update(o.id, state=state, reason="RECOVERED_AFTER_RESTART",
                         detail={"onchain_confirmed": st})
            if o.side == "SELL" and state == "UNKNOWN":
                with self.db.session() as s:
                    p = s.scalars(select(LivePosition).where(LivePosition.mint == o.mint,
                                                             LivePosition.status == "OPEN")).first()
                    pid = p.id if p else None
                if pid:
                    self._mark_pending_exit(pid)
            ids.append(o.id)
        if any(True for _ in ids):
            await self.notify("LIVE: ordini da verificare", f"{len(ids)} ordini interrotti dal riavvio: controlla la "
                              "pagina Live e Solscan.", "high", "warning")
        return ids

    def summary(self) -> dict[str, Any]:
        with self.db.session() as s:
            n_orders = s.scalar(select(func.count()).select_from(LiveOrder))
        return {"enabled": self.enabled, "armed": self.is_armed(), "wallet": self.lc.wallet_pubkey,
                "spent_today_sol": self.spent_today_lamports() / LAMPORTS_PER_SOL,
                "cap_day_sol": float(self.lc.max_sol_per_day), "open_positions": len(self.open_positions()),
                "orders": n_orders}
