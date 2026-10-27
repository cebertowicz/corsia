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
    bwrap --ro-bind / / --unshare-pid --proc /proc -- /bin/true >/dev/null 2>&1
}

if probe; then
    printf '%s\n' 'Linux sandbox ready; no AppArmor change needed.'
    exit 0
fi
sudo apt-get install --yes --no-install-recommends bubblewrap
if probe; then
    printf '%s\n' 'Installed bubblewrap; remove with sudo apt remove bubblewrap. No AppArmor change needed.'
    exit 0
fi
sudo apt-get install --yes --no-install-recommends apparmor-profiles
profile=/usr/share/apparmor/extra-profiles/bwrap-userns-restrict
target=/etc/apparmor.d/corsia-bwrap-userns-restrict
if [ ! -f "$profile" ]; then
    printf '%s\n' 'Linux sandbox refused: the approved bwrap-userns-restrict profile is unavailable.' >&2
    exit 1
fi
sudo install -m 0644 "$profile" "$target"
if ! sudo apparmor_parser -r "$target" || ! probe; then
    sudo apparmor_parser -R "$target" || true
    sudo rm -f "$target"
    printf '%s\n' 'Linux sandbox refused: the approved AppArmor profile could not enable startup. No permissive fallback was installed.' >&2
    exit 1
fi
printf '%s\n' 'Installed bubblewrap and enabled the packaged bwrap-userns-restrict profile; remove the rule with sudo apparmor_parser -R /etc/apparmor.d/corsia-bwrap-userns-restrict && sudo rm /etc/apparmor.d/corsia-bwrap-userns-restrict.'
