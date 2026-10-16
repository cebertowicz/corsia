# Runtime helpers for local services.

# CORSIA runs one backend, so a lifecycle verb takes no service selector.
# --force is the only argument these verbs accept. Anything else -- a bare
# word like `server`, or a mistyped option like `--forse` -- is rejected
# rather than ignored, so a command that does not mean what the user typed
# cannot proceed and report success.
#
# This must return nonzero: `fail` only echoes, and under `set -e` a dispatch
# branch ending in `fail` returns echo's 0 and reports success for a command
# that did nothing.
require_no_extra_lifecycle_args() {
    local verb="$1" arg
    shift
    for arg in "$@"; do
        [ "$arg" = "--force" ] && continue
        fail "Unexpected argument '$arg'. Usage: corsia $verb [--force]"
        return 1
    done
}

# Once the CORSIA app adopts an install it runs its own service on that state
# root and records it in service.json (its app folder, platform_paths). A second
# backend on the same state root would share one database, so start and restart
# from a source checkout refuse. The app runs this same script on that root and
# is never refused. A missing file, or one without state_dir, leaves the install
# unclaimed; a file that cannot be read is treated as claimed.
refuse_if_app_owns_install() {
    is_app_mode && return 0
    local verdict
    verdict="$(cd "$BACKEND_DIR" && "$PYTHON" -I -B -c '
import os, sys
sys.path.insert(0, sys.argv[1])
from infra.platform_paths import SERVICE_FILE, app_metadata_dir, service_file_state_dir
path = app_metadata_dir() / SERVICE_FILE
try:
    claimed = service_file_state_dir(path)
except ValueError:
    print("unreadable", path)
else:
    if claimed is not None and str(claimed) == os.path.realpath(sys.argv[2]):
        print("owned", path)
' "$BACKEND_DIR" "$CORSIA_STATE_DIR" 2>/dev/null)" || verdict="unreadable service.json"
    case "$verdict" in
        owned*)
            fail "The CORSIA app owns this install (${verdict#owned }). Use the CORSIA app to start or restart it."
            return 1
            ;;
        unreadable*)
            fail "Cannot read the CORSIA app's ${verdict#unreadable }, so this install may belong to the app. Use the CORSIA app, or fix that file."
            return 1
            ;;
    esac
    return 0
}

is_backend_running() { curl -sf --max-time 3 "http://127.0.0.1:$BACKEND_PORT/api/health" > /dev/null 2>&1; }

# A healthy backend must publish readiness within one minute. Provider catalog
# refresh and durable inbox recovery continue as owned post-readiness work; this
# bound covers only the critical database and continuity path.
CORSIA_HEALTH_WAIT_SECONDS="${CORSIA_HEALTH_WAIT_SECONDS:-60}"
CORSIA_DRAIN_MAX_WAIT="${CORSIA_DRAIN_MAX_WAIT:-60}"

runtime_epoch_seconds() { date +%s; }

wait_for_backend_health() {
    local started_at elapsed tty=0 line_open=0
    [ -t 1 ] && tty=1
    started_at="$(runtime_epoch_seconds)"
    while :; do
        if is_backend_running; then
            [ "$line_open" -eq 0 ] || echo
            return 0
        fi
        elapsed=$(( $(runtime_epoch_seconds) - started_at ))
        if [ "$tty" -eq 1 ]; then
            if [ "$elapsed" -ge 10 ]; then
                printf '\r  Starting CORSIA… (%ss)' "$elapsed"
            else
                printf '\r  Starting CORSIA…'
            fi
            line_open=1
        elif [ "$elapsed" -eq 0 ] || [ "$elapsed" -eq 30 ]; then
            echo "  Starting CORSIA…"
        fi
        [ "$elapsed" -lt "$CORSIA_HEALTH_WAIT_SECONDS" ] || break
        sleep 1
    done
    [ "$line_open" -eq 0 ] || echo
    fail "CORSIA didn't finish starting within ${CORSIA_HEALTH_WAIT_SECONDS} seconds."
    echo "     Run 'corsia logs' to see why (log: ${BACKEND_LOG:-the backend log})."
    return 1
}

# The interface every CORSIA listener binds, from the one Python reader
# (backend/infra/bind_host.py). An unknown or mismatched setup refuses here,
# with the fix on stderr, before anything is stopped, drained or rendered.
resolve_bind_host() {
    (cd "$BACKEND_DIR" && "$PYTHON" -m cli bind-host ${BOOTSTRAP_CONFIG:+--bootstrap "$BOOTSTRAP_CONFIG"})
}

print_endpoints() {
    echo "  Local:     http://localhost:$BACKEND_PORT"
    local ts_ip
    # A loopback-only setup is not reachable over the tailnet; don't offer it.
    [ "$(resolve_bind_host 2>/dev/null)" = 127.0.0.1 ] && return 0
    ts_ip=$(tailscale ip -4 2>/dev/null | head -1)
    [ -n "$ts_ip" ] && echo "  Tailscale: http://$ts_ip:$BACKEND_PORT"
    return 0
}

# `start --open`, the install's last step: recheck and summarize the model
# sources with a short-lived key, name the installed version, open the browser.
# The summary is information, never a reason to fail a healthy start.
open_after_start() {
    run_sources_cli recheck || :
    open_browser
}

# The installed release as a person reads it. A packaged app replaces it with
# its bundle version (corsia.d/app_mode.sh).
installed_release_label() {
    local version commit
    version="$(git -C "$CORSIA_DIR" describe --tags --always 2>/dev/null)" || version=unknown
    commit="$(git -C "$CORSIA_DIR" rev-parse --short HEAD 2>/dev/null)" || commit=unknown
    echo "CORSIA $version (commit $commit)"
}

open_browser() {
    local url="http://localhost:$BACKEND_PORT" release
    echo "     ready at $url"
    release="$(installed_release_label)"
    if command -v open > /dev/null 2>&1 && open "$url"; then
        echo "     Opening your browser…  Installed $release."
    else
        echo "     Open $url in your browser.  Installed $release."
    fi
}

cancel_failed_drain() {
    local drain_url="$1" operation_id="${2:-${CORSIA_RESTART_OPERATION_ID:-}}"
    # A caller that terminates the service next sets DRAIN_FAILURE_ACTION=hold.
    # Releasing the hold for it would readmit work into the gap before teardown
    # and then kill that work without a recovery registration. The surviving
    # hold is safe: startup adopts any pre-bounce phase and settles it.
    if [ "${DRAIN_FAILURE_ACTION:-release}" = hold ]; then
        return 0
    fi
    if [ -z "$operation_id" ]; then
        warn "Could not cancel drain mode without its exact operation id"
        return 1
    fi
    if curl -sf --max-time 5 -X DELETE \
        "${drain_url}&operation_id=${operation_id}" > /dev/null 2>&1
    then
        return 0
    fi
    warn "Could not cancel drain mode after failed drain"
    return 1
}

