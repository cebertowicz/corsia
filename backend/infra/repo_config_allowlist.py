"""The fixed allowlist of repository-local Git config keys an agent may register.

A repository's own Git config can name a program Git will run: ``core.fsmonitor``,
``filter.<name>.clean``, ``diff.<name>.textconv``, ``core.sshCommand``, ``core.pager``,
or a key that pulls a further file in. After a repository is registered the backend
runs Git inside it outside the agent sandbox, so an agent that writes such a key into
a checkout and then registers it would have that program run as the backend. The
shared override set (``infra.backend_git.GIT_CONFIG_ARGS``) neutralises hooks,
fsmonitor, the ext transport and submodule recursion on every backend Git call, but
driver names are open-ended and cannot be pre-listed there, so a repository's config
is checked once instead: at agent registration, before any Git command runs in it.

Every agent route that registers a repository calls :func:`repo_config_refusal`.
The person's own Project-settings registration is deliberately not checked — those
are the person's repositories, and the refusal points there when an agent is refused.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

from host_access.credential_sanitizer import sanitize_credential_text
from infra.backend_git import git_argv

_CONFIG_TIMEOUT_SECONDS = 5

# The whole allowlist. Anything not listed here is refused, so a key Git learns to
# read tomorrow is refused until it is deliberately admitted.
_CORE_KEYS = frozenset({
    "core.bare",
    "core.filemode",
    "core.ignorecase",
    "core.logallrefupdates",
    "core.precomposeunicode",
    "core.repositoryformatversion",
})

# Keys whose name carries a repository-chosen subsection: ``remote.<name>.url``.
_SECTION_VARIABLES: dict[str, frozenset[str]] = {
    "remote": frozenset({"url", "fetch", "pushurl", "tagopt"}),
    "branch": frozenset({"remote", "merge"}),
}

# Any variable written directly under this section.
_OPEN_SECTIONS = frozenset({"user"})

_UNREADABLE_REFUSAL = (
    "Not registered: this repository's Git config could not be read or parsed, so "
    "CORSIA cannot check it. Repair its .git/config, or add the repository yourself "
    "in Project settings."
)


def _key_is_allowed(name: str) -> bool:
    """Return whether one Git-canonical key name is on the allowlist.

    Git prints each key as ``section[.<subsection>].<variable>`` with the section and
    variable lower-cased (it reads them case-insensitively too). A subsection may
    itself contain dots, so the parts come from the first and the last dot only. A
    subsection is expected only where the allowlist names one, and an unreadable
    shape is refused rather than guessed at.
    """
    section, sectioned, remainder = name.partition(".")
    if not sectioned:
        return False
    subsection, dotted, variable = remainder.rpartition(".")
    if not dotted:
        subsection, variable = "", remainder
    if not variable:
        return False
    if subsection:
        allowed = _SECTION_VARIABLES.get(section)
        return allowed is not None and variable in allowed
    if section in _OPEN_SECTIONS:
        return True
    return f"{section}.{variable}" in _CORE_KEYS


def _read_pointer(pointer: Path) -> str | None:
    """Return a Git pointer file's content, or None when it cannot be read."""
    try:
        return pointer.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _local_config_files(repo: Path) -> list[Path] | None:
    """Return the repository's own config files to check; None when unresolvable.

    Resolves ``.git`` as a directory or a ``gitdir:`` file, then the common directory
    through ``commondir`` for a linked worktree, so the config Git would use is the
    config that gets checked. Global and system config is never read: those are the
    person's files. A checkout with no ``.git`` has nothing of its own to run, and
    returns no files.
    """
    marker = repo / ".git"
    if not marker.exists():
        return []
    git_dir: Path | None = None
    if marker.is_dir():
        git_dir = marker.resolve(strict=False)
    elif marker.is_file():
        raw = _read_pointer(marker)
        git_dir = None if raw is None else _pointer_target(marker, raw)
    if git_dir is None:
        return None

    common_dir = git_dir
    commondir = git_dir / "commondir"
    if commondir.is_file():
        raw = _read_pointer(commondir)
        if raw is None:
            return None
        resolved = _pointer_target(commondir, raw)
        if resolved is None:
            return None
        common_dir = resolved

    files: list[Path] = []
    for path in (
        common_dir / "config",
        common_dir / "config.worktree",
        git_dir / "config.worktree",
    ):
        if path == common_dir / "config" or path.is_file():
            if path not in files:
                files.append(path)
    return files


