# Delegate/doc CLI credential guard (#385).

API_TOKEN_FILE="${CORSIA_API_TOKEN_FILE:-$CORSIA_DIR/.api-token}"

delegate_has_explicit_token() {
    local a
    for a in "$@"; do
        case "$a" in
            create-thread|send|status|comment|review|reply|resolve|ack|comments)
                return 1 ;;
            --token|--token=*) return 0 ;;
        esac
    done
    return 1
}

ensure_delegate_token() {
    [ "$#" -eq 0 ] && return 0
    local a
    for a in "$@"; do
        case "$a" in -h|--help|--version) return 0 ;; esac
    done
    [ -n "${CORSIA_API_TOKEN:-}" ] && return 0
    delegate_has_explicit_token "$@" && return 0

    if [ -s "$API_TOKEN_FILE" ]; then
        return 0
    fi

    echo "  ERROR: No delegation API token." >&2
    echo "  $API_TOKEN_FILE is missing or empty, and no override was given." >&2
    echo "  Provision it (any one):" >&2
    echo "    - mint one into the file (recommended; one time):" >&2
    echo "        corsia token create --write-delegate-file" >&2
    echo "    - export CORSIA_API_TOKEN=<tok>   (operator/CI override)" >&2
    echo "    - pass --token <tok>               (per-invocation override)" >&2
    echo "  <tok> is an API token from 'corsia token create'." >&2
    exit 2
}
