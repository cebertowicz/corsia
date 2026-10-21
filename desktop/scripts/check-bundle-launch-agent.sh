#!/bin/bash
set -euo pipefail

app="${1:?usage: $0 /path/to/CORSIA.app}"
plist="$app/Contents/Library/LaunchAgents/ai.corsia.service.plist"
[ -f "$plist" ] || { echo "missing launch agent: $plist" >&2; exit 1; }
plutil -lint "$plist"
value() { plutil -extract "$1" raw "$plist"; }
[ "$(value Label)" = "ai.corsia.service" ]
[ "$(value BundleProgram)" = "Contents/Resources/bin/corsia-service" ]
[ "$(value RunAtLoad)" = "true" ]
[ "$(value KeepAlive.SuccessfulExit)" = "false" ]
[ "$(value AssociatedBundleIdentifiers.0)" = "ai.corsia.app" ]
echo "bundle launch agent is valid: $plist"
