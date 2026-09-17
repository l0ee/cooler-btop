#!/usr/bin/env bash
set -eu

ROOT_DIR=$(CDPATH=; cd -- "$(dirname -- "$0")" && pwd)
DIST_DIR="$ROOT_DIR/dist"
VERSION=$(sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"[:space:]]*\)".*/\1/p' "$ROOT_DIR/pyproject.toml" | sed -n '1p')

if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    SOURCE_DATE_EPOCH=$(git -C "$ROOT_DIR" log -1 --format=%ct 2>/dev/null || printf '0')
    export SOURCE_DATE_EPOCH
fi

if [ -z "$VERSION" ]; then
    printf '%s\n' 'Could not determine project version from pyproject.toml.' >&2
    exit 1
fi

mkdir -p "$DIST_DIR"
python3 -m pip wheel \
  --no-deps \
	--wheel-dir "$DIST_DIR" \
	"$ROOT_DIR"

wheel_path="$DIST_DIR/cooler_btop-${VERSION}-py3-none-any.whl"
if [ ! -f "$wheel_path" ]; then
    printf 'Expected wheel was not built: %s\n' "$wheel_path" >&2
    exit 1
fi

printf '%s\n' \
	"Built a local wheel in $DIST_DIR." \
	"No package or daemon service was installed." \
	"To install for only your user, run:" \
	"  python3 -m pip install --user $(printf '%q' "$wheel_path")"
