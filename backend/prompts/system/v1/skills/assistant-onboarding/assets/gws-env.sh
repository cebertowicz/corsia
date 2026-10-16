#!/usr/bin/env bash
# Source this helper before every gws command. It deliberately produces no output.
# Optional: source .../gws-env.sh --client-json /path/to/downloaded-desktop-client.json

_gws_fail() { printf '%s\n' 'Unable to prepare the Google Workspace environment.' >&2; }

_gws_client_json=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --client-json)
      [ "$#" -ge 2 ] || { _gws_fail; return 1 2>/dev/null || exit 1; }
      _gws_client_json=$2
      shift 2
      ;;
    *)
      _gws_fail; return 1 2>/dev/null || exit 1
      ;;
  esac
done

# The store is this thread's own gws home, outside the workspace. CORSIA sets CORSIA_GWS_HOME
# for the thread's shell; without it there is nowhere safe to keep credentials, so fail closed.
case "${CORSIA_GWS_HOME:-}" in
  /?*) _gws_root="$CORSIA_GWS_HOME" ;;
  *)
    printf '%s\n' 'Unable to prepare the Google Workspace environment: this shell has no Google Workspace home (CORSIA_GWS_HOME).' >&2
    return 1 2>/dev/null || exit 1
    ;;
esac
[ -d "$_gws_root" ] || { _gws_fail; return 1 2>/dev/null || exit 1; }

umask 077
export SSL_CERT_FILE=/etc/ssl/cert.pem
export GOOGLE_WORKSPACE_CLI_CONFIG_DIR="$_gws_root/config"
export GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file
export TMPDIR="$_gws_root/tmp"
mkdir -p "$GOOGLE_WORKSPACE_CLI_CONFIG_DIR" "$TMPDIR" || { _gws_fail; return 1 2>/dev/null || exit 1; }
chmod 700 "$_gws_root" "$GOOGLE_WORKSPACE_CLI_CONFIG_DIR" "$TMPDIR" 2>/dev/null || { _gws_fail; return 1 2>/dev/null || exit 1; }
_gws_client_config="$GOOGLE_WORKSPACE_CLI_CONFIG_DIR/desktop-client.json"

# One-time move of a store an earlier helper kept inside the workspace. The workspace is
# found from this helper's place in the skills projection, else CORSIA_WORKSPACE; the old
# copy is deleted only after every file has moved.
_gws_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
_gws_tail='/.corsia/skills/assistant-onboarding/assets'
case "$_gws_here" in
  /?*"$_gws_tail") _gws_old_ws="${_gws_here%"$_gws_tail"}" ;;
  *) _gws_old_ws="${CORSIA_WORKSPACE:-}" ;;
esac
_gws_old="$_gws_old_ws/.corsia-assistant/gws"
if [ -n "$_gws_old_ws" ] && [ -d "$_gws_old/config" ] && [ ! -L "$_gws_old/config" ] \
    && [ -z "$(ls -A "$GOOGLE_WORKSPACE_CLI_CONFIG_DIR" 2>/dev/null)" ]; then
  if cp -Rp "$_gws_old/config/." "$GOOGLE_WORKSPACE_CLI_CONFIG_DIR/" 2>/dev/null; then
    rm -rf "$_gws_old"
    rmdir "$_gws_old_ws/.corsia-assistant" 2>/dev/null
  else
    rm -rf "$GOOGLE_WORKSPACE_CLI_CONFIG_DIR"
    mkdir -p "$GOOGLE_WORKSPACE_CLI_CONFIG_DIR" && chmod 700 "$GOOGLE_WORKSPACE_CLI_CONFIG_DIR"
  fi
fi
unset _gws_here _gws_tail _gws_old_ws _gws_old

if [ -n "$_gws_client_json" ]; then
  [ -f "$_gws_client_json" ] && [ ! -L "$_gws_client_json" ] || { _gws_fail; return 1 2>/dev/null || exit 1; }
  [ ! -e "$_gws_client_config" ] || { _gws_fail; return 1 2>/dev/null || exit 1; }
  if ! python3 - "$_gws_client_json" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as source:
        payload = json.load(source)
    client = payload.get("installed") or payload.get("web")
    valid = isinstance(client, dict) and all(
        isinstance(client.get(field), str) and client[field]
        for field in ("client_id", "client_secret")
    )
except (OSError, ValueError):
    valid = False
if not valid:
    raise SystemExit(1)
PY
  then
    _gws_fail; return 1 2>/dev/null || exit 1
  fi
  mv "$_gws_client_json" "$_gws_client_config" || { _gws_fail; return 1 2>/dev/null || exit 1; }
  chmod 600 "$_gws_client_config" || { _gws_fail; return 1 2>/dev/null || exit 1; }
fi

[ -f "$_gws_client_config" ] || { _gws_fail; return 1 2>/dev/null || exit 1; }
_gws_client_id="$(python3 - "$_gws_client_config" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
client = payload.get("installed") or payload.get("web")
sys.stdout.write(client["client_id"])
PY
)" || { _gws_fail; return 1 2>/dev/null || exit 1; }
_gws_client_secret="$(python3 - "$_gws_client_config" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
client = payload.get("installed") or payload.get("web")
sys.stdout.write(client["client_secret"])
PY
)" || { _gws_fail; return 1 2>/dev/null || exit 1; }
export GOOGLE_WORKSPACE_CLI_CLIENT_ID="$_gws_client_id"
export GOOGLE_WORKSPACE_CLI_CLIENT_SECRET="$_gws_client_secret"
unset _gws_client_id _gws_client_secret _gws_client_json _gws_client_config _gws_root
