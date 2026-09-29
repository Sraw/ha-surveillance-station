#!/usr/bin/env bash
# Deploy to a Home Assistant container:
#   HA_CONFIG=/path/to/config [HA_CONTAINER=homeassistant] scripts/deploy.sh
#
# 1. The integration is copied to <config>/custom_components/.
# 2. The synology_ss_playback library is installed into <config>/deps, the
#    user site HA puts on sys.path when it isn't in a venv (the official
#    container), from this checkout rather than PyPI, so an unreleased change
#    to the library can be tried; there it survives image updates. After an HA update that moves to a newer Python,
#    run this again (the integration then fails to load with a requirement
#    error until you do).
#
# Restart Home Assistant afterwards (Python changes need it). The cards are
# cached for a month by version, so a card-only change under the same version
# needs a reload that bypasses the browser's cache (see docs/development.md).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
config="${HA_CONFIG:?set HA_CONFIG to the Home Assistant config directory}"
container="${HA_CONTAINER:-homeassistant}"

dest="$config/custom_components/surveillance_station"
mkdir -p "$dest"
rsync -a --delete --exclude '__pycache__' "$here/custom_components/surveillance_station/" "$dest/"
echo "deployed integration to $dest"

build="/tmp/synology_ss_playback-build"
docker exec "$container" rm -rf "$build"
docker cp -q "$here/synology_ss" "$container:$build"
docker exec -e PYTHONUSERBASE=/config/deps "$container" \
  python3 -m pip install --quiet --user --no-deps --no-index --no-build-isolation --upgrade \
  --disable-pip-version-check --root-user-action=ignore "$build"
docker exec "$container" rm -rf "$build"
docker exec -e PYTHONUSERBASE=/config/deps "$container" \
  python3 -c 'import synology_ss_playback, importlib.metadata as m; print("installed synology-ss-playback", m.version("synology-ss-playback"), "->", synology_ss_playback.__file__)'
