# Launchd service — the one durable backend for this machine (#242).
if [ "$(uname -s)" = Linux ]; then
    source "$SCRIPT_DIR/corsia.d/systemd.sh"
    return
fi

LAUNCH_AGENTS_DIR="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
SERVICE_PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SERVICE_EXIT_TIMEOUT_SECONDS=120
SERVICE_PID_EXIT_WAIT_SECONDS=$((SERVICE_EXIT_TIMEOUT_SECONDS + 5))

# One checkout owns one launchd job. The label is this checkout's configured
# name for that job (CORSIA_SERVICE_LABEL from its .env, default
# com.corsia.prod). A plist under that name that describes another checkout
# is not this checkout's service: lifecycle verbs treat it as absent and route
# the direct-process way, while install/uninstall refuse to touch it.
#
# The label goes into a file name and a launchd target, so it must be a launchd
# label: letters, digits, '.', '-', '_'. A request for anything else is refused
# at load time rather than corrected.
case "${CORSIA_SERVICE_LABEL-}" in
    "") ;;
    *[!A-Za-z0-9._-]*)
        fail "CORSIA_SERVICE_LABEL '$CORSIA_SERVICE_LABEL' is not a launchd label (letters, digits, '.', '-', '_')"
        return 1
        ;;
esac
service_label() { echo "${CORSIA_SERVICE_LABEL:-com.corsia.prod}"; }
service_plist_path() { echo "$LAUNCH_AGENTS_DIR/$(service_label).plist"; }
service_domain() { echo "gui/$(id -u)"; }
service_is_loaded() { launchctl print "$(service_domain)/$(service_label)" > /dev/null 2>&1; }

# Two paths name the same directory. Either side may be a symlinked spelling
# (a plist rendered through ~/.corsia, a listener's physical cwd), so compare
# the resolved directories when both exist and the literal strings otherwise.
same_directory() {
    local left="$1" right="$2" left_real right_real
    [ "$left" != "$right" ] || return 0
    left_real="$(cd -P "$left" 2>/dev/null && pwd)" || return 1
    right_real="$(cd -P "$right" 2>/dev/null && pwd)" || return 1
    [ "$left_real" = "$right_real" ]
}

# The installed definition under this checkout's label describes this checkout.
# Deeper identity (port, interpreter, roots, generation) is proved separately
# by require_owned_service_definition before any effect.
service_definition_describes_this_checkout() {
    local plist workdir
    plist="$(service_plist_path)" || return 1
    [ -f "$plist" ] || return 1
    command -v plutil > /dev/null 2>&1 || return 1
    [ "$(service_definition_value "$plist" Label)" = "$(service_label)" ] || return 1
    workdir="$(service_definition_value "$plist" WorkingDirectory)" || return 1
    same_directory "$workdir" "$BACKEND_DIR"
}
service_installed() { service_definition_describes_this_checkout; }
# A foreign definition under this label may also be loaded; that job is not
# managed by this checkout. Only a loaded job whose definition is ours is.
service_managed() { service_installed && service_is_loaded; }

service_loaded_pid() {
    local loaded pid
    loaded="$(launchctl print "$(service_domain)/$(service_label)" 2>/dev/null)" \
        || return 1
    pid="$(printf '%s\n' "$loaded" | awk '/^[[:space:]]*pid = /{print $3; exit}')"
    case "$pid" in
        "") return 0 ;;
        *[!0-9]*) return 1 ;;
    esac
    printf '%s\n' "$pid"
}

# A loaded label proves ownership of the network endpoint only when launchd's
# current numeric PID is one of the processes listening on this checkout's
# configured port. Plist identity alone cannot authorize draining a foreign
# listener when the owned job failed to bind.
require_loaded_service_owns_port() {
    local loaded_pid pids
    if ! loaded_pid="$(service_loaded_pid)"; then
        fail "Launchd service $(service_label) is loaded, but its PID cannot be determined; cannot prove which process belongs to it"
        return 1
    fi
    case "$loaded_pid" in
        ""|*[!0-9]*)
            fail "Launchd service $(service_label) is loaded, but its PID cannot be determined; cannot prove which process belongs to it"
            return 1
            ;;
    esac
    pids="$(backend_pids)"
    if ! printf '%s\n' "$pids" | grep -qx -- "$loaded_pid"; then
        fail "Launchd service $(service_label) is loaded, but PID $loaded_pid does not own the backend listener on :$BACKEND_PORT"
        return 1
    fi
}

