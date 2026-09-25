#!/usr/bin/env bash
# Copy the integration into a Home Assistant config directory.
#   HA_CONFIG=/path/to/config scripts/deploy.sh
# Restart Home Assistant afterwards (Python changes need it; a card-only change
# just needs a browser reload, the card URL carries the manifest version).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
dest="${HA_CONFIG:?set HA_CONFIG to the Home Assistant config directory}/custom_components/surveillance_station"
mkdir -p "$dest"
rsync -a --delete --exclude '__pycache__' "$here/custom_components/surveillance_station/" "$dest/"
echo "deployed to $dest"
