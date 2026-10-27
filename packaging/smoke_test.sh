#!/usr/bin/env bash
# Prova l'eseguibile congelato: CLI (doctor, demo, report, wallets) e app headless (dashboard HTTP).
# Uso: packaging/smoke_test.sh "<percorso eseguibile>"
set -euo pipefail
BIN="$1"
export M4DESK_HOME="$(mktemp -d)/home"
PORT=8799
echo "== eseguibile: $BIN  · dati: $M4DESK_HOME"
"$BIN" --version
"$BIN" doctor
"$BIN" demo | tee /tmp/m4-demo.txt
grep -q "SIMULATO / DATI DI ESEMPIO" /tmp/m4-demo.txt
"$BIN" report --db demo | grep -q "Report giornaliero"
"$BIN" status --db demo
if "$BIN" run --mode live 2>/dev/null; then echo "ERRORE: la modalità live non deve partire"; exit 1; fi
"$BIN" live status | grep -q "DISABILITATO"
"$BIN" live --help >/dev/null
grep -q "^NTFY_TOPIC=m4desk-" "$M4DESK_HOME/.env"

"$BIN" --headless --port "$PORT" > "$M4DESK_HOME/headless.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do curl -fs "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 1; done
curl -fs "http://127.0.0.1:$PORT/health"; echo
for p in / /agenti /trader /token /operazioni /portafoglio /rischi /report /impostazioni /log /static/app.css; do
  code=$(curl -s -o /dev/null -w "%{http_code}" -b "m4db=demo" "http://127.0.0.1:$PORT$p")
  echo "GET $p -> $code"; [ "$code" = "200" ]
done
curl -fs -b "m4db=demo" "http://127.0.0.1:$PORT/" | grep -q "SIMULATO / DATI DI ESEMPIO"
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:$PORT/api/control/pause" -d '{"confirm":true}')
echo "POST senza token -> $code"; [ "$code" = "403" ]
code=$(curl -s -o /dev/null -w "%{http_code}" -H "Host: evil.example" "http://127.0.0.1:$PORT/")
echo "Host estraneo -> $code"; [ "$code" = "403" ]
test "$(stat -c %a "$M4DESK_HOME/.env" 2>/dev/null || stat -f %Lp "$M4DESK_HOME/.env")" = "600"
echo "== SMOKE TEST OK"
