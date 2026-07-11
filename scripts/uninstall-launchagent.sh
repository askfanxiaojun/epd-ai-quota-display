#!/bin/zsh
set -euo pipefail

LABEL="com.local.epd-ai-quota-display"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [[ -f "$TARGET" ]]; then
  rm "$TARGET"
fi

echo "Uninstalled $LABEL. Project files and logs were kept."

