#!/bin/bash
# Post-clone CORSIA installer for macOS on Apple Silicon, and for the server
# and web app on Ubuntu and Debian Linux (x86_64 or arm64; see the Linux
# section below).
#
# Run this from the clone the user already placed in ~/.corsia:
#
#   cd ~/.corsia
#   bash scripts/install.sh
#
# Readable first, and safe to run again. Every step prints one line before it
# acts. It finishes the whole install before CORSIA starts: prerequisites, the
# Python environment, the first run (where your work lives, how you will use
# CORSIA, your account), a working model source (a Claude Code or Codex
# sign-in, else the local model, set up here with no server), and only then
# starts CORSIA, prints the model-source summary and opens the browser. A
# failed model setup exits non-zero and starts nothing. It saves no API token;
# the summary uses a one-hour token that is never written to disk.
#
# A re-run never pulls: it prints the installed version, starts CORSIA only if
# it is not running, and points at Settings > Update, which stays the only
# update path. It never exports CORSIA_WORKSPACE and never touches the
# launchd service, so the first-run block in scripts/corsia owns setup.

set -euo pipefail

# Homebrew 7 stops at "[y/n]" before installing dependencies; the install never asks.
export HOMEBREW_NO_ASK=1

CORSIA_HOME="$HOME/.corsia"
# Held equal to infra.default_port by tests/test_default_port.py.
BACKEND_PORT="${BACKEND_PORT:-1956}"
APP_URL="http://localhost:$BACKEND_PORT"
# The PATH the launchd service runs with: the same value as SERVICE_PATH in
# scripts/corsia.d/service.sh (a test holds them equal). Prerequisites are
# resolved against it, because a tool only your shell can see fails inside the
# service. CORSIA_INSTALL_SERVICE_PATH replaces it for tests only.
SERVICE_PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SERVICE_PATH="${CORSIA_INSTALL_SERVICE_PATH:-$SERVICE_PATH}"
# Questions are read from the terminal, never stdin. CORSIA_INSTALL_TTY
# replaces it for tests only.
PROMPT_INPUT="${CORSIA_INSTALL_TTY:-/dev/tty}"
# Test-only command seams. Production keeps the official installer and sudo.
SUDO_COMMAND="${CORSIA_INSTALL_SUDO:-sudo}"
HOMEBREW_INSTALLER="${CORSIA_INSTALL_HOMEBREW_INSTALLER:-/bin/bash}"
HOMEBREW_INSTALLED=0
HOMEBREW_SHELLENV_ADDED=0
CORSIA_COMMAND_READY=0
LOCAL_SETUP_FORCED=0

# label | formula | binary
PREREQS=(
    "git|git|git"
    "Python 3.11|python@3.11|python3.11"
    "Node 22|node|node"
    "safehouse|eugene1g/safehouse/agent-safehouse|safehouse"
    "GitHub CLI|gh|gh"
)
# The browser CLI CORSIA drives: npm, because brew's build is unpinned and CORSIA
# admits only 0.31.x (backend/host_access/browser_cli.py).
AGENT_BROWSER_PACKAGE="agent-browser@0.31"

say() { printf '%s\n' "$*"; }
stop() { printf '%s\n' "$@" >&2; exit 1; }

open_prompt_input() {
    { exec 3< "$PROMPT_INPUT"; } 2> /dev/null \
        || stop "The installer asks a few questions and needs a terminal. Run it from Terminal."
}

check_platform() {
    [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] \
        || stop "CORSIA installs on macOS with Apple Silicon only (found $(uname -s) $(uname -m))."
    # The corrections classifier always runs on the local model, which needs macOS 15.
    local macos
    macos="$(sw_vers -productVersion)"
    version_at_least "$macos" 15 0 0 \
        || stop "CORSIA needs macOS 15 or later (found macOS $macos). Update macOS in System Settings › General › Software Update, then run this again."
}

service_which() { (PATH="$SERVICE_PATH" command -v "$1") || return 1; }

