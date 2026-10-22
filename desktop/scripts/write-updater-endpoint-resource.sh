#!/bin/bash
# Copy the resolved Tauri updater endpoint into a packaged backend resource.
set -euo pipefail

[ "$#" -eq 2 ] || { echo "usage: $0 <tauri config> <resource output>" >&2; exit 64; }
config="$1"
output="$2"

python3 - "$config" "$output" <<'PY'
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

config_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
payload = json.loads(config_path.read_text(encoding="utf-8"))
endpoints = payload.get("plugins", {}).get("updater", {}).get("endpoints")
if not isinstance(endpoints, list) or len(endpoints) != 1 or not isinstance(endpoints[0], str):
    raise SystemExit("Tauri updater config must define exactly one endpoint")
endpoint = endpoints[0]
parsed = urlparse(endpoint)
if parsed.scheme != "https" or not parsed.netloc or any(character.isspace() for character in endpoint):
    raise SystemExit("Tauri updater endpoint must be an HTTPS URL")
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text(json.dumps({"endpoint": endpoint}) + "\n", encoding="utf-8")
PY
