"""Protected-mode host path policy.

Reviewed actions consume their exact permit instead of consulting this policy.
Without a permit, direct filesystem actions and the terminal sandbox share this
inventory so protected execution remains consistent across tools.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import tempfile
from collections.abc import Callable
from typing import Iterable

from tools.host_path_resolution import (
    canonical,
    contains,
    denied_paths_in,
    resolution_touches,
)


_CONTROL_FILES = (
    Path("/tmp/corsia-restart"),
    Path("/tmp/corsia-rebuild-ui"),
    Path("/tmp/corsia-restart.log"),
)
_RAW_DEVICE_RE = re.compile(r"^/dev/(?:r?disk)[^/]*$")
_CORSIA_PLIST_RE = re.compile(r"corsia.*\.plist$", re.IGNORECASE)
GENERATED_WORKSPACE_PROJECTIONS = (
    Path(".corsia/context"),
    Path(".corsia/skills"),
)

# Hard-floor denials are not lease-eligible. Saying so stops an agent from
# requesting a lease that can never open the path.
_NO_LEASE = " No host-access lease can open it."
_PRIVATE_STATE_DENIAL = "CORSIA and Safehouse private state is blocked in protected mode." + _NO_LEASE
_CREDENTIAL_DENIAL = "Stored secrets are closed to every agent route." + _NO_LEASE

def _sandbox_text(value: Path | str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _sandbox_regex(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True, slots=True)
class ProtectedHostPathPolicy:
    """Canonical protected-mode path policy for host-capable tools."""

    home: Path
    state_root: Path
    generated_profile: Path | None = None
    safehouse_paths: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "home", canonical(self.home))
        object.__setattr__(self, "state_root", canonical(self.state_root))
        if self.generated_profile is not None:
            object.__setattr__(
                self,
                "generated_profile",
                canonical(self.generated_profile),
            )
        object.__setattr__(
            self,
            "safehouse_paths",
            tuple(dict.fromkeys(canonical(path) for path in self.safehouse_paths)),
        )
        object.__setattr__(
            self,
            "write_roots",
            tuple(dict.fromkeys(canonical(path) for path in self.write_roots)),
        )

    @property
    def ssh_root(self) -> Path:
        return self.home / ".ssh"

    @property
    def private_roots(self) -> tuple[Path, ...]:
        return (
            self.home / ".corsia",
            self.home / ".config" / "safehouse",
        )

    @property
    def credential_roots(self) -> tuple[Path, ...]:
        """Folders of stored secrets, closed to every agent route, leases included.

        The System hard floor covers protected credentials and a lease never
        opens it. Add a root here only with the project owner's approval.
        """
        return (self.state_root / "keys",)

    @property
    def control_files(self) -> tuple[Path, ...]:
        return tuple(
            canonical(path)
            for path in (
                *_CONTROL_FILES,
                self.state_root / "runtime" / "corsia-restart.signal",
                self.state_root / "logs" / "restart.log",
                self.state_root / "logs" / "self-restart.log",
            )
        )

    def hard_floor_denial(self, path: Path) -> str | None:
        """Return the non-leaseable denial when ``path`` reaches a credential root.

        Symlinks and ``..`` are traced component-wise, so a route into a
        credential root is refused as well as the canonical path.
        """
        try:
            candidate = canonical(path)
        except (OSError, RuntimeError):
            # A symlink loop reaches no credential root; the caller's ordinary
            # boundary still treats an unresolved loop as needing review.
            candidate = None
        for root in self.credential_roots:
            if candidate is not None and contains(root, candidate):
                return _CREDENTIAL_DENIAL
            if resolution_touches(root, path, loop_touches=False):
                return _CREDENTIAL_DENIAL
        return None

    def denial(self, path: Path, *, for_write: bool) -> str | None:
        """Return the protected-mode denial for ``path``, if any."""
        if message := self.hard_floor_denial(path):
            return message
        candidate = canonical(path)
        if any(contains(root, candidate) for root in self.private_roots):
            return _PRIVATE_STATE_DENIAL

        if contains(self.ssh_root, candidate):
            read_literals = {
                self.ssh_root / "config",
                self.ssh_root / "known_hosts",
                self.ssh_root / "known_hosts.old",
                self.ssh_root / "allowed_signers",
            }
            write_literals = {
                self.ssh_root / "known_hosts",
                self.ssh_root / "known_hosts.old",
            }
            if for_write and candidate in write_literals:
                return None
            if not for_write and (
                candidate in read_literals
                or contains(self.ssh_root / "config.d", candidate)
            ):
                return None
            return "SSH private material is blocked in protected mode." + _NO_LEASE

        if _RAW_DEVICE_RE.fullmatch(str(candidate)):
            return "Raw device access is blocked in protected mode." + _NO_LEASE

        temporary_roots = {
            canonical(Path(tempfile.gettempdir())),
            canonical(Path("/tmp")),
        }
        if (
            candidate.parent in temporary_roots
            and candidate.suffix == ".sb"
            and candidate.name.startswith(
                ("corsia-shell-envelope-", "corsia-controlled-git-", "corsia-mcp-envelope-")
            )
        ):
            return (
                "Generated Safehouse profiles are blocked in protected mode."
                + _NO_LEASE
            )

        if not for_write:
            return None

        if candidate in self.control_files:
            return (
                "CORSIA control-file writes are blocked in protected mode."
                + _NO_LEASE
            )
        if self.generated_profile is not None and candidate == self.generated_profile:
            return (
                "The active Safehouse profile is blocked in protected mode."
                + _NO_LEASE
            )
        if candidate in self.safehouse_paths:
            return (
                "Safehouse executable writes are blocked in protected mode."
                + _NO_LEASE
            )
        launch_agents = self.home / "Library" / "LaunchAgents"
        if candidate.parent == launch_agents and _CORSIA_PLIST_RE.search(
            candidate.name
        ):
            return (
                "CORSIA launch-agent files are blocked in protected mode."
                + _NO_LEASE
            )
        if candidate in {Path("/"), self.home}:
            return (
                "Root and home-directory writes are blocked in protected mode."
                + _NO_LEASE
            )
        return None

    def _state_denial(self) -> str:
        """Name the resolved state root and the one route that opens it.

        Naming the resolved path is the whole discovery win, and it holds on
        every installation. Anything that depends on how a particular machine
        is laid out belongs in that installation's agent context instead.
        """
        return (
            f"CORSIA runtime state ({self.state_root}) requires reviewed host "
            "access. Request a temporary host-access lease, then retry the "
            "exact action."
        )

    def ordinary_denial(self, path: Path, *, for_write: bool) -> str | None:
        """Return a denial that applies before reviewed lease elevation."""
        # Trace the lexical path first: Python 3.11 raises while resolving a
        # symlink loop, while this boundary deliberately treats an unresolved
        # loop as requiring reviewed host access.
        if message := self.hard_floor_denial(path):
            # Credential roots (the key store inside the state root) outrank
            # the lease-eligible state-root answer.
            return message
        if resolution_touches(self.state_root, path):
            return self._state_denial()
        candidate = canonical(path)
        if contains(self.state_root, candidate):
            return self._state_denial()
        return self.denial(candidate, for_write=for_write)

    def denial_guidance_for_output(self, text: str) -> str | None:
        """Name the refused path from tool output and its lease eligibility.

        The shell sandbox reports only ``Operation not permitted``, which tells
        an agent nothing about whether to apply for a lease or stop trying. The
        policy already words both outcomes, so reuse those exact messages
        rather than restating them.
        """
        return self.denial_guidance_for_paths(denied_paths_in(text))

    def denial_guidance_for_paths(self, paths: Iterable[Path]) -> str | None:
        """Name the first denied protected path from an already-parsed path list."""
        for path in paths:
            message = self.ordinary_denial(path, for_write=False) or self.denial(path, for_write=True)
            if message:
                return f"{path} — {message}"
        return None

    def hard_floor_sandbox_rules(self) -> str:
        """Render only the credential-root denies; leased profiles keep the rest open."""
        return "\n".join(
            f'(deny file-read* file-write* (subpath "{_sandbox_text(path)}"))'
            for path in self.credential_roots
        )

    def sandbox_rules(self) -> str:
        """Render protected Seatbelt rules from the protected path inventory."""
        ssh = self.ssh_root
        launch_agents = self.home / "Library" / "LaunchAgents"
        denied_roots = (self.state_root, *self.private_roots)
        lines = [
            f'(deny file-read* file-write* (subpath "{_sandbox_text(path)}"))'
            for path in denied_roots
        ]
        lines.append(self.hard_floor_sandbox_rules())
        if self.generated_profile is not None:
            lines.append(
                f"(deny file-read* file-write* "
                f'(literal "{_sandbox_text(self.generated_profile)}"))'
            )
        lines.extend(
            [
                *(
                    f'(deny file-write* (literal "{_sandbox_text(path)}"))'
                    for path in self.control_files
                ),
                *(
                    f'(deny file-write* process-exec (literal "{_sandbox_text(path)}"))'
                    for path in self.safehouse_paths
                ),
                *(
                    f'(deny file-write* (subpath "{_sandbox_text(path)}"))'
                    for path in self.write_roots
                ),
                (
                    f"(deny file-write* "
                    f'(regex #"^{_sandbox_regex(str(launch_agents))}/'
                    r'[^/]*[Mm][Aa][Ee][Ss][Tt][Rr][Oo][^/]*\.plist$"))'
                ),
                '(deny file-read* file-write* (regex #"^/dev/(r?disk|rdisk)[0-9].*"))',
                (
                    f'(deny file-write* (literal "/") '
                    f'(literal "{_sandbox_text(self.home)}"))'
                ),
                f'(deny file-read* file-write* (subpath "{_sandbox_text(ssh)}"))',
                f'(allow file-read-metadata (literal "{_sandbox_text(ssh)}"))',
                (
                    f"(allow file-read* "
                    f'(literal "{_sandbox_text(ssh / "config")}") '
                    f'(literal "{_sandbox_text(ssh / "known_hosts")}") '
                    f'(literal "{_sandbox_text(ssh / "known_hosts.old")}") '
                    f'(literal "{_sandbox_text(ssh / "allowed_signers")}") '
                    f'(subpath "{_sandbox_text(ssh / "config.d")}"))'
                ),
                (
                    f"(allow file-write* "
                    f'(literal "{_sandbox_text(ssh / "known_hosts")}") '
                    f'(literal "{_sandbox_text(ssh / "known_hosts.old")}"))'
                ),
            ]
        )
        return "\n".join(lines)


def current_protected_host_path_policy(
    *,
    generated_profile: Path | None = None,
    safehouse_paths: Iterable[Path] = (),
    write_roots: Iterable[Path] = (),
) -> ProtectedHostPathPolicy:
    """Build the live policy without caching mutable environment paths."""
    from infra.config import STATE_ROOT

    discovered = shutil.which("safehouse")
    paths = tuple(safehouse_paths)
    if discovered is not None:
        paths = (*paths, Path(discovered))
    return ProtectedHostPathPolicy(
        home=Path.home(),
        state_root=STATE_ROOT,
        generated_profile=generated_profile,
        safehouse_paths=paths,
        write_roots=tuple(write_roots),
    )


def ordinary_host_path_denial(
    path: Path,
    *,
    for_write: bool,
) -> str | None:
    """Apply protected-execution denial before any lease permit is consumed."""
    return current_protected_host_path_policy().ordinary_denial(
        path,
        for_write=for_write,
    )


def path_review_classification(
    *paths: Path,
    for_write: bool,
) -> tuple[bool, str | None]:
    """Classify the lexical and resolved forms of one host action path.

    Returns ``(needs_review, hard_floor_denial)``. A hard-floor denial wins and
    is never lease-eligible; otherwise ``needs_review`` says whether any form
    is refused in protected mode and so needs a reviewed lease.
    """
    forms = tuple(dict.fromkeys(paths))
    policy = current_protected_host_path_policy()
    for form in forms:
        if message := policy.hard_floor_denial(form):
            return True, message
    return any(
        ordinary_host_path_denial(form, for_write=for_write) for form in forms
    ), None


def denial_guidance_for_output(text: str) -> str | None:
    """Explain a sandbox refusal found in ``text`` against the live policy."""
    return current_protected_host_path_policy().denial_guidance_for_output(text)


def denial_guidance_prefix(text: str) -> str:
    """Return that explanation as a block ready to prepend, or empty."""
    guidance = denial_guidance_for_output(text)
    return f"{guidance}\n" if guidance else ""


def skip_credential_roots(root: Path, skip_directory: Callable[[Path], bool]) -> Callable[[Path], bool]:
    """Extend ``skip_directory`` to refuse credential roots beneath ``root``; fails closed."""
    base = canonical(root)
    pruned = {
        canonical(path).relative_to(base)
        for path in current_protected_host_path_policy().credential_roots
        if canonical(path).is_relative_to(base) and canonical(path) != base
    }
    return lambda relative: relative in pruned or skip_directory(relative)