version_at_least() {
    # version_at_least "1.2.3" 1 2 0 -> true when the dotted version >= 1.2.0
    local major minor patch
    IFS=. read -r major minor patch <<< "$1"
    major="${major//[!0-9]/}"; minor="${minor//[!0-9]/}"; patch="${patch//[!0-9]/}"
    major="${major:-0}"; minor="${minor:-0}"; patch="${patch:-0}"
    [ "$major" -gt "$2" ] && return 0; [ "$major" -lt "$2" ] && return 1
    [ "$minor" -gt "$3" ] && return 0; [ "$minor" -lt "$3" ] && return 1
    [ "$patch" -ge "$4" ]
}

node_version_ok() {
    # Vite 7: Node 20.19+ on the 20 line, or 22.12+.
    local version="${1#v}" major
    major="${version%%.*}"
    case "$major" in ""|*[!0-9]*) return 1 ;; esac
    if [ "$major" -eq 20 ]; then version_at_least "$version" 20 19 0; return; fi
    version_at_least "$version" 22 12 0
}

binary_ok() {
    # binary_ok <binary> <path>: the version requirement for one prerequisite.
    case "$1" in
        # Git 2.36 added `cat-file --batch-command`, which the lesson report reads blobs through.
        git) version_at_least "$("$2" --version 2> /dev/null | grep -Eo '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)" 2 36 0 ;;
        # SQLite 3.45 keeps JSON subtypes through an ordered aggregate and takes json_valid(X, 2).
        python3.11) "$2" -c 'import sqlite3, sys; sys.exit(sys.version_info[:2] != (3, 11) or sqlite3.sqlite_version_info < (3, 45, 0))' 2> /dev/null ;;
        node) node_version_ok "$("$2" --version 2> /dev/null)" ;;
        safehouse) version_at_least "$("$2" --version 2> /dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -1)" 0 11 1 ;;
    esac
}

agent_browser_ok() {
    # agent_browser_ok <path>: an installed version inside the admitted 0.31 line.
    local version
    version="$("$1" --version 2> /dev/null | grep -Eo '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)"
    [ -n "$version" ] && version_at_least "$version" 0 31 0 && ! version_at_least "$version" 0 32 0
}

prereq_state() {
    # Prints ok, missing, old, or "invisible <path>" for one binary.
    local binary="$1" found
    if found="$(service_which "$binary")"; then
        if binary_ok "$binary" "$found"; then echo ok; else echo old; fi
    elif found="$(command -v "$binary" 2> /dev/null)"; then
        echo "invisible $found"
    else
        echo missing
    fi
}

check_prereqs() {
    # Prints the status line; sets NEEDED (label|formula) and INVISIBLE fixes.
    local entry label formula binary state line="    "
    NEEDED=(); INVISIBLE=()
    for entry in "${PREREQS[@]}"; do
        IFS='|' read -r label formula binary <<< "$entry"
        state="$(prereq_state "$binary")"
        case "$state" in
            ok) line+=" ✓ $label  " ;;
            invisible*)
                line+=" ! $label  "
                INVISIBLE+=("$label is installed but not visible to the service. Fix: ln -s ${state#invisible } ~/.local/bin/$binary")
                ;;
            *) line+=" ✗ $label  "; NEEDED+=("$label|$formula") ;;
        esac
    done
    say "${line%  }"
}

find_brew() {
    [ -n "${CORSIA_INSTALL_BREW:-}" ] && [ -x "$CORSIA_INSTALL_BREW" ] \
        && { echo "$CORSIA_INSTALL_BREW"; return 0; }
    command -v brew 2> /dev/null && return 0
    [ -x /opt/homebrew/bin/brew ] && echo /opt/homebrew/bin/brew
    [ -x /usr/local/bin/brew ] && echo /usr/local/bin/brew
}

find_installed_brew() {
    [ -n "${CORSIA_INSTALL_BREW:-}" ] && [ -x "$CORSIA_INSTALL_BREW" ] \
        && { echo "$CORSIA_INSTALL_BREW"; return 0; }
    [ -x /opt/homebrew/bin/brew ] && echo /opt/homebrew/bin/brew
    [ -x /usr/local/bin/brew ] && echo /usr/local/bin/brew
}

