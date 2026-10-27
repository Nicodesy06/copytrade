# Threat model

## Beni da proteggere

1. Fondi dell'utente: con il live attivo, l'intero saldo Solana del wallet la cui chiave è nel Portachiavi.
2. Chiavi API gratuite (Helius, Jupiter) e token della dashboard.
3. Integrità di decisioni, ledger e report (nessun risultato falsato o ottimistico).
4. Disponibilità entro budget gratuiti (nessun costo a sorpresa).

## Superficie e controlli

| Minaccia | Controllo |
|---|---|
| Esecuzione reale accidentale | Live opt-in con doppio consenso (`live_enabled: true` + `network: solana-mainnet` + `live.wallet_pubkey`), chiave nel Portachiavi, preflight e frase "ARMA LIVE" dopo ogni avvio; disarmo automatico (riavvio, SAFE, pausa, errori). Il codice di firma esiste solo in `m4desk/live/` (test che lo verifica); il motore PAPER usa Jupiter senza `taker` e il suo RPC vieta `sendTransaction`. |
| Transazione di swap malevola o alterata | Ispezione prima della firma: un solo firmatario, allowlist di programmi e istruzioni, SOL solo verso il nostro WSOL, nessun Transfer/Approve, priority fee limitata; simulazione on-chain con controllo del saldo. Router RFQ (doppia firma) escluso. |
| Chiave del wallet Fomo (scelta dell'utente) | La chiave controlla tutti i fondi Solana del wallet: importata solo da input nascosto, salvata solo nel Portachiavi, mai in file/log/DB/LLM/dashboard; `forget-key` per revocarla. Consigliato in alternativa un wallet dedicato a basso saldo. Se il Mac è compromesso la chiave è a rischio. |
| Furto di segreti | Segreti solo in `.env` (gitignored, 0600 con `m4desk init`), tipo `SecretStr`, mai loggati né mostrati in dashboard; la chiave Helius va in query string solo verso Helius e non viene loggata; nessun segreto inviato all'LLM. |
| Prompt injection da metadati on-chain / API | I metadati sono testo **non fidato**: normalizzati, troncati, privati di caratteri di markup e mai inviati all'LLM. Testo sospetto ⇒ `METADATA_SUSPICIOUS` (token rifiutato). L'LLM riceve solo id di wallet e metriche numeriche; l'output deve rispettare uno schema rigido (`extra=forbid`) e contenere solo id forniti, altrimenti viene ignorato. |
| LLM che "decide" un acquisto | Impossibile per costruzione: l'LLM non ha accesso a broker, ledger o rischio; può solo ordinare candidati già idonei. |
| Dashboard raggiunta da altri host / CSRF / DNS rebinding | Bind su `127.0.0.1` (altri host rifiutati dalla config), middleware che rifiuta `Host` non locali, token richiesto in header `X-M4-Token`, conferma esplicita `{"confirm": true}`, controllo `Origin`, CSP restrittiva, nessuna risorsa da CDN, `X-Frame-Options: DENY`. |
| Provider che cambia piano/autenticazione | HTTP 401/402/403 ⇒ connettore disabilitato con avviso; mai upgrade automatico; budget locali giornalieri/mensili/al minuto. |
| Dati manipolati o incoerenti dai provider | Schemi validati, fonti incrociate (authority on-chain vs Rugcheck), valore LP più prudente, quotazione confrontata col prezzo di mercato, max chase, prezzi stantii esclusi. |
| Supply chain Python | Poche dipendenze note (MIT/BSD/Apache); le librerie di firma (`solders`, `keyring`) sono usate solo dal pacchetto `live`; versioni minime dichiarate in `pyproject.toml`. Consigliato `pip install --require-hashes` in ambienti più rigidi. |
| Wallet malevolo nella watchlist | Solo indirizzi pubblici validati base58; la CLI rifiuta qualunque altro input e avverte di non inserire chiavi o seed phrase. |

## Rischi residui dichiarati

- Il P&L simulato può essere **migliore** del reale (latenza, MEV, fallimenti di transazione reali non modellati).
- RPC pubblico: 429 frequenti e possibili buchi di dati; gap-fill li riduce ma non li elimina.
- Rugcheck e DEX Screener sono servizi terzi gratuiti senza SLA: se cambiano schema i connettori si fermano (fail-closed).
- Il parser riconosce solo programmi DEX noti: swap su programmi nuovi vengono ignorati (`UNKNOWN_TX_CLASSIFICATION`).
