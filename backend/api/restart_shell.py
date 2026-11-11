"""Sanitized detached Bash ownership for backend-acquired restart operations."""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TextIO

_BASH_STARTUP_ENV = frozenset({
    "BASHOPTS",
    "BASH_COMPAT",
    "BASH_ENV",
    "BASH_XTRACEFD",
    "CDPATH",
    "ENV",
    "POSIXLY_CORRECT",
    "PS4",
    "SHELLOPTS",
})
LIFECYCLE_SHELL_COMMAND = r"""
set +x
_corsia_lifecycle_is_update=false
[ "${2:-}" != restart-prepared ] || _corsia_lifecycle_is_update=true
readonly _corsia_lifecycle_is_update
_corsia_lifecycle_done=false
_corsia_update_stopping=
_corsia_unconfirmed_bootout=false
_corsia_lifecycle_cleanup() {
    _corsia_lifecycle_status=$?
    local refusal_reason="${_corsia_lifecycle_refusal_reason:-}"
    trap - EXIT HUP INT TERM
    local backend_healthy=false
    local refusal_line=
    if [ "$_corsia_lifecycle_status" -ne 0 ] && [ "$_corsia_lifecycle_is_update" = true ] \
        && [ -z "${_corsia_update_stopping:-}" ] && [ -n "$refusal_reason" ]; then
        printf 'Update refused: %s\n' "$refusal_reason"
        if declare -F is_backend_running > /dev/null && is_backend_running; then
            backend_healthy=true
        fi
        refusal_line="${refusal_reason:0:500}"
        refusal_line="${refusal_line//$'\n'/ }"
        refusal_line="${refusal_line//$'\r'/ }"
    fi
    # The backend that spawned this lifecycle owns its exit: it reads this
    # record when the child exits and settles the operation once.
    if [ "$_corsia_lifecycle_done" != true ] \
        && [ "$_corsia_lifecycle_status" -ne 0 ] \
        && [ -n "$_corsia_lifecycle_result_file" ]; then
        {
            printf 'status=%s\n' "$_corsia_lifecycle_status"
            printf 'is_update=%s\n' "$_corsia_lifecycle_is_update"
            printf 'unconfirmed_bootout=%s\n' "$_corsia_unconfirmed_bootout"
            printf 'backend_healthy=%s\n' "$backend_healthy"
            printf 'refusal_reason=%s\n' "$refusal_line"
        } > "$_corsia_lifecycle_result_file.tmp" \
            && mv -f "$_corsia_lifecycle_result_file.tmp" "$_corsia_lifecycle_result_file" \
            || printf 'Lifecycle result record not written\n'
    fi
    exit "$_corsia_lifecycle_status"
}
trap _corsia_lifecycle_cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
_corsia_lifecycle_token="${CORSIA_RESTART_CALLER_TOKEN:-}"
_corsia_lifecycle_port="${CORSIA_RESTART_BACKEND_PORT:-}"
_corsia_lifecycle_operation_id="${CORSIA_RESTART_OPERATION_ID:-}"
_corsia_lifecycle_curl="${CORSIA_RESTART_CURL:-}"
_corsia_lifecycle_result_file="${CORSIA_RESTART_RESULT_FILE:-}"
export -n _corsia_lifecycle_token _corsia_lifecycle_port \
    _corsia_lifecycle_operation_id _corsia_lifecycle_curl _corsia_lifecycle_result_file
readonly _corsia_lifecycle_token _corsia_lifecycle_port \
    _corsia_lifecycle_operation_id _corsia_lifecycle_curl _corsia_lifecycle_result_file
unset CORSIA_RESTART_CALLER_TOKEN CORSIA_RESTART_CURL
if ! declare -p _CORSIA_REQUEST_BACKEND_PORT > /dev/null 2>&1; then
    readonly _CORSIA_REQUEST_BACKEND_PORT="$_corsia_lifecycle_port"
fi
if ! declare -p _CORSIA_REQUEST_OPERATION_ID > /dev/null 2>&1; then
    readonly _CORSIA_REQUEST_OPERATION_ID="$_corsia_lifecycle_operation_id"
fi
if ! declare -p _CORSIA_REQUEST_CALLER_TOKEN > /dev/null 2>&1; then
    readonly _CORSIA_REQUEST_CALLER_TOKEN="$_corsia_lifecycle_token"
fi
readonly _CORSIA_LIFECYCLE_ENVELOPE_ACTIVE=true
source "$1" "${@:2}"
_corsia_lifecycle_status=$?
if [ "$_corsia_lifecycle_status" -eq 0 ]; then
    _corsia_lifecycle_done=true
fi
exit "$_corsia_lifecycle_status"
""".strip()


