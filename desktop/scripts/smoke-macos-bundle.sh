#!/bin/bash
# Build, ad-hoc-sign, and smoke-test the macOS bundle without user tooling.
set -euo pipefail

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly SOURCE_APP="$ROOT/desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/CORSIA.app"
readonly DMG_DIR="$ROOT/desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/dmg"
readonly DMG="$DMG_DIR/CORSIA-smoke-aarch64.dmg"
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
  [ -d "$SOURCE_APP" ] || { echo "error: no built app at $SOURCE_APP" >&2; exit 1; }
else
  if "$skip_dmg"; then
    "$ROOT/desktop/scripts/build-macos-bundle.sh" --skip-dmg
  else
    "$ROOT/desktop/scripts/build-macos-bundle.sh"
  fi
  "$ROOT/desktop/scripts/sign-macos-bundle.sh" - "$SOURCE_APP"
fi
if ! "$skip_dmg"; then
  mkdir -p "$DMG_DIR"
  rm -f "$DMG"
  diskutil image create from --format UDZO --volumeName CORSIA "$SOURCE_APP" "$DMG"
fi

# Test the exact shipped resource layout after relocation rather than the build path.
readonly APP="$TEMP_ROOT/relocated/CORSIA.app"
mkdir -p "$(dirname "$APP")"
ditto "$SOURCE_APP" "$APP"
readonly RESOURCES="$APP/Contents/Resources"
readonly PYTHON="$RESOURCES/python/bin/python3"
readonly GIT="$RESOURCES/bin/git"
readonly GIT_HTTP="$RESOURCES/libexec/git-core/git-remote-http"

assert_system_libraries() {
  local binary="$1"
  if otool -L "$binary" | tail -n +2 | grep -Ev '^[[:space:]]*(/System/Library/|/usr/lib/)'; then
    echo "error: $binary links a non-system library" >&2
    exit 1
  fi
}
[ -x "$GIT" ] || { echo "error: bundled git is missing" >&2; exit 1; }
[ -x "$GIT_HTTP" ] || { echo "error: bundled HTTPS git helper is missing" >&2; exit 1; }
[ -f "$RESOURCES/licenses/git/COPYING" ] || { echo "error: bundled Git license is missing" >&2; exit 1; }
[ -f "$RESOURCES/licenses/git/SOURCE" ] || { echo "error: bundled Git source notice is missing" >&2; exit 1; }
"$GIT" --version
assert_system_libraries "$GIT"
assert_system_libraries "$GIT_HTTP"

# The fail-closed shim would win before /usr/bin/git if resource resolution regressed.
mkdir -p "$TEMP_ROOT/empty-home" "$TEMP_ROOT/git-workspace" "$TEMP_ROOT/no-system-git"
printf '#!/bin/sh\necho "/usr/bin/git must not run" >&2\nexit 99\n' > "$TEMP_ROOT/no-system-git/git"
chmod 0755 "$TEMP_ROOT/no-system-git/git"
env -i HOME="$TEMP_ROOT/empty-home" \
  PATH="$RESOURCES/bin:$TEMP_ROOT/no-system-git:/usr/bin:/bin" \
  GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
  git -C "$TEMP_ROOT/git-workspace" init --quiet
touch "$TEMP_ROOT/git-workspace/smoke"
env -i HOME="$TEMP_ROOT/empty-home" \
  PATH="$RESOURCES/bin:$TEMP_ROOT/no-system-git:/usr/bin:/bin" \
  GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
  git -C "$TEMP_ROOT/git-workspace" add smoke
env -i HOME="$TEMP_ROOT/empty-home" \
  PATH="$RESOURCES/bin:$TEMP_ROOT/no-system-git:/usr/bin:/bin" \
  GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
  git -C "$TEMP_ROOT/git-workspace" -c user.name=CORSIA -c user.email=corsia@local commit --quiet -m smoke
printf 'bundled-git-uncompressed-kib=%s\n' "$(du -sk "$GIT" "$RESOURCES/libexec/git-core" "$RESOURCES/share/git-core/templates" "$RESOURCES/licenses/git" | awk '{sum += $1} END {print sum}')"

port="$($PYTHON -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
mkdir -p "$TEMP_ROOT/home" "$TEMP_ROOT/workspace" "$TEMP_ROOT/state"
env -i \
  HOME="$TEMP_ROOT/home" TMPDIR="$TEMP_ROOT" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
  CORSIA_TEST_MODE=1 CORSIA_ANALYTICS_DISABLE=1 PYTHONDONTWRITEBYTECODE=1 \
  CORSIA_WORKSPACE="$TEMP_ROOT/workspace" CORSIA_STATE_DIR="$TEMP_ROOT/state" \
  "$RESOURCES/bin/corsia-service" --host 127.0.0.1 --port "$port" \
  >"$TEMP_ROOT/backend.log" 2>&1 &
BACKEND_PID=$!
startup_receipt="CORSIA bundled backend startup: state_dir=$TEMP_ROOT/state db_path=$TEMP_ROOT/state/corsia.db port=$port workspace=$TEMP_ROOT/workspace"
for _ in $(seq 1 30); do
  if grep -F "$startup_receipt" "$TEMP_ROOT/state/logs/service.log" >/dev/null 2>&1; then break; fi
  sleep 1
done
grep -F "$startup_receipt" "$TEMP_ROOT/state/logs/service.log"
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