drain_operation_is_confirmed() {
    local drain_url="$1" operation_id="$2" status
    status="$(curl -sf --max-time 5 \
        "${drain_url}&operation_id=${operation_id}" 2>/dev/null)" || return 1
    printf '%s' "$status" | python3 -c 'import json,sys
expected = sys.argv[1]
payload = json.load(sys.stdin)
if payload.get("operation_id") != expected or payload.get("status") != "in_flight":
    raise ValueError("restart operation is not the exact active operation")' \
        "$operation_id" 2>/dev/null
}

cancel_drain_if_confirmed() {
    local drain_url="$1" operation_id="$2"
    drain_operation_is_confirmed "$drain_url" "$operation_id" || return 0
    cancel_failed_drain "$drain_url" "$operation_id"
}

# Release a direct CLI operation using the exact id available when its owner
# exits. The lifecycle command can acquire that id after this wrapper begins.
_corsia_manual_restart_cleanup() {
    local status="$1" operation_id="${CORSIA_RESTART_OPERATION_ID:-}"
    trap - EXIT
    if [ "$status" -ne 0 ] && [ -n "$operation_id" ]; then
        cancel_drain_if_confirmed \
            "$(current_drain_url)" "$operation_id" || :
    fi
    return "$status"
}

# A detached restart shell is the existing lifecycle owner after the HTTP
# response returns. The outer envelope owns backend-launched failures. Direct
# CLI invocations retain normal errexit semantics and clean up on shell exit.
run_owned_restart_lifecycle() {
    if [ "${_CORSIA_LIFECYCLE_ENVELOPE_ACTIVE:-}" = true ]; then
        "$@"
        return $?
    fi
    local status
    trap '_corsia_manual_restart_cleanup "$?"' EXIT
    "$@"
    status=$?
    trap - EXIT
    _corsia_manual_restart_cleanup "$status"
}

parse_started_drain_operation() {
    local response="$1" requested_id="$2"
    printf '%s' "$response" | python3 -c 'import json,sys
requested_id = sys.argv[1]
payload = json.load(sys.stdin)
if payload.get("ok") is not True or payload.get("operation_id") != requested_id:
    raise ValueError("drain start did not establish the requested operation")
print(requested_id)' "$requested_id" 2>/dev/null
}

parse_drain_refusal_detail() {
    local response="$1"
    printf '%s' "$response" | python3 -c 'import json,sys
payload = json.load(sys.stdin)
detail = payload.get("detail")
if (
    not isinstance(detail, str)
    or not detail
    or len(detail) > 200
    or detail != detail.strip()
    or not detail.isprintable()
):
    raise ValueError("drain refusal detail must be a bounded printable string")
print(detail)' 2>/dev/null
}

acquire_restart_operation() {
    local operation_id="$1" reason="$2" acquired
    acquired="$("$PYTHON" -c '
import sys
sys.path.insert(0, sys.argv[1])
from automation import self_restart
operation_id = self_restart.try_acquire(
    None, None, None, None, sys.argv[3], operation_id=sys.argv[2],
)
if operation_id is None:
    raise SystemExit(1)
print(operation_id)
' "$BACKEND_DIR" "$operation_id" "$reason" 2>/dev/null)" || return 1
    [ "$acquired" = "$operation_id" ]
}

begin_legacy_settings_drain() {
    local drain_url="$1" status operation_id response
    status="$(curl -sf --max-time 5 "$drain_url" 2>/dev/null)" || return 1
    operation_id="$(printf '%s' "$status" | python3 -c 'import json,re,sys
payload = json.load(sys.stdin)
if payload.get("active") is not True:
    print("none")
    raise SystemExit
operation_id = payload.get("operation_id")
if (
    payload.get("reason") != "update"
    or not isinstance(operation_id, str)
    or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{3}", operation_id) is None
):
    raise ValueError("active operation is not the legacy Settings Update handoff")
print(operation_id)' 2>/dev/null)" || return 1
    [ "$operation_id" != none ] || return 2
    response="$(curl -sf --max-time 5 -X POST "$drain_url" \
        -H "Content-Type: application/json" \
        -d "{\"reason\":\"update\",\"operation_id\":\"$operation_id\"}" \
        2>/dev/null)" || {
            cancel_failed_drain "$drain_url" "$operation_id" || :
            return 1
        }
    local confirmed
    if ! confirmed="$(printf '%s' "$response" | python3 -c 'import json,sys
expected = sys.argv[1]
payload = json.load(sys.stdin)
if payload.get("ok") is not True or payload.get("operation_id") != expected:
    raise ValueError("legacy Settings drain changed operation")
print(expected)' "$operation_id" 2>/dev/null)"; then
        cancel_drain_if_confirmed "$drain_url" "$operation_id" || :
        return 1
    fi
    printf '%s' "$confirmed"
}

current_drain_url() {
    local port="${1:-$BACKEND_PORT}"
    printf 'http://127.0.0.1:%s/api/system/drain?%s=%s' \
        "$port" "to""ken" "$CORSIA_TOKEN"
}

