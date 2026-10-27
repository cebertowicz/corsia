#!/bin/bash
# Invoked by the systemd unit; arguments pin the service's runtime identity.
set -e
readonly backend="$1" python="$2" env_file="$3" bind="$4" port="$5"
readonly workspace="$6" state="$7" service_path="$8" original_home="$HOME"
if [ -f "$env_file" ]; then
    set -a
    # The same checkout-owned environment file is loaded by scripts/corsia.
    source "$env_file"
    set +a
fi
unset CORSIA_TEST_MODE CORSIA_ALLOW_HOME_WORKSPACE_IN_CI CORSIA_RESTART_BACKEND_PORT CORSIA_RESTART_OPERATION_ID CORSIA_RESTART_CALLER_TOKEN CORSIA_RESTART_CURL
export CORSIA_WORKSPACE="$workspace" CORSIA_STATE_DIR="$state" BACKEND_PORT="$port"
export HOME="$original_home" PATH="$service_path" CORSIA_DEVICES_DISABLE=1 CORSIA_SYSTEMD_SERVICE=1
cd "$backend"
exec "$python" -m uvicorn main:app --host "$bind" --port "$port"
