#!/usr/bin/env bash
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DIST_DIR="$ROOT_DIR/dist"

mkdir -p "$DIST_DIR"
python3 -m pip wheel \
  --no-deps \
  --no-build-isolation \
  --wheel-dir "$DIST_DIR" \
  "$ROOT_DIR"

printf '%s\n' \
  "Built a local wheel in $DIST_DIR." \
  "No package or daemon service was installed." \
  "To install for only your user, run:" \
  "  python3 -m pip install --user $DIST_DIR/cooler_btop-2.0.0-py3-none-any.whl"
