#!/bin/bash
# Build, ad-hoc-sign, and smoke-test the macOS bundle without user tooling.
set -euo pipefail

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly APP="$ROOT/desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/CORSIA.app"
readonly DMG_DIR="$ROOT/desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/dmg"
readonly DMG="$DMG_DIR/CORSIA-smoke-aarch64.dmg"
readonly RESOURCES="$APP/Contents/Resources"
readonly PYTHON="$RESOURCES/python/bin/python3"
readonly TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/corsia-bundle-smoke.XXXXXX")"

success=false
cleanup() {
  if [ -n "${BACKEND_PID:-}" ]; then kill "$BACKEND_PID" 2>/dev/null || true; wait "$BACKEND_PID" 2>/dev/null || true; fi
  if "$success"; then rm -rf "$TEMP_ROOT"; else echo "smoke diagnostics: $TEMP_ROOT" >&2; fi
}
trap cleanup EXIT

reuse_build=false
skip_dmg=false
for argument in "$@"; do
  case "$argument" in
    --reuse-build) reuse_build=true ;;
    --skip-dmg) skip_dmg=true ;;
    *) echo "usage: $0 [--reuse-build] [--skip-dmg]" >&2; exit 64 ;;
  esac
done
if "$reuse_build"; then
  [ -d "$APP" ] || { echo "error: no built app at $APP" >&2; exit 1; }
else
  if "$skip_dmg"; then
    "$ROOT/desktop/scripts/build-macos-bundle.sh" --skip-dmg
  else
    "$ROOT/desktop/scripts/build-macos-bundle.sh"
  fi
  "$ROOT/desktop/scripts/sign-macos-bundle.sh" - "$APP"
fi
if ! "$skip_dmg"; then
  mkdir -p "$DMG_DIR"
  rm -f "$DMG"
  diskutil image create from --format UDZO --volumeName CORSIA "$APP" "$DMG"
fi

port="$($PYTHON -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
mkdir -p "$TEMP_ROOT/home" "$TEMP_ROOT/workspace" "$TEMP_ROOT/state"
env -i \
  HOME="$TEMP_ROOT/home" TMPDIR="$TEMP_ROOT" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
  CORSIA_TEST_MODE=1 CORSIA_ANALYTICS_DISABLE=1 PYTHONDONTWRITEBYTECODE=1 \
  CORSIA_WORKSPACE="$TEMP_ROOT/workspace" CORSIA_STATE_DIR="$TEMP_ROOT/state" \
  "$RESOURCES/bin/corsia-service" --host 127.0.0.1 --port "$port" \
  >"$TEMP_ROOT/backend.log" 2>&1 &
BACKEND_PID=$!
for _ in $(seq 1 30); do
  if curl --fail --silent "http://127.0.0.1:$port/api/health" >"$TEMP_ROOT/health.json"; then break; fi
  sleep 1
done
grep -q '"status":"ok"' "$TEMP_ROOT/health.json"
curl --fail --silent "http://127.0.0.1:$port/api/version" >"$TEMP_ROOT/version.json"
"$RESOURCES/bin/omlx" --version
PYTHONPATH="$RESOURCES/python/omlx-packages" "$PYTHON" -c 'import mlx.core as mx; value = mx.sum(mx.ones((1024, 1024)) @ mx.ones((1024, 1024))); mx.eval(value); print(f"MLX device={mx.default_device()} value={value.item()}")'
codesign --verify --deep --strict --verbose=2 "$APP"
printf 'health=%s\n' "$(cat "$TEMP_ROOT/health.json")"
printf 'version=%s\n' "$(cat "$TEMP_ROOT/version.json")"
du -sh "$APP"
if ! "$skip_dmg"; then du -sh "$DMG"; fi
success=true
