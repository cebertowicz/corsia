#!/bin/bash
# Build CORSIA's signed-app resources. Run from any directory on macOS 15+.
set -euo pipefail

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly BUILD_ROOT="$ROOT/desktop/.build-macos"
readonly BUNDLE_ROOT="$ROOT/desktop/bundle"
readonly PYTHON_VERSION="3.11.17+20261013"
readonly PYTHON_ARCHIVE="cpython-${PYTHON_VERSION}-aarch64-apple-darwin-install_only.tar.gz"
readonly PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.11.17%2B20261013-aarch64-apple-darwin-install_only.tar.gz"
readonly PYTHON_SHA256="d81d757a9785850393cd695a9a686c765a6370fbdc83738589ef2ecd3e5c44b3"
# Must equal OMLX_PIN in backend/llm/local_model.py: the first release with the model recommender.
readonly OMLX_VERSION="0.7.1rc1"
readonly OMLX_WHEEL="omlx-${OMLX_VERSION}-cp311-cp311-macosx_15_0_universal2.whl"
readonly OMLX_URL="https://github.com/jundot/omlx/releases/download/v${OMLX_VERSION}/${OMLX_WHEEL}"
readonly OMLX_SHA256="5b1487bd09fd85da6e7566cd94f0e4b30be03277e49a9250811cf05addc9b26e"
# Build Git from its upstream release instead of copying Apple's CLT binary. Runtime
# prefix discovery makes helpers and templates follow the executable after CORSIA.app moves.
readonly GIT_VERSION="2.53.0"
readonly GIT_ARCHIVE="git-${GIT_VERSION}.tar.xz"
readonly GIT_URL="https://mirrors.edge.kernel.org/pub/software/scm/git/${GIT_ARCHIVE}"
readonly GIT_SHA256="5818bd7d80b061bbbdfec8a433d609dc8818a05991f731ffc4a561e2ca18c653"
readonly GIT_SOURCE="$BUILD_ROOT/git-${GIT_VERSION}"

skip_dmg=false
for argument in "$@"; do
  case "$argument" in
    --skip-dmg) skip_dmg=true ;;
    *) echo "usage: $0 [--skip-dmg]" >&2; exit 64 ;;
  esac
done

