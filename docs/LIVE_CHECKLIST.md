# Modalità LIVE (soldi veri) — guida e checklist

> **Rischio reale.** Una volta armato, il bot compra e vende **da solo** con il tuo wallet. Puoi perdere
> l'intero importo impiegato. Nessun rendimento è garantito. Le soglie sono ipotesi prudenti, non garanzie.

## Come funziona

- Il live **non decide nulla da solo**: replica soltanto gli ordini PAPER *eseguiti* della strategia
  primaria (`switch2`), che hanno già superato tutti i controlli di rischio (token, liquidità, max chase,
  limiti di portafoglio, breaker…).
- Ogni acquisto reale ha un importo **fisso** (`live.max_sol_per_trade`, default 0,05 SOL) e la spesa
  reale del giorno, commissioni incluse, non supera `live.max_sol_per_day` (default 0,2 SOL).
- Le vendite replicano la frazione venduta in PAPER (leader che vende, stop-loss, take-profit, tempo
  massimo) e riguardano **solo i token comprati dal bot**: i token che hai già nel wallet non vengono
  mai toccati, e se il wallet detiene già un token il bot non lo compra (per non mescolare le posizioni).

### Pipeline di ogni ordine (fail-closed)

1. Limiti: armato, sistema RUNNING, tetto giornaliero, posizioni massime, riserva minima di SOL.
2. `GET https://api.jup.ag/swap/v2/order` con `taker` = il tuo indirizzo ed `excludeRouters=jupiterz`
   (così ogni transazione ha **un solo firmatario**: il tuo wallet).
3. **Ispezione** della transazione (`m4desk/live/txcheck.py`): solo programmi in allowlist
   (ComputeBudget, System, Token, Token-2022, Associated Token, Jupiter v6); trasferimenti di SOL solo
   verso il *tuo* account WSOL e non oltre l'importo; nessun Transfer/Approve di token; chiusure di
   account solo verso di te; priority fee entro il limite.
4. **Simulazione** on-chain (`simulateTransaction`): la transazione non deve fallire e il saldo non
   deve scendere oltre importo + margine commissioni; nelle vendite non devono uscire più token del previsto.
5. Firma locale con la chiave letta dal Portachiavi → `POST /swap/v2/execute`.
6. Contabilità con gli importi **reali** (saldo prima/dopo, `totalInputAmount`/`totalOutputAmount`),
   notifica sul telefono e registrazione in `live_orders` (pagina **Live** della dashboard).

### Disarmo automatico

A ogni riavvio del motore; in modalità SICURA; con il sistema in pausa o arrestato; dopo
`live.max_consecutive_failures` errori consecutivi. Da disarmato non si apre nulla di nuovo, ma le
**vendite protettive restano attive** (`live.allow_exits_when_disarmed`); se una vendita fallisce viene
ritentata ogni minuto e ricevi una notifica urgente.

## Collegare il wallet Fomo (scelta dell'utente)

Fomo non ha un'API di trading: per farlo operare dall'app serve la **chiave privata** del wallet, che
Fomo permette di esportare (con FaceID). Conseguenze da accettare consapevolmente:

- l'app controllerà **tutti i fondi Solana** di quel wallet (i limiti limitano la spesa del bot, non
  ciò che una chiave rubata permetterebbe di fare);
- la chiave dello stesso wallet Fomo può valere anche per altre reti supportate da Fomo;
- in Fomo vedrai i movimenti fatti dal bot come transazioni del tuo wallet.

**Alternativa più sicura (consigliata):** un wallet dedicato con poco saldo, finanziato da Fomo.

### Passi

1. `m4desk init` (crea anche `NTFY_TOPIC` nel `.env`).
2. In Fomo esporta la chiave privata (FaceID), poi subito:
   `m4desk live import-key` → incolla la chiave (input nascosto) → controlla che l'indirizzo mostrato
   coincida con quello di Fomo → rispondi `s` per scriverlo in `config.yaml`.
   Dall'app: menu **M4 → Live (soldi veri) → Importa chiave wallet…** (campo nascosto; abilita il live).
   Non incollare la chiave da nessun'altra parte (chat, note, email). Non serve la seed phrase.
3. In `config.yaml`: `live_enabled: true`, `network: solana-mainnet`, `notify.enabled: true`
   (l'app lo fa da sola tranne `notify.enabled`).
4. Notifiche: installa l'app **ntfy** sul telefono e iscriviti al topic scritto in `NTFY_TOPIC` (`.env`).
5. Avvia il motore (`m4desk run --mode paper` o dall'app), poi `m4desk live preflight`: deve essere tutto OK.
6. `m4desk live arm` e digita `ARMA LIVE` (oppure dalla dashboard → **Live** → ARMA LIVE, o dal menu dell'app).
7. Controllo: `m4desk live status`, pagina **Live**, notifiche sul telefono, transazioni su Solscan.
8. Per fermare: `m4desk live disarm` (o DISARMA LIVE). Per revocare tutto: `m4desk live forget-key`
   e, se temi che la chiave sia stata esposta, sposta i fondi su un wallet nuovo.

## Checklist prima di armare

- [ ] Ho capito che posso perdere l'intero importo impiegato e che il bot agisce da solo.
- [ ] `m4desk live preflight` è tutto OK (chiave = indirizzo atteso, saldo, prova di swap simulata).
- [ ] I limiti in `config.yaml` (`max_sol_per_trade`, `max_sol_per_day`, `min_sol_reserve`) sono quelli voluti.
- [ ] Le notifiche ntfy arrivano sul telefono.
- [ ] Ho fatto girare il PAPER abbastanza a lungo da conoscere il comportamento della strategia.
- [ ] Il Mac resta acceso (l'app usa `caffeinate`), e so come disarmare dal Terminale o dalla dashboard.

## Limiti noti del live

- Testato con Jupiter e RPC simulati e con transazioni reali di Jupiter ispezionate/simulate on-chain
  **senza firma**; la prima esecuzione firmata avverrà sul tuo Mac con i tuoi fondi: inizia con i limiti minimi.
- Firma isolata in un modulo dedicato, non in un processo separato né nel Secure Enclave
  (le chiavi Solana ed25519 non sono supportate dal Secure Enclave).
- Su app non notarizzata, dopo ogni aggiornamento macOS può chiedere di nuovo l'accesso al Portachiavi.
- Nessuna protezione MEV dedicata oltre a quella di Jupiter; transazioni fallite pagano comunque la fee di rete.