launchctl_bootout_wait() {
    "$PYTHON" -c '
import subprocess
import sys

try:
    completed = subprocess.run(
        ["launchctl", "bootout", "--wait", sys.argv[1]],
        check=False,
        timeout=int(sys.argv[2]),
    )
except subprocess.TimeoutExpired:
    raise SystemExit(124)
raise SystemExit(completed.returncode)
' "$1" "$SERVICE_PID_EXIT_WAIT_SECONDS"
}

bootout_service_and_wait() {
    local domain="$1" label="$2" bootout_status
    # launchd owns KeepAlive and termination; Python only bounds the native wait.
    if launchctl_bootout_wait "$domain/$label"; then
        return 0
    else
        bootout_status=$?
    fi
    if [ "$bootout_status" -eq 124 ]; then
        fail "$label bootout did not complete after ${SERVICE_PID_EXIT_WAIT_SECONDS}s; no replacement was started"
        return 2
    fi
    if service_is_loaded; then
        return 1
    fi
    fail "$label bootout did not confirm teardown completed; launchd no longer reports the job and no replacement was started"
    return 2
}

new_service_generation() {
    printf '%s-%s-%s' "$(date -u +%Y%m%dT%H%M%SZ)" "$$" "${RANDOM:-0}"
}

service_loaded_definition_matches() {
    local plist expected loaded
    plist="$(service_plist_path)" || return 1
    command -v plutil > /dev/null 2>&1 || return 1
    expected="$(plutil -extract EnvironmentVariables.CORSIA_SERVICE_GENERATION raw "$plist" 2>/dev/null)" \
        || return 1
    [ -n "$expected" ] || return 1
    loaded="$(launchctl print "$(service_domain)/$(service_label)" 2>/dev/null)" \
        || return 1
    printf '%s\n' "$loaded" \
        | grep -F -- "CORSIA_SERVICE_GENERATION => $expected" > /dev/null
}

install_preserved_service_definition() {
    local previous_plist="$1" plist="$2" staged
    staged="$(mktemp "${plist}.restore.XXXXXX")" || return 1
    if ! cp -p "$previous_plist" "$staged"; then
        rm -f "$staged"
        return 1
    fi
    if ! mv -f "$staged" "$plist"; then
        rm -f "$staged"
        return 1
    fi
}

preserve_unquiesced_service_state() {
    local previous_plist="$1" plist="$2" label="$3" drain_attempted="$4"
    local already_restored="${5:-false}" ingress_state
    # "held" rather than "drained": the hold is retained whenever a drain was
    # attempted, including one that timed out and force-stopped past unyielding
    # work. Reporting that as drained would overstate what actually finished.
    if [ "$drain_attempted" = "true" ]; then
        ingress_state="ingress remains held"
    else
        ingress_state="restart was forced without an ingress drain"
    fi
    if [ "$already_restored" = "true" ]; then
        rm -f "$previous_plist"
    elif install_preserved_service_definition "$previous_plist" "$plist"; then
        rm -f "$previous_plist"
    else
        fail "$label restart stopped because old-process teardown was not confirmed; $ingress_state and no service was bootstrapped. Preserved recovery copy: $previous_plist"
        return 1
    fi
    fail "$label restart stopped because old-process teardown was not confirmed; the prior plist is restored on disk, $ingress_state, and no service was bootstrapped"
    return 1
}

