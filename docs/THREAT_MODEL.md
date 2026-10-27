# Threat model

## Beni da proteggere

1. Fondi dell'utente (in MVP 0.1 **nessuna chiave** è presente: il bene principale è non introdurne).
2. Chiavi API gratuite (Helius, Jupiter) e token della dashboard.
3. Integrità di decisioni, ledger e report (nessun risultato falsato o ottimistico).
4. Disponibilità entro budget gratuiti (nessun costo a sorpresa).

## Superficie e controlli

| Minaccia | Controllo |
|---|---|
| Esecuzione reale accidentale | `live_enabled=true` o `mode: live` rifiutati dalla config; nessun codice di firma (test che cerca `Keypair`, `solders`, `signTransaction`, endpoint execute); `SolanaRpc` vieta `sendTransaction`; Jupiter usato senza `taker` (nessuna transazione restituita, eventuale campo scartato); modulo `live` solleva sempre `LiveDisabledError`. |
| Furto di segreti | Segreti solo in `.env` (gitignored, 0600 con `m4desk init`), tipo `SecretStr`, mai loggati né mostrati in dashboard; la chiave Helius va in query string solo verso Helius e non viene loggata; nessun segreto inviato all'LLM. |
| Prompt injection da metadati on-chain / API | I metadati sono testo **non fidato**: normalizzati, troncati, privati di caratteri di markup e mai inviati all'LLM. Testo sospetto ⇒ `METADATA_SUSPICIOUS` (token rifiutato). L'LLM riceve solo id di wallet e metriche numeriche; l'output deve rispettare uno schema rigido (`extra=forbid`) e contenere solo id forniti, altrimenti viene ignorato. |
| LLM che "decide" un acquisto | Impossibile per costruzione: l'LLM non ha accesso a broker, ledger o rischio; può solo ordinare candidati già idonei. |
| Dashboard raggiunta da altri host / CSRF / DNS rebinding | Bind su `127.0.0.1` (altri host rifiutati dalla config), middleware che rifiuta `Host` non locali, token richiesto in header `X-M4-Token`, conferma esplicita `{"confirm": true}`, controllo `Origin`, CSP restrittiva, nessuna risorsa da CDN, `X-Frame-Options: DENY`. |
| Provider che cambia piano/autenticazione | HTTP 401/402/403 ⇒ connettore disabilitato con avviso; mai upgrade automatico; budget locali giornalieri/mensili/al minuto. |
| Dati manipolati o incoerenti dai provider | Schemi validati, fonti incrociate (authority on-chain vs Rugcheck), valore LP più prudente, quotazione confrontata col prezzo di mercato, max chase, prezzi stantii esclusi. |
| Supply chain Python | Poche dipendenze note (MIT/BSD/Apache), nessun pacchetto di firma; versioni minime dichiarate in `pyproject.toml`. Consigliato `pip install --require-hashes` in ambienti più rigidi. |
| Wallet malevolo nella watchlist | Solo indirizzi pubblici validati base58; la CLI rifiuta qualunque altro input e avverte di non inserire chiavi o seed phrase. |

## Rischi residui dichiarati

- Il P&L simulato può essere **migliore** del reale (latenza, MEV, fallimenti di transazione reali non modellati).
- RPC pubblico: 429 frequenti e possibili buchi di dati; gap-fill li riduce ma non li elimina.
- Rugcheck e DEX Screener sono servizi terzi gratuiti senza SLA: se cambiano schema i connettori si fermano (fail-closed).
- Il parser riconosce solo programmi DEX noti: swap su programmi nuovi vengono ignorati (`UNKNOWN_TX_CLASSIFICATION`).