drain_and_wait() {
    local reason="${1:-restart}"
    local port="${2:-$BACKEND_PORT}"
    local drain_url
    drain_url="$(current_drain_url "$port")"
    local max_wait="$CORSIA_DRAIN_MAX_WAIT"
    local drain_base="${drain_url%%\?*}"
    local drain_query="${drain_url#*\?}"
    local ready_url="$drain_base/ready?$drain_query"
    local hard_cap_url="$drain_base/hard-cap?$drain_query"
    case "$max_wait" in
        ""|*[!0-9]*)
            warn "Invalid drain timeout: $max_wait"
            return 1
            ;;
    esac
    if [ "$max_wait" -gt 1800 ]; then
        warn "Drain timeout capped at 1800 seconds (was $max_wait)"
        max_wait=1800
    fi

    local operation_id="${CORSIA_RESTART_OPERATION_ID:-}"
    local compatibility_handoff="${3:-}" handoff_status
    if [ -z "$operation_id" ] && [ "$compatibility_handoff" = legacy-settings-update ]; then
        if operation_id="$(begin_legacy_settings_drain "$drain_url")"; then
            :
        else
            handoff_status=$?
            if [ "$handoff_status" -ne 2 ]; then
                warn "Could not establish the exact prepared Update drain operation"
                return 1
            fi
            operation_id=""
        fi
    fi
    if [ -z "$operation_id" ]; then
        local requested_id start_output start_response start_http_status start_refusal
        local returned_operation_id
        operation_id="$(python3 -c 'import uuid; print(uuid.uuid4().hex)' 2>/dev/null)" \
            || { warn "Could not create an exact drain operation id"; return 1; }
        requested_id="$operation_id"
        reason="$reason [corsia-operation:$requested_id]"
        if ! acquire_restart_operation "$requested_id" "$reason"; then
            warn "Could not acquire this restart operation"
            return 1
        fi
        if ! start_output=$(curl -sS --max-time 5 \
            --write-out $'\n%{http_code}' \
            -X POST "$drain_url" \
            -H "Content-Type: application/json" \
            -d "{\"reason\":\"$reason\",\"operation_id\":\"$requested_id\"}" 2>/dev/null); then
            warn "Could not set drain mode"
            cancel_drain_if_confirmed "$drain_url" "$requested_id" || :
            return 1
        fi
        if [[ "$start_output" != *$'\n'* ]]; then
            warn "Could not set drain mode"
            cancel_drain_if_confirmed "$drain_url" "$requested_id" || :
            return 1
        fi
        start_http_status="${start_output##*$'\n'}"
        start_response="${start_output%$'\n'*}"
        case "$start_http_status" in
            [0-9][0-9][0-9]) ;;
            *)
                warn "Could not set drain mode"
                cancel_drain_if_confirmed "$drain_url" "$requested_id" || :
                return 1
                ;;
        esac
        if [ "$start_http_status" != "200" ]; then
            if start_refusal="$(parse_drain_refusal_detail "$start_response")"; then
                warn "Could not set drain mode: $start_refusal"
            else
                warn "Could not set drain mode"
            fi
            cancel_drain_if_confirmed "$drain_url" "$requested_id" || :
            return 1
        fi
        returned_operation_id="$(
            parse_started_drain_operation "$start_response" "$requested_id"
        )" || {
            warn "Drain start response did not establish this restart operation"
            cancel_drain_if_confirmed "$drain_url" "$requested_id" || :
            return 1
        }
        operation_id="$returned_operation_id"
    fi

    export CORSIA_RESTART_OPERATION_ID="$operation_id"
    echo "  Drain started..."
    local started_at waited
    started_at="$(runtime_epoch_seconds)"
    while :; do
        waited=$(( $(runtime_epoch_seconds) - started_at ))
        [ "$waited" -lt "$max_wait" ] || break
        local status
        if ! status=$(curl -sf --max-time 5 "$drain_url" 2>/dev/null); then
            warn "Could not read drain status"
            cancel_failed_drain "$drain_url" "$operation_id" || :
            return 1
        fi
        local poll_status
        if ! poll_status=$(
            printf '%s' "$status" |
                python3 -c 'import json,sys
expected = sys.argv[1]
payload = json.load(sys.stdin)
active_state = payload.get("active")
active = payload.get("activeProcesses")
if isinstance(active, bool) or not isinstance(active, int) or active < 0:
    raise ValueError("activeProcesses must be a non-negative integer")
if active_state is False:
    print("ended")
    print()
    raise SystemExit(0)
if active_state is not True:
    raise ValueError("drain must remain active")
actual = payload.get("operation_id")
if not isinstance(actual, str) or not actual:
    raise ValueError("active drain must identify its operation")
if actual != expected:
    print("replaced")
    print()
    raise SystemExit(0)
blocker = ""
turns = payload.get("blockingTurns", [])
valid_turns = []
if isinstance(turns, list):
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        title = turn.get("title")
        thread_id = turn.get("threadId")
        if isinstance(title, str) and isinstance(thread_id, str):
            valid_turns.append((" ".join(title.split()), " ".join(thread_id.split())))
if valid_turns:
    blocker = f"{valid_turns[0][0]} ({valid_turns[0][1]})"
parts = []
active_turns = payload.get("activeTurns", 0)
if blocker:
    active_turns = max(active_turns, len(valid_turns)) - 1
labels = (
    ("turnAdmissions", "turn admission"),
    (None, "more active turn" if blocker else "active turn"),
    ("hostAccessSupervisors", "host-access supervisor"),
    ("ownedProcessGroups", "owned process group"),
    ("browserCommands", "browser command"),
)
for key, label in labels:
    value = active_turns if key is None else payload.get(key, 0)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        suffix = "s" if value != 1 else ""
        parts.append(f"{value} {label}{suffix}")
details = ", ".join(parts)
if blocker and details:
    blocker = f"{blocker}; plus {details}"
elif not blocker:
    blocker = details
print(active)
print(blocker)' "$operation_id" 2>/dev/null
        ); then
            warn "Drain status was malformed"
            cancel_failed_drain "$drain_url" "$operation_id" || :
            return 1
        fi
        local active="${poll_status%%$'\n'*}"
        local blocker=""
        if [[ "$poll_status" == *$'\n'* ]]; then
            blocker="${poll_status#*$'\n'}"
        fi
        case "$active" in
            ended)
                warn "Restart operation ended before drain completed"
                cancel_failed_drain "$drain_url" "$operation_id" || :
                return 1
                ;;
            replaced)
                warn "Restart operation was replaced before drain completed"
                cancel_failed_drain "$drain_url" "$operation_id" || :
                return 1
                ;;
        esac
        if [ "$active" = "0" ]; then
            local ready_output ready_response ready_http_status
            if ! ready_output=$(curl -sS --max-time 5 \
                --write-out $'\n%{http_code}' \
                -X POST "$ready_url" \
                -H "Content-Type: application/json" \
                -d "{\"operation_id\":\"$operation_id\"}" 2>/dev/null); then
                warn "Could not mark drain ready"
                cancel_failed_drain "$drain_url" "$operation_id" || :
                return 1
            fi
            if [[ "$ready_output" != *$'\n'* ]]; then
                warn "Drain ready response was malformed"
                cancel_failed_drain "$drain_url" "$operation_id" || :
                return 1
            fi
            ready_http_status="${ready_output##*$'\n'}"
            ready_response="${ready_output%$'\n'*}"
            if [ "$ready_http_status" = "409" ] \
                && printf '%s' "$ready_response" |
                    python3 -c 'import json,sys
