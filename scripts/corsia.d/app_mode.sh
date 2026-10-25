# App mode: this script running from inside a packaged CORSIA.app.
#
# The app ships scripts/corsia and corsia.d at Contents/Resources/scripts,
# beside Resources/backend, so one lifecycle implementation (drain, health,
# exit codes) serves a source checkout and the app. App mode is one path check
# on the resolved script directory, never an environment variable, and this
# file is the only place that knows about it. It replaces these resolvers and
# nothing else:
#
#   checkout root  Contents/Resources, the script's parent as for a checkout
#   python         the bundled interpreter, with Resources/bin/corsia's PYTHONPATH
#   config source  service.json in the app folder (workspace, port, and a
#                  state_dir when it names one); never a .env or the machine
#                  bootstrap a source install reads
#   state root     service.json's state_dir when present, else the backend's
#                  default_state_root() for this bundle
#   version        CFBundleShortVersionString+CFBundleVersion, the generation
#                  the launcher gives the service
#   service verbs  the bundle's launchd job; Update stages and swaps the app
#                  instead of pulling and building
#
# Nothing here writes inside the bundle: Resources is signed and read-only.

_corsia_app_bundle=""
_corsia_app_dir=""
_corsia_app_state_root=""
case "$SCRIPT_DIR" in
    */?*.app/Contents/Resources/scripts)
        _corsia_app_bundle="${SCRIPT_DIR%/Contents/Resources/scripts}"
        ;;
esac

is_app_mode() { [ -n "$_corsia_app_bundle" ]; }

APP_SERVICE_LABEL=ai.corsia.service

# Checkout resolvers, run once the generic ones have set CORSIA_DIR (which is
# Resources here). An explicit CORSIA_WORKSPACE or BACKEND_PORT still wins
# over service.json, as it does for the launcher.
app_mode_checkout() {
    local resolved line config_error=""
    PYTHON="$CORSIA_DIR/python/bin/python3"
    ENV_FILE=""
    BOOTSTRAP_CONFIG=""
    export PYTHONPATH="$CORSIA_DIR/python/backend-packages"
    export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
    if ! resolved="$("$PYTHON" -I -B -c '
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from infra.platform_paths import app_metadata_dir, default_state_root
from infra.service_file import SERVICE_FILE, service_file_lines
app_dir = app_metadata_dir()
print(f"app={app_dir}")
print(f"state={default_state_root(app_bundle=Path(sys.argv[2]))}")
if (app_dir / SERVICE_FILE).is_file():
    print("\n".join(service_file_lines(app_dir / SERVICE_FILE)))
' "$BACKEND_DIR" "$_corsia_app_bundle")"; then
        echo "Could not resolve this CORSIA app's folders from $BACKEND_DIR" >&2
        return 1
    fi
    while IFS= read -r line; do
        case "$line" in
            app=*) _corsia_app_dir="${line#app=}" ;;
            state=*) _corsia_app_state_root="${line#state=}" ;;
            state_dir=*)
                [ -n "${CORSIA_STATE_DIR:-}" ] \
                    || _corsia_app_state_root="${line#state_dir=}"
                ;;
            error=*) config_error="${line#error=}" ;;
            workspace=*)
                [ -n "${CORSIA_WORKSPACE:-}" ] \
                    || export CORSIA_WORKSPACE="${line#workspace=}"
                ;;
            port=*) [ -n "${BACKEND_PORT:-}" ] || BACKEND_PORT="${line#port=}" ;;
        esac
    done <<< "$resolved"
    # The launcher and the app refuse the same way: falling back to the default state root
    # would open a different, empty folder beside the data service.json names.
    if [ -z "${CORSIA_STATE_DIR:-}" ] && [ -n "$config_error" ]; then
        echo "CORSIA refused to run: $config_error, so no other state folder was opened" >&2
        return 1
    fi
}

