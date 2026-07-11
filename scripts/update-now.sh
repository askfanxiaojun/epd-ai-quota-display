#!/bin/zsh
set -euo pipefail

LABEL="com.local.epd-ai-quota-display"
launchctl kickstart "gui/$(id -u)/$LABEL"
echo "Requested an immediate display update."