if json.load(sys.stdin) != {"detail": "restart operation is not quiescent"}:
    raise ValueError("not the readiness race")' 2>/dev/null
            then
                sleep 2
                continue
            fi

            if [ "$ready_http_status" != "200" ]; then
                warn "Could not mark drain ready"
                cancel_failed_drain "$drain_url" "$operation_id" || :
                return 1
            fi
            if ! printf '%s' "$ready_response" |
                python3 -c 'import json,sys
expected = sys.argv[1]
payload = json.load(sys.stdin)
if payload.get("ok") is not True or payload.get("operation_id") != expected:
    raise ValueError("ready response lost the exact operation")' "$operation_id" 2>/dev/null; then
                warn "Drain ready response was malformed"
                cancel_failed_drain "$drain_url" "$operation_id" || :
                return 1
            fi
            ok "Drain complete"
            return 0
        fi
        # The /ready endpoint, not a projected phase, owns quiescence.
        if [ -n "$blocker" ]; then
            printf '  Waiting... %s (%ss)\n' "$blocker" "$waited"
        else
            echo "  Waiting... $active blocking work item$([ "$active" = "1" ] || printf 's') (${waited}s)"
        fi
        sleep 2
    done

    warn "Drain timed out; enforcing hard cap"
    local hard_cap_response hard_cap_forced
    if ! hard_cap_response=$(curl -sf --max-time 30 \
        -X POST "$hard_cap_url" \
        -H "Content-Type: application/json" \
        -d "{\"operation_id\":\"$operation_id\"}" 2>/dev/null); then
        warn "Could not enforce drain hard cap; cancelling restart"
        cancel_failed_drain "$drain_url" "$operation_id" || :
        return 1
    fi
    if ! hard_cap_forced=$(printf '%s' "$hard_cap_response" |
        python3 -c 'import json,sys
expected = sys.argv[1]
payload = json.load(sys.stdin)
if (
    payload.get("ok") is not True
    or payload.get("force_stop_ready") is not True
    or payload.get("operation_id") != expected
):
    raise ValueError("hard-cap response is not force-stop ready")
failures = payload.get("failures")
if failures is not None and not isinstance(failures, list):
    raise ValueError("hard-cap failures must be a list")
print(",".join(str(item) for item in (failures or [])))' "$operation_id" 2>/dev/null); then
        warn "Drain hard cap was not ready; cancelling restart"
        cancel_failed_drain "$drain_url" "$operation_id" || :
        return 1
    fi
    if [ -n "$hard_cap_forced" ]; then
        warn "Force-stopped task(s) that would not yield: $hard_cap_forced"
        warn "Each one is registered to resume after startup"
    fi
    ok "Drain hard cap complete"
    return 0
}

backend_pids() {
    local pids pid
    pids=$(lsof -nP -iTCP:"$BACKEND_PORT" -sTCP:LISTEN -t 2>/dev/null || true)
    for pid in $pids; do
        if ps -o command= -p "$pid" 2>/dev/null | grep -Eq "uvicorn|python"; then
            echo "$pid"
        fi
    done
}

# The directory a backend process runs from. Every supported launch (plist
# WorkingDirectory, start_backend's cd) makes that this checkout's backend dir,
# so it identifies which checkout a listener belongs to without guessing from
# ports or process names.
backend_pid_working_directory() {
    local cwd
    command -v lsof > /dev/null 2>&1 || return 1
    cwd="$(lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
    [ -n "$cwd" ] || return 1
    printf '%s\n' "$cwd"
}

backend_pid_serves_this_checkout() {
    local cwd
    cwd="$(backend_pid_working_directory "$1")" || return 1
    same_directory "$cwd" "$BACKEND_DIR"
}

# Direct-process verbs act on whatever listens on this checkout's port. Before
# they drain, stop, or replace it, every listener must be proven to run from
# this checkout; a listener that cannot be attributed is refused, not assumed.
# A launchd job under this checkout's label that owns the port is also refused
# here: killing it directly would only have KeepAlive bring it back.
require_port_served_by_this_checkout() {
    local pids pid cwd
    pids="$(backend_pids)"
    [ -n "$pids" ] || return 0
    if service_is_loaded; then
        require_loaded_service_owns_port || return 1
        fail "$(foreign_loaded_service_guidance "$(service_label)" "$(service_plist_path)")"
        return 1
    fi
    for pid in $pids; do
        if ! cwd="$(backend_pid_working_directory "$pid")"; then
            fail "Cannot prove the backend on :$BACKEND_PORT (PID $pid) runs from this checkout; refusing to manage it"
            return 1
        fi
        same_directory "$cwd" "$BACKEND_DIR" || {
            fail "Port :$BACKEND_PORT is served by another checkout ($cwd, PID $pid), not $BACKEND_DIR. Set BACKEND_PORT in this checkout's .env to run a second instance."
            return 1
        }
    done
}

start_backend() {
    local bind
    require_port_served_by_this_checkout || return 1
    if is_backend_running; then
        ok "Backend already running on :$BACKEND_PORT"
        return 0
    fi
    bind="$(resolve_bind_host)" || return 1
    local port_wait=0
    while [ -n "$(backend_pids)" ]; do
        if [ $port_wait -ge 15 ]; then
            local STALE_PID=$(backend_pids | head -1)
            if [ -n "$STALE_PID" ]; then
                backend_pid_serves_this_checkout "$STALE_PID" || {
                    fail "Port :$BACKEND_PORT is held by a process from another checkout (PID $STALE_PID); refusing to kill it"
                    return 1
                }
                kill -9 "$STALE_PID" 2>/dev/null && sleep 1
            fi
            break
        fi
        sleep 1
        port_wait=$((port_wait + 1))
    done
    echo "  Starting CORSIA backend on :$BACKEND_PORT..."
    cd "$BACKEND_DIR"
    env -u CORSIA_RESTART_BACKEND_PORT -u CORSIA_RESTART_OPERATION_ID \
        -u CORSIA_TEST_MODE -u CORSIA_ALLOW_HOME_WORKSPACE_IN_CI \
        BACKEND_PORT="$BACKEND_PORT" \
        nohup "$PYTHON" -m uvicorn main:app --host "$bind" --port "$BACKEND_PORT" > "$BACKEND_LOG" 2>&1 &
}

