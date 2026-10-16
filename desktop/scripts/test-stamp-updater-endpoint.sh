#!/bin/bash
set -euo pipefail

[ "$#" -eq 1 ] || { echo "usage: $0 <https updater endpoint>" >&2; exit 64; }
dev_endpoint="$1"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
temp="$(mktemp -d "${TMPDIR:-/tmp}/corsia-updater-endpoint.XXXXXX")"
cleanup() { rm -rf "$temp"; }
trap cleanup EXIT

mkdir -p "$temp/desktop/src-tauri" "$temp/desktop/scripts"
cp "$root/desktop/src-tauri/tauri.conf.json" "$temp/desktop/src-tauri/"
cp "$root/desktop/scripts/stamp-updater-endpoint.sh" "$temp/desktop/scripts/"

(
  cd "$temp"
  desktop/scripts/stamp-updater-endpoint.sh "$dev_endpoint"
  node -e 'const fs=require("fs"); const endpoint=JSON.parse(fs.readFileSync("desktop/src-tauri/tauri.conf.json")).plugins.updater.endpoints; if (endpoint.length !== 1 || endpoint[0] !== process.argv[1]) process.exit(1)' "$dev_endpoint"
)

for invalid_endpoint in 'http://example.test/latest.json' 'not-a-url'; do
  if (
    cd "$temp"
    desktop/scripts/stamp-updater-endpoint.sh "$invalid_endpoint"
  ); then
    echo "invalid updater endpoint unexpectedly succeeded: $invalid_endpoint" >&2
    exit 1
  fi
done