fail() { echo "error: $*" >&2; exit 1; }
fetch_verified() {
  local url="$1" file="$2" digest="$3"
  curl --fail --location --retry 3 --silent --show-error "$url" --output "$file"
  echo "${digest}  ${file}" | shasum --algorithm 256 --check --status || fail "SHA-256 mismatch for $(basename "$file")"
}
build_git_runtime() {
  local archive="$BUILD_ROOT/downloads/$GIT_ARCHIVE"
  local stage="$BUILD_ROOT/git-stage"
  local stage_root="$stage/git-runtime"
  local stage_core="$stage_root/libexec/git-core"
  local bundle_core="$BUNDLE_ROOT/libexec/git-core"
  local -a git_make_args=(
    prefix=/git-runtime
    RUNTIME_PREFIX=YesPlease
    SKIP_DASHED_BUILT_INS=YesPlease
    NO_GETTEXT=YesPlease
    NO_TCLTK=YesPlease
    NO_PERL=YesPlease
    NO_PYTHON=YesPlease
    NO_BASH_COMPLETION=YesPlease
  )
  # Git runs these built-ins by their dashed names for local transports.
  # SKIP_DASHED_BUILT_INS installs them only in bin/; they ship in the exec
  # path instead, the one directory Git puts ahead of the caller's PATH.
  local -a transport_built_ins=(git-upload-pack git-receive-pack git-upload-archive)
  # Server-side helpers, and scalar, which is its own binary; none ship.
  local -a excluded_core=(git-daemon git-http-backend git-shell git-cvsserver scalar)
  local entry name

  fetch_verified "$GIT_URL" "$archive" "$GIT_SHA256"
  tar -xJf "$archive" -C "$BUILD_ROOT"
  make -C "$GIT_SOURCE" -j2 "${git_make_args[@]}"
  make -C "$GIT_SOURCE" "${git_make_args[@]}" INSTALL_SYMLINKS=YesPlease DESTDIR="$stage" install

  # The installed stage, rather than selected build outputs, is the runtime
  # contract: it retains every Git scripted subcommand and relocatable template.
  [ -x "$stage_root/bin/git" ] || fail "Git install did not produce bin/git"
  [ -d "$stage_core" ] || fail "Git install did not produce libexec/git-core"
  [ -d "$stage_root/share/git-core/templates" ] || fail "Git install did not produce templates"
  mkdir -p "$BUNDLE_ROOT/bin" "$bundle_core" \
    "$BUNDLE_ROOT/share/git-core/templates" "$BUNDLE_ROOT/licenses/git"
  install -m 0755 "$stage_root/bin/git" "$BUNDLE_ROOT/bin/git"

  # Tauri copies resources with symlinks dereferenced, so stage the regular files
  # that will ship: each kept link becomes a copy of its target here, and a link
  # to a binary that does not ship is dropped instead of becoming a copy of it.
  local -a core_filter=()
  for name in "${excluded_core[@]}"; do core_filter+=(--exclude "/$name"); done
  while IFS= read -r -d '' entry; do
    case "$(realpath "$entry")" in
      "$(realpath "$stage_core")"/*|"$(realpath "$stage_root/bin/git")") ;;
      *) core_filter+=(--exclude "/${entry#"$stage_core/"}") ;;
    esac
  done < <(find "$stage_core" -type l -print0)
  rsync -a --copy-links --delete "${core_filter[@]}" "$stage_core/" "$bundle_core/"
  for name in "${transport_built_ins[@]}"; do
    [ "$(realpath "$stage_root/bin/$name")" = "$(realpath "$stage_root/bin/git")" ] \
      || fail "Git install did not provide $name as the git built-in"
    install -m 0755 "$stage_root/bin/git" "$bundle_core/$name"
  done
  rsync -a --copy-links --delete "$stage_root/share/git-core/templates/" "$BUNDLE_ROOT/share/git-core/templates/"
  entry="$(find "$BUNDLE_ROOT/bin/git" "$BUNDLE_ROOT/libexec" "$BUNDLE_ROOT/share" -type l -print -quit)"
  [ -z "$entry" ] || fail "staged Git resource is a symlink Tauri would copy: $entry"

  install -m 0644 "$GIT_SOURCE/COPYING" "$BUNDLE_ROOT/licenses/git/COPYING"
  install -m 0644 "$archive" "$BUNDLE_ROOT/licenses/git/$GIT_ARCHIVE"
  printf 'Corresponding source for Git %s\nUpstream URL: %s\nSHA-256: %s\nBuild: %s\n' \
    "$GIT_VERSION" "$GIT_URL" "$GIT_SHA256" "${git_make_args[*]:1}" > "$BUNDLE_ROOT/licenses/git/SOURCE"
}

[ "$(uname -s)" = "Darwin" ] || fail "macOS is required"
[ "$(uname -m)" = "arm64" ] || fail "Apple silicon is required"
command -v npm >/dev/null || fail "npm is required"
command -v make >/dev/null || fail "make is required to build bundled Git"
build_number="$(git -C "$ROOT" rev-list --count HEAD)"
app_version="${CORSIA_APP_VERSION:-0.1.0-dev.$build_number}"
macos_short_version="${CORSIA_MACOS_SHORT_VERSION:-0.1.0}"
# CFBundleVersion is an integer-only build sequence, deliberately separate
# from the prerelease marketing version (for example, 0.1.0-dev.N).
macos_bundle_version="${CORSIA_MACOS_BUNDLE_VERSION:-1.0.$build_number}"
[[ "$app_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || fail "invalid app version: $app_version"
[[ "$macos_short_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "invalid macOS short version: $macos_short_version"
[[ "$macos_bundle_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "invalid macOS bundle version: $macos_bundle_version"
"$ROOT/desktop/scripts/install-rust-toolchain.sh"
export RUSTUP_HOME="$ROOT/desktop/.rust-toolchain/rustup"
export CARGO_HOME="$ROOT/desktop/.rust-toolchain/cargo"
export PATH="$CARGO_HOME/bin:$PATH"
command -v cargo >/dev/null || fail "scoped Rust cargo is required"

rm -rf "$BUILD_ROOT" "$BUNDLE_ROOT"
mkdir -p "$BUILD_ROOT/downloads" "$BUNDLE_ROOT/bin"
fetch_verified "$PYTHON_URL" "$BUILD_ROOT/downloads/$PYTHON_ARCHIVE" "$PYTHON_SHA256"
tar -xzf "$BUILD_ROOT/downloads/$PYTHON_ARCHIVE" -C "$BUNDLE_ROOT"
readonly PYTHON="$BUNDLE_ROOT/python/bin/python3"
[ -x "$PYTHON" ] || fail "standalone Python did not contain python/bin/python3"
"$PYTHON" -c 'import sys; assert sys.version_info[:2] == (3, 11), sys.version'

fetch_verified "$OMLX_URL" "$BUILD_ROOT/downloads/$OMLX_WHEEL" "$OMLX_SHA256"
"$PYTHON" -m pip install --no-cache-dir --target "$BUNDLE_ROOT/python/backend-packages" --requirement "$ROOT/backend/requirements.lock"
"$PYTHON" -m pip install --no-cache-dir --target "$BUNDLE_ROOT/python/omlx-packages" "$BUILD_ROOT/downloads/$OMLX_WHEEL"
PYTHONPATH="$BUNDLE_ROOT/python/backend-packages" "$PYTHON" -m pip check
PYTHONPATH="$BUNDLE_ROOT/python/omlx-packages" "$PYTHON" -m pip check

build_git_runtime
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' "$ROOT/backend/" "$BUNDLE_ROOT/backend/"
(
  cd "$ROOT/frontend"
  npm ci
  CORSIA_BUILD_OUTPUT="$BUNDLE_ROOT/frontend/dist" npm run build
)
install -m 0755 "$ROOT/desktop/scripts/corsia" "$BUNDLE_ROOT/bin/corsia"
install -m 0755 "$ROOT/desktop/scripts/corsia-service" "$BUNDLE_ROOT/bin/corsia-service"
install -m 0755 "$ROOT/desktop/scripts/omlx" "$BUNDLE_ROOT/bin/omlx"
# The lifecycle script and its parts, beside backend/, so scripts/corsia runs in
# app mode (corsia.d/app_mode.sh) and Update stages and swaps this app.
mkdir -p "$BUNDLE_ROOT/scripts"
install -m 0755 "$ROOT/scripts/corsia" "$BUNDLE_ROOT/scripts/corsia"
rsync -a --delete "$ROOT/scripts/corsia.d/" "$BUNDLE_ROOT/scripts/corsia.d/"

node "$ROOT/desktop/scripts/gen-shell-tokens.mjs"
(
  cd "$ROOT/desktop/src-tauri"
  "$ROOT/frontend/node_modules/.bin/tauri" build --target aarch64-apple-darwin --bundles app --config "{\"version\":\"$app_version\",\"bundle\":{\"macOS\":{\"bundleVersion\":\"$macos_bundle_version\"}}}"
)
readonly APP="$ROOT/desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/CORSIA.app"
readonly DMG_DIR="$ROOT/desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/dmg"
readonly DMG="$DMG_DIR/CORSIA-unsigned-aarch64.dmg"
[ -d "$APP" ] || fail "Tauri did not produce $APP"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $macos_short_version" "$APP/Contents/Info.plist"
# SMAppService discovers this static, signed definition inside the app bundle.
# It intentionally contains no per-user environment; the launcher reads the
# service record and constructs its identity itself at every launch.
install -d "$APP/Contents/Library/LaunchAgents"
install -m 0644 "$ROOT/desktop/launchd/ai.corsia.service.plist" \
  "$APP/Contents/Library/LaunchAgents/ai.corsia.service.plist"
"$ROOT/desktop/scripts/check-bundle-launch-agent.sh" "$APP"
if ! "$skip_dmg"; then
  mkdir -p "$DMG_DIR"
  rm -f "$DMG"
  diskutil image create from --format UDZO --volumeName CORSIA "$APP" "$DMG"
fi
echo "Built resources at $BUNDLE_ROOT"