read_backend_start_iso() {
    local port="${1:-$BACKEND_PORT}" payload parser
    if ! payload=$(curl -sf --max-time 5 \
        "http://127.0.0.1:$port/api/version" 2>/dev/null); then
        return 1
    fi
    parser="${PYTHON:-python3}"
    printf '%s' "$payload" | "$parser" -c 'import json,sys
value = (json.load(sys.stdin).get("backend") or {}).get("start_iso")
if not isinstance(value, str) or not value:
    raise ValueError("backend start identity is missing")
print(value)' 2>/dev/null
}

prove_backend_replaced() {
    local port="$1" before="${2:-}" after
    if ! after="$(read_backend_start_iso "$port")"; then
        fail "Backend is healthy, but its replacement identity could not be read"
        return 1
    fi
    if [ -n "$before" ] && [ "$after" = "$before" ]; then
        fail "Backend stayed on the original process after restart"
        return 1
    fi
}

print_backend_status() {
    local payload revision checkout topology pid
    if ! payload=$(curl -sf --max-time 5 \
        "http://127.0.0.1:$BACKEND_PORT/api/version" 2>/dev/null); then
        fail "Backend health responded, but build identity could not be read"
        return 1
    fi
    if ! revision=$(
        printf '%s' "$payload" |
            python3 -c 'import json,sys
payload = json.load(sys.stdin)
backend = payload.get("backend") or {}
frontend = payload.get("frontend") or {}
backend_sha = backend.get("full_hash")
frontend_sha = frontend.get("full_hash")
current_sha = (backend.get("current_head") or {}).get("full_hash")
if not isinstance(backend_sha, str) or not backend_sha:
    raise ValueError("backend revision is missing")
if not isinstance(frontend_sha, str) or frontend_sha != backend_sha:
    raise ValueError("frontend and backend revisions differ")
if current_sha is not None and current_sha != backend_sha:
    raise ValueError("running backend and checkout revisions differ")
print(backend_sha)' 2>/dev/null
    ); then
        fail "Backend is reachable, but backend, frontend, and checkout revisions do not agree"
        return 1
    fi

    checkout="$(serving_checkout_root)"
    if service_managed; then
        topology="launchd $(service_label)"
    else
        pid="$(backend_pids | head -1)"
        topology="legacy process PID ${pid:-unknown}"
    fi
    ok "Health: ok"
    echo "  Serving on :$BACKEND_PORT"
    echo "  Checkout: $checkout"
    echo "  Revision: ${revision:0:12} (backend + frontend)"
    echo "  Process: $topology"
}

start_server() {
    if service_installed; then
        service_start || return 1
    else
        start_backend || return 1
    fi
    wait_for_backend_health || return 1
    print_backend_status
}

# Direct-process stop for this checkout's backend. Drains first unless forced,
# and only after the listener is proven to be this checkout's.
stop_direct_backend() {
    local drain_attempted=false
    require_port_served_by_this_checkout || return 1
    if [ "$FORCE" = "false" ] && is_backend_running; then
        # Same guarantee as the managed stop: the drain is bounded courtesy,
        # the stop is the promise, and the hold is kept across the gap.
        drain_attempted=true
        DRAIN_FAILURE_ACTION=hold drain_and_wait "stop" \
            || warn "Drain did not complete; stopping anyway"
    fi
    # A failed stop leaves the old backend serving with no restart coming to
    # settle the operation, so the hold has to come off here.
    if ! stop_backend; then
        [ "$drain_attempted" = false ] \
            || cancel_failed_drain "$(current_drain_url)" || :
        return 1
    fi
}

stop_backend() {
    local PID=$(backend_pids | head -1)
    if [ -n "$PID" ]; then
        backend_pid_serves_this_checkout "$PID" || {
            fail "Port :$BACKEND_PORT is served by a process from another checkout (PID $PID); refusing to stop it"
            return 1
        }
        kill "$PID" 2>/dev/null
        for _ in $(seq 1 15); do
            kill -0 "$PID" 2>/dev/null || { ok "Backend stopped (PID $PID)"; return 0; }
            sleep 1
        done
        # A non-zero kill here usually means the process exited during the TERM
        # wait above, so liveness - not kill's exit code - decides the outcome.
        kill -9 "$PID" 2>/dev/null || :
        sleep 1
        if kill -0 "$PID" 2>/dev/null; then
            fail "Backend remained running after forced stop (PID $PID)"
            return 1
        fi
        warn "Backend force-killed"
    else
        warn "Backend not running"
    fi
}

# Restart THIS backend (drain + bounce), routing a loaded or installed launchd
# service to its sole owner. Shared by `restart` and the sanctioned `redeploy`.
bounce_server() {
    local after_teardown="${1:-}" before_start_iso="" drain_attempted=false
    if is_backend_running; then
        if ! before_start_iso="$(read_backend_start_iso "$BACKEND_PORT")"; then
            fail "Backend is healthy, but its pre-restart identity could not be read"
            return 1
        fi
    fi
    if service_installed; then
        service_restart "$after_teardown" || return 1   # drains internally
    else
        require_port_served_by_this_checkout || return 1
        # Refuse a bad setup while the old backend is still serving.
        resolve_bind_host > /dev/null || return 1
        if [ "$FORCE" = "false" ] && is_backend_running; then
            drain_attempted=true
            DRAIN_FAILURE_ACTION=hold drain_and_wait "restart" \
                || warn "Drain did not complete; restarting anyway"
        fi
        # A failed stop leaves the old backend serving with no bounce coming to
        # settle the operation, so the hold has to come off here. Past this
        # point the backend is down and startup owns the surviving operation.
        if ! stop_backend; then
            [ "$drain_attempted" = false ] \
                || cancel_failed_drain "$(current_drain_url)" || :
            return 1
        fi
        sleep 1
        [ -z "$after_teardown" ] || "$after_teardown" || return 1
        start_backend || return 1
    fi
    wait_for_backend_health || return 1
    prove_backend_replaced "$BACKEND_PORT" "$before_start_iso" || return 1
    print_backend_status
}

# The checkout containing this corsia script = the serving checkout the backend
# spawned us from (resolved from the running code, no topology guess). Overridable
# via SELF_CHECKOUT for tests.
serving_checkout_root() {
    echo "${SELF_CHECKOUT:-$(dirname "$SCRIPT_DIR")}"
}

# The recovery rule is shared with backend controlled fast-forwards. Runtime
# supplies its existing update wording; the shared file itself has no runtime
# globals or top-level effects.
CORSIA_GIT_LOCK_RECOVERY_FAILURE_PREFIX="Redeploy refused: "
source "$(dirname "${BASH_SOURCE[0]}")/git_lock_recovery.sh"

