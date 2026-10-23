#!/bin/bash
# Install Rust without modifying a user's toolchain, PATH, or shell configuration.
set -euo pipefail

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly TOOLCHAIN_ROOT="${CORSIA_RUST_TOOLCHAIN_ROOT:-$ROOT/desktop/.rust-toolchain}"
readonly TOOLCHAIN_RUSTUP_HOME="$TOOLCHAIN_ROOT/rustup"
readonly TOOLCHAIN_CARGO_HOME="$TOOLCHAIN_ROOT/cargo"
readonly RUSTUP_INIT="$TOOLCHAIN_ROOT/rustup-init"
# The archive URL is versioned; dist/ always serves the newest rustup, which breaks the pin.
readonly RUSTUP_VERSION="1.29.1"
readonly RUSTUP_INIT_URL="https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/aarch64-apple-darwin/rustup-init"
readonly RUSTUP_INIT_SHA256="ec1b9233e7f72990ecd8e62063fa7f6c3dfc2bec8e97f88bff165f9100ac696a"

mkdir -p "$TOOLCHAIN_ROOT"
if [ ! -x "$RUSTUP_INIT" ]; then
  curl --fail --location --retry 3 --retry-all-errors --silent --show-error "$RUSTUP_INIT_URL" --output "$RUSTUP_INIT"
  chmod 755 "$RUSTUP_INIT"
fi
echo "$RUSTUP_INIT_SHA256  $RUSTUP_INIT" | shasum --algorithm 256 --check --status || {
  echo "error: SHA-256 mismatch for rustup-init $RUSTUP_VERSION" >&2
  rm -f "$RUSTUP_INIT"
  exit 1
}
RUSTUP_HOME="$TOOLCHAIN_RUSTUP_HOME" CARGO_HOME="$TOOLCHAIN_CARGO_HOME" "$RUSTUP_INIT" \
  --no-modify-path -y --profile minimal --default-toolchain 1.99.0
RUSTUP_HOME="$TOOLCHAIN_RUSTUP_HOME" CARGO_HOME="$TOOLCHAIN_CARGO_HOME" "$TOOLCHAIN_CARGO_HOME/bin/rustup" \
  target add aarch64-apple-darwin
