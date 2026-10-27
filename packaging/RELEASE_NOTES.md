## M4 CopyDesk 0.1.0 — app per Mac (Apple Silicon)

Sistema locale di **ricerca** sul copy-trading Solana. Non è una promessa di rendimento.

**Scarica** `M4-CopyDesk-0.1.0-macOS-arm64.dmg` qui sotto (verifica con il file `.sha256`).

### Cosa contiene
- App nella barra dei menu ("M4") con dashboard locale in italiano su `http://127.0.0.1:8765`.
- Demo offline con dati sintetici (etichettati SIMULATO / DATI DI ESEMPIO) al primo avvio.
- Motore **PAPER** su mainnet in sola lettura: scoperta di trader da wallet pubblici, analisi dei token,
  5 strategie confrontate, operazioni simulate con commissioni e slippage reali, report giornalieri.
- **Live con soldi veri (opzionale, spento di default)**: replica gli ordini paper della strategia
  primaria con la chiave del tuo wallet (es. Fomo) salvata solo nel Portachiavi; 0,05 SOL per acquisto,
  0,2 SOL al giorno; ispezione e simulazione di ogni transazione prima della firma; si arma con
  "ARMA LIVE" e si disarma a ogni riavvio. Notifiche sul telefono con ntfy.

### Installazione
1. Apri il DMG e trascina "M4 CopyDesk" in Applicazioni.
2. L'app è firmata ad-hoc ma **non notarizzata**: al primo avvio clic destro → Apri, oppure
   Impostazioni di Sistema → Privacy e sicurezza → "Apri comunque".
3. Istruzioni complete nel file `LEGGIMI.txt` dentro il DMG.

### Avvertenze
- Con il live attivo puoi perdere l'intero importo impiegato. Parti con i limiti minimi.
- La prima esecuzione firmata avverrà sul tuo Mac: durante lo sviluppo nessun ordine reale è stato inviato.
- €0 di abbonamenti; le operazioni reali hanno comunque costi di rete, commissioni DEX e slippage.