install_homebrew() {
    if ! "$SUDO_COMMAND" -v < "$PROMPT_INPUT"; then
        stop "Homebrew needs an administrator account. Sign in with an administrator account, then run this again."
    fi
    if ! NONINTERACTIVE=1 "$HOMEBREW_INSTALLER" -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; then
        stop "Homebrew installation failed. Fix Homebrew, then run this again."
    fi
    HOMEBREW_INSTALLED=1
}

install_needed() {
    local brew entry label formula
    if ! brew="$(find_brew)"; then
        install_homebrew
        brew="$(find_installed_brew)" || stop "Homebrew installation failed. Fix Homebrew, then run this again."
    fi
    for entry in "${NEEDED[@]}"; do
        IFS='|' read -r label formula <<< "$entry"
        say "     Installing ${label}…"
        "$brew" install "$formula" || stop "Homebrew could not install $label. Run: brew install $formula"
    done
}

require_checkout() {
    local checkout_path
    [ -d "$CORSIA_HOME" ] || stop "Clone CORSIA into ~/.corsia, then run: cd ~/.corsia && bash scripts/install.sh"
    checkout_path="$(cd "$CORSIA_HOME" && pwd -P)"
    [ "$(pwd -P)" = "$checkout_path" ] \
        || stop "Clone CORSIA into ~/.corsia, then run: cd ~/.corsia && bash scripts/install.sh"
    [ -f "$CORSIA_HOME/scripts/install.sh" ] \
        || stop "Clone CORSIA into ~/.corsia, then run: cd ~/.corsia && bash scripts/install.sh"
}

ensure_agent_browser() {
    # After Node is ready: install the pinned browser CLI unless a supported one is already visible.
    local npm found
    if found="$(service_which agent-browser)" && agent_browser_ok "$found"; then
        return 0
    fi
    npm="$(service_which npm)" || stop "npm is not visible to the service. Run: brew reinstall node"
    say "     Installing agent-browser…"
    "$npm" install -g "$AGENT_BROWSER_PACKAGE" \
        || stop "npm could not install agent-browser. Run: npm install -g $AGENT_BROWSER_PACKAGE"
    found="$(service_which agent-browser)" && agent_browser_ok "$found" \
        || stop "agent-browser is not visible to the service after install. Check: npm prefix -g"
    "$found" install || stop "agent-browser could not finish setup. Run: agent-browser install"
}

ensure_prereqs() {
    say "1/8  Checking prerequisites"
    check_prereqs
    [ "${#INVISIBLE[@]}" -eq 0 ] || stop "${INVISIBLE[@]}"
    if [ "${#NEEDED[@]}" -gt 0 ]; then
        install_needed
        check_prereqs
        [ "${#INVISIBLE[@]}" -eq 0 ] || stop "${INVISIBLE[@]}"
        [ "${#NEEDED[@]}" -eq 0 ] || stop "Still not ready: ${NEEDED[*]%%|*}. Upgrade it (brew upgrade), then run this again."
    fi
    ensure_agent_browser
}