# A stage has never been activated and is safe to discard when no rollback
# backup/absence marker exists. Kept as a seam for failure-path tests.
discard_stale_redeploy_stage() {
    rm -rf "$1"
}

frontend_release_paths() {
    local fe="$1/frontend" stage backup
    stage="$fe/node_modules/.corsia-dist-stage"
    backup="$fe/node_modules/.corsia-dist-previous"
    printf '%s\t%s\t%s\t%s\t%s\n' \
        "$fe" "$stage" "${stage}.prepared" "$backup" "${backup}.absent"
}

# Build into one checkout-local staging directory. Activation is deferred until
# drain succeeds, so every pre-bounce failure leaves live assets untouched.
redeploy_build() {
    local checkout="$1" fe stage prepared backup absent
    IFS=$'\t' read -r fe stage prepared backup absent \
        <<< "$(frontend_release_paths "$checkout")"
    [ -d "$fe" ] || return 0
    # Always sync from package-lock.json: the pull may have added a dependency
    # to an existing node_modules. Not `npm ci`, which deletes node_modules and
    # with it the dist stage/backup state below. --no-save keeps npm from
    # rewriting the tracked lock, which would leave the checkout dirty and
    # refuse every later Update.
    echo "  Installing locked frontend dependencies ..."
    ( cd "$fe" && npm install --no-audit --no-fund --no-save ) \
        || { fail "Redeploy refused: npm install failed — instance left serving the current build"; return 1; }
    # Backup/absence markers mean activation or rollback may have started and
    # remain fail-closed. A stage by itself was never activated, so discard that
    # exact disposable directory and rebuild it.
    if [ -e "$backup" ] || [ -e "$absent" ]; then
        fail "Redeploy refused: an earlier restart left rollback state in $fe/node_modules \
(.corsia-dist-previous / .corsia-dist-previous.absent). Confirm $fe/dist is the build \
you want before changing that recovery state."
        return 1
    fi
    if [ -e "$prepared" ]; then
        fail "Redeploy refused: a validated frontend update is already awaiting restart at $stage"
        return 1
    fi
    if [ -e "$stage" ]; then
        if ! discard_stale_redeploy_stage "$stage" || [ -e "$stage" ]; then
            fail "Redeploy refused: could not remove stale frontend stage $stage"
            return 1
        fi
        warn "Discarded stale unactivated frontend stage at $stage"
    fi
    mkdir "$stage" || { fail "Redeploy refused: could not stage the frontend build"; return 1; }
    echo "  Building frontend ..."
    if ! ( cd "$fe" && CORSIA_BUILD_OUTPUT="$stage" npm run build ); then
        rm -rf "$stage" || warn "Could not remove the failed staged frontend build"
        fail "Redeploy refused: frontend build failed — instance left serving the current build"
        return 1
    fi
    if ! : > "$prepared"; then
        rm -rf "$stage" || warn "Could not remove the unowned staged frontend build"
        fail "Redeploy refused: could not mark the staged frontend build as prepared"
        return 1
    fi
}

discard_redeploy_build() {
    local checkout="$1" fe stage prepared backup absent
    IFS=$'\t' read -r fe stage prepared backup absent \
        <<< "$(frontend_release_paths "$checkout")"
    rm -rf "$stage"
    rm -f "$prepared"
}

# Install the pulled backend/requirements.lock into the serving venv, then
# validate it. Like the pull before it, this changes what the running process
# would load next; a failure leaves that process serving and prevents a bounce.
redeploy_sync_python_dependencies() {
    local lock="$1/backend/requirements.lock"
    echo "  Installing locked Python dependencies ..."
    if ! "$PYTHON" -m pip install -r "$lock"; then
        fail "Redeploy refused: pip install of backend/requirements.lock failed — instance left running; the venv may be partly updated until the next successful Update"
        return 1
    fi
    echo "  Checking Python dependencies ..."
    if ! "$PYTHON" -m pip check; then
        fail "Redeploy refused: Python dependency check failed — instance left running"
        return 1
    fi
}

UPDATE_PREPARE_FETCH_FAILED=41
UPDATE_PREPARE_BUILD_FAILED=42
# Refusals before any pull: nothing can change by retrying until the person acts.
UPDATE_PREPARE_INDEX_LOCKED=43
UPDATE_PREPARE_DETACHED=44
UPDATE_PREPARE_DIRTY=45
UPDATE_PREPARE_AWAITING_RESTART=46

# Pull, dependency-check, and stage a build from the serving checkout's CURRENT
# branch. The caller holds the Update mutex for this whole function.
prepare_redeploy_locked() {
    local checkout="$1" git_dir="$2" branch fe stage prepared backup absent

    recover_stale_git_index_lock "$git_dir" || return "$UPDATE_PREPARE_INDEX_LOCKED"
    branch="$(git -C "$checkout" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
    if [ -z "$branch" ]; then
        fail "Redeploy refused: serving checkout is in detached HEAD — no branch to pull"
        return "$UPDATE_PREPARE_DETACHED"
    fi
    if [ -n "$(git -C "$checkout" status --porcelain 2>/dev/null)" ]; then
        fail "Redeploy refused: serving checkout has uncommitted changes (deployment surface must stay clean)"
        return "$UPDATE_PREPARE_DIRTY"
    fi
    IFS=$'\t' read -r fe stage prepared backup absent \
        <<< "$(frontend_release_paths "$checkout")"
    if [ -e "$prepared" ]; then
        if [ ! -d "$stage" ]; then
            fail "Redeploy refused: prepared frontend ownership marker has no matching staged build at $stage"
            return 1
        fi
        fail "Redeploy refused: a validated frontend update is already awaiting restart at $stage"
        return "$UPDATE_PREPARE_AWAITING_RESTART"
    fi
    echo "  Pulling --ff-only ($branch) in $checkout ..."
    if ! git -C "$checkout" pull --ff-only; then
        fail "Redeploy refused: git pull --ff-only failed (diverged / non-fast-forward / offline) — checkout left untouched"
        return "$UPDATE_PREPARE_FETCH_FAILED"
    fi
    redeploy_sync_python_dependencies "$checkout" || return "$UPDATE_PREPARE_BUILD_FAILED"
    redeploy_build "$checkout" || return "$UPDATE_PREPARE_BUILD_FAILED"
    echo "  Pull + dependency check + build OK"
}

