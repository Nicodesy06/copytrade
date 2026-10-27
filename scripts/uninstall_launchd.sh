#!/bin/zsh
# Rimuove i LaunchAgent di M4 CopyDesk. I dati in ./data restano intatti.
set -euo pipefail
for label in com.m4copydesk.engine com.m4copydesk.dashboard; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
  echo "rimosso $label"
done