# While the app moves the data folder (CORSIA/state-move.json exists), nothing
# may start the service: a start between the rename and the service.json rewrite
# would recreate an empty folder at the old path. Stopping and reading stay
# allowed, since the move itself stops the service through `corsia stop`.
app_mode_refuse_during_state_move() {
    [ -e "$_corsia_app_dir/state-move.json" ] || return 0
    case "${1:-status} ${2:-}" in
        "stop "*|"status "*|"logs "*|"service stop"|"service status") return 0 ;;
    esac
    echo "CORSIA refused to run: the app is moving CORSIA's data to its new folder. Open CORSIA to finish the move, then try again." >&2
    return 1
}

app_mode_runtime_identity() {
    if [ -z "${CORSIA_WORKSPACE:-}" ]; then
        echo "This CORSIA app has no workspace recorded in $_corsia_app_dir/service.json; open CORSIA to finish setting it up." >&2
        return 1
    fi
    CORSIA_STATE_DIR="${CORSIA_STATE_DIR:-$_corsia_app_state_root}"
    export CORSIA_WORKSPACE CORSIA_STATE_DIR
    # The launcher sends the service's output here.
    BACKEND_LOG="${BACKEND_LOG:-$CORSIA_STATE_DIR/logs/service.log}"
}

# CFBundleShortVersionString+CFBundleVersion of the bundle at $1.
app_mode_generation() {
    "$PYTHON" -I -B -c 'import plistlib, sys
with open(sys.argv[1], "rb") as handle:
    info = plistlib.load(handle)
print(info["CFBundleShortVersionString"] + "+" + info["CFBundleVersion"])' \
        "$1/Contents/Info.plist"
}

app_mode_backend_generation() {
    local payload
    payload="$(curl -sf --max-time 5 \
        "http://127.0.0.1:$BACKEND_PORT/api/version" 2>/dev/null)" || return 1
    printf '%s' "$payload" | "$PYTHON" -I -B -c 'import json, sys
value = (json.load(sys.stdin).get("backend") or {}).get("generation")
if not isinstance(value, str) or not value:
    raise SystemExit(1)
print(value)' 2>/dev/null
}

# Reads `launchctl print` of the loaded job on stdin: it is this bundle's when
# its definition or program lies inside the bundle. Only the job's own
# top-level keys count, never nested blocks such as its environment.
app_mode_job_is_this_bundle() {
    awk -v bundle="$_corsia_app_bundle" -v label="$APP_SERVICE_LABEL" '
        function normalize(value, parts, count, i, top, stack, result) {
            count = split(value, parts, "/"); top = 0
            for (i = 1; i <= count; i++) {
                if (parts[i] == "" || parts[i] == ".") continue
                if (parts[i] == "..") { if (top > 0) top--; continue }
                stack[++top] = parts[i]
            }
            result = ""
            for (i = 1; i <= top; i++) result = result "/" stack[i]
            return result
        }
        {
            line = $0; sub(/^[[:space:]]*/, "", line)
            if (line == "}") { if (depth > 0) depth--; next }
            if (substr(line, length(line), 1) == "{") { depth++; next }
            if (depth != 1 || line !~ /^(path|program|program identifier) = /) next
            key = line; sub(/ = .*/, "", key)
            value = substr(line, index(line, " = ") + 3)
            sub(/ \(mode: [^)]*\)$/, "", value)
            gsub(/^"|"$/, "", value)
            if (value == "") next
            if (key == "path") {
                plist = bundle "/Contents/Library/LaunchAgents/" label ".plist"
                if (normalize(value) == plist) owned = 1
                next
            }
            if (substr(value, 1, 1) != "/") {
                if (key == "program") next
                value = bundle "/" value
            }
            if (index(normalize(value), bundle "/") == 1) owned = 1
        }
        END { exit owned ? 0 : 1 }
    '
}