# Run one release operation under the checkout's existing Update mutex.
run_with_update_mutex() {
    local checkout="$1" refusal="$2" owner="$3" git_dir result
    if ! git -C "$checkout" rev-parse --is-inside-work-tree > /dev/null 2>&1; then
        fail "$refusal: $checkout is not a git checkout"
        return 1
    fi
    git_dir="$(git -C "$checkout" rev-parse --absolute-git-dir 2>/dev/null || true)"
    if [ -z "$git_dir" ] || [ ! -d "$git_dir" ]; then
        fail "$refusal: could not resolve the serving checkout Git directory"
        return 1
    fi

    exec 8> "$git_dir/corsia-update.lock" || {
        fail "$refusal: could not open the Update mutex"
        return 1
    }
    if ! acquire_fd_mutex 8; then
        exec 8>&-
        fail "$refusal: another Update is already preparing this checkout"
        return 1
    fi
    if "$owner" "$checkout" "$git_dir"; then
        result=0
    else
        result=$?
    fi
    exec 8>&-
    return "$result"
}

# Serialize pull/build preparation. A completed frontend build also carries a
# durable `.prepared` marker until restart_prepared activates or discards it.
prepare_redeploy() {
    run_with_update_mutex \
        "$(serving_checkout_root)" "Redeploy refused" prepare_redeploy_locked
}

activate_prepared_frontend() {
    local checkout expected_sha fe stage prepared backup absent recovering=false
    checkout="${1:-$(serving_checkout_root)}"
    expected_sha="${2:-}"
    IFS=$'\t' read -r fe stage prepared backup absent \
        <<< "$(frontend_release_paths "$checkout")"
    [ -d "$fe" ] || { REDEPLOY_FRONTEND_STATE=activated; return 0; }
    REDEPLOY_FRONTEND_STATE=failed

    if [ -e "$backup" ] || [ -e "$absent" ]; then
        recovering=true
        if [ -e "$fe/dist" ] || [ -z "$expected_sha" ] \
            || [ ! -f "$prepared" ] \
            || ! frontend_release_matches_sha "$stage" "$expected_sha"
        then
            fail "Frontend activation recovery refused: incomplete state was preserved"
            return 1
        fi
        if [ -d "$backup" ] && [ ! -e "$absent" ] \
            && [ -s "$backup/index.html" ] \
            && frontend_release_sha "$backup" > /dev/null
        then
            :
        elif [ ! -e "$backup" ] && [ -f "$absent" ]; then
            :
        else
            fail "Frontend activation recovery refused: rollback state was inconsistent and preserved"
            return 1
        fi
    fi

    if [ "$recovering" != true ] && [ -e "$fe/dist" ]; then
        if ! mv "$fe/dist" "$backup"; then
            fail "Frontend activation stopped after old-backend absence was confirmed: the prior build remains at $fe/dist and the prepared build remains at $stage. No service was bootstrapped."
            return 1
        fi
    elif [ "$recovering" != true ] && ! touch "$absent"; then
        fail "Frontend activation stopped after old-backend absence was confirmed: prior-build absence could not be recorded at $absent and the prepared build remains at $stage. No service was bootstrapped."
        return 1
    fi
    if ! mv "$stage" "$fe/dist"; then
        fail "Frontend activation failed after old-backend absence was confirmed. Prepared build: $stage. Prior build or absence marker: $backup / $absent. No service was bootstrapped; preserve these paths for recovery."
        return 1
    fi
    REDEPLOY_FRONTEND_STATE=activated
}

frontend_release_sha() {
    "$PYTHON" -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
value = payload.get("full_hash")
if (
    not isinstance(value, str)
    or not value
    or value == "unknown"
    or payload.get("dirty") is not False
):
    raise SystemExit(1)
print(value)
' "$1/build-info.json" 2>/dev/null
}

frontend_release_matches_sha() {
    local release_dir="$1" expected_sha="$2" actual_sha
    [ -d "$release_dir" ] && [ -s "$release_dir/index.html" ] || return 1
    actual_sha="$(frontend_release_sha "$release_dir")" || return 1
    [ "$actual_sha" = "$expected_sha" ]
}

clear_frontend_release_state() {
    local checkout="$1" fe stage prepared backup absent result=0
    IFS=$'\t' read -r fe stage prepared backup absent \
        <<< "$(frontend_release_paths "$checkout")"
    rm -rf "$backup" || {
        warn "Could not remove the prior frontend build backup"
        result=1
    }
    rm -f "$absent" || {
        warn "Could not remove the frontend rollback marker"
        result=1
    }
    rm -f "$prepared" || {
        warn "Could not remove the prepared frontend marker"
        result=1
    }
    return "$result"
}

ensure_current_frontend_release_locked() {
    local checkout="$1" fe stage prepared backup absent head_sha
    IFS=$'\t' read -r fe stage prepared backup absent \
        <<< "$(frontend_release_paths "$checkout")"
    [ -d "$fe" ] || return 0
    head_sha="$(git -C "$checkout" rev-parse HEAD 2>/dev/null || true)"
    if [ -z "$head_sha" ]; then
        fail "Frontend startup refused: could not resolve the serving checkout revision"
        return 1
    fi
    if is_backend_running; then
        if [ ! -e "$prepared" ] &&
            [ ! -e "$backup" ] &&
            [ ! -e "$absent" ] &&
            frontend_release_matches_sha "$fe/dist" "$head_sha"
        then
            return 0
        fi
        fail "Frontend startup refused: another backend is still healthy; active assets and prepared state were preserved"
        return 1
    fi

    if [ ! -d "$stage" ] &&
        frontend_release_matches_sha "$fe/dist" "$head_sha" &&
        { [ -e "$prepared" ] || [ -e "$backup" ] || [ -e "$absent" ]; }
    then
        if ! clear_frontend_release_state "$checkout"; then
            fail "Frontend startup refused: completed activation cleanup did not finish"
            return 1
        fi
        return 0
    fi
    if { [ -e "$backup" ] || [ -e "$absent" ]; } \
        && { [ -e "$fe/dist" ] || [ ! -f "$prepared" ] \
            || ! frontend_release_matches_sha "$stage" "$head_sha"; }
    then
        fail "Frontend startup refused: incomplete activation state remains in $fe/node_modules"
        return 1
    fi

    if [ -e "$prepared" ]; then
        if [ ! -d "$stage" ] || ! frontend_release_matches_sha "$stage" "$head_sha"; then
            fail "Frontend startup refused: prepared build does not match checkout $head_sha; retained at $stage"
            return 1
        fi
    elif frontend_release_matches_sha "$fe/dist" "$head_sha"; then
        return 0
    else
        redeploy_build "$checkout" || return 1
        if [ ! -e "$prepared" ] \
            || ! frontend_release_matches_sha "$stage" "$head_sha"
        then
            fail "Frontend startup refused: completed build does not match checkout $head_sha; retained at $stage"
            return 1
        fi
    fi

    activate_prepared_frontend "$checkout" "$head_sha" || return 1
    if ! clear_frontend_release_state "$checkout"; then
        fail "Frontend startup refused: activated release cleanup did not complete"
        return 1
    fi
    if ! frontend_release_matches_sha "$fe/dist" "$head_sha"; then
        fail "Frontend startup refused: activated assets do not match checkout $head_sha"
        return 1
    fi
}

