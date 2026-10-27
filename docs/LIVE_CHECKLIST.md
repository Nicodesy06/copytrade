# Checklist modalità LIVE — SOLO FUTURO

> **Stato: NON IMPLEMENTATO.** In MVP 0.1 l'esecuzione reale è disabilitata e la configurazione
> la rifiuta. Questa lista descrive i prerequisiti minimi prima di scrivere una sola riga di codice
> live. Se uno qualsiasi non è realizzabile, il live resta disabilitato.

Motivo per cui oggi è disabilitato: non sono ancora implementati né revisionati la firma isolata,
l'allowlist dei programmi con ispezione della transazione, i limiti di spesa on-chain e un periodo
di forward-paper sufficiente. Senza questi controlli il rischio di perdita totale è inaccettabile.

## Prerequisiti di processo

- [ ] Almeno 30 giorni di **forward-paper** con la stessa versione di strategia, report giornalieri archiviati.
- [ ] Analisi dei risultati con intervalli di confidenza e mediane, non solo i picchi.
- [ ] Revisione indipendente del codice live e del threat model aggiornato.
- [ ] Nessuna attivazione automatica dopo un backtest: decisione umana documentata.

## Prerequisiti tecnici

- [ ] Componente live **separato** (processo/pacchetto distinto) con configurazione runtime propria.
- [ ] Wallet **dedicato** a basso saldo; nessun auto-funding, nessun prelievo, nessun bridge.
- [ ] Firma isolata: macOS Keychain / Secure Enclave dove compatibile, oppure processo di firma
      separato senza accesso a rete e LLM. La chiave non entra mai in log, prompt o DB.
- [ ] Allowlist rigida dei programmi di swap; rifiuto di qualsiasi istruzione di trasferimento a
      destinatari arbitrari, approvazioni, chiusura account non propri.
- [ ] Simulazione (`simulateTransaction`) e **ispezione** della transazione prima della firma:
      mint in/out, importi, destinatari, slippage massimo, programmi invocati.
- [ ] Limiti di spesa per ordine, per giorno e totali, applicati prima della firma.
- [ ] Preflight bloccante (`m4desk.live.preflight()` tutto verde) e conferma umana esplicita
      ad ogni armamento ("ARMA LIVE" con digitazione di conferma), disarmo automatico al riavvio.
- [ ] Riconciliazione on-chain dei saldi reali vs ledger dopo ogni fill; mismatch ⇒ SAFE.
- [ ] Niente leva, margine, derivati.

## Costi reali da considerare

Commissioni di rete, priority fee, commissioni DEX/aggregatore, rent degli account, impatto,
slippage, MEV e transazioni fallite (che pagano comunque la fee di rete).