restore_previous_service_definition() {
    local previous_plist="$1" domain="$2" label="$3" plist="$4" drain_attempted="$5"
    local bootout_status restore_error restore_status
    if ! install_preserved_service_definition "$previous_plist" "$plist"; then
        fail "Production unavailable: could not atomically reinstall the prior $label service definition; preserved recovery copy: $previous_plist"
        return 1
    fi
    if service_loaded_definition_matches; then
        if wait_for_backend_health; then
            warn "$label replacement failed; the prior service definition remained loaded and is healthy"
            return 0
        fi
        fail "Production unavailable: the prior $label service definition is loaded but unhealthy"
        return 1
    fi
    if service_is_loaded; then
        if bootout_service_and_wait "$domain" "$label"; then
            :
        else
            bootout_status=$?
            if [ "$bootout_status" -eq 2 ]; then
                preserve_unquiesced_service_state \
                    "$previous_plist" "$plist" "$label" "$drain_attempted" true || :
                return 2
            fi
            fail "Production unavailable: could not quiesce the failed $label replacement before restoring the prior definition"
            return 1
        fi
    fi
    if restore_error="$(launchctl bootstrap "$domain" "$plist" 2>&1)"; then
        restore_status=0
    else
        restore_status=$?
        if [ "$restore_status" -ne 5 ] || ! service_loaded_definition_matches; then
            [ -n "$restore_error" ] || restore_error="(no error output)"
            fail "Production unavailable: the replacement failed and the prior $label service definition could not be restored (exit $restore_status): $restore_error"
            return 1
        fi
        warn "$label restore bootstrap returned exit 5, but launchd confirms the prior definition is loaded"
    fi
    if ! wait_for_backend_health; then
        fail "Production unavailable: the prior $label service definition was restored but did not become healthy"
        return 1
    fi
    warn "$label replacement failed; restored the prior service definition and proved backend health"
}

restored_service_restart_result() {
    local after_teardown="$1" label="$2"
    if [ -n "$after_teardown" ]; then
        fail "$label prior service definition was restored after release activation; update did not complete"
        return 1
    fi
}

service_definition_value() {
    plutil -extract "$2" raw "$1" 2>/dev/null
}

# The guidance for a loaded job under this label whose definition is not this
# checkout's: the operator either recovers their own job or names a label.
foreign_loaded_service_guidance() {
    local label="$1" plist="$2"
    if [ -f "$plist" ]; then
        printf '%s' "$label is loaded from a definition for another checkout ($plist). If this checkout should run its own service, set CORSIA_SERVICE_LABEL in its .env; inspect the loaded job with: launchctl print $(service_domain)/$label"
    else
        printf '%s' "Loaded $label has no installed definition at $plist; refusing to replace an unverified job. Inspect it with: launchctl print $(service_domain)/$label"
    fi
}

# The way out of a definition installed for a different backend port than the
# one requested: move the service to the requested port, or keep the old one.
# Pure text; the ownership guard stays what refuses.
service_port_mismatch_guidance() {
    local label="$1" installed="$2" requested="$3" stop_env="" keep="" then_step
    if [ -n "$installed" ]; then
        stop_env="BACKEND_PORT=$installed "
        keep=" To keep port $installed instead, set BACKEND_PORT=$installed in $ENV_FILE."
    fi
    # The default port is what install picks when nothing is set; any other
    # requested port has to be named again on the install and start commands.
    if [ -n "${DEFAULT_BACKEND_PORT:-}" ] && [ "$requested" = "$DEFAULT_BACKEND_PORT" ]; then
        then_step="with BACKEND_PORT and CORSIA_RESTART_BACKEND_PORT unset and not set in $ENV_FILE: corsia service install; corsia service start"
    else
        then_step="with CORSIA_RESTART_BACKEND_PORT unset: BACKEND_PORT=$requested corsia service install; BACKEND_PORT=$requested corsia service start"
    fi
    printf '%s' "Installed $label service definition targets backend port ${installed:-unknown}, not request-bound port $requested. To move the service to port $requested, run: ${stop_env}corsia service stop; ${stop_env}corsia service uninstall; then, $then_step.$keep"
}

