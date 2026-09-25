#!/usr/bin/env bash
# Run all tests in a throwaway Python 3.14 container (HA 2026.9 needs 3.14;
# the host's Python is older). The venv lives in a docker volume, so only the
# first run downloads Home Assistant. The library is used from its source
# tree (PYTHONPATH), so nothing is written into the repo.
#   scripts/test.sh              # everything
#   scripts/test.sh -k config    # extra pytest arguments
# The container is capped at 4 GB (no extra swap) and pytest at 10 min, so a
# runaway or hung test dies on its own instead of dragging the host into swap.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
exec docker run --rm -t \
  --memory=4g --memory-swap=4g \
  -v "$here:/src" -w /src \
  -v ss-playback-test-venv:/venv \
  -e UV_LINK_MODE=copy -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/src/synology_ss/src \
  ghcr.io/astral-sh/uv:python3.14-bookworm-slim \
  sh -c '
    set -e
    [ -x /venv/bin/python ] || uv venv -q /venv
    . /venv/bin/activate
    stamp=/venv/.requirements.sha
    want=$(sha256sum < requirements_test.txt)
    if [ "$(cat $stamp 2>/dev/null)" != "$want" ]; then
      uv pip install -q -r requirements_test.txt && echo "$want" > $stamp
    fi
    exec timeout -k 10 600 python -m pytest -p no:cacheprovider "$@"
  ' sh "$@"
