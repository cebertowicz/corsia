#!/bin/bash
# Security audit — writes reports and alert state under the selected state root.

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CORSIA_DIR="$HOME/.corsia"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")/backend"
PYTHON="$CORSIA_DIR/.venv/bin/python"
PIP_AUDIT="$CORSIA_DIR/.venv/bin/pip-audit"
CACHE_DIR="$CORSIA_DIR/.pip-audit-cache"

if [ ! -x "$PYTHON" ]; then
    echo "ERROR: CORSIA Python not found at $PYTHON" >&2
    exit 1
fi

ALERT_FILE=$(
    "$PYTHON" -c \
        'import sys; sys.path.insert(0, sys.argv[1]); from infra.config import STATE_ROOT; print(STATE_ROOT / "security" / "ALERT")' \
        "$BACKEND_DIR"
) || {
    echo "ERROR: Failed to resolve the selected CORSIA state root" >&2
    exit 1
}
OUTPUT_DIR=$(dirname "$ALERT_FILE")

if [ "${1:-}" = "--print-alert-path" ]; then
    printf '%s\n' "$ALERT_FILE"
    exit 0
fi

TIMESTAMP=$(date +%Y-%m-%d)
REPORT="$OUTPUT_DIR/audit-$TIMESTAMP.md"
ERRORS=0
TOTAL_VULNS=0

CORSIA_VENV_SITE=$(find "$CORSIA_DIR/.venv/lib" -maxdepth 2 -name "site-packages" -type d 2>/dev/null | head -1)

if [ ! -x "$PIP_AUDIT" ]; then
    echo "ERROR: pip-audit not found at $PIP_AUDIT" >&2
    echo "       Install: $CORSIA_DIR/.venv/bin/pip install pip-audit" >&2
    exit 1
fi
if [ -z "$CORSIA_VENV_SITE" ]; then
    echo "ERROR: Could not locate site-packages under $CORSIA_DIR/.venv/lib" >&2
    exit 1
fi

mkdir -p "$OUTPUT_DIR" "$CACHE_DIR"

echo "# Security Audit — $TIMESTAMP" > "$REPORT"
echo "" >> "$REPORT"

echo "## Auto-Fix Attempts" >> "$REPORT"
echo "" >> "$REPORT"

echo "### npm audit fix" >> "$REPORT"
echo '```' >> "$REPORT"
if [ -d "$CORSIA_DIR/frontend" ] && command -v npm > /dev/null 2>&1; then
    if ! (cd "$CORSIA_DIR/frontend" && npm audit fix --omit=dev 2>&1) >> "$REPORT"; then
        echo "npm audit fix failed" >> "$REPORT"
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "(skipped — frontend dir or npm not available)" >> "$REPORT"
fi
echo '```' >> "$REPORT"
echo "" >> "$REPORT"

echo "### pip upgrade (CORSIA venv)" >> "$REPORT"
echo '```' >> "$REPORT"
FIX_OUTPUT=$("$PIP_AUDIT" --path "$CORSIA_VENV_SITE" --cache-dir "$CACHE_DIR" --fix --dry-run 2>&1) || true
if echo "$FIX_OUTPUT" | grep -q "would install"; then
    if ! "$CORSIA_DIR/.venv/bin/pip" install --upgrade $(echo "$FIX_OUTPUT" | grep "would install" | awk '{print $NF}') 2>&1 >> "$REPORT"; then
        echo "pip upgrade failed" >> "$REPORT"
        ERRORS=$((ERRORS + 1))
    fi
    echo "Applied available upgrades" >> "$REPORT"
else
    echo "No auto-fixable vulnerabilities" >> "$REPORT"
fi
echo '```' >> "$REPORT"
echo "" >> "$REPORT"

audit_python_env() {
    local name="$1"
    local path="$2"

    echo "### $name" >> "$REPORT"
    echo '```' >> "$REPORT"

    if [ ! -d "$path" ]; then
        echo "❌ ERROR: Path not found: $path" >> "$REPORT"
        ERRORS=$((ERRORS + 1))
    else
        local output
        output=$("$PIP_AUDIT" --path "$path" --cache-dir "$CACHE_DIR" 2>&1) || true
        echo "$output" >> "$REPORT"
        if [ -z "$output" ]; then
            echo "❌ ERROR: pip-audit produced no output for $path" >> "$REPORT"
            ERRORS=$((ERRORS + 1))
        fi
        local env_vulns
        env_vulns=$(echo "$output" | grep -c "CVE-\|GHSA-\|PYSEC-" || true)
        TOTAL_VULNS=$((TOTAL_VULNS + env_vulns))
    fi

    echo '```' >> "$REPORT"
    echo "" >> "$REPORT"
}

