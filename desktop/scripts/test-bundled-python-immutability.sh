#!/bin/bash
# Stage a macOS app and prove bundled Python imports cannot modify it.
set -euo pipefail

[ "$#" -eq 1 ] || { echo "usage: $0 /path/to/CORSIA.app" >&2; exit 64; }
readonly SOURCE_APP="$1"
[ -d "$SOURCE_APP" ] || { echo "error: app bundle not found: $SOURCE_APP" >&2; exit 1; }

readonly TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/corsia-python-immutability.XXXXXX")"
cleanup() { rm -rf "$TEMP_ROOT"; }
trap cleanup EXIT

readonly APP="$TEMP_ROOT/CORSIA.app"
ditto "$SOURCE_APP" "$APP"
readonly RESOURCES="$APP/Contents/Resources"
readonly PYTHON_ROOT="$RESOURCES/python"
readonly PYTHON="$PYTHON_ROOT/bin/python3"
readonly BACKEND_PACKAGES="$PYTHON_ROOT/backend-packages"
[ -x "$PYTHON" ] || { echo "error: bundled Python is missing: $PYTHON" >&2; exit 1; }
[ -d "$BACKEND_PACKAGES" ] || { echo "error: bundled site-packages are missing: $BACKEND_PACKAGES" >&2; exit 1; }

snapshot() {
  # The mtime/mode list makes the required before/after file inventory readable;
  # the hashes close the same-second-mtime and content-preserving-path gaps.
  {
    printf '%s\n' 'mtime\tmode\tpath'
    find "$APP" -type f -print0 | xargs -0 stat -f '%m\t%p\t%N' | LC_ALL=C sort
    printf '%s\n' 'sha256\tpath'
    find "$APP" -type f -print0 | xargs -0 shasum -a 256 | LC_ALL=C sort
  }
}

readonly BEFORE="$TEMP_ROOT/before"
readonly AFTER="$TEMP_ROOT/after"
snapshot > "$BEFORE"

python_sources="$(find "$PYTHON_ROOT" -type f -name '*.py' | wc -l | tr -d ' ')"
python_bytecode="$(find "$PYTHON_ROOT" -type f -name '*.pyc' | wc -l | tr -d ' ')"
printf 'bundled-python-py=%s\n' "$python_sources"
printf 'bundled-python-pyc=%s\n' "$python_bytecode"

env -i PATH="/usr/bin:/bin:/usr/sbin:/sbin" "$PYTHON" -I -c '
import sys
sys.path.insert(0, sys.argv[1])
import sitecustomize
import json
import ssl
import fastapi
print("bundled-python-imports=sitecustomize,json,ssl,fastapi")
' "$BACKEND_PACKAGES"

snapshot > "$AFTER"
if ! cmp -s "$BEFORE" "$AFTER"; then
  echo "error: bundled Python imports modified the staged app" >&2
  diff -u "$BEFORE" "$AFTER" >&2 || true
  exit 1
fi
printf 'bundled-python-immutability=passed\n'
