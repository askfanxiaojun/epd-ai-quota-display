#!/bin/zsh
set -euo pipefail

ROOT_DIR="${0:A:h:h}"
LABEL="com.local.epd-ai-quota-display"
TEMPLATE="$ROOT_DIR/launchd/com.example.epd-ai-quota-display.plist.template"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
UPDATE_INTERVAL="${EPD_UPDATE_INTERVAL_SECONDS:-1800}"

if ! [[ "$UPDATE_INTERVAL" =~ ^[0-9]+$ ]] || (( UPDATE_INTERVAL < 60 )); then
  echo "EPD_UPDATE_INTERVAL_SECONDS must be an integer of at least 60." >&2
  exit 1
fi

mkdir -p "$ROOT_DIR/logs" "$HOME/Library/LaunchAgents"

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$ROOT_DIR/.venv"
fi
"$ROOT_DIR/.venv/bin/pip" install -r "$ROOT_DIR/requirements.txt"

sed -e "s|__PROJECT_DIR__|$ROOT_DIR|g" \
    -e "s|__HOME_DIR__|$HOME|g" \
    -e "s|__UPDATE_INTERVAL__|$UPDATE_INTERVAL|g" \
    "$TEMPLATE" > "$TARGET"
plutil -lint "$TARGET"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$TARGET"

echo "Installed $LABEL"
echo "Runs every $UPDATE_INTERVAL seconds and once immediately after loading."
echo "Log: $ROOT_DIR/logs/update.log"
