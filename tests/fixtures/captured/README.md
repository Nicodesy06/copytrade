# Risposte reali catturate (dati pubblici)

Catturate il 2026-10-27 dagli endpoint pubblici gratuiti per verificare gli schemi
usati dagli adapter (nessuna chiave, nessun dato privato). Usate solo nei test offline.

- `dexscreener_tokens_v1.json`: `GET https://api.dexscreener.com/tokens/v1/solana/{mint}` (BONK)
- `rug_summary.json`: `GET https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary` (BONK)
- `rug_report.json`: `GET https://api.rugcheck.xyz/v1/tokens/{mint}/report` (BONK, liste troncate a 10 elementi)
- `jup_order.json`: `GET https://api.jup.ag/swap/v2/order` senza `taker` (nessuna transazione restituita)
- `rpc_mint_account.json`, `rpc_mint_account_t22.json`: `getAccountInfo` jsonParsed (SPL e Token-2022 con estensioni)
- `tx.json`: `getTransaction` jsonParsed, `maxSupportedTransactionVersion: 1` (transazione pubblica swap Jupiter)

Nota: per BONK il summary riporta `lpLockedPct≈15.96`, mentre i singoli mercati del report
riportano `lp.lpLockedPct = 0`: il valore dipende dall'endpoint e non va mai assunto.
- `jup_order_taker.json`: `GET /swap/v2/order` CON `taker` (indirizzo pubblico) e `excludeRouters=jupiterz`:
  transazione v0 NON firmata (1 firmatario) per 0,01 SOL → BONK. Mai firmata né inviata.
- `rpc_simulate_buy.json`: `simulateTransaction` della transazione sopra (`sigVerify: false`,
  `replaceRecentBlockhash: true`): saldo del taker dopo la simulazione = saldo − 0,01 SOL − commissioni.
