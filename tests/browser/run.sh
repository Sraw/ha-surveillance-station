#!/usr/bin/env bash
# Drive the card in Google Chrome decoding the real H.265 (VA-API) against a
# running Home Assistant. Chrome has no software HEVC decoder on Linux and
# --headless can't use hardware decode, so Chrome runs headful on a headless
# Weston inside a container, with the host's Intel GPU render node.
#   HA_URL=http://172.26.0.4:8123 HA_TOKEN_FILE=~/.ha_token tests/browser/run.sh playback.mjs
# The card is served from this checkout (not the deployed copy). Needs Node
# 20+ and `npm install` in tests/browser (Playwright, without its browsers).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
docker image inspect ss-hevc-chrome >/dev/null 2>&1 || docker build -q -t ss-hevc-chrome "$here" >/dev/null
node="$(readlink -f "$(command -v node)")"
exec docker run --rm --device /dev/dri --group-add "$(stat -c %g /dev/dri/renderD128)" --network host \
  -e HA_URL="${HA_URL:-http://172.26.0.4:8123}" -e DASHBOARD="${DASHBOARD:-/ss-playback/playback}" \
  -v "$node:/usr/local/bin/node:ro" -v "$here:/pw" -v "$(readlink -f "${HA_TOKEN_FILE:-$HOME/.ha_token}"):/token:ro" \
  -v "$repo/custom_components/surveillance_station/frontend:/card:ro" \
  -w /pw ss-hevc-chrome /usr/local/bin/node "$@"
