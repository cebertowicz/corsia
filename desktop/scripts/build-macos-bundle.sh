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
readonly OMLX_VERSION="0.7.0"
readonly OMLX_WHEEL="omlx-${OMLX_VERSION}-cp311-cp311-macosx_15_0_universal2.whl"
readonly OMLX_URL="https://github.com/jundot/omlx/releases/download/v${OMLX_VERSION}/${OMLX_WHEEL}"
readonly OMLX_SHA256="a25b0fbf86579512ced2a45b9aba420a51e6e8f3ee9ea3832ea1b9111d2cc4a7"

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

[ "$(uname -s)" = "Darwin" ] || fail "macOS is required"
[ "$(uname -m)" = "arm64" ] || fail "Apple silicon is required"
command -v npm >/dev/null || fail "npm is required"
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

rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' "$ROOT/backend/" "$BUNDLE_ROOT/backend/"
(
  cd "$ROOT/frontend"
  npm ci
  CORSIA_BUILD_OUTPUT="$BUNDLE_ROOT/frontend/dist" npm run build
)
install -m 0755 "$ROOT/desktop/scripts/corsia" "$BUNDLE_ROOT/bin/corsia"
install -m 0755 "$ROOT/desktop/scripts/corsia-service" "$BUNDLE_ROOT/bin/corsia-service"
install -m 0755 "$ROOT/desktop/scripts/omlx" "$BUNDLE_ROOT/bin/omlx"

(
  cd "$ROOT/desktop/src-tauri"
  "$ROOT/frontend/node_modules/.bin/tauri" build --target aarch64-apple-darwin --bundles app --config "{\"version\":\"$app_version\",\"bundle\":{\"macOS\":{\"bundleVersion\":\"$macos_bundle_version\"}}}"
)
readonly APP="$ROOT/desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/CORSIA.app"
readonly DMG_DIR="$ROOT/desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/dmg"
readonly DMG="$DMG_DIR/CORSIA-unsigned-aarch64.dmg"
[ -d "$APP" ] || fail "Tauri did not produce $APP"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $macos_short_version" "$APP/Contents/Info.plist"
if ! "$skip_dmg"; then
  mkdir -p "$DMG_DIR"
  rm -f "$DMG"
  diskutil image create from --format UDZO --volumeName CORSIA "$APP" "$DMG"
fi
echo "Built resources at $BUNDLE_ROOT"
