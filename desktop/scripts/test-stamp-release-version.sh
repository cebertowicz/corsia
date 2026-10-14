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
  desktop/scripts/stamp-release-version.sh v2026.1014.0
  node -e 'const fs=require("fs"); for (const path of ["desktop/src-tauri/tauri.conf.json", "frontend/package.json"]) { if (JSON.parse(fs.readFileSync(path)).version !== "2026.1014.0") process.exit(1); }'
  grep -Fx 'version = "2026.1014.0"' desktop/src-tauri/Cargo.toml
)

if (
  cd "$temp"
  desktop/scripts/stamp-release-version.sh v2026.10.14
); then
  echo "invalid release version unexpectedly succeeded" >&2
  exit 1
fi
