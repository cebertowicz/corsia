"""Run backend Git without executing programs named in a repository's own config.

The backend runs Git outside the agent sandbox on repositories an agent may have
built. A repository's local or worktree config (and any file it ``include``s)
can name programs Git then runs as the backend: a filesystem monitor, an ssh
command, a credential helper, a content filter and so on. Command-line config
beats every file, so each such local value is overridden:

* with the person's own system, global or command-scope value when there is
  one, so their ssh command, credential helper or git-lfs filters keep working;
* otherwise with a neutral value that runs nothing.

Nothing is refused. ``git config --list`` failing degrades only the extra
neutralisation: the fixed overrides below still apply and the command still runs.

A submodule carries its own config, which no override of the parent reaches, so
fetch and status are given command-line flags that keep submodules out of the
call. A timeout is re-raised naming only the caller's own arguments, because
callers format that text into a message an agent reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import shlex
import subprocess
from pathlib import Path

_PERSON_SCOPES = frozenset({"system", "global", "command"})
_LOCAL_SCOPES = frozenset({"local", "worktree"})
_FILTER_RE = re.compile(r"^filter\.(?P<name>.+)\.(?P<part>clean|smudge|process)$")
_HELPER_RE = re.compile(r"^credential\.(?:.+\.)?helper$")
_UPLOADPACK_RE = re.compile(r"^remote\.(?P<name>.+)\.uploadpack$")
_VCS_RE = re.compile(r"^remote\..+\.vcs$")
# Standard git-lfs filter commands only run the trusted ``git-lfs`` binary; a
# repository that installed LFS locally keeps its filters.
_LFS_FILTERS = frozenset({
    "git-lfs clean -- %f", "git-lfs smudge -- %f", "git-lfs smudge --skip -- %f",
    "git-lfs filter-process", "git-lfs filter-process --skip",
})
_CONFIG_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class ConfigEntry:
    """One effective config value and the scope it came from."""

    scope: str
    key: str
    value: str | None


@dataclass(frozen=True)
class GitGuard:
    """The overrides one Git call carries."""

    config: tuple[tuple[str, str], ...]
    env: dict[str, str] = field(default_factory=dict)
    upload_pack_remotes: frozenset[str] = frozenset()

    def config_args(self) -> list[str]:
        """Return the ``-c`` arguments for every override a ``-c`` can express."""
        return [arg for k, v in self.config if "=" not in k for arg in ("-c", f"{k}={v}")]

    def environment(self) -> dict[str, str] | None:
        """Return the child environment, or None when it needs no change."""
        odd = [(k, v) for k, v in self.config if "=" in k]
        if not odd and not self.env:
            return None
        env = {**os.environ, **self.env}
        try:
            count = int(env.get("GIT_CONFIG_COUNT") or 0)
        except ValueError:
            count = 0
        for key, value in odd:  # a key containing "=" cannot be written as ``-c``
            env[f"GIT_CONFIG_KEY_{count}"], env[f"GIT_CONFIG_VALUE_{count}"] = key, value
            count += 1
        env["GIT_CONFIG_COUNT"] = str(count)
        return env


def read_config(repo: Path) -> list[ConfigEntry] | None:
    """Return every effective config entry with its scope, or None if unreadable."""
    try:
        done = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "config", "--list", "--show-scope", "-z"],
            cwd=str(repo), capture_output=True, timeout=_CONFIG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    parts = done.stdout.decode("utf-8", "surrogateescape").split("\0")
    entries = []
    for scope, item in zip(parts[0::2], parts[1::2]):  # records: scope NUL key LF value NUL
        key, sep, value = item.partition("\n")
        entries.append(ConfigEntry(scope, key, value if sep else None))
    return entries


def _ssh_neutral(environ: dict[str, str]) -> str:
    """Plain ssh, or the person's GIT_SSH program that a core.sshCommand would hide."""
    legacy = environ.get("GIT_SSH")
    return shlex.quote(legacy) if legacy else "ssh"


_NEUTRAL_KEYS = {
    "core.askpass": "",
    "core.alternaterefscommand": "true",  # Git cannot unset a key; ``true`` lists no tips
    "merge.verifysignatures": "false",
    "gpg.program": "gpg",
    "gpg.openpgp.program": "gpg",
    "gpg.x509.program": "gpgsm",
    "gpg.ssh.program": "ssh-keygen",
}


def _neutral(key: str, value: str | None, environ: dict[str, str]) -> str | None:
    """The value that runs nothing for a program-naming key, or None to leave it."""
    if key == "core.sshcommand":
        return _ssh_neutral(environ)
    if key in _NEUTRAL_KEYS:
        return _NEUTRAL_KEYS[key]
    if _FILTER_RE.match(key) and value not in _LFS_FILTERS:
        return ""  # an empty filter command means no filter
    if key.lower().startswith("alias.remote-"):
        return ""  # Git starts a remote helper as ``git remote-<name>``, which aliases answer
    if _VCS_RE.match(key) and value and "/" in value:
        return ""  # a helper name with a slash is a path, run from the repository
    return None


def _ext_policy(person: list[ConfigEntry]) -> str:
    """The person's own ext transport policy; the ext transport is off otherwise."""
    for wanted in ("protocol.ext.allow", "protocol.allow"):
        values = [e.value for e in person if e.key == wanted and e.value]
        if values:
            return values[-1]
    return "never"


