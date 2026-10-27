# PLAN.md — M4 CopyDesk

Documento di avanzamento: le decisioni sono fissate qui man mano che vengono verificate con test.
Il software è uno strumento di **ricerca e simulazione (PAPER)**: non promette rendimenti.

## Dipendenze verificate (2026-10-27)

Verifiche fatte con richieste reali, senza chiavi; le risposte catturate sono in
`tests/fixtures/captured/` e i test di parsing girano offline su queste.

| Provider | Endpoint usati | Auth | Note / limiti verificati |
|---|---|---|---|
| Solana RPC pubblico | `getSignaturesForAddress`, `getTransaction` (jsonParsed, `maxSupportedTransactionVersion: 1`), `getAccountInfo`, WS `logsSubscribe` (`mentions` con **un** pubkey) | nessuna | Risponde `429` già con poche richieste: solo esperimenti. Esistono già transazioni versione `1`: con `maxSupportedTransactionVersion: 0` la RPC risponde errore `-32015`. |
| Helius (opzionale) | stessa interfaccia JSON-RPC standard + WS standard | API key gratuita | Piano Free: 0 $/mese, 1M crediti, 10 req/s RPC (pagina piani dal 7/4/2026). Nessun endpoint "enhanced" richiesto. |
| DEX Screener | `GET /tokens/v1/solana/{mints}` | nessuna | Liquidità, prezzo, `pairCreatedAt`. La risposta **non** contiene timestamp del prezzo: usiamo l'ora di fetch + TTL. Budget prudente 60 rpm. |
| Jupiter | `GET https://api.jup.ag/swap/v2/order` **senza `taker`** | keyless (limite ridotto) o chiave gratuita `x-api-key` | Senza `taker` non viene restituita alcuna transazione (`transaction: null`): usato solo come quotazione. `lite-api.jup.ag` è deprecato: non usato. `/execute` mai chiamato. |
| Rugcheck | `GET /v1/tokens/{mint}/report/summary`, `GET /v1/tokens/{mint}/report` | nessuna (lettura) | `lpLockedPct` del summary (es. 15.96) ≠ `markets[].lp.lpLockedPct` (0) per lo stesso token: si salva la fonte e `None` = sconosciuto ⇒ rifiuto. |
| Ollama locale | `POST http://127.0.0.1:11434/api/chat` con `format` (JSON schema) | locale | Modello `qwen3.5:9b`. Solo consultivo. |

Non usati / vietati: endpoint a pagamento, Helius `transactionSubscribe` (enhanced), PumpPortal,
API proprietarie di bot, endpoint Jupiter deprecati, qualsiasi LLM remoto.

## Fasi

- [x] **F0** Ispezione repo (vuoto), verifica provider, `PLAN.md`.
- [x] **F1** Fondamenta: tipi/enum/reason code, config versionata (hash strategia), SQLite WAL, event bus tipizzato con dedupe persistente.
- [x] **F2** Contabilità: ledger in `Decimal`, commissioni (rete, priorità, DEX, rent), impatto prezzo, slippage stimato vs effettivo, P&L realizzato/non realizzato, prezzi stantii.
- [x] **F3** Risk engine deterministico fail-closed + paper broker con quotazione ritardata, ordini a due fasi, ordini non eseguibili.
- [x] **F4** Agenti: Discovery, TraderAnalyst, TokenAnalyst, Strategy (+LLM consultivo), PositionManager, Audit, Supervisor.
- [x] **F5** Demo/replay offline deterministico da fixture sintetica etichettata `SIMULATO / DATI DI ESEMPIO`.
- [x] **F6** Dashboard italiana (FastAPI + Jinja, nessun CDN), azioni PAUSA/RIPRENDI/ARRESTA protette e auditate.
- [x] **F7** CLI `m4desk` completa, `launchd`, documentazione, test.
- [x] **F8** Adapter reali (RPC, DEX Screener, Jupiter, Rugcheck) con budget, cache, backoff, circuit breaker; modalità PAPER mainnet read-only.
  Verificato dal vivo il 2026-10-27: `m4desk test-providers` tutto OK; percorso controllo token reale →
  quotazione Jupiter ritardata → fill virtuale riuscito (BONK, 50 $, impatto 0,087%, slippage 55 bps,
  ritardo 7 s, 6 richieste totali). L'RPC pubblico da IP cloud condiviso risponde `429` dopo poche
  richieste: il sistema resta fail-closed e riprova ogni 2 minuti; per uso continuativo serve Helius free.
- [ ] **F9 (futuro)** Ricostruzione storica estesa con finestre hold-out su dati reali (richiede raccolta forward-paper).
- [ ] **F10 (futuro, opzionale)** Modulo live isolato: **non implementato**, vedi `docs/LIVE_CHECKLIST.md`.

## Addendum sicurezza (04_ADDENDUM_SAFETY_FMEA_E_RISK_ENGINE.md)

Il file dell'addendum **non era presente** nel repository al momento dello sviluppo. Sono stati
implementati i controlli elencati nel prompt: verifiche di contratto (invarianti contabili ⇒ SAFE),
max chase 15% (ipotesi di test) tra fill osservato del leader e quotazione eseguibile, sia pre-trade
sia all'esecuzione, libri a frazione fissa per leader, prenotazioni di posizione atomiche a livello
di portafoglio, breaker di ritardo feed/quotazioni, uscite sicure e registro FMEA (`docs/FMEA.md`)
con test negativi bloccanti (`tests/test_fmea.py`). Da riconciliare col testo dell'addendum quando
sarà aggiunto al repository.

## Decisioni fissate

1. Monolite modulare asyncio (nessun LangGraph: il flusso è lineare e i checkpoint sono già nel DB).
2. Un solo processo motore; la dashboard può girare in un processo separato e comunica solo tramite il DB
   (tabella `control` + `audit_log`), quindi PAUSA/RIPRENDI/ARRESTA funzionano anche da CLI.
3. Strategie confrontate su **identico flusso di eventi**: `switch2` (primaria), `switch3`, `naive`,
   `ai_rank`, `flat20` — ognuna con ledger virtuale separato.
4. Perdita = posizione copiata **chiusa completamente** con P&L netto (tutte le commissioni) < 0.
   Le chiusure parziali accumulano P&L sulla posizione; il conteggio avviene solo alla chiusura totale.
5. Copia delle vendite: frazione = quantità venduta dal leader / saldo del leader **prima** della vendita
   (da `preTokenBalances`). Se il saldo precedente è sconosciuto ⇒ uscita totale prudenziale (`LEADER_HOLDINGS_UNKNOWN`).
6. Leader non più seguito: le posizioni aperte restano in gestione (`solo gestione`): si continuano a
   copiare le sue vendite e valgono stop/take-profit/tempo massimo. Nessuna posizione orfana.
7. Stop-loss simulato eseguito al prezzo **osservato dopo il ritardo**, mai al prezzo dello stop.
8. Ordine a due fasi: `PENDING` persistito → `FILLED`/`REJECTED`/`UNFILLABLE` in un'unica transazione DB
   insieme alle scritture del ledger. Al riavvio gli ordini `PENDING` diventano `FAILED`
   (`RECOVERED_INCOMPLETE_ORDER`): mai un acquisto implicito.
9. L'LLM può solo riordinare candidati già idonei; output fuori schema ⇒ ignorato, si usa la baseline.