echo "## Python Environments" >> "$REPORT"
echo "" >> "$REPORT"

audit_python_env "CORSIA venv (~/.corsia/.venv)" "$CORSIA_VENV_SITE"

PIPX_VENVS_DIR="$HOME/.local/pipx/venvs"
if [ -d "$PIPX_VENVS_DIR" ]; then
    for venv_dir in "$PIPX_VENVS_DIR"/*/; do
        [ -d "$venv_dir" ] || continue
        pkg_name=$(basename "$venv_dir")
        site_pkgs=$(find "$venv_dir/lib" -maxdepth 2 -name "site-packages" -type d 2>/dev/null | head -1)
        if [ -n "$site_pkgs" ]; then
            audit_python_env "pipx: $pkg_name" "$site_pkgs"
        else
            echo "### pipx: $pkg_name" >> "$REPORT"
            echo '```' >> "$REPORT"
            echo "❌ ERROR: No site-packages found in $venv_dir" >> "$REPORT"
            echo '```' >> "$REPORT"
            echo "" >> "$REPORT"
            ERRORS=$((ERRORS + 1))
        fi
    done
else
    echo "### pipx" >> "$REPORT"
    echo "No pipx venvs found" >> "$REPORT"
    echo "" >> "$REPORT"
fi

for brew_prefix in /opt/homebrew /usr/local; do
    [ -d "$brew_prefix/lib" ] || continue
    for site_dir in "$brew_prefix"/lib/python3.*/site-packages; do
        [ -d "$site_dir" ] || continue
        pyver=$(basename "$(dirname "$site_dir")")  # e.g., python3.14
        audit_python_env "Homebrew ${pyver} (${brew_prefix})" "$site_dir"
    done
done

echo "## npm (frontend)" >> "$REPORT"
echo '```' >> "$REPORT"
if [ -d "$CORSIA_DIR/frontend" ] && command -v npm > /dev/null 2>&1; then
    NPM_OUTPUT=$(cd "$CORSIA_DIR/frontend" && npm audit --omit=dev 2>&1) || true
    echo "$NPM_OUTPUT" >> "$REPORT"
    NPM_VULNS=$(echo "$NPM_OUTPUT" | grep -c "vulnerability\|Severity:" || true)
    TOTAL_VULNS=$((TOTAL_VULNS + NPM_VULNS))
else
    echo "(skipped — frontend dir or npm not available)" >> "$REPORT"
fi
echo '```' >> "$REPORT"
echo "" >> "$REPORT"

echo "## Homebrew (outdated)" >> "$REPORT"
echo '```' >> "$REPORT"
if command -v brew > /dev/null 2>&1; then
    brew outdated >> "$REPORT" 2>/dev/null || echo "(brew outdated failed — run manually)" >> "$REPORT"
else
    echo "(skipped — brew not installed)" >> "$REPORT"
fi
echo '```' >> "$REPORT"
echo "" >> "$REPORT"

echo "## Summary" >> "$REPORT"
echo "- **Total vulnerability mentions:** $TOTAL_VULNS" >> "$REPORT"
echo "- **Audit errors:** $ERRORS" >> "$REPORT"
echo "- **Report:** $REPORT" >> "$REPORT"

if [ "$ERRORS" -gt 0 ]; then
    echo "" >> "$REPORT"
    echo "⚠️ **Some audits failed to run. Review errors above.**" >> "$REPORT"
fi

if [ "$TOTAL_VULNS" -gt 0 ] || [ "$ERRORS" -gt 0 ]; then
    echo "{\"vulns\": $TOTAL_VULNS, \"errors\": $ERRORS, \"report\": \"$REPORT\", \"date\": \"$TIMESTAMP\"}" > "$ALERT_FILE"
else
    rm -f "$ALERT_FILE"
fi

find "$OUTPUT_DIR" -name "audit-*.md" -mtime +30 -delete 2>/dev/null || true

echo "Audit complete: $REPORT (vulns=$TOTAL_VULNS, errors=$ERRORS)"
[ "$ERRORS" -eq 0 ] || exit 1
