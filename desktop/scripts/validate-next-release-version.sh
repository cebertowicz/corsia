#!/bin/bash
# Verify that the release-owner-controlled next version advances stable semver.
set -euo pipefail

[ "$#" -eq 2 ] || { echo "usage: $0 <next X.Y.Z> <latest stable vX.Y.Z>" >&2; exit 64; }
next_version="$1"
latest_tag="$2"
[[ "$next_version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || {
  echo "next release version must be X.Y.Z, got: $next_version" >&2
  exit 1
}
next_major=$((10#${BASH_REMATCH[1]}))
next_minor=$((10#${BASH_REMATCH[2]}))
next_patch=$((10#${BASH_REMATCH[3]}))
[[ "$latest_tag" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || {
  echo "latest stable tag must be vX.Y.Z, got: $latest_tag" >&2
  exit 1
}
latest_major=$((10#${BASH_REMATCH[1]}))
latest_minor=$((10#${BASH_REMATCH[2]}))
latest_patch=$((10#${BASH_REMATCH[3]}))

if (( next_major < latest_major ||
  (next_major == latest_major && next_minor < latest_minor) ||
  (next_major == latest_major && next_minor == latest_minor && next_patch <= latest_patch) )); then
  echo "next release version $next_version must be strictly above latest stable $latest_tag" >&2
  exit 1
fi
