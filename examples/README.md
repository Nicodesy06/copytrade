# Esempi (SIMULATO / DATI DI ESEMPIO)

- `demo_scenario.json`: scenario **sintetico** usato da `m4desk demo` (wallet, token, firme e prezzi
  inventati). Si rigenera in modo deterministico con `python scripts/generate_demo_fixture.py`.
  Riproducibile con `m4desk replay examples/demo_scenario.json`.
- `watchlist_example.txt`: formato per aggiungere indirizzi PUBBLICI in blocco.

Formato fixture (per creare propri replay): `meta` (label, stream_start, end), `tokens`
(per mint: decimals, program, extensions, authority, `rugcheck`, `liquidity`, `prices` come
`{start, step_s, values, points}`), `seed_wallets`, `wallet_labels`, `swaps` (firma, slot, wallet,
mint, side, quantità raw, quote, saldi pre/post del wallet, event_at), `retractions`.
