# Provider, costo zero e provenienza dei dati

## Conferma costi

- **€0 di abbonamenti software/API.** Tutti i provider usati sono gratuiti o keyless; l'LLM è locale
  (Ollama su `127.0.0.1:11434`). Nessun piano a pagamento viene attivato o suggerito dal software.
- **Le transazioni reali avrebbero comunque costi:** commissioni di rete Solana, priority fee,
  commissioni DEX/aggregatore, rent degli account token, impatto sul prezzo e slippage. In PAPER
  questi costi sono simulati e addebitati al capitale virtuale.

## Verifica (2026-10-27)

Le richieste sono state eseguite davvero e le risposte salvate in `tests/fixtures/captured/`.

| Provider | Endpoint | Auth | Limiti | Uso |
|---|---|---|---|---|
| Solana RPC pubblico (`api.mainnet-beta.solana.com`) | JSON-RPC `getSignaturesForAddress`, `getTransaction` (jsonParsed, `maxSupportedTransactionVersion: 1`), `getAccountInfo`, `getSignatureStatuses`; WS `logsSubscribe` con `mentions: [un pubkey]` | nessuna | `429` osservato già a poche richieste ravvicinate. **Non production-grade.** | storico swap, authority mint, notifiche |
| Helius (opzionale) | stessa interfaccia standard | API key gratuita | piano Free: 0 $/mese, 1M crediti, 10 req/s RPC (pagina piani, aprile 2026). Nessuna carta richiesta. Non usiamo API "enhanced" né `transactionSubscribe`. | alternativa più stabile al pubblico |
| DEX Screener | `GET /tokens/v1/solana/{mint}` | nessuna | la documentazione indica 60 rpm per alcuni endpoint, non dichiarato per questo: budget prudente 50/min | prezzo, liquidità, età della coppia |
| Jupiter Swap API v2 | `GET https://api.jup.ag/swap/v2/order` **senza `taker`** | keyless (limiti ridotti) o chiave gratuita `x-api-key` | budget prudente 30/min | quotazione ritardata per i fill simulati |
| Rugcheck | `GET /v1/tokens/{mint}/report`, `/report/summary` | nessuna per la lettura | budget prudente 10/min, cache 300 s | rischi, authority, holder, LP |
| Ollama | `POST /api/chat` con `format` = JSON schema | locale | concorrenza 1, coda 8, timeout 60 s | ranking consultivo |

### Note di schema importanti

- Rugcheck `lpLockedPct`: per lo stesso token il **summary** ha restituito ~15,96 mentre ogni
  `markets[].lp.lpLockedPct` del **report** valeva 0. Il sistema registra la fonte e usa il valore
  più prudente; se nessuna fonte lo fornisce resta `None` ⇒ rifiuto (`LP_LOCK_UNKNOWN`).
- Jupiter v2 `/order` restituisce `priceImpactPct` (frazione) e `priceImpact` (già in percentuale,
  = `priceImpactPct × 100`): usiamo `priceImpact`, valori negativi trattati come 0.
- Solana: esistono già transazioni versione `1`; con `maxSupportedTransactionVersion: 0` la RPC
  risponde errore `-32015`. Versioni future non supportate vengono ignorate e contate.
- DEX Screener non fornisce l'orario del prezzo: si usa l'orario del fetch (dichiarato nella provenienza).

### Non usati

Endpoint deprecati `lite-api.jup.ag` / `quote-api.jup.ag`, `/swap/v2/execute`, Helius enhanced
websockets / `transactionSubscribe`, PumpPortal, API proprietarie di bot, qualsiasi LLM remoto.

## Limiti del costo zero (onestà)

- Non è possibile costruire una classifica esaustiva dei trader profittevoli di tutta Solana a costo
  zero garantito: la scoperta parte da seed pubblici e da un'esplorazione **limitata** dei token
  già osservati.
- Lo storico ricostruito è parziale (vendite senza acquisto noto, account token non toccati
  dalla transazione): le metriche mostrano `completezza dati` e vengono penalizzate.
- Con RPC pubblico gli aggiornamenti possono arrivare in ritardo: il breaker di ritardo e
  `max_event_age_s` impediscono copie fuori tempo.

## Provenienza dei dati

Ogni decisione salva in `facts`: firma, slot, provider e origine (`provenance`), prezzo di fill del
leader e sua fonte, quotazione/mercato usati, esito e motivi del controllo token. Ogni ordine salva
orari di quotazione e fill, prezzi, commissioni separate, impatto, slippage stimato ed effettivo,
ritardo dal fill del leader. Ogni riga di `token_checks` salva i fatti grezzi con il provider.

## Licenze delle dipendenze

| Libreria | Licenza |
|---|---|
| httpx | BSD-3-Clause |
| websockets | BSD-3-Clause |
| pydantic, pydantic-settings | MIT |
| SQLAlchemy | MIT |
| FastAPI | MIT |
| Starlette | BSD-3-Clause |
| uvicorn | BSD-3-Clause |
| Jinja2 | BSD-3-Clause |
| PyYAML | MIT |
| pytest, pytest-asyncio (dev) | MIT, Apache-2.0 |
| ruff (dev) | MIT |
| Ollama (esterno, opzionale) | MIT |

Nessun asset frontend esterno: CSS/JS/icona sono scritti nel repository.
