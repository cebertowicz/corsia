# Account commands and the lifecycle script's own credential.
#
# This script calls its backend's authenticated API (drain, readiness, hard-cap,
# cancel) before it may stop or replace an instance. It used to take the first
# key out of a hardcoded table for that. There is no such table now, so it mints
# a real credential the same way anything else does: a one-hour service token
# for the oldest account, which expires on its own if a lifecycle run dies
# half-way.
#
# Minting can legitimately fail — a fresh install with no account, or a database
# that predates the accounts tables. That prints one line naming the command
# that fixes it and leaves the token empty on purpose: the drain path already
# reports an unauthenticated lifecycle call, and inventing a fallback credential
# here is exactly the thing this change removes.

account_cli() {
    # Run the backend CLI from the directory `python -m cli` needs.
    (cd "$BACKEND_DIR" && "$PYTHON" -m cli "$@")
}

account_exists() {
    # True when this install has at least one account. False also covers a
    # database written before the accounts tables existed, which is the same
    # answer for the caller: there is nobody to sign in as yet.
    "$PYTHON" -c "
import sys
sys.path.insert(0, '$BACKEND_DIR')
from infra.accounts import has_users
sys.exit(0 if has_users() else 1)
" > /dev/null 2>&1
}

ensure_first_account() {
    # First run: create the account the operator will sign in with. The CLI
    # creates the schema itself, so this works before the backend has ever run.
    # Sets FIRST_ACCOUNT_NAME to the account just created (empty when one
    # already existed); the prompts own stdout, so the name cannot go there.
    FIRST_ACCOUNT_NAME=""
    account_exists && return 0
    echo ""
    echo "${INSTALL_STEP_5:-}Create the account you'll sign in with:"
    account_cli user add || return 1
    FIRST_ACCOUNT_NAME="$(account_cli user list | cut -f1)"
    case "$FIRST_ACCOUNT_NAME" in
        ""|*$'\n'*) FIRST_ACCOUNT_NAME=""; return 1 ;;
    esac
}

named_account_exists() {
    # True when this install already has an account called "$1".
    "$PYTHON" -c "
import sys
sys.path.insert(0, '$BACKEND_DIR')
from infra.accounts import get_user
sys.exit(0 if get_user(sys.argv[1]) else 1)
" "$1" > /dev/null 2>&1
}

seed_disposable_account() {
    # Provision a throwaway install's account and print one API token.
    #
    # For test harnesses that boot a backend against a disposable workspace.
    # Nothing ships with a credential any more, so they make one the way an
    # operator would. The password is random and discarded — only the printed
    # token is ever used, and the whole database goes away with the harness.
    #
    # Safe to call twice in one run: an existing account has its password
    # reset rather than failing, so a reused workspace works the same way a
    # fresh one does. Callers must already have the disposable
    # CORSIA_STATE_DIR and CORSIA_WORKSPACE in the environment, or this
    # writes to the real install.
    local name="${1:-harness}" label="${2:-harness}" verb="add"
    named_account_exists "$name" && verb="passwd"
    "$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(16))' \
        | account_cli user "$verb" "$name" --password-stdin >&2 || return 1
    account_cli token create --user "$name" --label "$label"
}

needs_service_token() {
    # Only lifecycle commands call the drain API. Minting writes a token row,
    # so status, help and the account commands must not mint one. The backend
    # runs ensure-current-release on every start, before its schema is
    # current, and never drains.
    [ "${1:-}" = ensure-current-release ] && return 1
    is_mutating_lifecycle_command "$@"
}

mint_service_token() {
    # Print a one-hour service token, or nothing plus the CLI's own reason.
    # The CLI names the actual failure (no account, no account tables, a
    # database or import error), so its stderr is passed through untouched.
    local token
    if token=$(account_cli token service) && [ -n "$token" ]; then
        printf '%s' "$token"
        return 0
    fi
    echo "This lifecycle command could not get a credential (reason above). Updating an install that predates sign-in? Use Settings > Update first." >&2
    return 1
}

run_sources_cli() {
    # The model-source summary needs a credential for one call. Mint a
    # short-lived service token and hand it to the child through its
    # environment (never argv, never a file). A failed mint prints the CLI's
    # own reason and skips the summary.
    local svc
    svc="$(mint_service_token)" || return 1
    (cd "$BACKEND_DIR" \
        && CORSIA_SOURCES_SERVICE_TOKEN="$svc" BACKEND_PORT="$BACKEND_PORT" \
            "$PYTHON" -m cli sources "$@")
}

run_corsia_agent_cli() {
    # The CORSIA agent warm calls the running backend. Same credential handling as
    # the sources summary. A failed mint is not an install failure: the warm is
    # only a head start, so the caller skips it.
    local svc
    svc="$(mint_service_token)" || return 1
    (cd "$BACKEND_DIR" \
        && CORSIA_SOURCES_SERVICE_TOKEN="$svc" BACKEND_PORT="$BACKEND_PORT" \
            "$PYTHON" -m cli agent "$@")
}

run_local_setup_cli() {
    # The serving backend is the only owner of local setup, so its model updates
    # reach the installer-open browser. With no backend the CLI says to start it.
    local svc
    svc="$(mint_service_token)" || return 1
    (cd "$BACKEND_DIR" \
        && CORSIA_SOURCES_SERVICE_TOKEN="$svc" BACKEND_PORT="$BACKEND_PORT" \
            "$PYTHON" -m cli local setup "$@")
}