require_owned_service_definition() {
    local plist label actual
    plist="$(service_plist_path)" || return 1
    label="$(service_label)"
    [ -f "$plist" ] || { fail "$label not installed. Run: corsia service install"; return 1; }
    command -v plutil > /dev/null 2>&1 || {
        fail "Cannot verify the installed $label service definition without plutil"
        return 1
    }

    actual="$(service_definition_value "$plist" Label)" || actual=""
    [ "$actual" = "$label" ] || {
        fail "Installed service definition label '$actual' does not match $label"
        return 1
    }
    actual="$(service_definition_value "$plist" WorkingDirectory)" || actual=""
    same_directory "${actual:-/nonexistent}" "$BACKEND_DIR" || {
        fail "Installed $label service definition does not describe this checkout: expected $BACKEND_DIR, found ${actual:-missing WorkingDirectory}"
        return 1
    }
    actual="$(service_definition_value "$plist" EnvironmentVariables.BACKEND_PORT)" || actual=""
    [ "$actual" = "$BACKEND_PORT" ] || {
        fail "$(service_port_mismatch_guidance "$label" "$actual" "$BACKEND_PORT")"
        return 1
    }
    actual="$(service_definition_value "$plist" ProgramArguments.0)" || actual=""
    [ "$actual" = "$PYTHON" ] || {
        fail "Installed $label service definition uses a different CORSIA interpreter"
        return 1
    }

    local installed_workspace installed_state
    installed_workspace="$(
        service_definition_value "$plist" EnvironmentVariables.CORSIA_WORKSPACE
    )" || installed_workspace=""
    installed_state="$(
        service_definition_value "$plist" EnvironmentVariables.CORSIA_STATE_DIR
    )" || installed_state=""
    if [ -z "$installed_workspace" ] && [ -z "$installed_state" ]; then
        : # The immediately preceding plist shape; restart upgrades it atomically.
    elif [ -z "$installed_workspace" ] || [ -z "$installed_state" ]; then
        fail "Installed $label service definition partially pins CORSIA runtime roots"
        return 1
    elif [ "$installed_workspace" != "$CORSIA_WORKSPACE" ]; then
        fail "Installed $label service definition uses a different CORSIA_WORKSPACE"
        return 1
    elif [ "$installed_state" != "$CORSIA_STATE_DIR" ]; then
        fail "Installed $label service definition uses a different CORSIA_STATE_DIR"
        return 1
    fi
    if service_is_loaded && ! service_loaded_definition_matches; then
        fail "Loaded $label generation does not match its installed service definition"
        return 1
    fi
}

xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

render_env_file_vars() {
    [ -f "$ENV_FILE" ] || return 0
    local line key val
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in \#*|"") continue ;; esac
        line="${line#export }"
        key="${line%%=*}"
        val="${line#*=}"
        case "$key" in
            *[!A-Za-z0-9_]*|[0-9]*|"") continue ;;
        esac
        case "$val" in
            \"*\") val="${val%\"}"; val="${val#\"}" ;;
            \'*\') val="${val%\'}"; val="${val#\'}" ;;
        esac
        case "$key" in
            CORSIA_WORKSPACE|CORSIA_STATE_DIR)
                warn "$ENV_FILE sets $key — $key is resolved by infra.config and rendered separately" >&2
                continue ;;
            CORSIA_TEST_MODE|CORSIA_ALLOW_HOME_WORKSPACE_IN_CI|CORSIA_DEVICES_DISABLE)
                warn "$ENV_FILE sets $key — ignored: test-only safety bypasses cannot configure the production service" >&2
                continue ;;
            BACKEND_PORT|CORSIA_E2E_MODEL|CORSIA_SERVICE_GENERATION|\
                CORSIA_RESTART_BACKEND_PORT|CORSIA_RESTART_OPERATION_ID|\
                CORSIA_RESTART_CALLER_TOKEN|CORSIA_RESTART_CURL|PATH)
                warn "$ENV_FILE sets $key — ignored: pinned by the canonical service plist" >&2
                continue ;;
        esac
        printf '        <key>%s</key>\n        <string>%s</string>\n' \
            "$(printf '%s' "$key" | xml_escape)" "$(printf '%s' "$val" | xml_escape)"
    done < "$ENV_FILE"
}

render_service_identity_vars() {
    local key value
    for key in CORSIA_WORKSPACE CORSIA_STATE_DIR; do
        value="${!key:-}"
        [ -n "$value" ] || continue
        printf '        <key>%s</key>\n        <string>%s</string>\n' \
            "$key" "$(printf '%s' "$value" | xml_escape)"
    done
}

