# Architettura

M4 CopyDesk è un **monolite modulare asyncio** che gira su un solo Mac (nessun servizio esterno a
pagamento). Gli "agenti" sono servizi logici concorrenti dentro lo stesso processo; comunicano con
**envelope immutabili** passati da un event bus persistito su SQLite (WAL).

```
           ┌──────────── provider (gratuiti, sola lettura) ────────────┐
           │ Solana RPC / Helius free · DEX Screener · Jupiter v2 order │
           │ Rugcheck report+summary · Ollama locale (127.0.0.1)        │
           └───────────────▲───────────────────────────────▲───────────┘
                ProviderGate: budget, rate limit, cache TTL, circuit breaker
                           │                               │
  GapFiller + logsSubscribe│                               │
   ┌───────────────────────┴──┐   LEADER_SWAP   ┌──────────┴──────────┐
   │ ingest (deterministico)  ├────────────────►│ PositionManager      │──► PaperBroker ──► Ledger
   └──────────────────────────┘                 │  (copia proporz.)    │   (2 fasi)      (Decimal)
   ┌──────────────┐ WALLET_CANDIDATE ┌─────────┐│  RiskEngine + prenot.│
   │ Discovery    ├─────────────────►│ Trader  ││  TokenAnalyst        │
   └──────────────┘                  │ Analyst │└──────────┬──────────┘
                    TRADER_SCORED    └────┬────┘   POSITION_CLOSED
                                          ▼               ▼
                                     ┌─────────────────────────┐
                                     │ StrategyAgent (+LLM      │
                                     │ consultivo, coda unica)  │
                                     └─────────────────────────┘
   AuditAgent: invarianti, prezzi stantii, snapshot equity, report     Supervisor: avvio, recovery,
   giornalieri (DAY_END)                                               loop con backoff, heartbeat
```

## Principi

- **Deterministico prima dell'AI.** Monitoraggio chain, contabilità, rischio ed esecuzione simulata
  sono codice Python deterministico. L'LLM (un solo modello condiviso, `qwen3.5:9b`, concorrenza 1,
  coda limitata, timeout) può solo **riordinare** wallet già idonei nella variante `ai_rank`.
- **Fail-closed.** Ogni dato mancante, vecchio, incoerente o fuori budget produce un `ReasonCode`
  di rifiuto. Un errore non diventa mai un acquisto.
- **Stesso flusso, più strategie.** `switch2` (primaria), `switch3`, `naive`, `ai_rank`, `flat20`
  ricevono esattamente gli stessi eventi e hanno ledger virtuali separati: il confronto è a parità
  di input, ritardi e slippage.

## Envelope ed event bus

`EventEnvelope` (pydantic, `frozen=True`): `event_id`, `type`, `source`, `slot`, `observed_at`,
`event_at`, `wallet`, `token`, `idempotency_key`, `provenance{provider, origin, ref, fetched_at}`,
`payload`.

- **Dedupe deterministico:** `idempotency_key` = hash(tipo, chiavi naturali) con vincolo UNIQUE.
  Una firma Solana consegnata due volte viene scartata.
- **At-least-once:** l'evento viene persistito prima della consegna; ogni consumer registra la
  conferma (`consumer_done`). Al riavvio `redeliver_pending()` riconsegna ciò che non è confermato.
  Gli handler sono idempotenti (vincolo UNIQUE su decisione per strategia/evento/lato e su
  idempotency key dell'ordine).
- **Code limitate:** `asyncio.Queue(maxsize)` ⇒ backpressure sul produttore.
- **Checkpoint:** cursori per wallet (gap-fill), funnel di scoperta, equity d'inizio giornata.

## Ordini a due fasi

1. `PENDING` persistito (idempotency key unica).
2. Attesa `fill_delay_s`, nuova quotazione, controlli (età quotazione, impatto, coerenza col
   mercato, liquidità, **max chase** rispetto al fill osservato del leader).
3. In **una sola transazione DB**: ordine `FILLED` + scritture del ledger + posizione.

Al riavvio gli ordini `PENDING` diventano `FAILED / RECOVERED_INCOMPLETE_ORDER` e le prenotazioni
vengono rilasciate: mai un acquisto implicito.

## Contabilità

- Importi `Decimal`, quantità raw intere (salvate come stringhe: superano 2^63), decimali del token.
- Costo = nozionale + commissione rete + priorità + DEX + rent dell'account token.
- Vendita parziale: P&L = incasso netto − quota proporzionale del costo residuo.
- Rent rimborsato solo alla chiusura totale.
- Identità verificata dall'AuditAgent: `cassa + costo_residuo − P&L_realizzato = capitale_iniziale`.
- Equity **prudenziale** (usata per dimensionare) esclude le posizioni con prezzo stantio.

## Regole di copia

- **Acquisto:** solo se il wallet è `SELECTED` nella strategia e il RiskEngine approva.
- **Vendita:** frazione = venduto / saldo del leader prima della vendita (da `preTokenBalances`).
  Saldo ignoto ⇒ uscita totale prudenziale.
- **Perdita:** posizione copiata chiusa **completamente** con P&L netto < 0. Le chiusure parziali
  si accumulano sulla posizione; il conteggio avviene solo a chiusura totale.
- **Leader non più seguito:** stato `BLOCKED` per i nuovi ingressi; le sue posizioni aperte restano
  in gestione (vendite replicate, stop/TP, tempo massimo). Nessuna posizione orfana.
- **Sostituzione:** dopo il blocco si ricalcolano i punteggi con i soli dati osservati fino a quel
  momento; se nessun candidato supera le soglie lo slot resta vuoto (`NO_REPLACEMENT_CANDIDATE`).

## Stati del sistema

`RUNNING` → `PAUSED` (PAUSA NUOVI ACQUISTI) → `RUNNING` (RIPRENDI)
`*` → `SAFE` (automatico: invarianti violati, uscita non affidabile, loop instabile) → `RUNNING` solo da umano
`*` → `STOPPED` (ARRESTA) → `RUNNING` solo da umano

Lo stato vive nella tabella `control` del DB: CLI e dashboard (anche in processi separati)
agiscono subito e ogni transizione è nell'audit log.

## Modalità

| Modalità | Provider | Orologio | Etichetta |
|---|---|---|---|
| `m4desk demo` | fixture sintetica inclusa | simulato | SIMULATO / DATI DI ESEMPIO |
| `m4desk replay F` | fixture JSON | simulato | REPLAY (o SIMULATO se la fixture è sintetica) |
| `m4desk run --mode paper` | provider gratuiti reali, sola lettura | reale | MAINNET READ-ONLY |
| live | — | — | **NON IMPLEMENTATO** |