# The ownership predicate, before any effect: a loaded job is this bundle's,
# and a running backend reports this bundle's generation. Anything else is
# another copy of CORSIA and is refused.
app_mode_require_owned_service() {
    local job expected actual
    service_is_loaded || return 0
    if ! job="$(launchctl print "$(service_domain)/$(service_label)" 2>/dev/null)"; then
        fail "Could not inspect $(service_label); refusing to act on it"
        return 1
    fi
    if ! printf '%s\n' "$job" | app_mode_job_is_this_bundle; then
        fail "$(service_label) is running from another copy of CORSIA, not $_corsia_app_bundle; refusing to act on it"
        return 1
    fi
    is_backend_running || return 0
    expected="$(app_mode_generation "$_corsia_app_bundle")" || return 1
    if ! actual="$(app_mode_backend_generation)"; then
        fail "The running CORSIA service did not report its version; refusing to act on it"
        return 1
    fi
    if [ "$actual" != "$expected" ]; then
        fail "The running CORSIA service is version $actual, but this app is $expected; refusing to act on it"
        return 1
    fi
}

# Every shipped app already understands these two SMAppService operations. Keep
# the restart sequence here rather than requiring a newer CLI verb: rollback
# starts the previously installed app, which can predate this script. Neither
# is handed the update lock's descriptor.
app_mode_service_reload() {
    "$_corsia_app_bundle/Contents/MacOS/CORSIA" --service unregister 7>&- \
        && "$_corsia_app_bundle/Contents/MacOS/CORSIA" --service register 7>&-
}

