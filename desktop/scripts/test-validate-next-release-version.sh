#!/bin/bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
validator="$root/desktop/scripts/validate-next-release-version.sh"

"$validator" 0.4.0 v0.3.0

for next_version in 0.3.0 0.2.9 invalid; do
  if "$validator" "$next_version" v0.3.0; then
    echo "invalid next release version unexpectedly succeeded: $next_version" >&2
    exit 1
  fi
done

if "$validator" 0.4.0 v0.3.0-dev.1; then
  echo "prerelease latest tag unexpectedly succeeded" >&2
  exit 1
fi