def _lifecycle_launch_env(
    restart_env: dict[str, str],
    operation_id: str,
    caller_credential: str | None,
) -> dict[str, str]:
    """Validate and sanitize the immutable detached-shell launch tuple."""
    port = restart_env.get("CORSIA_RESTART_BACKEND_PORT", "")
    credential = (
        caller_credential
        or restart_env.get("CORSIA_RESTART_CALLER_TOKEN", "")
    )
    curl_path = shutil.which("curl", path=restart_env.get("PATH"))
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("restart operation id must be 32 lowercase hex characters")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("restart backend port must be between 1 and 65535")
    if not credential or "\x00" in credential:
        raise ValueError("restart caller credential is missing or invalid")
    if (
        curl_path is None
        or not Path(curl_path).is_absolute()
        or not Path(curl_path).is_file()
        or not os.access(curl_path, os.X_OK)
    ):
        raise ValueError("restart cleanup requires an absolute curl executable")

    launch_env = {
        name: value
        for name, value in restart_env.items()
        if (
            name not in _BASH_STARTUP_ENV
            and name != "CORSIA_TOKEN"
            and not name.startswith("BASH_FUNC_")
            and not name.startswith("_CORSIA_LIFECYCLE_")
            and not name.startswith("_CORSIA_REQUEST_")
            and not name.startswith("_corsia_lifecycle_")
            and not name.startswith("_corsia_cleanup_")
        )
    }
    launch_env.update({
        "CORSIA_RESTART_BACKEND_PORT": port,
        "CORSIA_RESTART_OPERATION_ID": operation_id,
        "CORSIA_RESTART_CALLER_TOKEN": credential,
        "CORSIA_RESTART_CURL": curl_path,
    })
    return launch_env


def spawn_restart_lifecycle(
    corsia_script: Path,
    script_args: list[str],
    restart_env: dict[str, str],
    operation_id: str,
    log_fh: TextIO,
    *,
    caller_credential: str | None = None,
):
    """Start one sanitized Bash owner whose static envelope spans script loading."""
    if (
        not corsia_script.is_absolute()
        or not corsia_script.is_file()
        or not os.access(corsia_script, os.X_OK)
    ):
        raise ValueError("restart lifecycle script must be an absolute executable file")
    if script_args not in (["restart"], ["restart-prepared"], ["redeploy"]):
        raise ValueError("restart lifecycle verb is not allowed")
    launch_env = _lifecycle_launch_env(
        restart_env, operation_id, caller_credential
    )
    result_path = Path(log_fh.name).parent / f"lifecycle-{operation_id}.result"
    launch_env["CORSIA_RESTART_RESULT_FILE"] = str(result_path)
    command = [
        "/bin/bash",
        "-c",
        LIFECYCLE_SHELL_COMMAND,
        "corsia-lifecycle",
        str(corsia_script),
        *script_args,
    ]
    if sys.platform.startswith("linux") and launch_env.get("CORSIA_SYSTEMD_SERVICE") == "1":
        # A new session stays in the backend's cgroup. Let the user manager
        # start the restart owner separately, passing its sanitized environment
        # through stdin rather than exposing credentials in unit properties.
        helper = Path(__file__).resolve().parent.parent / "scripts" / "systemd_restart.py"
        process_env = dict(launch_env)
        process_env.pop("CORSIA_RESTART_CALLER_TOKEN", None)
        process = subprocess.Popen(
            ["systemd-run", "--user", "--pipe", "--quiet", "--collect", sys.executable, "-I", str(helper)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log_fh,
            start_new_session=True,
            env=process_env,
        )
        assert process.stdin is not None and process.stdout is not None
        try:
            payload = json.dumps({"command": command, "environment": launch_env}).encode()
            process.stdin.write(payload + b"\n")
            process.stdin.flush()
            ready, _, _ = select.select([process.stdout], [], [], 10)
            if not ready or process.stdout.readline() != b"ready\n":
                raise OSError("systemd restart owner did not start")
            process.stdin.write(b"go\n")
            process.stdin.flush()
        finally:
            # Without go the helper exits on EOF and cannot stop the service.
            process.stdin.close()
            process.stdout.close()
        setattr(process, "lifecycle_result_path", result_path)
        return process
    process = subprocess.Popen(
        command,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=launch_env,
    )
    setattr(process, "lifecycle_result_path", result_path)
    return process