# The one update lock (update.lock in the state root), which the app binary's
# stage, swap and rollback take, held from before the service stops until the
# new or restored app is proven. The bundled Python locks descriptor 7, which
# this shell opened, so the lock belongs to the shell's open file: it outlives
# that Python child and carries through the handoff's source or exec into the
# new app's script. Called again after the handoff, it confirms the inherited
# lock. A descriptor 7 that is not this state root's update.lock is replaced.
# Returns 75 when another holder has it.
app_mode_hold_update_lock() {
    local lock="$CORSIA_STATE_DIR/update.lock" status
    mkdir -p "$CORSIA_STATE_DIR" && (umask 077 && : >> "$lock") || return 1
    { : >&7; } 2>/dev/null || exec 7<> "$lock" || return 1
    "$PYTHON" -I -B -c 'import fcntl, os, stat, sys
held, lock = os.fstat(7), os.stat(sys.argv[1])
if not stat.S_ISREG(held.st_mode) or (held.st_dev, held.st_ino) != (lock.st_dev, lock.st_ino):
    raise SystemExit(3)
try:
    fcntl.flock(7, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(75)' "$lock"
    status=$?
    if [ "$status" -eq 3 ]; then
        exec 7<> "$lock" || return 1
        app_mode_hold_update_lock
        return $?
    fi
    return "$status"
}

app_mode_release_update_lock() { exec 7>&-; }

# Swap and rollback are the only children told about the held lock: the app
# binary checks the descriptor is this state root's update.lock and locks that
# same open file instead of opening its own, which the shell's lock would refuse.
app_mode_update_binary() {
    CORSIA_UPDATE_LOCK_FD=7 "$_corsia_app_bundle/Contents/MacOS/CORSIA" "$@"
}

app_mode_service_start() {
    require_owned_service_definition || return 1
    if service_is_loaded; then
        ok "$(service_label) already running"
        return 0
    fi
    require_no_unmanaged_backend "$BACKEND_PORT" || return 1
    if ! app_mode_service_reload; then
        fail "Could not start $(service_label)"
        return 1
    fi
    ok "$(service_label) started"
}

# Restart in place: kickstart -k keeps the registered definition, so a restart
# never re-registers. It returns once launchd has a new process, so the health
# wait that follows cannot be answered by the old one.
app_mode_service_restart() {
    local drain_attempted=false old_pid waited=0
    if ! service_is_loaded; then
        app_mode_service_start
        return $?
    fi
    require_owned_service_definition || return 1
    if [ "$FORCE" = "false" ] \
        && { is_backend_running || [ -n "$(backend_pids)" ]; }; then
        require_loaded_service_owns_port || return 1
        drain_attempted=true
        DRAIN_FAILURE_ACTION=hold drain_and_wait "restart" "$BACKEND_PORT" \
            || warn "Drain did not complete; restarting anyway"
    fi
    old_pid="$(service_loaded_pid)" || old_pid=""
    if ! launchctl kickstart -k "$(service_domain)/$(service_label)"; then
        [ "$drain_attempted" = false ] \
            || cancel_failed_drain "$(current_drain_url)" "${CORSIA_RESTART_OPERATION_ID:-}" \
            || :
        fail "$(service_label) did not restart; the running service was left as it was"
        return 1
    fi
    while [ -n "$old_pid" ] && [ "$(service_loaded_pid 2>/dev/null)" = "$old_pid" ]; do
        if [ "$waited" -ge "$SERVICE_EXIT_TIMEOUT_SECONDS" ]; then
            fail "$(service_label) still runs its old process after ${waited}s"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    ok "$(service_label) restarted"
}

app_mode_refuse_registration() {
    fail "corsia service $1 does not apply to the CORSIA app, which registers its own service: use $_corsia_app_bundle/Contents/MacOS/CORSIA --service register|unregister"
    return 1
}

# Beside the installed app, as the app binary names them (update_stage.rs).
app_mode_staged_path() { echo "$(dirname "$_corsia_app_bundle")/CORSIA.app.staged"; }
app_mode_previous_path() { echo "$(dirname "$_corsia_app_bundle")/CORSIA.app.previous"; }

# Update's prepare step: the app fetches, verifies and stages the signed
# release beside itself while CORSIA keeps serving. Its exit status is the
# contract the backend maps (41 fetch, 42 build, 75 busy), so it passes through.
app_mode_stage_update() {
    local operation_id="${CORSIA_RESTART_OPERATION_ID:-}"
    if [ -z "$operation_id" ]; then
        operation_id="$("$PYTHON" -I -c 'import uuid; print(uuid.uuid4().hex)')" \
            || return 1
    fi
    "$_corsia_app_bundle/Contents/MacOS/CORSIA" --update-stage "$operation_id"
}

app_mode_discard_staged() { rm -rf "$(app_mode_staged_path)"; }

# Update's restart step, in two phases told apart by what sits beside the app:
#   CORSIA.app.staged    before the swap (this script runs from the old app):
#                        prove ownership, drain, stop, swap the staged app in,
#                        then hand off to the new app's own script
#   CORSIA.app.previous  after the swap (the new app's script): start, prove
#                        health and the new generation, then delete the old app
app_mode_restart_prepared() {
    local staged previous status
    staged="$(app_mode_staged_path)"
    previous="$(app_mode_previous_path)"
    if [ -d "$staged" ]; then
        app_mode_swap_staged_update "$staged"
        status=$?
        app_mode_release_update_lock
        return "$status"
    elif [ -d "$previous" ]; then
        # Inherited from the swap. An app from before the lock hands off
        # without it, so it is taken here; another holder stops the update
        # before it starts, checks or rolls back anything.
        if ! app_mode_hold_update_lock; then
            fail "Update stopped: another update or start of CORSIA is in progress, so the installed app was neither started, checked nor rolled back. The previous app remains at $previous."
            return 1
        fi
        app_mode_start_swapped_update "$previous"
        status=$?
        app_mode_release_update_lock
        return "$status"
    else
        fail "There is no staged update to install at $staged"
        return 1
    fi
}

app_mode_swap_staged_update() {
    local staged="$1" drain_attempted=false swap_status
    local operation_id="${CORSIA_RESTART_OPERATION_ID:-update}"
    require_owned_service_definition || return 1
    if service_is_loaded; then
        if is_backend_running || [ -n "$(backend_pids)" ]; then
            require_loaded_service_owns_port || return 1
        fi
    else
        require_no_unmanaged_backend "$BACKEND_PORT" || return 1
    fi
    # Held through the stop, the swap and the new app's proof, or the rollback.
    if ! app_mode_hold_update_lock; then
        fail "Update stopped: another update or start of CORSIA is in progress, so the staged update was kept at $staged and CORSIA was left as it was"
        return 1
    fi
    if [ "$FORCE" = "false" ]; then
        if ! is_backend_running; then
            fail "Safe update refused: the current backend is not healthy, so drain ownership cannot be proven. The staged update was kept at $staged; recover the running service, or explicitly use --force if interruption is intended."
            return 1
        fi
        drain_attempted=true
        DRAIN_FAILURE_ACTION=hold drain_and_wait \
            "update and restart" "$BACKEND_PORT" legacy-settings-update \
            || warn "Drain did not complete; updating anyway"
    fi
    if service_is_loaded \
        && ! bootout_service_and_wait "$(service_domain)" "$(service_label)"; then
        if service_is_loaded; then
            [ "$drain_attempted" = false ] \
                || cancel_failed_drain "$(current_drain_url)" "${CORSIA_RESTART_OPERATION_ID:-}" \
                || :
        else
            app_mode_service_reload || :
        fi
        fail "Update stopped: CORSIA did not stop, so the staged update was not installed"
        return 1
    fi
    # The exchange is atomic, so the installed path always holds a whole app;
    # a failure leaves this one there, and it is started again.
    if app_mode_update_binary --update-swap "$operation_id"; then
        :
    else
        swap_status=$?
        fail "The update could not be installed (swap exit $swap_status); starting this version again"
        app_mode_service_reload \
            || fail "CORSIA could not be started again; open CORSIA to start it"
        return 1
    fi
    # This shell runs the old app's code from memory; everything after the
    # swap comes from the new app, reached by the installed path.
    handoff_redeploy_restart "$_corsia_app_bundle/Contents/Resources"
}

# Succeeds when the database's committed schema is newer than the newest the app
# at $1 can open, read from that app's own infra/schema.py. A new build migrates
# at startup and an older one refuses a newer schema on every start, so swapping
# one back then crash-loops. An answer that cannot be read counts as newer, since
# preventing that swap is this check's whole purpose. Read through SQLite, never
# the file header: a migration committed just before its process was stopped can
# still sit in the WAL.
app_mode_data_outgrew() {
    local previous="$1" versions
    if ! versions="$("$PYTHON" -I -B -c '
import ast, sqlite3, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from infra.platform_paths import runtime_db_paths

def supported(source):
    for node in ast.parse(Path(source).read_text()).body:
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        if any(isinstance(t, ast.Name) and t.id == "SCHEMA_VERSION" for t in targets):
            value = getattr(node.value, "value", None)
            if type(value) is int:
                return value
    raise ValueError("no SCHEMA_VERSION")

newest = supported(sys.argv[3])
db = runtime_db_paths(Path(sys.argv[2]))[0]
# A missing database cannot be read, so it counts as newer: after the data
# folder moves, a stale state path must never approve a downgrade.
conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=10)
try:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
finally:
    conn.close()
print(current, newest)
' "$BACKEND_DIR" "$CORSIA_STATE_DIR" "$previous/Contents/Resources/backend/infra/schema.py" 2>/dev/null)" \
        || ! [[ "$versions" =~ ^([0-9]+)\ ([0-9]+)$ ]]; then
        warn "Could not tell whether $previous can open CORSIA's data"
        return 0
    fi
    [ "${BASH_REMATCH[1]}" -gt "${BASH_REMATCH[2]}" ]
}

