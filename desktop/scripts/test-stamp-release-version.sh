#!/bin/bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
temp="$(mktemp -d "${TMPDIR:-/tmp}/corsia-version-stamp.XXXXXX")"
cleanup() { rm -rf "$temp"; }
trap cleanup EXIT

mkdir -p "$temp/desktop/src-tauri" "$temp/frontend" "$temp/desktop/scripts"
cp "$root/desktop/src-tauri/tauri.conf.json" "$temp/desktop/src-tauri/"
cp "$root/desktop/src-tauri/Cargo.toml" "$temp/desktop/src-tauri/"
cp "$root/frontend/package.json" "$temp/frontend/"
cp "$root/desktop/scripts/stamp-release-version.sh" "$temp/desktop/scripts/"

(
  cd "$temp"
  desktop/scripts/stamp-release-version.sh v0.1.0
  node -e 'const fs=require("fs"); for (const path of ["desktop/src-tauri/tauri.conf.json", "frontend/package.json"]) { if (JSON.parse(fs.readFileSync(path)).version !== "0.1.0") process.exit(1); }'
  grep -Fx 'version = "0.1.0"' desktop/src-tauri/Cargo.toml
)

for invalid_tag in v2026.10.14 v0.1.0-dev.1 v1.0.0; do
  if (
    cd "$temp"
    desktop/scripts/stamp-release-version.sh "$invalid_tag"
  ); then
    echo "invalid release version unexpectedly succeeded: $invalid_tag" >&2
    exit 1
  fi
done