def _pointer_target(pointer: Path, raw: str) -> Path | None:
    """Resolve one Git pointer file's target, relative to the file's own folder.

    The ``.git`` file of a linked checkout writes ``gitdir: <path>``, `commondir`
    writes the path alone; both may be relative to the folder holding the pointer.
    Git reads the path literally — no ``~`` expansion — so this does too, or the
    check could read a different config from the one Git then uses.
    """
    target = raw.strip()
    if target.startswith("gitdir:"):
        target = target[len("gitdir:"):].strip()
    if not target or "\n" in target:
        return None
    path = Path(target)
    return (path if path.is_absolute() else pointer.parent / path).resolve(strict=False)


def _config_key_names(config_file: Path) -> list[str] | None:
    """Return the key names Git reads from one config file; None when unreadable.

    Git's own parser decides the list, because a key this check cannot see is a key no
    comparison could refuse. ``--file`` reads that file alone — never global or system
    config — and lists its keys without evaluating any of their values, so nothing the
    repository names is run. Includes are not followed; an ``include`` or ``includeIf``
    section is refused like any other key off the allowlist.
    """
    try:
        result = subprocess.run(
            git_argv(
                "config",
                "--file",
                str(config_file),
                "--list",
                "--name-only",
                "--no-includes",
                "--null",
            ),
            capture_output=True,
            text=True,
            timeout=_CONFIG_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return [name for name in result.stdout.split("\0") if name]


def _refusal_line(keys: list[str]) -> str:
    """Return the refusal naming the first three keys in order, then a count."""
    listed = ", ".join(keys[:3])
    # The key names come from _scan_repo_config already credential-masked.
    if len(keys) == 1:
        return (
            f"Not registered: this repository's Git config sets {listed}, which isn't "
            "among the Git settings an agent may register (some settings can run a "
            f"program). Remove it (git config --local --unset {listed}), or add the "
            "repository yourself in Project settings."
        )
    extra = f" and {len(keys) - 3} more" if len(keys) > 3 else ""
    return (
        f"Not registered: this repository's Git config sets {listed}{extra}, which "
        "isn't among the Git settings an agent may register (some settings can run a "
        "program). Remove each one (git config --local --unset <key>), or add the "
        "repository yourself in Project settings."
    )


def _scan_repo_config(repo: Path) -> tuple[list[str], bool]:
    """Return this repository's refused key names, sorted, and whether a config
    file could not be read.

    Reading is the expensive part and both public entry points need it, so they share
    this scan rather than each walking the repository's own files again.
    """
    config_files = _local_config_files(repo)
    if config_files is None:
        return [], True

    offenders: set[str] = set()
    unreadable = False
    for config_file in config_files:
        names = _config_key_names(config_file)
        if names is None:
            unreadable = True
            continue
        offenders.update(name for name in names if not _key_is_allowed(name))
    # A subsection can hold a URL with credentials in it (``url.<url>.insteadof``),
    # so a key name is masked like any other text before it is reported.
    return sorted({sanitize_credential_text(name) for name in offenders}), unreadable


def refused_repo_config_keys(repo_path: Path) -> list[str]:
    """Return every key name of this repository's own Git config that falls outside
    the registration allowlist, sorted, with no cap.

    Key names only, never values — a value can hold a token or a path. The whole list
    is for the person's Settings surface; the agent refusal is capped by
    :func:`repo_config_refusal`. An unreadable config yields no key names.
    """
    repo = repo_path.expanduser().resolve(strict=False)
    return _scan_repo_config(repo)[0]


def repo_config_refusal(repo_path: Path) -> str | None:
    """Return one refusal line when this repository's own Git config is unsafe to
    register for an agent, or None when it passes.

    The single entry point every agent registration route calls, including the
    automatic ``source/`` registration. It reads and never writes, runs nothing the
    repository names, and reports key names only — a value can hold a token or a path.
    """
    repo = repo_path.expanduser().resolve(strict=False)
    keys, unreadable = _scan_repo_config(repo)
    if unreadable:
        return _UNREADABLE_REFUSAL
    return None if not keys else _refusal_line(keys)


__all__ = ["refused_repo_config_keys", "repo_config_refusal"]
