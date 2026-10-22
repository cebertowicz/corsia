#!/bin/bash
# Prove stamp-bundle-version.sh gives the same CORSIAVersion on both channels:
# the release version as given, with no channel-specific branch.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
temp="$(mktemp -d "${TMPDIR:-/tmp}/corsia-bundle-version.XXXXXX")"
trap 'rm -rf "$temp"' EXIT

check() {
  local release_version="$1" expected_short="$2" plist="$temp/Info.plist"
  cat >"$plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>CFBundleShortVersionString</key><string>0.0.0</string><key>CFBundleVersion</key><string>1.0.412</string></dict></plist>
PLIST
  # The short version is derived exactly as release-macos.yml derives it.
  local short_version="${release_version%%-dev.*}"
  "$root/desktop/scripts/stamp-bundle-version.sh" "$plist" "$short_version" "$release_version"
  [ "$(/usr/libexec/PlistBuddy -c 'Print :CORSIAVersion' "$plist")" = "$release_version" ]
  [ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$plist")" = "$expected_short" ]
  [ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$plist")" = "1.0.412" ]
}

check 0.4.1-dev.412 0.4.1
check 0.4.1 0.4.1
echo "bundle version stamping ok"