link_points_into_corsia() {
    local link_path="$1" target link_dir resolved_target
    target="$(readlink "$link_path")" || return 1
    case "$target" in
        "$CORSIA_HOME"/*) return 0 ;;
        /*) return 1 ;;
    esac
    link_dir="$(cd "$(dirname "$link_path")" && pwd -P)" || return 1
    resolved_target="$(cd "$link_dir/$(dirname "$target")" 2> /dev/null && pwd -P)/$(basename "$target")"
    case "$resolved_target" in "$CORSIA_HOME"/*) return 0 ;; *) return 1 ;; esac
}

link_corsia_command() {
    local brew="$1" command_path
    command_path="$("$brew" --prefix)/bin/corsia"
    if [ -e "$command_path" ] || [ -L "$command_path" ]; then
        if link_points_into_corsia "$command_path"; then
            rm "$command_path"
        else
            say "     A different corsia command already exists at $command_path; leaving it unchanged."
            return 1
        fi
    fi
    ln -s "$CORSIA_HOME/scripts/corsia" "$command_path" \
        || stop "Could not link the corsia command. Check Homebrew, then run this again."
    CORSIA_COMMAND_READY=1
}

add_homebrew_shellenv() {
    local brew="$1" zprofile line
    [ "$HOMEBREW_INSTALLED" -eq 1 ] || return 0
    zprofile="$HOME/.zprofile"
    line="eval \"\$($brew shellenv)\""
    if [ -f "$zprofile" ] && grep -Fqx "$line" "$zprofile"; then
        return 0
    fi
    printf '%s\n' "$line" >> "$zprofile" \
        || stop "Could not update ~/.zprofile. Add Homebrew to your shell, then run this again."
    HOMEBREW_SHELLENV_ADDED=1
}

installed_version() {
    printf '%s (commit %s)' \
        "$(git -C "$CORSIA_HOME" describe --tags --always 2> /dev/null || echo unknown)" \
        "$(git -C "$CORSIA_HOME" rev-parse --short HEAD 2> /dev/null || echo unknown)"
}

ensure_python_env() {
    # Skipped once the environment imports; the frontend is built by start.
    local python="$CORSIA_HOME/.venv/bin/python"
    if [ -x "$python" ] && "$python" -c 'import fastapi, uvicorn' 2> /dev/null; then
        return 0
    fi
    if [ "${CORSIA_INSTALL_LINUX:-}" = 1 ]; then
        say "     Building…"
    else
        say "3/8  Building (about 2 minutes)"
    fi
    "$(service_which python3.11)" -m venv "$CORSIA_HOME/.venv"
    "$python" -m pip install --quiet --disable-pip-version-check \
        -r "$CORSIA_HOME/backend/requirements.lock"
}

is_running() { curl -sf --max-time 3 "http://127.0.0.1:$BACKEND_PORT/api/health" > /dev/null 2>&1; }

start_corsia() {
    CORSIA_INSTALL_FLOW=1 "$CORSIA_HOME/scripts/corsia" start 3<&-
}

summarize_sources() {
    "$CORSIA_HOME/scripts/corsia" sources --install-summary || :
}

warm_corsia_agent() {
    # A head start for the CORSIA agent's first reply; prints nothing unless the
    # local starter model is ready, and never fails the install.
    CORSIA_INSTALL_FLOW=1 "$CORSIA_HOME/scripts/corsia" agent warm || :
}

open_browser() {
    say "Opening CORSIA at $APP_URL"
    open "$APP_URL" > /dev/null 2>&1 || say "     Open that address in your browser."
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --local) LOCAL_SETUP_FORCED=1 ;;
            *) stop "Usage: bash scripts/install.sh [--local]" ;;
        esac
        shift
    done
}

setup_local_model() {
    # A re-run with --local: the running server owns the setup flight.
    "$CORSIA_HOME/scripts/corsia" local setup --automatic --start-only || :
}

ensure_model_source() {
    # Before CORSIA starts. The first run asks its questions here (steps 4 and
    # 5); then a working Claude Code or Codex sign-in, else the local model.
    local args=(local setup --before-start --if-no-subscription)
    [ "$LOCAL_SETUP_FORCED" -eq 0 ] || args=(local setup --before-start)
    local status=0
    CORSIA_INSTALL_FLOW=1 "$CORSIA_HOME/scripts/corsia" "${args[@]}" <&3 3<&- || status=$?
    [ "$status" -eq 0 ] && return
    # 3: no model source can work on this Mac until the person signs in; the
    # lines above say how, so a bare retry line would only mislead.
    [ "$status" -eq 3 ] && exit 1
    stop "Run the install command again to retry."
}

# ---------------------------------------------------------------------------
# Linux: Ubuntu and Debian, the server and web app. main() calls linux_main
# only when uname reports Linux; nothing above reaches the functions below.
#
# apt supplies git, gh, Python and build tools. The managed-runtime module
# verifies pinned uv and Node archives; uv supplies Python 3.11.
# uv checks the Python build it installs against hashes built into that uv
# release. Only apt uses sudo, and the installer says so first. There is no
# No local model on Linux; protected commands use bubblewrap.
# The model source is connected in the app.
# ---------------------------------------------------------------------------

LINUX_APT_BASE=(build-essential curl ca-certificates xz-utils unzip jq sqlite3 python3 lsof)
LINUX_OS_RELEASE="${CORSIA_INSTALL_OS_RELEASE:-/etc/os-release}"
LINUX_LOCAL_BIN="$HOME/.local/bin"
UV_BIN=""

linux_arch() {
    case "$(uname -m)" in
        x86_64|amd64) echo x86_64 ;;
        aarch64|arm64) echo arm64 ;;
        *) return 1 ;;
    esac
}

check_platform_linux() {
    local distro version
    # shellcheck disable=SC1090
    distro="$( (. "$LINUX_OS_RELEASE" 2> /dev/null; printf '%s' "${ID:-}") || true)"
    # shellcheck disable=SC1090
    version="$( (. "$LINUX_OS_RELEASE" 2> /dev/null; printf '%s' "${VERSION_ID:-}") || true)"
    case "$distro" in
        ubuntu) version_at_least "$version" 24 4 0 || stop "CORSIA needs Ubuntu 24.04 or later, or Debian 12 or later." ;;
        debian) version_at_least "$version" 12 0 0 || stop "CORSIA needs Ubuntu 24.04 or later, or Debian 12 or later." ;;
        *) stop "CORSIA needs Ubuntu 24.04 or later, or Debian 12 or later." ;;
    esac
    linux_arch > /dev/null \
        || stop "CORSIA installs on Linux x86_64 or arm64 only (found $(uname -m))."
    [ "$LOCAL_SETUP_FORCED" -eq 0 ] \
        || stop "CORSIA has no local model on Linux. Run the install without --local, then connect a provider in the app."
}

apt_package_missing() { [ "$(dpkg-query -W -f='${Status}' "$1" 2> /dev/null)" != "install ok installed" ]; }

as_admin() {
    if [ "$(id -u)" -eq 0 ]; then "$@"; else "$SUDO_COMMAND" "$@"; fi
}

install_apt_linux() {
    say "     Installing with apt: $*"
    if [ "$(id -u)" -ne 0 ]; then
        say "     This needs administrator access (sudo); you may be asked for your password."
        "$SUDO_COMMAND" -v < "$PROMPT_INPUT" \
            || stop "Installing system packages needs an administrator account. Sign in with one, then run this again."
    fi
    as_admin env DEBIAN_FRONTEND=noninteractive apt-get update -qq \
        || stop "apt couldn’t refresh its package lists. Run: sudo apt-get update"
    as_admin env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" \
        || stop "apt couldn’t install: $*. Run: sudo apt-get install $*"
}

managed_runtime_linux() {
    PYTHONPATH="$CORSIA_HOME/backend" python3 -c '
import sys
from pathlib import Path
from tools.managed_runtime import ManagedRuntimeError, ensure_runtime
try:
    print(ensure_runtime(sys.argv[1], root=Path.home() / ".cache" / "corsia" / "runtimes", arch="linux-" + sys.argv[2]))
except ManagedRuntimeError as error:
    if "checksum" in str(error):
        print(f"Couldn’t verify the {sys.argv[1]} download. Run this again.", file=sys.stderr)
    else:
        print(f"Couldn’t install {sys.argv[1]}. Check your connection, then run this again.", file=sys.stderr)
    sys.exit(1)
' "$1" "$(linux_arch)"
}

install_python_linux() {
    local python
    say "     Installing Python 3.11…"
    UV_BIN="$(managed_runtime_linux uv)" || return 1
    mkdir -p "$LINUX_LOCAL_BIN"
    [ -e "$LINUX_LOCAL_BIN/uv" ] || [ -L "$LINUX_LOCAL_BIN/uv" ] || ln -s "$UV_BIN" "$LINUX_LOCAL_BIN/uv"
    [ -e "$LINUX_LOCAL_BIN/uvx" ] || [ -L "$LINUX_LOCAL_BIN/uvx" ] || ln -s "${UV_BIN%/*}/uvx" "$LINUX_LOCAL_BIN/uvx"
    "$UV_BIN" python install 3.11 || stop "uv couldn’t install Python 3.11. Run: uv python install 3.11"
    python="$(UV_PYTHON_PREFERENCE=only-managed "$UV_BIN" python find 3.11)" \
        || stop "uv installed Python 3.11 but couldn’t find it. Run: uv python find 3.11"
    mkdir -p "$LINUX_LOCAL_BIN"
    ln -sfn "$python" "$LINUX_LOCAL_BIN/python3.11"
}

link_managed_node_linux() {
    local node tool
    say "     Installing Node…"
    node="$(managed_runtime_linux node)" || return 1
    mkdir -p "$LINUX_LOCAL_BIN"
    for tool in node npm npx; do ln -sfn "${node%/*}/$tool" "$LINUX_LOCAL_BIN/$tool"; done
}

linux_install_needed() {
    # NEEDED holds label|source from check_prereqs: apt:<package>, uv or node.
    local packages=() package entry via want_python=0 want_node=0
    for package in "${LINUX_APT_BASE[@]}"; do
        if apt_package_missing "$package"; then packages+=("$package"); fi
    done
    if [ "${#NEEDED[@]}" -gt 0 ]; then
        for entry in "${NEEDED[@]}"; do
            via="${entry#*|}"
            case "$via" in
                apt:*) packages+=("${via#apt:}") ;;
                uv) want_python=1 ;;
                node) want_node=1 ;;
            esac
        done
    fi
    if [ "${#packages[@]}" -gt 0 ]; then install_apt_linux "${packages[@]}"; fi
    if [ "$want_python" -eq 1 ]; then install_python_linux; fi
    if [ "$want_node" -eq 1 ]; then link_managed_node_linux; fi
}

linux_browser_deps() {
    local distro packages=(libcairo2 libgbm1 libpango-1.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 fonts-liberation)
    distro="$( (. "$LINUX_OS_RELEASE"; printf '%s' "${ID:-}") )"
    if [ "$distro" = ubuntu ]; then
        packages+=(libasound2t64 libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 libcups2t64)
    else
        packages+=(libasound2 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 libcups2)
    fi
    install_apt_linux apparmor "${packages[@]}"
}

linux_install_chromium() {
    local arch url digest archive_dir staging parent owner mode
    arch="$(linux_arch)"
    if [ "$arch" = arm64 ]; then
        url='https://cdn.playwright.dev/builds/cft/156.0.8078.4/linux-arm64/chrome-linux-arm64.zip'
        digest='910a12fff6170202195f72ef3d353801c7063d301ab80b1f51b77d791fda60ce'
        archive_dir=chrome-linux-arm64
    else
        url='https://storage.googleapis.com/chrome-for-testing-public/156.0.8078.4/linux64/chrome-linux64.zip'
        digest='43fd31766c7533a02dda2ded5ad50c2f861cd5400be7509c0abad71129f79d79'
        archive_dir=chrome-linux64
    fi
    for parent in / /opt; do
        [ ! -L "$parent" ] || stop "The browser install path is not safe. Check $parent, then run this again."
        owner="$(stat -c %u "$parent")"; mode="$(stat -c %a "$parent")"
        [ "$owner" = 0 ] && (( (8#$mode & 0022) == 0 )) || stop "The browser install path is not safe. Check $parent, then run this again."
    done
    [ ! -L /opt/corsia ] && [ ! -L /opt/corsia/chromium ] || stop "The browser install path is not safe. Check /opt/corsia, then run this again."
    if [ -e /opt/corsia ]; then
        owner="$(stat -c %u /opt/corsia)"; mode="$(stat -c %a /opt/corsia)"
        [ "$owner" = 0 ] && (( (8#$mode & 0022) == 0 )) || stop "The browser install path is not safe. Check /opt/corsia, then run this again."
    fi
    as_admin install -d -m 0755 /opt/corsia
    staging="$(as_admin mktemp -d /opt/corsia/.browser-XXXXXXXX)"
    as_admin curl -fsSL "$url" -o "$staging/chrome.zip" || stop "Couldn’t download the browser. Check your connection, then run this again."
    as_admin sh -c 'printf "%s  %s\n" "$1" "$2" | sha256sum -c -' _ "$digest" "$staging/chrome.zip" \
        || stop "Couldn’t verify the browser download. Run the installer again."
    as_admin unzip -q "$staging/chrome.zip" -d "$staging"
    as_admin chmod -R go-w "$staging/$archive_dir"
    if [ -e /opt/corsia/chromium ]; then as_admin mv /opt/corsia/chromium "$staging/previous"; fi
    as_admin mv "$staging/$archive_dir" /opt/corsia/chromium
    [ ! -L /etc/apparmor.d/corsia-chromium ] || stop "The browser rule path is not safe. Check /etc/apparmor.d/corsia-chromium, then run this again."
    printf '%s\n' '#include <tunables/global>' 'profile corsia-chromium /opt/corsia/chromium/chrome flags=(unconfined) {' '  userns,' '}' \
        | as_admin tee "$staging/corsia-chromium" > /dev/null
    as_admin install -m 0644 "$staging/corsia-chromium" /etc/apparmor.d/corsia-chromium
    as_admin apparmor_parser -r /etc/apparmor.d/corsia-chromium \
        || stop "Couldn’t enable the browser sandbox. Check AppArmor, then run the installer again."
    as_admin rm -rf "$staging"
    say '     The browser is ready. Remove its AppArmor rule with sudo apparmor_parser -R /etc/apparmor.d/corsia-chromium && sudo rm /etc/apparmor.d/corsia-chromium.'
}

linux_ensure_agent_browser() {
    local npm found
    if ! found="$(service_which agent-browser)" || ! agent_browser_ok "$found"; then
        npm="$(service_which npm)" || stop "npm is not visible to the service. Re-run the CORSIA installer."
        "$npm" install -g --prefix "$HOME/.local" "$AGENT_BROWSER_PACKAGE" \
            || stop "Couldn’t install agent-browser. Re-run the CORSIA installer."
        found="$(service_which agent-browser)" && agent_browser_ok "$found" \
            || stop "agent-browser is not visible to the service. Re-run the CORSIA installer."
    fi
    if [ -x /opt/google/chrome/chrome ]; then return; fi
    linux_browser_deps
    linux_install_chromium
}

enable_linger_linux() {
    local user linger
    user="$(id -un)" || stop "Couldn’t determine this account. Run the installer again."
    linger="$(loginctl show-user "$user" -p Linger --value)" \
        || stop "Couldn’t check whether this account starts services at boot. Check systemd, then run this again."
    [ "$linger" != yes ] || return 0
    [ "$linger" = no ] || stop "Couldn’t check whether this account starts services at boot. Check systemd, then run this again."
    if [ "$(id -u)" -ne 0 ]; then
        say "     Enabling services at boot needs administrator access (sudo); you may be asked for your password."
        "$SUDO_COMMAND" -v < "$PROMPT_INPUT" \
            || stop "Enabling services at boot needs an administrator account. Sign in with one, then run this again."
    fi
    as_admin loginctl enable-linger "$user" \
        || stop "Couldn’t enable services at boot. Check systemd, then run this again."
    say "     Enabled services at boot for $user. To undo: sudo loginctl disable-linger $user"
}

linux_ensure_prereqs() {
    say "1/3  Checking prerequisites"
    PREREQS=(
        "git|apt:git|git"
        "Python 3.11|uv|python3.11"
        "Node|node|node"
        "GitHub CLI|apt:gh|gh"
    )
    check_prereqs
    [ "${#INVISIBLE[@]}" -eq 0 ] || stop "${INVISIBLE[@]}"
    linux_install_needed
    check_prereqs
    [ "${#INVISIBLE[@]}" -eq 0 ] || stop "${INVISIBLE[@]}"
    [ "${#NEEDED[@]}" -eq 0 ] || stop "Still not ready: ${NEEDED[*]%%|*}. Install it by hand, then run this again."
}

link_corsia_command_linux() {
    local command_path="$LINUX_LOCAL_BIN/corsia"
    mkdir -p "$LINUX_LOCAL_BIN"
    if [ -e "$command_path" ] || [ -L "$command_path" ]; then
        if link_points_into_corsia "$command_path"; then
            rm "$command_path"
        else
            say "     A different corsia command is already at $command_path."
            return 1
        fi
    fi
    ln -s "$CORSIA_HOME/scripts/corsia" "$command_path" \
        || stop "Couldn’t link the corsia command in $LINUX_LOCAL_BIN."
    CORSIA_COMMAND_READY=1
}

open_browser_linux() {
    if command -v xdg-open > /dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        say "Opening CORSIA at $APP_URL"
        xdg-open "$APP_URL" > /dev/null 2>&1 || say "     Open that address in your browser."
    else
        say "CORSIA is running at $APP_URL"
        say "     Open that address in a browser."
    fi
}

linux_main() {
    local path_had_local_bin=1
    export CORSIA_INSTALL_LINUX=1
    say "CORSIA install"
    require_checkout
    check_platform_linux
    open_prompt_input
    case ":$PATH:" in *":$LINUX_LOCAL_BIN:"*) ;; *) path_had_local_bin=0 ;; esac
    export PATH="$LINUX_LOCAL_BIN:$PATH"
    linux_ensure_prereqs
    linux_ensure_agent_browser
    enable_linger_linux
    say "     Checking the protected Linux command sandbox…"
    bash "$CORSIA_HOME/scripts/install-linux-sandbox.sh"
    git -C "$CORSIA_HOME" rev-parse --git-dir > /dev/null 2>&1 \
        || stop "$CORSIA_HOME exists but is not a CORSIA checkout. Move it aside, then run this again."
    say "2/3  Using CORSIA $(installed_version) in ~/.corsia"
    ensure_python_env
    link_corsia_command_linux || true
    if [ "$CORSIA_COMMAND_READY" -eq 1 ]; then
        say "     Stop or start CORSIA any time with: corsia stop / corsia start"
        [ "$path_had_local_bin" -eq 1 ] \
            || say "     To use the corsia command, log out and back in, or run: export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
    if is_running; then
        say "3/3  CORSIA is running at $APP_URL"
        say "     To update, use Settings › Update."
        exit 0
    fi
    # The first run asks its questions inside start, from the terminal.
    CORSIA_INSTALL_FLOW=1 "$CORSIA_HOME/scripts/corsia" start <&3 3<&-
    say "     There is no local model on Linux: connect Claude Code, Codex or an API provider in the app."
    summarize_sources
    open_browser_linux
}

main() {
    parse_args "$@"
    case "$(uname -s)" in Linux) linux_main; return ;; esac
    say "CORSIA install"
    require_checkout
    check_platform
    open_prompt_input
    ensure_prereqs
    git -C "$CORSIA_HOME" rev-parse --git-dir > /dev/null 2>&1 \
        || stop "$CORSIA_HOME exists but is not a CORSIA checkout. Move it aside, then run this again."
    say "2/8  Using CORSIA $(installed_version) in ~/.corsia"
    ensure_python_env
    local brew
    if brew="$(find_brew)"; then
        add_homebrew_shellenv "$brew"
        link_corsia_command "$brew" || true
    fi
    [ "$CORSIA_COMMAND_READY" -eq 1 ] \
        && say "     Stop or start CORSIA any time with: corsia stop / corsia start"
    [ "$HOMEBREW_SHELLENV_ADDED" -eq 1 ] && say "     Open a new Terminal window first."
    if is_running; then
        say "     CORSIA is running at $APP_URL"
        say "     To update, use Settings › Update."
        [ "$LOCAL_SETUP_FORCED" -eq 1 ] && setup_local_model
        exit 0
    fi
    ensure_model_source
    start_corsia
    summarize_sources
    warm_corsia_agent
    open_browser
}

if [ "${CORSIA_INSTALL_SOURCE_ONLY:-}" != 1 ]; then
    main "$@"
fi
