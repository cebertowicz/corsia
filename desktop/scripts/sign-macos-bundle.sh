#!/bin/bash
# Sign all executable code inside a macOS app before sealing the outer bundle.
set -euo pipefail

[ "$#" -eq 2 ] || { echo "usage: $0 <identity> <CORSIA.app>" >&2; exit 64; }
identity="$1"
app="$2"
entitlements="$(cd "$(dirname "${BASH_SOURCE[0]}")/../src-tauri" && pwd -P)/Entitlements.plist"
[ -d "$app/Contents" ] || { echo "error: not an app bundle: $app" >&2; exit 1; }
[ -f "$entitlements" ] || { echo "error: missing entitlements: $entitlements" >&2; exit 1; }

python_bin="$app/Contents/Resources/python/bin/python3"
python_versioned_bin="$app/Contents/Resources/python/bin/python3.11"
while IFS= read -r -d '' path; do
  if file -b "$path" | grep -q 'Mach-O'; then
    if [ "$path" = "$python_bin" ] || [ "$path" = "$python_versioned_bin" ]; then
      codesign --force --options runtime --entitlements "$entitlements" --timestamp=none --sign "$identity" "$path"
    else
      codesign --force --options runtime --timestamp=none --sign "$identity" "$path"
    fi
  fi
done < <(find "$app/Contents" -type f -print0)
codesign --force --options runtime --entitlements "$entitlements" --timestamp=none --sign "$identity" "$app"
codesign --verify --deep --strict --verbose=2 "$app"
