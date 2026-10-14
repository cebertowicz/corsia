#!/bin/bash
# Stamp CORSIA package manifests from one validated release tag.
set -euo pipefail

[ "$#" -eq 1 ] || { echo "usage: $0 <vYYYY.MMDD.N>" >&2; exit 64; }
tag="$1"
[[ "$tag" =~ ^v[0-9]{4}\.[1-9][0-9]{2,3}\.[0-9]+$ ]] || {
  echo "release tag must be vYYYY.MMDD.N" >&2
  exit 1
}
version="${tag#v}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"

python3 - "$root" "$version" <<'PY'
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
version = sys.argv[2]
tauri = root / "desktop/src-tauri/tauri.conf.json"
cargo = root / "desktop/src-tauri/Cargo.toml"
frontend = root / "frontend/package.json"

payload = json.loads(tauri.read_text())
payload["version"] = version
tauri.write_text(json.dumps(payload, indent=2) + "\n")

cargo_text = cargo.read_text()
cargo_text, replacements = re.subn(
    r'(?m)^(version\s*=\s*)"[^"]+"$', rf'\g<1>"{version}"', cargo_text, count=1,
)
if replacements != 1:
    raise SystemExit("expected one package version in Cargo.toml")
cargo.write_text(cargo_text)

frontend_payload = json.loads(frontend.read_text())
frontend_payload["version"] = version
frontend.write_text(json.dumps(frontend_payload, indent=2) + "\n")
PY