def _single_overrides(
    local: list[ConfigEntry], person: list[ConfigEntry], environ: dict[str, str],
) -> list[tuple[str, str]]:
    """Override each program-naming key a repository sets: person value, else neutral."""
    out: list[tuple[str, str]] = []
    done: set[str] = set()
    for entry in local:
        neutral = _neutral(entry.key, entry.value, environ)
        if neutral is None or entry.key in done:
            continue
        done.add(entry.key)
        mine = [e.value for e in person if e.key == entry.key and e.value is not None]
        out.append((entry.key, mine[-1] if mine else neutral))
        match = _FILTER_RE.match(entry.key)
        if match and not mine and neutral == "":
            # A required filter with no command would fail every command.
            out.append((f"filter.{match['name']}.required", "false"))
    return out


def _helper_overrides(local: list[ConfigEntry], person: list[ConfigEntry]) -> list[tuple[str, str]]:
    """Reset the helper list (url-scoped helpers share it), then re-add the person's."""
    if not any(_HELPER_RE.match(e.key) for e in local):
        return []
    again = [(e.key, e.value) for e in person if _HELPER_RE.match(e.key) and e.value is not None]
    return [("credential.helper", ""), *again]


def build_guard(
    entries: list[ConfigEntry] | None, environ: dict[str, str] | None = None,
) -> GitGuard:
    """Compute the overrides for one repository's config; fixed ones come first."""
    environ = dict(os.environ if environ is None else environ)
    person = [e for e in entries or [] if e.scope in _PERSON_SCOPES]
    local = [e for e in entries or [] if e.scope in _LOCAL_SCOPES]
    config = [
        ("core.hooksPath", "/dev/null"), ("core.fsmonitor", "false"),
        ("protocol.ext.allow", _ext_policy(person)),
        # A submodule has its own config this call never reads, so no command
        # may recurse into it. Fixed, so it holds with an unreadable config too.
        ("submodule.recurse", "false"),
        *_single_overrides(local, person, environ),
        *_helper_overrides(local, person),
    ]
    if any(e.key == "gc.recentobjectshook" for e in local):
        # The hook is a list a later value cannot remove; it runs from the
        # automatic housekeeping a fetch starts, so that is switched off.
        config += [("gc.auto", "0"), ("maintenance.auto", "false")]
    env: dict[str, str] = {}
    if (
        any(e.key == "core.gitproxy" for e in local)
        and not any(e.key == "core.gitproxy" for e in person)
        and "GIT_PROXY_COMMAND" not in environ
    ):
        env["GIT_PROXY_COMMAND"] = ""  # the environment beats config; first value wins in config
    # The first remote.<name>.uploadpack wins, so a person's own value already
    # beats the repository's; only an unshadowed local one needs a fetch option.
    shadowed = {e.key for e in person}
    remotes = frozenset(
        m["name"] for e in local if (m := _UPLOADPACK_RE.match(e.key)) and e.key not in shadowed
    )
    if remotes:
        env["GIT_NO_LAZY_FETCH"] = "1"  # the fetch of a missing object takes no option
    return GitGuard(tuple(config), env, remotes)


def guard_for(repo: Path) -> GitGuard:
    """Return the guard for ``repo``; an unreadable config keeps only the fixed overrides."""
    return build_guard(read_config(repo))


def _submodule_argv(argv: list[str], guard: GitGuard) -> list[str]:
    """Keep submodules out of the call: their own config is never read here.

    Fetch never recurses and uses the stock upload-pack when the repository
    names its own; every caller names the remote, so a bare fetch is not covered.
    Status ignores submodule state, because it spawns a child
    process inside each populated submodule, where that submodule's own
    filters would otherwise run. The command-line flag is used rather than
    ``diff.ignoreSubmodules`` because a repository-set ``submodule.<name>.ignore``
    overrides that config key."""
    if not argv:
        return argv
    if argv[0] == "fetch":
        extra = ["--no-recurse-submodules"]
        if any(arg in guard.upload_pack_remotes for arg in argv[1:]):
            extra.append("--upload-pack=git-upload-pack")
    elif argv[0] == "status":
        extra = ["--ignore-submodules=dirty"]
    else:
        return argv
    return [argv[0], *extra, *argv[1:]]


def guarded_run(repo: Path, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run ``git <argv>`` in ``repo`` with the repository's program-naming config overridden.

    Raises what ``subprocess.run`` raises, so callers keep their own handling.

    A timeout names the caller's own arguments only: the override list can carry
    the person's credential helper or ssh command value, and callers format this
    exception's text into a message an agent reads.
    """
    guard = guard_for(repo)
    try:
        return subprocess.run(
            ["git", *guard.config_args(), *_submodule_argv(argv, guard)],
            cwd=str(repo), capture_output=True, text=True, timeout=timeout,
            env=guard.environment(),
        )
    except subprocess.TimeoutExpired as exc:
        # ``from None``: the cause's args carry the same argv, and a traceback
        # printed elsewhere would show the overrides to an agent as well.
        raise subprocess.TimeoutExpired(["git", *argv], timeout) from None