# Proves a started update: healthy, running this app's generation, and its
# Update operation confirmed complete. On failure, says why in
# _corsia_app_update_failure.
app_mode_verify_swapped_update() {
    local expected actual
    if ! wait_for_backend_health; then
        _corsia_app_update_failure="The updated service did not become healthy."
        return 1
    fi
    if ! expected="$(app_mode_generation "$_corsia_app_bundle")"; then
        _corsia_app_update_failure="The updated app's version could not be read."
        return 1
    fi
    if ! actual="$(app_mode_backend_generation)"; then
        _corsia_app_update_failure="The updated service did not report its version."
        return 1
    fi
    if [ "$actual" != "$expected" ]; then
        _corsia_app_update_failure="The updated service is version $actual, not $expected."
        return 1
    fi
    if ! app_mode_report_update true "" || [ "$_corsia_app_update_status" != complete ]; then
        _corsia_app_update_failure="The updated service could not complete its Update operation."
        return 1
    fi
}

# Gives up on a swapped update that failed a check: rolls back to the previous
# app, unless the new one has already upgraded the data past what the previous
# app can open. Then it keeps the new app (roll forward) and checks it once
# more. Succeeds only when a kept update passes those checks.
app_mode_abandon_swapped_update() {
    local previous="$1" reason="$2"
    _corsia_app_update_kept=false
    if app_mode_data_outgrew "$previous"; then
        app_mode_keep_swapped_update "$previous" "$reason" running && return 0
        return 1
    fi
    app_mode_rollback_swapped_update "$previous" "$reason" || :
    [ "$_corsia_app_update_kept" = true ]
}

