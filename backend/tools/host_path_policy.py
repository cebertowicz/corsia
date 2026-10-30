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
from collections.abc import Callable
from typing import Iterable

from tools.host_path_resolution import (
    canonical,
    contains,
    denied_paths_in,
    resolution_touches,
)
from tools.launch_agent_plists import corsia_plist_deny_rule, is_corsia_plist
from tools.protected_path_rules import ancestor_pins
from tools.sandbox_git import sandbox_literal
from tools.workspace_git_paths import is_workspace_git_control, workspace_git_paths, workspace_git_rules


_CONTROL_FILES = (
    Path("/tmp/corsia-restart"),
    Path("/tmp/corsia-rebuild-ui"),
    Path("/tmp/corsia-restart.log"),
)
_RAW_DEVICE_RE = re.compile(r"^/dev/(?:r?disk)[^/]*$")
GENERATED_WORKSPACE_PROJECTIONS = (Path(".corsia/context"), Path(".corsia/skills"))
# Hard-floor denials cannot be opened by a host-access lease.
_NO_LEASE = " No host-access lease can open it."
_PRIVATE_STATE_DENIAL = "CORSIA and Safehouse private state is blocked in protected mode." + _NO_LEASE
_CREDENTIAL_DENIAL = "Stored secrets are closed to every agent route." + _NO_LEASE

@dataclass(frozen=True, slots=True)
class ProtectedHostPathPolicy:
    """Canonical protected-mode path policy for host-capable tools."""

    home: Path
    state_root: Path
    generated_profile: Path | None = None
    safehouse_paths: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()
    workspace: Path | None = None
    granted_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "home", canonical(self.home))
        object.__setattr__(self, "state_root", canonical(self.state_root))
        if self.workspace is not None:
            object.__setattr__(self, "workspace", canonical(self.workspace))
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
    def generated_roots(self) -> tuple[Path, ...]:
        """Workspace projections the backend regenerates; agents never write them."""
        if self.workspace is None:
            return ()
        return tuple(canonical(self.workspace / path) for path in GENERATED_WORKSPACE_PROJECTIONS)

    @property
    def ancestor_pins(self) -> tuple[Path, ...]:
        """Ancestors of every protected path inside a granted write root."""
        return ancestor_pins(
            (*self.generated_roots, *self.write_roots, *self.control_files),
            (root for root in (self.workspace, *self.granted_roots) if root is not None),
            (Path("/"), self.home),
        )

    @property
    def credential_roots(self) -> tuple[Path, ...]:
        """Folders of stored secrets, closed to every agent route, leases included.

        The System hard floor covers protected credentials and a lease never
        opens it. Add a root here only with the project owner's approval.
        """
        return (self.state_root / "keys", self.state_root / "browser-profile")

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

    def hard_floor_denial(self, path: Path, *, for_write: bool = False) -> str | None:
        """Return the non-leaseable denial for a credential root or a workspace Git write.

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
        if for_write and candidate is not None and is_workspace_git_control(self.workspace, candidate):
            return "Workspace Git settings and hooks are blocked in protected mode." + _NO_LEASE
        return None

    def denial(self, path: Path, *, for_write: bool) -> str | None:
        """Return the protected-mode denial for ``path``, if any."""
        if message := self.hard_floor_denial(path, for_write=for_write):
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

        if not for_write:
            return None

        if any(contains(root, candidate) for root in self.generated_roots):
            return "CORSIA manages the context and skills files under .corsia, so they cannot be changed in protected mode."

        if candidate in self.control_files:
            return (
                "CORSIA control-file writes are blocked in protected mode."
                + _NO_LEASE
            )
        if self.generated_profile is not None and contains(self.generated_profile, candidate):
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
        if candidate.parent == launch_agents and is_corsia_plist(candidate.name):
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
        if message := self.hard_floor_denial(path, for_write=for_write):
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
        """Render permanent credential and browser boundaries, including under a lease."""
        from tools.host_sandbox_floor import hard_floor_rules
        floor = hard_floor_rules(self.credential_roots, sandbox_literal)
        return "\n".join([floor, *workspace_git_rules(workspace_git_paths(self.workspace))])

    def sandbox_rules(self) -> str:
        """Render protected Seatbelt rules from the protected path inventory."""
        ssh = self.ssh_root
        launch_agents = self.home / "Library" / "LaunchAgents"
        denied_roots = (self.state_root, *self.private_roots)
        lines = [
            f'(deny file-read* file-write* (subpath "{sandbox_literal(path)}"))'
            for path in denied_roots
        ]
        lines.append(self.hard_floor_sandbox_rules())
        if self.generated_profile is not None:
            lines.append(
                f"(deny file-read* file-write* "
                f'(literal "{sandbox_literal(self.generated_profile)}"))'
            )
        lines.extend(
            [
                *(
                    f'(deny file-write* (literal "{sandbox_literal(path)}"))'
                    for path in self.control_files
                ),
                *(
                    f'(deny file-write* process-exec (literal "{sandbox_literal(path)}"))'
                    for path in self.safehouse_paths
                ),
                *(
                    f'(deny file-write* (subpath "{sandbox_literal(path)}"))'
                    for path in dict.fromkeys((*self.write_roots, *self.generated_roots))
                ),
                *(
                    f'(deny file-write* (literal "{sandbox_literal(path)}"))'
                    for path in self.ancestor_pins
                ),
                corsia_plist_deny_rule(sandbox_literal(launch_agents)),
                '(deny file-read* file-write* (regex #"^/dev/(r?disk|rdisk)[0-9].*"))',
                (
                    f'(deny file-write* (literal "/") '
                    f'(literal "{sandbox_literal(self.home)}"))'
                ),
                f'(deny file-read* file-write* (subpath "{sandbox_literal(ssh)}"))',
                f'(allow file-read-metadata (literal "{sandbox_literal(ssh)}"))',
                (
                    f"(allow file-read* "
                    f'(literal "{sandbox_literal(ssh / "config")}") '
                    f'(literal "{sandbox_literal(ssh / "known_hosts")}") '
                    f'(literal "{sandbox_literal(ssh / "known_hosts.old")}") '
                    f'(literal "{sandbox_literal(ssh / "allowed_signers")}") '
                    f'(subpath "{sandbox_literal(ssh / "config.d")}"))'
                ),
                (
                    f"(allow file-write* "
                    f'(literal "{sandbox_literal(ssh / "known_hosts")}") '
                    f'(literal "{sandbox_literal(ssh / "known_hosts.old")}"))'
                ),
            ]
        )
        return "\n".join(lines)


def current_protected_host_path_policy(
    *,
    generated_profile: Path | None = None,
    safehouse_paths: Iterable[Path] = (),
    write_roots: Iterable[Path] = (),
    granted_roots: Iterable[Path] = (),
) -> ProtectedHostPathPolicy:
    """Build the live policy without caching mutable environment paths."""
    from infra.config import STATE_ROOT, WORKSPACE

    from tools.managed_runtime import application_cache_root

    discovered = shutil.which("safehouse")
    paths = tuple(safehouse_paths)
    if discovered is not None:
        paths = (*paths, Path(discovered))
    return ProtectedHostPathPolicy(
        home=Path.home(),
        state_root=STATE_ROOT,
        generated_profile=generated_profile,
        safehouse_paths=paths,
        write_roots=(application_cache_root(), *write_roots),
        workspace=WORKSPACE,
        granted_roots=tuple(granted_roots),
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
        if message := policy.hard_floor_denial(form, for_write=for_write):
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
