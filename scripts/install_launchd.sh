#!/bin/zsh
# Installa M4 CopyDesk come LaunchAgent utente (macOS). Nessun privilegio di amministratore.
# Uso: ./scripts/install_launchd.sh   (dalla cartella del progetto, dopo `m4desk init`)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
M4DESK="$PROJECT_DIR/.venv/bin/m4desk"
CAFFEINATE="/usr/bin/caffeinate"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$PROJECT_DIR/data/logs"

if [[ ! -x "$M4DESK" ]]; then
  echo "Errore: $M4DESK non trovato. Esegui prima: python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'"
  exit 1
fi
mkdir -p "$AGENTS" "$LOGS"

write_plist() {
  local label="$1"; shift
  local plist="$AGENTS/$label.plist"
  {
    cat <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
XML
    for arg in "$@"; do echo "    <string>$arg</string>"; done
    cat <<XML
  </array>
  <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOGS/$label.out.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$label.err.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
XML
  } > "$plist"
  plutil -lint "$plist" >/dev/null
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "installato $plist"
}

# Motore PAPER: caffeinate -i impedisce lo stop del Mac in sospensione automatica mentre il processo gira.
write_plist "com.m4copydesk.engine" "$CAFFEINATE" -i "$M4DESK" run --mode paper
# Dashboard: solo 127.0.0.1.
write_plist "com.m4copydesk.dashboard" "$M4DESK" dashboard --host 127.0.0.1 --port 8765

echo "Fatto. Dashboard: http://127.0.0.1:8765  · Log: $LOGS"
echo "Nota: dopo ARRESTA (exit pulito) launchd non riavvia il motore; per ripartire: m4desk resume && launchctl kickstart gui/$(id -u)/com.m4copydesk.engine"
