#!/bin/bash
# Stamp the updater feed used by one build without changing the source default.
set -euo pipefail

[ "$#" -eq 1 ] || { echo "usage: $0 <https updater endpoint>" >&2; exit 64; }
endpoint="$1"
[[ "$endpoint" =~ ^https://[^[:space:]]+$ ]] || {
  echo "updater endpoint must be an https URL" >&2
  exit 1
}
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"

python3 - "$root" "$endpoint" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
endpoint = sys.argv[2]
tauri = root / "desktop/src-tauri/tauri.conf.json"
payload = json.loads(tauri.read_text())
payload["plugins"]["updater"]["endpoints"] = [endpoint]
tauri.write_text(json.dumps(payload, indent=2) + "\n")
PY