# Keeps the swapped update that already upgraded the data, starting it again if
# the rollback had stopped it. Never restores the pre-upgrade backup: that would
# silently drop everything written since.
app_mode_keep_swapped_update() {
    local previous="$1" reason="$2" state="$3" previous_generation generation
    previous_generation="$(app_mode_generation "$previous" 2>/dev/null || echo unknown)"
    generation="$(app_mode_generation "$_corsia_app_bundle" 2>/dev/null || echo unknown)"
    warn "$reason The update already upgraded CORSIA's data, which version $previous_generation can't open, so CORSIA is keeping version $generation."
    if [ "$state" = stopped ] && ! app_mode_service_reload; then
        fail "CORSIA kept version $generation but could not start it. Open CORSIA to start it again. Version $previous_generation remains at $previous."
        return 1
    fi
    if [ "${_corsia_app_update_reported:-false}" != true ] && app_mode_verify_swapped_update; then
        _corsia_app_update_kept=true
        rm -rf "$previous" || warn "Could not remove the previous app at $previous"
        ok "CORSIA is running version $generation"
        print_backend_status
        return 0
    fi
    reason="${_corsia_app_update_failure:-$reason}"
    [ "${_corsia_app_update_reported:-false}" = true ] || app_mode_report_update false "$reason" || :
    fail "$reason CORSIA kept version $generation because it already upgraded your data, which version $previous_generation can't open. Open CORSIA to try starting it again. Version $previous_generation remains at $previous, and the copy of your data from before the upgrade is beside the database in CORSIA's data folder."
    return 1
}

app_mode_rollback_swapped_update() {
    local previous="$1" reason="$2" operation_id="${CORSIA_RESTART_OPERATION_ID:-update}" previous_generation rollback_status
    previous_generation="$(app_mode_generation "$previous" 2>/dev/null || echo unknown)"
    warn "$reason Trying to restore version $previous_generation."
    if [ "${_corsia_app_update_reported:-false}" != true ] \
        && ! app_mode_report_update false "$reason"; then
        if is_backend_running; then
            fail "The Update failure could not be recorded; the running service was left in place"
            return 1
        fi
        # A backend that never answered cannot acknowledge. Older bundles ignore
        # the non-holding verifying phase, so restoring one cannot hold ingress.
        warn "The Update failure could not be recorded because the service is not answering"
    fi
    # The acknowledged backend row is terminal and immutable. Stop, native
    # rollback, and start failures below are reported only by this shell.
    if service_is_loaded && ! bootout_service_and_wait "$(service_domain)" "$(service_label)"; then
        fail "$reason CORSIA could not stop the new service, so it was not rolled back. The new app remains installed at $_corsia_app_bundle and the previous app remains at $previous."
        return 1
    fi
    # Checked again once stopped: a migration can commit after the first check.
    if app_mode_data_outgrew "$previous"; then
        app_mode_keep_swapped_update "$previous" "$reason" stopped && return 0
        return 1
    fi
    if app_mode_update_binary --update-rollback "$operation_id"; then
        :
    else
        rollback_status=$?
        fail "$reason CORSIA could not roll back (rollback exit $rollback_status). The new app remains installed at $_corsia_app_bundle and the previous app remains at $previous."
        return 1
    fi
    if ! app_mode_service_reload; then
        fail "$reason CORSIA went back to version $previous_generation, but could not start it. The failed update remains at $previous."
        return 1
    fi
    if ! wait_for_backend_health; then
        fail "$reason CORSIA went back to version $previous_generation, but could not confirm it is healthy. The failed update remains at $previous."
        return 1
    fi
    if [ "$previous_generation" = unknown ] \
        || [ "$(app_mode_backend_generation 2>/dev/null)" != "$previous_generation" ]; then
        fail "CORSIA is answering, but the earlier version could not be confirmed. The failed update remains at $previous."
        return 1
    fi
    rm -rf "$previous" || warn "Could not remove the failed update at $previous"
    ok "The update didn't start, so CORSIA went back to version $previous_generation"
    print_backend_status
    return 0
}

