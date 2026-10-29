# Linux systemd user-service lifecycle. The caller resolves runtime identity first.
CORSIA_SERVICE_MANAGER=systemd
SERVICE_PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

case "${CORSIA_SERVICE_LABEL-}" in
    "") ;;
    *[!A-Za-z0-9._-]*) fail "Couldn’t use service label $CORSIA_SERVICE_LABEL; use letters, digits, periods, hyphens or underscores"; return 1 ;;
esac
service_label() { echo "${CORSIA_SERVICE_LABEL:-com.corsia.prod}"; }
service_definition_path() { echo "$HOME/.config/systemd/user/$(service_label).service"; }
service_is_loaded() { systemctl --user is-active --quiet "$(service_label).service"; }
service_loaded_pid() { systemctl --user show "$(service_label).service" -p MainPID --value; }
service_installed() {
    local unit
    unit="$(service_definition_path)"
    [ -f "$unit" ] && grep -Fxq -- "# CORSIA checkout: $BACKEND_DIR" "$unit"
}
service_managed() { service_installed && service_is_loaded; }
foreign_loaded_service_guidance() { echo "Couldn’t use the systemd service $1 for this checkout; inspect $2 before continuing."; }

render_systemd_unit() {
    local bind
    bind="$(resolve_bind_host)" || return 1
    "$PYTHON" - "$BACKEND_DIR" "$PYTHON" "$ENV_FILE" "$bind" "$BACKEND_PORT" \
        "$CORSIA_WORKSPACE" "$CORSIA_STATE_DIR" "$SERVICE_PATH" "$SCRIPT_DIR/corsia.d/systemd-server.sh" "$BACKEND_LOG" <<'PY'
import sys
backend, python, env_file, bind, port, workspace, state, path, wrapper, log = sys.argv[1:]
values = (backend, python, env_file, bind, port, workspace, state, path, wrapper, log)
if any(not value or "\n" in value or "\r" in value for value in values):
    raise SystemExit("Invalid or missing systemd service identity")
def quote(value, *, argument=False):
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if argument:
        value = value.replace('$', '$$')
    return '"' + value + '"'
command = ' '.join(quote(value, argument=True) for value in ('/bin/bash', wrapper, *values[:8]))
print(f'''# CORSIA checkout: {backend}
[Unit]
Description=CORSIA backend
After=network.target

[Service]
Type=simple
WorkingDirectory={backend.replace('%', '%%')}
Environment=XDG_RUNTIME_DIR=%t
ExecStart={command}
Restart=on-failure
RestartSec=5s
TimeoutStopSec=120s
StandardOutput=append:{log.replace('%', '%%')}
StandardError=append:{log.replace('%', '%%')}

[Install]
WantedBy=default.target''')
PY
}

require_owned_service_definition() {
    local unit expected
    unit="$(service_definition_path)"
    service_installed || { fail "$(foreign_loaded_service_guidance "$(service_label)" "$unit")"; return 1; }
    expected="$(render_systemd_unit)" || return 1
    [ "$(cat "$unit")" = "$expected" ] || {
        fail "The installed systemd unit does not match this checkout's runtime identity; inspect $unit before continuing"
        return 1
    }
}
# Check the manager's selected fragment, drop-ins and cached state before acting
# on a unit by name. A same-named higher-priority unit is not ours.
require_manager_service_definition() {
    local fragment dropins stale unit
    unit="$(service_definition_path)"
    fragment="$(systemctl --user show "$(service_label).service" -p FragmentPath --value)" || return 1
    dropins="$(systemctl --user show "$(service_label).service" -p DropInPaths --value)" || return 1
    stale="$(systemctl --user show "$(service_label).service" -p NeedDaemonReload --value)" || return 1
    if [ "$fragment" != "$unit" ] || [ -n "$dropins" ] || [ "$stale" != no ]; then
        fail "The systemd manager's loaded definition does not match $unit; inspect it before continuing"
        return 1
    fi
}
require_loaded_service_owns_port() {
    local pid pids
    pid="$(service_loaded_pid)" || return 1
    case "$pid" in ''|0|*[!0-9]*) fail "Couldn’t determine the systemd service PID; check the service status and try again"; return 1 ;; esac
    pids="$(backend_pids)"
    printf '%s\n' "$pids" | grep -qx -- "$pid" || {
        fail "The systemd service does not own the backend port :$BACKEND_PORT; inspect the process using that port before continuing"
        return 1
    }
}