ensure_current_frontend_release() {
    local checkout
    checkout="$(serving_checkout_root)"
    # A backend launched by the installed plist carries its generation stamp.
    # That backend must prove the loaded definition is this checkout's before
    # touching assets. A backend without one was not launched by any plist (a
    # worktree instance, a direct uvicorn), so no installed service can be its
    # owner and there is nothing to verify against.
    if [ -n "${CORSIA_SERVICE_GENERATION:-}" ]; then
        require_owned_service_definition || return 1
    fi
    run_with_update_mutex \
        "$checkout" "Frontend startup refused" ensure_current_frontend_release_locked
}

# Drain while the old release is intact. The lifecycle owner invokes frontend
# activation only after it has confirmed that no old backend remains.
restart_prepared() {
    local checkout fe stage prepared backup absent original_force
    local drain_attempted=false
    checkout="$(serving_checkout_root)"
    IFS=$'\t' read -r fe stage prepared backup absent \
        <<< "$(frontend_release_paths "$checkout")"

    if [ -d "$fe" ]; then
        [ -d "$stage" ] || { fail "Prepared frontend build is missing"; return 1; }
        [ -e "$prepared" ] || { fail "Prepared frontend ownership marker is missing"; return 1; }
        if [ -e "$backup" ] || [ -e "$absent" ]; then
            fail "Prepared frontend rollback path already exists"
            return 1
        fi
    fi
    # restart_prepared drains before bounce_server, so it must prove the same
    # owner bounce_server/service_restart would prove before that first effect.
    # Revalidation inside the bounce remains intentional across the drain gap.
    if service_installed; then
        require_owned_service_definition || return 1
        if service_is_loaded; then
            if is_backend_running || [ -n "$(backend_pids)" ]; then
                require_loaded_service_owns_port || return 1
            fi
        else
            require_no_unmanaged_backend "$BACKEND_PORT" || return 1
        fi
    else
        require_port_served_by_this_checkout || return 1
    fi
    if [ "$FORCE" = "false" ]; then
        if ! is_backend_running; then
            fail "Safe update refused: the current backend is not healthy, so drain ownership cannot be proven. The prepared build was retained at $stage; recover the running service, or explicitly use --force if interruption is intended."
            return 1
        fi
        # Update is the only path that pulls, so refusing here on an incomplete
        # drain means new code can never reach this machine -- and the only
        # escape left, --force, skips the drain entirely: no steering, no hard
        # cap, and no recovery registration for the work it kills. Proceeding
        # after a bounded drain is strictly the safer of the two. The teardown
        # sequencing below is unchanged, so release pairing still holds.
        drain_attempted=true
        if ! DRAIN_FAILURE_ACTION=hold drain_and_wait \
            "update and restart" "$BACKEND_PORT" legacy-settings-update
        then
            warn "Drain did not complete; updating anyway"
        fi
    fi
    REDEPLOY_FRONTEND_STATE=not_started
    original_force="$FORCE"
    FORCE=true
    if bounce_server activate_prepared_frontend; then
        FORCE="$original_force"
        clear_frontend_release_state "$checkout" || :
        return 0
    fi
    FORCE="$original_force"
    case "$REDEPLOY_FRONTEND_STATE" in
        activated)
            clear_frontend_release_state "$checkout" || :
            if [ -d "$fe" ]; then
                fail "Restart failed after release activation; the prepared frontend remains paired with the updated on-disk backend"
            else
                fail "Restart failed after release activation; no frontend assets were present and the updated on-disk backend was not bootstrapped"
            fi
            ;;
        failed)
            fail "Restart stopped because frontend activation failed; the prepared build remains at $stage and no service was bootstrapped"
            ;;
        *)
            # No teardown happened, so the old build keeps serving and no
            # bounce is coming to settle the operation at startup. Release the
            # hold here or ingress stays frozen on the instance that survived.
            [ "$drain_attempted" = false ] || cancel_failed_drain "$(current_drain_url)" || :
            fail "Restart failed before confirmed teardown; the prior frontend remains active and the prepared build remains at $stage"
            ;;
    esac
    return 1
}

# Restart never pulls. When an earlier explicit Update already prepared the
# current on-disk release, consume that exact stage so backend and frontend stay
# paired; otherwise bounce the current active build.
restart_current_release() {
    local checkout fe stage prepared backup absent
    checkout="$(serving_checkout_root)"
    IFS=$'\t' read -r fe stage prepared backup absent \
        <<< "$(frontend_release_paths "$checkout")"
    if [ -e "$prepared" ]; then
        [ -d "$stage" ] || {
            fail "Restart refused: prepared frontend ownership marker has no matching staged build at $stage"
            return 1
        }
        restart_prepared
        return $?
    fi
    bounce_server
}

# Execute the post-pull phase through the script now present in the serving
# checkout. The current shell sourced its functions before prepare_redeploy
# pulled, so calling restart_prepared in-process would run stale lifecycle code.
handoff_redeploy_restart() {
    local checkout="$1" script
    script="$checkout/scripts/corsia"
    if [ ! -x "$script" ]; then
        fail "Redeploy prepared successfully, but the updated lifecycle script is not executable: $script"
        return 1
    fi
    echo "  Handing off to the updated lifecycle..."
    if [ "${_CORSIA_LIFECYCLE_ENVELOPE_ACTIVE:-}" = true ]; then
        if [ "$FORCE" = "true" ]; then
            source "$script" restart-prepared --force
            return $?
        fi
        source "$script" restart-prepared
        return $?
    fi
    if [ "$FORCE" = "true" ]; then
        exec "$script" restart-prepared --force
    fi
    exec "$script" restart-prepared
}

# Sanctioned self-redeploy: prepare the current branch, then replace this
# pre-pull shell with the lifecycle implementation from the updated checkout.
redeploy_self() {
    local checkout
    checkout="$(serving_checkout_root)"
    prepare_redeploy || return 1
    handoff_redeploy_restart "$checkout"
}