# The plist is the durable definition of the production service. Render only
# from a fully resolved environment: a partially resolved one yields a plist
# launchd accepts and cannot start, silently replacing a working definition.
require_service_plist_inputs() {
    local port="$1" appdir="$2" log="$3" generation="$4"
    [ -n "$PYTHON" ] || { fail "Service plist needs an interpreter (PYTHON)"; return 1; }
    [ -n "$appdir" ] || { fail "Service plist needs a backend directory (BACKEND_DIR)"; return 1; }
    [ -n "$log" ] || { fail "Service plist needs a log path (BACKEND_LOG)"; return 1; }
    [ -n "$generation" ] || { fail "Service plist needs a generation stamp"; return 1; }
    [ -n "${CORSIA_WORKSPACE:-}" ] || { fail "Service plist needs a canonical workspace root"; return 1; }
    [ -n "${CORSIA_STATE_DIR:-}" ] || { fail "Service plist needs a canonical state root"; return 1; }
    case "$port" in
        ""|*[!0-9]*) fail "Service plist needs a numeric backend port (BACKEND_PORT): '$port'"; return 1 ;;
    esac
}

render_service_plist() {
    local generation="$1" label port bind appdir log
    port="$BACKEND_PORT"; appdir="$BACKEND_DIR"
    log="$BACKEND_LOG"
    require_service_plist_inputs "$port" "$appdir" "$log" "$generation" || return 1
    # Baked in at render time; a refusal here leaves the loaded job untouched.
    bind="$(resolve_bind_host)" || return 1
    label="$(service_label)"
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>main:app</string>
        <string>--host</string>
        <string>$bind</string>
        <string>--port</string>
        <string>$port</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$appdir</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CORSIA_SERVICE_GENERATION</key>
        <string>$generation</string>
        <key>BACKEND_PORT</key>
        <string>$port</string>
        <key>PATH</key>
        <string>$SERVICE_PATH</string>
$(render_service_identity_vars)
$(render_env_file_vars)
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>ExitTimeOut</key>
    <integer>$SERVICE_EXIT_TIMEOUT_SECONDS</integer>
    <key>StandardOutPath</key>
    <string>$log</string>
    <key>StandardErrorPath</key>
    <string>$log</string>
</dict>
</plist>
EOF
}

service_install() {
    local mode="${1:-direct}" plist tmp generation
    case "$mode" in
        direct|restart) ;;
        *) fail "Unknown service installation mode: $mode"; return 1 ;;
    esac
    plist="$(service_plist_path)" || return 1
    if service_is_loaded; then
        if ! service_installed; then
            fail "$(foreign_loaded_service_guidance "$(service_label)" "$plist")"
            return 1
        fi
        if ! require_owned_service_definition; then
            fail "Refusing to replace unowned definition at $plist; inspect the loaded job and reconcile that file manually"
            return 1
        fi
        if [ "$mode" != restart ]; then
            fail "$(service_label) is loaded; use corsia restart so its definition and running generation change atomically"
            return 1
        fi
    elif [ -f "$plist" ] && ! require_owned_service_definition; then
        fail "Refusing to replace unowned definition at $plist; inspect that file and reconcile it manually"
        return 1
    fi
    mkdir -p "$LAUNCH_AGENTS_DIR"
    tmp="$(mktemp "${plist}.tmp.XXXXXX")" \
        || { fail "Could not create temporary service plist for $plist"; return 1; }
    chmod 600 "$tmp" \
        || { rm -f "$tmp"; fail "Could not secure temporary service plist for $plist"; return 1; }
    generation="$(new_service_generation)" \
        || { rm -f "$tmp"; fail "Could not create service generation"; return 1; }
    if ! render_service_plist "$generation" > "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    if ! command -v plutil > /dev/null 2>&1; then
        rm -f "$tmp"
        fail "Cannot validate the rendered service definition without plutil"
        return 1
    fi
    if ! plutil -lint -s "$tmp"; then
        rm -f "$tmp"
        fail "Rendered plist failed plutil lint: $plist"
        return 1
    fi
    if ! mv -f "$tmp" "$plist"; then
        rm -f "$tmp"
        fail "Could not install service plist: $plist"
        return 1
    fi
    ok "Installed $(service_label) → $plist"
    [ "$mode" != direct ] || echo "  Load it with: corsia service start"
}

