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
readonly GIT_CORE="$RESOURCES/libexec/git-core"
readonly GIT_HTTP="$GIT_CORE/git-remote-http"
readonly GIT_VERSION="2.53.0"
readonly GIT_ARCHIVE="git-${GIT_VERSION}.tar.xz"
readonly GIT_SHA256="5818bd7d80b061bbbdfec8a433d609dc8818a05991f731ffc4a561e2ca18c653"
readonly GIT_SIZE_LIMIT_KIB=60000
# git itself plus the transport built-ins Git runs by their dashed names.
readonly GIT_CORE_COPY_LIMIT=4

macho_uuid() {
  otool -l "$1" 2>/dev/null | awk '$1 == "uuid" && !found {print $2; found = 1}'
}
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
[ -f "$RESOURCES/licenses/git/$GIT_ARCHIVE" ] || { echo "error: bundled Git corresponding source is missing" >&2; exit 1; }
echo "${GIT_SHA256}  $RESOURCES/licenses/git/$GIT_ARCHIVE" | shasum --algorithm 256 --check --status
"$GIT" --version
assert_system_libraries "$GIT"
assert_system_libraries "$GIT_HTTP"
while IFS= read -r -d '' link; do
  [ -e "$link" ] || { echo "error: broken bundled Git symlink: $link" >&2; exit 1; }
done < <(find "$GIT_CORE" -type l -print0)

# Tauri dereferences symlinks when it copies resources, so every Git alias in the
# app is a full copy of its target; measure the relocated app, not the stage.
git_kib="$(du -sk "$GIT" "$GIT_CORE" "$RESOURCES/share/git-core/templates" | awk '{sum += $1} END {print sum}')"
printf 'bundled-git-uncompressed-kib=%s limit=%s\n' "$git_kib" "$GIT_SIZE_LIMIT_KIB"
[ "$git_kib" -le "$GIT_SIZE_LIMIT_KIB" ] || { echo "error: bundled Git is $git_kib KiB, over $GIT_SIZE_LIMIT_KIB KiB" >&2; exit 1; }
# Signing gives each copy an identifier from its own file name, so copies differ
# byte for byte; the linker's Mach-O UUID still identifies them.
git_uuid="$(macho_uuid "$GIT")"
[ -n "$git_uuid" ] || { echo "error: bundled git has no Mach-O UUID" >&2; exit 1; }
git_copies=0
while IFS= read -r -d '' file; do
  if [ "$(macho_uuid "$file")" = "$git_uuid" ]; then git_copies=$((git_copies + 1)); fi
done < <(find "$GIT_CORE" -type f -print0)
printf 'bundled-git-core-copies-of-git=%s limit=%s\n' "$git_copies" "$GIT_CORE_COPY_LIMIT"
[ "$git_copies" -le "$GIT_CORE_COPY_LIMIT" ] || { echo "error: $git_copies files in libexec/git-core are copies of bin/git" >&2; exit 1; }

# These fail-closed shims would run, and record it, if Git's runtime-prefix
# helper lookup fell back to the Command Line Tools after the app has moved, or
# if the service's PATH reached a system Git ahead of the bundle.
readonly SYSTEM_GIT_CALLS="$TEMP_ROOT/system-git-calls"
mkdir -p "$TEMP_ROOT/empty-home" "$TEMP_ROOT/no-system-git"
for command in git git-upload-pack git-receive-pack git-upload-archive git-shell; do
  printf '#!/bin/sh\necho "%s $*" >> "%s"\necho "system Git stub must not run: %s" >&2\nexit 99\n' \
    "$command" "$SYSTEM_GIT_CALLS" "$command" > "$TEMP_ROOT/no-system-git/$command"
  chmod 0755 "$TEMP_ROOT/no-system-git/$command"
done
bundled_git() {
  env -i HOME="$TEMP_ROOT/empty-home" \
    PATH="$RESOURCES/bin:$TEMP_ROOT/no-system-git:/usr/bin:/bin" \
    GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
    git "$@"
}

readonly GIT_WORKSPACE="$TEMP_ROOT/git-workspace"
readonly GIT_REMOTE="$TEMP_ROOT/git-remote.git"
readonly GIT_CLONE="$TEMP_ROOT/git-clone"
mkdir -p "$GIT_WORKSPACE"
bundled_git -C "$GIT_WORKSPACE" init --quiet
touch "$GIT_WORKSPACE/smoke"
bundled_git -C "$GIT_WORKSPACE" add smoke
bundled_git -C "$GIT_WORKSPACE" -c user.name=CORSIA -c user.email=corsia@local commit --quiet -m smoke
bundled_git init --bare --quiet "$GIT_REMOTE"
bundled_git -C "$GIT_WORKSPACE" remote add origin "$GIT_REMOTE"
bundled_git -C "$GIT_WORKSPACE" push --quiet origin HEAD:main
bundled_git clone --quiet "$GIT_REMOTE" "$GIT_CLONE"
touch "$GIT_WORKSPACE/fetch-smoke"
bundled_git -C "$GIT_WORKSPACE" add fetch-smoke
bundled_git -C "$GIT_WORKSPACE" -c user.name=CORSIA -c user.email=corsia@local commit --quiet -m fetch-smoke
bundled_git -C "$GIT_WORKSPACE" push --quiet origin HEAD:main
bundled_git -C "$GIT_CLONE" fetch --quiet origin
bundled_git -C "$GIT_CLONE" show origin/main:fetch-smoke >/dev/null
bundled_git archive --remote="$GIT_REMOTE" --format=tar main > "$TEMP_ROOT/git-archive.tar"
tar -tf "$TEMP_ROOT/git-archive.tar" | grep -qx 'smoke'
bundled_git submodule -h >/dev/null 2>&1