app_mode_report_update() {
    local healthy="$1" reason="$2" operation_id="${CORSIA_RESTART_OPERATION_ID:-}"
    local drain_url payload response
    [ -n "$operation_id" ] || return 1
    drain_url="$(current_drain_url)"
    payload="$("$PYTHON" -I -B -c 'import json,sys
print(json.dumps({"operation_id": sys.argv[1], "healthy": sys.argv[2] == "true", "reason": sys.argv[3]}))' \
        "$operation_id" "$healthy" "$reason")" || return 1
    response="$(curl -sf --max-time 5 -X POST \
        "${drain_url%%\?*}/update?${drain_url#*\?}" \
        -H 'Content-Type: application/json' -d "$payload")" || return 1
    _corsia_app_update_status="$(printf '%s' "$response" | "$PYTHON" -I -B -c 'import json,sys
row = json.load(sys.stdin)
if row.get("operation_id") != sys.argv[1] or row.get("status") not in {"complete", "failed"}:
    raise SystemExit(1)
print(row["status"])' "$operation_id")" || return 1
    _corsia_app_update_reported=true
}

app_mode_start_swapped_update() {
    local previous="$1" job
    if service_is_loaded; then
        if ! job="$(launchctl print "$(service_domain)/$(service_label)" 2>/dev/null)" \
            || ! printf '%s\n' "$job" | app_mode_job_is_this_bundle; then
            fail "$(service_label) is running from another copy of CORSIA; the update was installed but not started"
            return 1
        fi
        if ! launchctl kickstart -k "$(service_domain)/$(service_label)" 7>&-; then
            app_mode_abandon_swapped_update "$previous" "The updated service could not be restarted." && return 0
            return 1
        fi
    else
        require_no_unmanaged_backend "$BACKEND_PORT" || return 1
        if ! app_mode_service_reload; then
            app_mode_abandon_swapped_update "$previous" "The updated service could not be started." && return 0
            return 1
        fi
    fi
    if ! app_mode_verify_swapped_update; then
        app_mode_abandon_swapped_update "$previous" "$_corsia_app_update_failure" && return 0
        return 1
    fi
    rm -rf "$previous" || warn "Could not remove the previous app at $previous"
    print_backend_status
}

# Installed after runtime.sh and service.sh are sourced, so these replace
# their generic definitions.
app_mode_install_overrides() {
    resolve_bind_host() { echo 127.0.0.1; }
    resolve_remote_address() {
        (cd "$BACKEND_DIR" && "$PYTHON" -m cli remote-address --state-root "${CORSIA_STATE_DIR:-$_corsia_app_state_root}")
    }
    resolve_runtime_identity() { app_mode_runtime_identity; }
    installed_release_label() {
        echo "CORSIA $(app_mode_generation "$_corsia_app_bundle" || echo unknown)"
    }
    service_label() { echo "$APP_SERVICE_LABEL"; }
    service_plist_path() {
        echo "$_corsia_app_bundle/Contents/Library/LaunchAgents/$APP_SERVICE_LABEL.plist"
    }
    service_installed() { [ -f "$(service_plist_path)" ]; }
    service_definition_describes_this_checkout() { service_installed; }
    service_managed() { service_is_loaded; }
    require_owned_service_definition() { app_mode_require_owned_service; }
    service_start() { app_mode_service_start; }
    service_restart() { app_mode_service_restart; }
    service_install() { app_mode_refuse_registration install; }
    service_uninstall() { app_mode_refuse_registration uninstall; }
    prepare_redeploy() { app_mode_stage_update; }
    discard_redeploy_build() { app_mode_discard_staged; }
    ensure_current_frontend_release() { :; }
    restart_current_release() { bounce_server; }
    restart_prepared() { app_mode_restart_prepared; }
}
