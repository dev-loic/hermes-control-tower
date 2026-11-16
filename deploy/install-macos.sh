#!/usr/bin/env bash
# Installs Hermes Control Tower as a macOS LaunchAgent (starts at login, restarts on crash).
# Usage: ./deploy/install-macos.sh "Your first name"
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEFAULT_NAME="the user"
USER_NAME="${1:-${HCT_USER_NAME:-$DEFAULT_NAME}}"
HERMES_BIN="$(command -v hermes)"
PYTHON="${HCT_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python}"
PLIST="$HOME/Library/LaunchAgents/com.hermes.control-tower.plist"
mkdir -p "$HOME/.hermes/control-tower"
sed -e "s|__PYTHON__|$PYTHON|g" -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__HERMES_BIN__|$HERMES_BIN|g" \
    -e "s|__HERMES_BIN_DIR__|$(dirname "$HERMES_BIN")|g" -e "s|__USER_NAME__|$USER_NAME|g" -e "s|__HOME__|$HOME|g" \
    "$APP_DIR/deploy/com.hermes.control-tower.plist.template" > "$PLIST"
launchctl bootout "gui/$(id -u)/com.hermes.control-tower" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Hermes Control Tower → http://127.0.0.1:${HCT_PORT:-4747}"
