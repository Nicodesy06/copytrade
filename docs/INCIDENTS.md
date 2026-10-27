# Procedure per incidenti

Regola generale: **nel dubbio, PAUSA.** Le uscite protettive restano attive in pausa.

| Situazione | Cosa vedi | Cosa fare |
|---|---|---|
| Modalità SAFE attivata | banner rosso, pagina Rischi, `m4desk status` | Leggi la causa in Log/Rischi (invariante violato, uscita non affidabile, loop instabile). Correggi la causa, poi `m4desk resume`. SAFE non si chiude da solo. |
| Uscita non affidabile (`EXIT_NOT_RELIABLE`) | allarme ERROR in Log | La posizione resta aperta nel ledger (nessuna uscita inventata). Verifica liquidità/quotazione del token; il sistema riproverà ai tick successivi con un nuovo tentativo idempotente. |
| Provider in 429 / circuit breaker | `CIRCUIT_OPEN` in Panoramica/Budget | Nessuna azione: backoff automatico. Se persistente, riduci `discovery.max_candidates` o passa a Helius free. |
| Budget esaurito | `BUDGET_ESAURITO` | Scoperta e nuovi acquisti si fermano fino al giorno/mese successivo. Non comprare crediti: alza il budget solo se il piano gratuito lo consente davvero. |
| Rugcheck disabilitato o in errore | "SOLO OSSERVAZIONE" / `RUGCHECK_MISSING` | Nessun nuovo acquisto (stop rigido). Controlla `m4desk test-providers`. |
| Connettore disabilitato (401/402/403) | audit `provider.disabled` | Il piano o l'autenticazione sono cambiati: verifica la documentazione ufficiale. Non attivare piani a pagamento. |
| Breaker di ritardo aperto | Rischi → Breaker | Rete o RPC lenti: attendi il rientro automatico (isteresi) o cambia RPC. |
| Crash / riavvio del Mac | — | Riavvia (`launchd` lo fa da solo). Al riavvio: ordini `PENDING` → `FAILED`, prenotazioni rilasciate, eventi non confermati riconsegnati, gap-fill dai checkpoint. |
| Sospetta compromissione di una chiave API | — | `m4desk stop`, rigenera la chiave dal portale del provider, aggiorna `.env`. Nessuna chiave di wallet è presente nel sistema. |
| Database corrotto | errori SQLite | `m4desk stop`, copia `data/paper.db*`, ripristina un backup o riparti con un DB nuovo (il P&L è virtuale). |

## Backup

```zsh
m4desk pause
sqlite3 data/paper.db ".backup 'data/paper-$(date +%Y%m%d).db'"
m4desk resume
```
