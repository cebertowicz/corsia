#!/bin/bash
# Install the protected Linux command sandbox. No permissive AppArmor fallback.
set -euo pipefail

probe() {
    command -v bwrap >/dev/null 2>&1 || return 1
    local version major minor
    version="$(bwrap --version 2>/dev/null)"
    version="${version##* }"
    IFS=. read -r major minor _ <<< "$version"
    [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] || return 1
    (( major > 0 || minor >= 8 )) || return 1
    bwrap --die-with-parent --new-session --unshare-user --disable-userns --unshare-pid --unshare-ipc --unshare-uts --ro-bind / / --proc /proc -- /bin/true >/dev/null 2>&1
}

if probe; then
    printf '%s\n' 'The sandbox for agents’ commands is ready.'
    exit 0
fi
sudo apt-get install --yes --no-install-recommends bubblewrap
if probe; then
    printf '%s\n' 'The sandbox for agents’ commands is ready. Bubblewrap was installed; remove it with sudo apt remove bubblewrap.'
    exit 0
fi
sudo apt-get install --yes --no-install-recommends apparmor-profiles
profile=/usr/share/apparmor/extra-profiles/bwrap-userns-restrict
target=/etc/apparmor.d/corsia-bwrap-userns-restrict
if [ ! -f "$profile" ]; then
    printf '%s\n' 'CORSIA couldn’t turn on the sandbox for agents’ commands because the bwrap-userns-restrict AppArmor profile is unavailable. Agents’ commands remain refused on this computer and setup stopped. Use a distribution that provides this profile, such as Ubuntu 24.04, or follow docs/linux-sandbox.md#sandbox-setup.' >&2
    exit 1
fi
sudo install -m 0644 "$profile" "$target"
if ! sudo apparmor_parser -r "$target" || ! probe; then
    sudo apparmor_parser -R "$target" || true
    sudo rm -f "$target"
    printf '%s\n' 'CORSIA couldn’t turn on the sandbox for agents’ commands with the installed AppArmor profile. Agents’ commands remain refused on this computer and setup stopped. Follow docs/linux-sandbox.md#sandbox-setup to check bubblewrap and AppArmor, then run the installer again.' >&2
    exit 1
fi
printf '%s\n' 'The sandbox for agents’ commands is ready. Bubblewrap and its AppArmor rule are installed; remove the rule with sudo apparmor_parser -R /etc/apparmor.d/corsia-bwrap-userns-restrict && sudo rm /etc/apparmor.d/corsia-bwrap-userns-restrict.'
