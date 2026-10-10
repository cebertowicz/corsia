# Shared, sourceable Git index-lock recovery helpers. Callers supply fail/warn
# and may set CORSIA_GIT_LOCK_RECOVERY_FAILURE_PREFIX for their own wording.

git_lock_recovery_fail() {
    fail "${CORSIA_GIT_LOCK_RECOVERY_FAILURE_PREFIX:-}$*"
}

git_lock_recovery_warn() {
    warn "$*"
}

# Stable identity for one exact lock-file generation. The platform split keeps
# the updater testable on Linux while production uses macOS stat(1).
git_lock_identity() {
    local lock="$1"
    if [ "$(uname -s)" = "Darwin" ]; then
        stat -f "%d:%i:%m:%z" "$lock" 2>/dev/null
    else
        stat -c "%d:%i:%Y:%s" "$lock" 2>/dev/null
    fi
}

# Return 0 and print PIDs only when a process has this exact lock open, 1 when
# the lock is unowned, and 2 when ownership cannot be proven. Unrelated Git work
# elsewhere on the host must not prevent recovery of this checkout's stale lock.
git_lock_owner_state() {
    local lock="$1" output rc
    command -v lsof > /dev/null 2>&1 || return 2
    if output="$(lsof -t "$lock" 2>&1)"; then
        rc=0
    else
        rc=$?
    fi
    if [ "$rc" = "0" ]; then
        [ -n "$output" ] || return 2
        printf "%s\n" "$output"
        return 0
    fi
    [ "$rc" = "1" ] && [ -z "$output" ] && return 1
    return 2
}

# Move the candidate aside atomically, verify that the moved inode is still the
# one we inspected, then unlink it. A replacement created during recovery stays
# at index.lock and is never removed.
remove_git_index_lock() {
    local lock="$1" expected="$2" quarantine actual
    quarantine="${lock}.corsia-stale.$$"
    if ! mv "$lock" "$quarantine" 2>/dev/null; then
        [ ! -e "$lock" ] && return 0
        return 1
    fi
    actual="$(git_lock_identity "$quarantine" || true)"
    if [ -z "$actual" ] || [ "$actual" != "$expected" ]; then
        [ -e "$lock" ] || mv "$quarantine" "$lock" 2>/dev/null || :
        return 2
    fi
    if ! rm -f "$quarantine"; then
        [ -e "$lock" ] || mv "$quarantine" "$lock" 2>/dev/null || :
        return 1
    fi
}

# Acquire the recovery mutex through the platform kernel-lock primitive. The
# empty mutex file remains in the Git directory; the lock itself is descriptor-
# scoped and cannot become stale after a process dies.
acquire_fd_mutex() {
    if command -v lockf > /dev/null 2>&1; then
        lockf -t 0 "$1"
        return $?
    fi
    if command -v flock > /dev/null 2>&1; then
        flock -n "$1"
        return $?
    fi
    return 2
}

# Recover an unowned, unchanged Git index lock after a short stabilization
# window. Every ambiguous state fails before pull/build/drain/restart. A kernel
# lock serializes concurrent Update attempts; the exact lock identity is checked
# again immediately before removal.
recover_stale_git_index_lock() {
    local git_dir="$1" lock mutex identity current_identity mtime now age
    local owner_output owner_state remove_state grace remaining
    lock="$git_dir/index.lock"
    [ -e "$lock" ] || return 0
    grace="${CORSIA_GIT_LOCK_STALE_SECONDS:-5}"
    case "$grace" in
        ""|*[!0-9]*)
            git_lock_recovery_fail "invalid Git lock recovery grace period"
            return 1
            ;;
    esac
    identity="$(git_lock_identity "$lock" || true)"
    [ -n "$identity" ] || {
        git_lock_recovery_fail "could not prove the Git index lock identity; it was preserved"
        return 1
    }
    mtime="$(printf "%s" "$identity" | awk -F: '{print $3}')"
    now="$(date +%s)"
    case "$mtime" in
        ""|*[!0-9]*)
            git_lock_recovery_fail "could not prove the Git index lock age; it was preserved"
            return 1
            ;;
    esac
    age=$((now - mtime))
    if owner_output="$(git_lock_owner_state "$lock")"; then
        git_lock_recovery_fail "Git index lock belongs to an active process (${owner_output}); it was preserved"
        return 1
    else
        owner_state=$?
        if [ "$owner_state" != "1" ]; then
            git_lock_recovery_fail "could not prove the Git index lock is unowned; it was preserved"
            return 1
        fi
    fi
    if [ "$age" -lt "$grace" ]; then
        remaining=$((grace - age))
        git_lock_recovery_warn "Git index lock is unowned but recent (${age}s old); waiting ${remaining}s to confirm it is orphaned"
        sleep "$remaining"
        [ -e "$lock" ] || return 0
        current_identity="$(git_lock_identity "$lock" || true)"
        if [ -z "$current_identity" ] || [ "$current_identity" != "$identity" ]; then
            git_lock_recovery_fail "Git index lock changed while settling; the replacement was preserved"
            return 1
        fi
        if owner_output="$(git_lock_owner_state "$lock")"; then
            git_lock_recovery_fail "Git index lock became active while settling (${owner_output}); it was preserved"
            return 1
        else
            owner_state=$?
            if [ "$owner_state" != "1" ]; then
                git_lock_recovery_fail "could not prove the Git index lock remained unowned; it was preserved"
                return 1
            fi
        fi
    fi

    mutex="$git_dir/corsia-index-lock-recovery.lock"
    exec 9> "$mutex" || {
        git_lock_recovery_fail "could not open the Git lock recovery mutex"
        return 1
    }
    if acquire_fd_mutex 9; then
        :
    else
        owner_state=$?
        exec 9>&-
        if [ "$owner_state" = "2" ]; then
            git_lock_recovery_fail "no supported Git lock recovery mutex is available"
        else
            git_lock_recovery_fail "another Update is checking the Git index lock"
        fi
        return 1
    fi

    if [ ! -e "$lock" ]; then
        exec 9>&-
        return 0
    fi
    current_identity="$(git_lock_identity "$lock" || true)"
    if [ -z "$current_identity" ] || [ "$current_identity" != "$identity" ]; then
        exec 9>&-
        git_lock_recovery_fail "Git index lock changed during recovery; the replacement was preserved"
        return 1
    fi
    if owner_output="$(git_lock_owner_state "$lock")"; then
        exec 9>&-
        git_lock_recovery_fail "Git index lock became active during recovery (${owner_output}); it was preserved"
        return 1
    else
        owner_state=$?
        if [ "$owner_state" != "1" ]; then
            exec 9>&-
            git_lock_recovery_fail "could not prove the Git index lock remained unowned; it was preserved"
            return 1
        fi
    fi

    if remove_git_index_lock "$lock" "$identity"; then
        remove_state=0
    else
        remove_state=$?
    fi
    exec 9>&-
    if [ "$remove_state" = "2" ]; then
        git_lock_recovery_fail "Git index lock changed during recovery; the replacement was preserved"
        return 1
    fi
    if [ "$remove_state" != "0" ]; then
        git_lock_recovery_fail "could not remove the proven-stale Git index lock; it was preserved"
        return 1
    fi
    git_lock_recovery_warn "Recovered stale Git index lock at $lock"
}
