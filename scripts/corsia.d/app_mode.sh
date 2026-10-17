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
#   config source  service.json in the app folder (workspace, port); never a
#                  .env or the machine bootstrap a source install reads
#   state root     the backend's default_state_root() for this bundle
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
    local resolved line
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
            workspace=*)
                [ -n "${CORSIA_WORKSPACE:-}" ] \
                    || export CORSIA_WORKSPACE="${line#workspace=}"
                ;;
            port=*) [ -n "${BACKEND_PORT:-}" ] || BACKEND_PORT="${line#port=}" ;;
        esac
    done <<< "$resolved"
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

app_mode_service_start() {
    require_owned_service_definition || return 1
    if service_is_loaded; then
        ok "$(service_label) already running"
        return 0
    fi
    require_no_unmanaged_backend "$BACKEND_PORT" || return 1
    if ! launchctl bootstrap "$(service_domain)" "$(service_plist_path)"; then
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
    local staged previous
    staged="$(app_mode_staged_path)"
    previous="$(app_mode_previous_path)"
    if [ -d "$staged" ]; then
        app_mode_swap_staged_update "$staged"
    elif [ -d "$previous" ]; then
        app_mode_start_swapped_update "$previous"
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
            launchctl bootstrap "$(service_domain)" "$(service_plist_path)" || :
        fi
        fail "Update stopped: CORSIA did not stop, so the staged update was not installed"
        return 1
    fi
    # The exchange is atomic, so the installed path always holds a whole app;
    # a failure leaves this one there, and it is started again.
    if "$_corsia_app_bundle/Contents/MacOS/CORSIA" --update-swap "$operation_id"; then
        :
    else
        swap_status=$?
        fail "The update could not be installed (swap exit $swap_status); starting this version again"
        launchctl bootstrap "$(service_domain)" "$(service_plist_path)" \
            || fail "CORSIA could not be started again; open CORSIA to start it"
        return 1
    fi
    # This shell runs the old app's code from memory; everything after the
    # swap comes from the new app, reached by the installed path.
    handoff_redeploy_restart "$_corsia_app_bundle/Contents/Resources"
}

app_mode_rollback_swapped_update() {
    local previous="$1" reason="$2" operation_id="${CORSIA_RESTART_OPERATION_ID:-update}" previous_generation rollback_status
    previous_generation="$(app_mode_generation "$previous" 2>/dev/null || echo unknown)"
    warn "$reason Rolling back to version $previous_generation."
    if service_is_loaded && ! bootout_service_and_wait "$(service_domain)" "$(service_label)"; then
        fail "$reason CORSIA could not stop the new service, so it was not rolled back. The new app remains installed at $_corsia_app_bundle and the previous app remains at $previous."
        return 1
    fi
    if "$_corsia_app_bundle/Contents/MacOS/CORSIA" --update-rollback "$operation_id"; then
        :
    else
        rollback_status=$?
        fail "$reason CORSIA could not roll back (rollback exit $rollback_status). The new app remains installed at $_corsia_app_bundle and the previous app remains at $previous."
        return 1
    fi
    if ! launchctl bootstrap "$(service_domain)" "$(service_plist_path)"; then
        fail "$reason CORSIA went back to version $previous_generation, but could not start it. The failed update remains at $previous."
        return 1
    fi
    if ! wait_for_backend_health; then
        fail "$reason CORSIA went back to version $previous_generation, but could not confirm it is healthy. The failed update remains at $previous."
        return 1
    fi
    rm -rf "$previous" || warn "Could not remove the failed update at $previous"
    ok "The update didn't start, so CORSIA went back to version $previous_generation"
    print_backend_status
    return 0
}

app_mode_start_swapped_update() {
    local previous="$1" job expected actual
    if service_is_loaded; then
        if ! job="$(launchctl print "$(service_domain)/$(service_label)" 2>/dev/null)" \
            || ! printf '%s\n' "$job" | app_mode_job_is_this_bundle; then
            fail "$(service_label) is running from another copy of CORSIA; the update was installed but not started"
            return 1
        fi
        if ! launchctl kickstart -k "$(service_domain)/$(service_label)"; then
            app_mode_rollback_swapped_update "$previous" "The updated service could not be restarted." || return 1
            return 1
        fi
    else
        require_no_unmanaged_backend "$BACKEND_PORT" || return 1
        if ! launchctl bootstrap "$(service_domain)" "$(service_plist_path)"; then
            app_mode_rollback_swapped_update "$previous" "The updated service could not be started." || return 1
            return 1
        fi
    fi
    if ! wait_for_backend_health; then
        app_mode_rollback_swapped_update "$previous" "The updated service did not become healthy." || return 1
        return 1
    fi
    if ! expected="$(app_mode_generation "$_corsia_app_bundle")"; then
        app_mode_rollback_swapped_update "$previous" "The updated app's version could not be read." || return 1
        return 1
    fi
    if ! actual="$(app_mode_backend_generation)"; then
        app_mode_rollback_swapped_update "$previous" "The updated service did not report its version." || return 1
        return 1
    fi
    if [ "$actual" != "$expected" ]; then
        app_mode_rollback_swapped_update "$previous" "The updated service is version $actual, not $expected." || return 1
        return 1
    fi
    rm -rf "$previous" || warn "Could not remove the previous app at $previous"
    print_backend_status
}

# Installed after runtime.sh and service.sh are sourced, so these replace
# their generic definitions.
app_mode_install_overrides() {
    resolve_bind_host() { echo 127.0.0.1; }
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