service_uninstall() {
    local plist loaded=false
    plist="$(service_plist_path)" || return 1
    if service_is_loaded; then
        loaded=true
        [ -f "$plist" ] || {
            fail "Loaded $(service_label) has no installed definition at $plist; refusing to boot out an unverified job"
            return 1
        }
    fi
    if [ -f "$plist" ] && ! require_owned_service_definition; then
        fail "Refusing to remove unowned definition at $plist; inspect the loaded job and reconcile that file manually"
        return 1
    fi
    if [ "$loaded" = true ]; then
        if ! launchctl bootout "$(service_domain)/$(service_label)"; then
            fail "Could not boot out $(service_label); installed definition was preserved"
            return 1
        fi
        ok "Booted out $(service_label)"
    fi
    rm -f "$plist"
    ok "Removed $plist (instance reverts to the legacy nohup lifecycle)"
}

require_no_unmanaged_backend() {
    local port="${1:-$BACKEND_PORT}"
    if is_backend_running || [ -n "$(backend_pids)" ]; then
        fail "Installed service is not loaded, but a backend still owns port $port; refusing to race it"
        return 1
    fi
}

service_start() {
    if service_is_loaded && ! service_installed; then
        fail "$(foreign_loaded_service_guidance "$(service_label)" "$(service_plist_path)")"
        return 1
    fi
    service_installed || { fail "$(service_label) not installed. Run: corsia service install"; return 1; }
    require_owned_service_definition || return 1
    if service_is_loaded; then
        ok "$(service_label) already loaded"
        return 0
    fi
    require_no_unmanaged_backend "$BACKEND_PORT" || return 1
    launchctl bootstrap "$(service_domain)" "$(service_plist_path)" \
        && ok "$(service_label) bootstrapped using the installed definition (RunAtLoad starts it). Source changes are not rendered by start; run corsia service install before start, or corsia service restart."
}

stop_local_model_before_bootout() {
    local port="$1"
    curl -sf --max-time 60 -X POST "http://127.0.0.1:$port/api/system/stop-local-model" \
        -H "Content-Type: application/json" -d '{}' > /dev/null 2>&1
}

service_stop() {
    local port="$BACKEND_PORT" drain_attempted=false plist label domain bootout_status
    if ! service_is_loaded; then
        warn "$(service_label) not loaded"
        return 0
    fi
    plist="$(service_plist_path)" || return 1
    label="$(service_label)"
    domain="$(service_domain)"
    if ! service_installed; then
        fail "$(foreign_loaded_service_guidance "$label" "$plist")"
        return 1
    fi
    require_owned_service_definition || return 1
    if [ "$FORCE" = "false" ] \
        && { is_backend_running || [ -n "$(backend_pids)" ]; }; then
        require_loaded_service_owns_port || return 1
        # Draining is a courtesy with a bound; stopping is the guarantee. An
        # incomplete drain used to abort here, which left the operator with a
        # running service and no way to stop it. Bootout below is bounded and
        # launchd-owned, so it is the terminal step either way. The hold is
        # kept so nothing is readmitted in the gap before teardown.
        drain_attempted=true
        DRAIN_FAILURE_ACTION=hold drain_and_wait "service stop" "$port" \
            || warn "Drain did not complete; stopping anyway"
        # Opt-in (--stop-local-model): stop the local model CORSIA started only
        # once in-flight turns have finished, while the service can still
        # answer. A failure here aborts with the service up and accepting turns.
        if [ "${STOP_LOCAL_MODEL:-false}" = true ] && ! stop_local_model_before_bootout "$port"; then
            cancel_failed_drain "$(current_drain_url "$port")" "$CORSIA_RESTART_OPERATION_ID" || :
            fail "Could not stop the local model; $label is still running"
            return 1
        fi
    elif [ "${STOP_LOCAL_MODEL:-false}" = true ]; then
        warn "--stop-local-model skipped: the backend is not answering"
    fi
    if bootout_service_and_wait "$domain" "$label"; then
        :
    else
        bootout_status=$?
        # Teardown failed, so the old backend is still serving and no restart
        # is coming to settle the operation at startup. Release whenever a
        # drain was attempted, not only when it completed: a held-but-failed
        # drain is exactly the case that would otherwise freeze ingress for
        # the life of that process.
        if [ "$drain_attempted" = "true" ]; then
            cancel_failed_drain \
                "$(current_drain_url "$port")" "$CORSIA_RESTART_OPERATION_ID" \
                || :
        fi
        fail "$label stop failed (bounded bootout exit $bootout_status)"
        return 1
    fi
    ok "$label stopped (booted out; installed definition preserved). To refresh source changes, run corsia service install before the next start, or corsia service restart."
}

