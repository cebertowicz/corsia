#!/bin/bash
# Stamp an app bundle's Info.plist with its versions. CFBundleShortVersionString
# must be numeric on macOS, so a dev build (0.4.1-dev.N) keeps its full version in
# CORSIAVersion, the key the updater compares against the manifest. Shared by
# build-macos-bundle.sh and the updater's tests so the two cannot drift apart.
set -euo pipefail

[ "$#" -eq 3 ] || { echo "usage: $0 <Info.plist> <short-version> <full-version>" >&2; exit 64; }
plist="$1"
short_version="$2"
full_version="$3"
plistbuddy=/usr/libexec/PlistBuddy

"$plistbuddy" -c "Set :CFBundleShortVersionString $short_version" "$plist" 2>/dev/null ||
  "$plistbuddy" -c "Add :CFBundleShortVersionString string $short_version" "$plist"
"$plistbuddy" -c "Set :CORSIAVersion $full_version" "$plist" 2>/dev/null ||
  "$plistbuddy" -c "Add :CORSIAVersion string $full_version" "$plist"