port="$($PYTHON -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
mkdir -p "$TEMP_ROOT/home" "$TEMP_ROOT/workspace" "$TEMP_ROOT/state"
# The service's login-shell PATH puts the system Git shims ahead of the system
# paths, where an Apple or Homebrew Git would be.
printf 'export PATH="%s:$PATH"\n' "$TEMP_ROOT/no-system-git" > "$TEMP_ROOT/home/.zprofile"
login_path="$(env -i HOME="$TEMP_ROOT/home" PATH="/usr/bin:/bin:/usr/sbin:/sbin" PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$RESOURCES/backend" "$PYTHON" -c 'from cli_location import login_shell_path; print(login_shell_path() or "")')"
case "$login_path" in
  "$TEMP_ROOT/no-system-git:"*) ;;
  *) echo "error: the service login PATH does not lead with the system Git shims: $login_path" >&2; exit 1 ;;
esac
env -i \
  HOME="$TEMP_ROOT/home" TMPDIR="$TEMP_ROOT" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
  CORSIA_TEST_MODE=1 CORSIA_ANALYTICS_DISABLE=1 PYTHONDONTWRITEBYTECODE=1 \
  GIT_TRACE="$TEMP_ROOT/service-git-trace.log" \
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
# Both the service's PATH and every Git its backend ran (version reads it) lead
# to the bundle, not the shims ahead of the system paths.
service_git="$(sed -n 's/^CORSIA bundled backend: git=//p' "$TEMP_ROOT/state/logs/service.log")"
printf 'service-git=%s\n' "$service_git"
[ "$service_git" -ef "$GIT_CORE/git" ] || { echo "error: the service resolves git to ${service_git:-nothing}, not the bundle" >&2; exit 1; }
touch "$TEMP_ROOT/service-git-trace.log"
service_git_runs="$(grep -c 'trace: resolved executable dir: ' "$TEMP_ROOT/service-git-trace.log" || true)"
printf 'service-git-runs=%s\n' "$service_git_runs"
[ "$service_git_runs" -gt 0 ] || { echo "error: the backend ran no Git" >&2; exit 1; }
while IFS= read -r git_dir; do
  [ "$git_dir" -ef "$GIT_CORE" ] || { echo "error: the backend ran Git from $git_dir" >&2; exit 1; }
done < <(sed -n 's/.*trace: resolved executable dir: //p' "$TEMP_ROOT/service-git-trace.log" | sort -u)
"$RESOURCES/bin/omlx" --version
"$RESOURCES/bin/agent-browser" --version
# The bundled sandbox really confines a shell that reaches the bundled Python read-only. This
# runner is not itself inside a sandbox, so the nested run is real; it is what every agent shell is.
"$RESOURCES/bin/safehouse" --version
sandbox_python="$(cd "$RESOURCES/python" && pwd -P)"
sandbox_out="$("$RESOURCES/bin/safehouse" --workdir= --trust-workdir-config=false --add-dirs-ro="$sandbox_python" -- \
  /bin/bash --noprofile --norc -c "\"$sandbox_python/bin/python3\" -c 'print(1)' && \"$sandbox_python/bin/python3\" -m pip --version")" \
  || { echo "error: the bundled safehouse could not run the bundled Python" >&2; exit 1; }
printf 'sandboxed-python=%s\n' "$sandbox_out"
case "$sandbox_out" in 1$'\n'pip\ *) ;; *) echo "error: unexpected sandboxed Python output" >&2; exit 1 ;; esac
PYTHONPATH="$RESOURCES/python/omlx-packages" "$PYTHON" -c 'import mlx.core as mx; value = mx.sum(mx.ones((1024, 1024)) @ mx.ones((1024, 1024))); mx.eval(value); print(f"MLX device={mx.default_device()} value={value.item()}")'
codesign --verify --deep --strict --verbose=2 "$APP"
[ ! -e "$SYSTEM_GIT_CALLS" ] || { echo "error: a system Git shim ran:" >&2; cat "$SYSTEM_GIT_CALLS" >&2; exit 1; }
printf 'health=%s\n' "$(cat "$TEMP_ROOT/health.json")"
printf 'version=%s\n' "$(cat "$TEMP_ROOT/version.json")"
du -sh "$APP"
if ! "$skip_dmg"; then du -sh "$DMG"; fi
success=true