service_restart() {
    local after_teardown="${1:-}" port="$BACKEND_PORT" drain_attempted=false
    local domain label plist previous_plist
    local bootstrap_attempt bootstrap_status bootstrap_error bootstrap_attempts
    local attempt_word bootout_status manual_command restore_status
    service_installed || { fail "$(service_label) not installed. Run: corsia service install"; return 1; }
    require_owned_service_definition || return 1
    if ! service_is_loaded; then
        require_no_unmanaged_backend "$port" || return 1
        if [ -n "$after_teardown" ] && ! "$after_teardown"; then
            fail "Release activation failed before service definition installation"
            return 1
        fi
        service_install restart || return 1
        service_start
        return $?
    fi
    if is_backend_running || [ -n "$(backend_pids)" ]; then
        require_loaded_service_owns_port || return 1
    fi
    domain="$(service_domain)"
    label="$(service_label)"
    plist="$(service_plist_path)"
    bootstrap_attempts="${CORSIA_LAUNCHD_BOOTSTRAP_ATTEMPTS:-15}"
    case "$bootstrap_attempts" in
        ""|*[!0-9]*|0)
            fail "Invalid launchd bootstrap attempt bound: $bootstrap_attempts"
            return 1
            ;;
    esac
    previous_plist="$(mktemp "${plist}.previous.XXXXXX")" \
        || { fail "Could not preserve the installed $label service definition"; return 1; }
    if ! cp -p "$plist" "$previous_plist"; then
        rm -f "$previous_plist"
        fail "Could not preserve the installed $label service definition"
        return 1
    fi
    if ! service_install restart; then
        rm -f "$previous_plist"
        return 1
    fi
    if [ "$FORCE" = "false" ] \
        && { is_backend_running || [ -n "$(backend_pids)" ]; }; then
        # Same guarantee as stop: the drain is bounded courtesy, the bounce is
        # the promise. Aborting here left the old generation serving with no way
        # to replace it. Teardown below is bounded and launchd-owned, and the
        # hold is kept so nothing is readmitted before it.
        drain_attempted=true
        DRAIN_FAILURE_ACTION=hold drain_and_wait "service restart" "$port" \
            || warn "Drain did not complete; restarting anyway"
    fi
    if bootout_service_and_wait "$domain" "$label"; then
        :
    else
        bootout_status=$?
        if [ "$bootout_status" -eq 2 ]; then
            # Teardown is unconfirmed, so the old backend may already be dying.
            # Readmitting work to it is the worse risk: keep the hold and let
            # the next startup settle the operation.
            preserve_unquiesced_service_state \
                "$previous_plist" "$plist" "$label" "$drain_attempted" || :
            return 1
        fi
        if ! install_preserved_service_definition "$previous_plist" "$plist"; then
            if [ "$drain_attempted" = "true" ]; then
                cancel_failed_drain \
                    "$(current_drain_url "$port")" "$CORSIA_RESTART_OPERATION_ID" \
                    || :
            fi
            fail "Production service definition is inconsistent: the old $label job is still loaded, but its installed plist could not be restored atomically. Preserved recovery copy: $previous_plist"
            return 1
        fi
        rm -f "$previous_plist"
        if [ "$drain_attempted" = "true" ]; then
            cancel_failed_drain \
                "$(current_drain_url "$port")" "$CORSIA_RESTART_OPERATION_ID" \
                || :
        fi
        fail "$label restart failed before the old service could be quiesced"
        return 1
    fi
    if [ -n "$after_teardown" ] && ! "$after_teardown"; then
        fail "$label restart stopped after confirmed teardown because release activation failed; no service was bootstrapped. Preserved prior definition: $previous_plist"
        return 1
    fi
    bootstrap_attempt=0
    while [ "$bootstrap_attempt" -lt "$bootstrap_attempts" ]; do
        bootstrap_attempt=$((bootstrap_attempt + 1))
        if bootstrap_error="$(launchctl bootstrap "$domain" "$plist" 2>&1)"; then
            rm -f "$previous_plist"
            ok "$label restarted with the installed plist"
            return 0
        else
            bootstrap_status=$?
        fi
        if [ "$bootstrap_status" -eq 5 ]; then
            if service_loaded_definition_matches; then
                rm -f "$previous_plist"
                warn "$label bootstrap returned exit 5, but launchd confirms the exact replacement generation is loaded"
                return 0
            fi
            if service_is_loaded; then
                warn "$label still has a stale launchd definition loaded; booting it out before retry"
                if bootout_service_and_wait "$domain" "$label"; then
                    :
                else
                    bootout_status=$?
                    if [ "$bootout_status" -eq 2 ]; then
                        preserve_unquiesced_service_state \
                            "$previous_plist" "$plist" "$label" "$drain_attempted" || :
                        return 1
                    fi
                    warn "$label restart could not quiesce the stale launchd generation after bootstrap exit 5; restoring the prior definition"
                    if restore_previous_service_definition \
                        "$previous_plist" "$domain" "$label" "$plist" "$drain_attempted"
                    then
                        rm -f "$previous_plist"
                        restored_service_restart_result "$after_teardown" "$label"
                        return $?
                    fi
                    return 1
                fi
            fi
        fi
        if [ "$bootstrap_status" -ne 5 ] || [ "$bootstrap_attempt" -eq "$bootstrap_attempts" ]; then
            attempt_word=attempt
            [ "$bootstrap_attempt" -eq 1 ] || attempt_word=attempts
            [ -n "$bootstrap_error" ] || bootstrap_error="(no error output)"
            printf -v manual_command 'launchctl bootstrap %q %q' "$domain" "$plist"
            warn "$label replacement failed after $bootstrap_attempt bootstrap $attempt_word; restoring the prior service definition"
            if restore_previous_service_definition \
                "$previous_plist" "$domain" "$label" "$plist" "$drain_attempted"
            then
                rm -f "$previous_plist"
                restored_service_restart_result "$after_teardown" "$label"
                return $?
            else
                restore_status=$?
            fi
            [ "$restore_status" -ne 2 ] || return 1
            fail "$label restart failed while loading the installed plist after $bootstrap_attempt bootstrap $attempt_word \
(domain: $domain; plist: $plist). Final launchctl error (exit $bootstrap_status): $bootstrap_error. \
Prior service restoration also failed; production is unavailable. Preserved recovery copy: $previous_plist. \
Manual recovery: $manual_command"
            return 1
        fi
        warn "$label is still being released by launchd; retrying bootstrap ($bootstrap_attempt/$bootstrap_attempts)"
        sleep 1
    done
}

service_status() {
    local label plist pid port="$BACKEND_PORT"
    label="$(service_label)"
    plist="$(service_plist_path)"
    echo "$label"
    if [ -f "$plist" ]; then ok "plist installed"; else warn "plist not installed ($plist)"; fi
    if service_is_loaded; then
        pid="$(service_loaded_pid)" || pid=""
        ok "loaded (pid ${pid:-unknown})"
    else
        warn "not loaded"
    fi
    if curl -sf --max-time 3 "http://127.0.0.1:$port/api/health" > /dev/null 2>&1; then
        ok "healthy on :$port"
    else
        warn "no health response on :$port"
    fi
}