service_install() {
    local unit temp
    unit="$(service_definition_path)"
    if [ -e "$unit" ] && ! service_installed; then
        fail "$(foreign_loaded_service_guidance "$(service_label)" "$unit")"
        return 1
    fi
    if service_is_loaded; then
        require_owned_service_definition || return 1
        require_manager_service_definition || return 1
        systemctl --user daemon-reload || return 1
        require_manager_service_definition || return 1
        systemctl --user enable "$(service_label).service" || return 1
        return 0
    fi
    if is_backend_running || [ -n "$(backend_pids)" ]; then
        fail "A backend already owns :$BACKEND_PORT; stop it before installing the service"
        return 1
    fi
    mkdir -p "$(dirname "$unit")" "$(dirname "$BACKEND_LOG")"
    temp="$(mktemp "${unit}.XXXXXX")" || return 1
    if ! render_systemd_unit > "$temp"; then rm -f "$temp"; return 1; fi
    chmod 600 "$temp"
    mv -f "$temp" "$unit"
    systemctl --user daemon-reload || return 1
    require_manager_service_definition || return 1
    systemctl --user enable "$(service_label).service" || return 1
    ok "Installed $(service_label) as a systemd user service"
}
service_start() {
    require_owned_service_definition || return 1
    if service_is_loaded; then
        require_manager_service_definition || return 1
    fi
    # Reconcile both operations on every start, including after an interrupted install.
    systemctl --user daemon-reload || return 1
    require_manager_service_definition || return 1
    systemctl --user enable "$(service_label).service" || return 1
    if service_is_loaded; then return 0; fi
    if is_backend_running || [ -n "$(backend_pids)" ]; then
        fail "A backend already owns :$BACKEND_PORT; stop it before starting the service"
        return 1
    fi
    systemctl --user start "$(service_label).service"
}
service_stop() {
    local drain_attempted=false
    service_installed || { fail "$(foreign_loaded_service_guidance "$(service_label)" "$(service_definition_path)")"; return 1; }
    if ! service_is_loaded; then
        [ "${STOP_LOCAL_MODEL:-false}" != true ] || echo "No local model runs on Linux; only the server is stopped."
        return 0
    fi
    require_manager_service_definition || return 1
    local pid
    pid="$(service_loaded_pid)" || return 1
    case "$pid" in ''|0|*[!0-9]*) fail "Couldn’t determine the systemd service PID; check the service status and try again"; return 1 ;; esac
    backend_pid_serves_this_checkout "$pid" || {
        fail "$(foreign_loaded_service_guidance "$(service_label)" "$(service_definition_path)")"
        return 1
    }
    if [ "$FORCE" = false ] && is_backend_running; then
        require_loaded_service_owns_port || return 1
        drain_attempted=true
        DRAIN_FAILURE_ACTION=hold drain_and_wait "service stop" "$BACKEND_PORT" || warn "Drain did not complete; stopping anyway"
    fi
    _corsia_teardown_possible=true
    if ! systemctl --user stop "$(service_label).service"; then
        [ "$drain_attempted" = false ] || cancel_failed_drain "$(current_drain_url "$BACKEND_PORT")" "$CORSIA_RESTART_OPERATION_ID" || :
        fail "Couldn’t stop the systemd service; check its status and try again"
        return 1
    fi
    ok "Stopped $(service_label)"
    [ "${STOP_LOCAL_MODEL:-false}" != true ] || echo "No local model runs on Linux; only the server is stopped."
}
service_restart() {
    local after_teardown="${1:-}"
    service_stop || return 1
    [ -z "$after_teardown" ] || "$after_teardown" || return 1
    service_install || return 1
    service_start
}
service_uninstall() {
    local unit
    unit="$(service_definition_path)"
    service_installed || { fail "$(foreign_loaded_service_guidance "$(service_label)" "$unit")"; return 1; }
    service_stop || return 1
    require_manager_service_definition || return 1
    systemctl --user disable "$(service_label).service" || return 1
    rm -f "$unit"
    systemctl --user daemon-reload
}
service_status() {
    systemctl --user status "$(service_label).service" --no-pager
}
