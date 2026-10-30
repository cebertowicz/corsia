"""Read-only reviewer access to one immutable PR revision."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Iterator
from pathlib import Path

from claude_controls import confined_claude_controls
from infra.backend_git import git_argv

_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_READER = Path(__file__).with_name("reviewer_read.py")
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_MIN_RUN_NAME = 16


class ExactSourceError(RuntimeError):
    """The reviewed base/head could not be grounded in the object store."""


def claude_exact_source_controls(
    git_dir: str, base_sha: str, head_sha: str,
) -> tuple[list[str], dict[str, str]]:
    """Return Claude's narrow exact-source tool and Git environment controls.

    The only command the model may run is ``reviewer_read.py`` with its object
    store and revisions fixed by the rule prefix. Raw Git is never allowed: a
    wildcard after a Git command would let the model add Git options.
    """
    reader = reader_command(git_dir, base_sha, head_sha)
    args = [
        *confined_claude_controls(),
        "--tools", "Bash",
        "--allowedTools",
        f"Bash({reader} stat)",
        f"Bash({reader} names)",
        f"Bash({reader} diff)",
        f"Bash({reader} diff *)",
        f"Bash({reader} show *)",
        f"Bash({reader} grep *)",
    ]
    env = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return args, env


def reader_command(git_dir: str, base_sha: str, head_sha: str) -> str:
    """Return the fixed shell prefix of every exact-source read."""
    _require_sha(base_sha, "base_sha")
    _require_sha(head_sha, "head_sha")
    resolved = str(Path(git_dir).resolve())
    parts = [sys.executable, "-I", str(_READER), resolved, base_sha, head_sha]
    return " ".join(shlex.quote(part) for part in parts)


@dataclass(frozen=True)
class ConfigStateScope:
    """The Claude config paths a reviewer run may write, besides its own dirs.

    ``root`` is the effective config directory, ``global_file`` the global
    config file, and ``run_name`` the unique basename of the run's working
    directory. The CLI names its per-working-directory state folder after that
    path with every non-alphanumeric character replaced by ``-``, so the run's
    folder is the one whose name ends with ``run_name``. ``temp_root`` is the
    CLI's own temp directory (``/tmp/claude-<uid>``, not ``TMPDIR``), where its
    Bash tool makes a folder named the same way before running any command.
    """

    root: Path
    global_file: Path
    run_name: str
    temp_root: Path
    allow_global_file: bool = False


def claude_config_state_scope(env: dict[str, str], run_dir: str) -> ConfigStateScope:
    """Resolve the config paths for a launch environment and run directory.

    ``CLAUDE_CONFIG_DIR`` is absent for the default account, whose root is
    ``~/.claude`` and whose global file is ``~/.claude.json``; a custom root
    keeps the global file inside it.
    """
    custom = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    if custom:
        root = Path(custom).expanduser().resolve()
        global_file = root / ".claude.json"
    else:
        home = Path(env.get("HOME") or Path.home()).expanduser()
        root = (home / ".claude").resolve()
        global_file = (home / ".claude.json").resolve()
    run_name = Path(run_dir).name
    if len(run_name) < _MIN_RUN_NAME or not run_name.isascii():
        raise ExactSourceError("reviewer run directory name is not unique enough")
    temp_root = Path("/tmp").resolve() / f"claude-{os.getuid()}"
    return ConfigStateScope(
        root=root, global_file=global_file, run_name=run_name, temp_root=temp_root,
    )


def _reject_control_chars(path: str) -> str:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        raise ExactSourceError("reviewer write path contains a control character")
    return path


def _sb_string(path: str) -> str:
    escaped = _reject_control_chars(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _sb_regex_path(path: str) -> str:
    if '"' in _reject_control_chars(path):
        raise ExactSourceError("reviewer write path contains a double quote")
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", path)


def _sb_regex_name(name: str) -> str:
    """Match ``name`` the way the CLI folds a path component into a folder name."""
    return "".join(ch if ch.isalnum() else "[^A-Za-z0-9]" for ch in name)


def render_write_profile(
    writable_dirs: list[str], config_state: ConfigStateScope | None = None,
) -> str:
    """Render the Seatbelt profile: deny writes, then allow the listed scopes."""
    allowed = " ".join(
        f"(subpath {_sb_string(str(Path(path).resolve()))})" for path in writable_dirs
    )
    rules = [
        f"(allow file-write* {allowed} (literal \"/dev/null\") (literal \"/dev/tty\")"
        " (regex #\"^/dev/fd/\") (regex #\"^/dev/ttys\"))"
    ]
    if config_state is not None:
        root = _sb_regex_path(str(config_state.root))
        folder = f"[^/]*-{_sb_regex_name(config_state.run_name)}"
        rules.append(f'(allow file-write* (regex #"^{root}/projects/{folder}(/.*)?$"))')
        temp_root = str(config_state.temp_root)
        rules.append(
            f"(allow file-write* (literal {_sb_string(temp_root)})"
            f' (regex #"^{_sb_regex_path(temp_root)}/{folder}(/.*)?$"))'
        )
        temp_parent = _sb_regex_path(str(config_state.temp_root.parent))
        # Seatbelt treats counted intervals in regex predicates as never matching.
        # The CLI writes per-command claude-...-cwd files directly in the system
        # temp directory; [^/]* cannot cross a separator. Evidence: job 114450929931.
        rules.append(
            f'(allow file-write* (regex #"^{temp_parent}/claude-[^/]*-cwd$"))'
        )
        if config_state.allow_global_file:
            global_file = _sb_regex_path(str(config_state.global_file))
            rules.append(f'(allow file-write* (regex #"^{global_file}(\\.[^/]*)?$"))')
    return "(version 1)(allow default)(deny file-write*)" + "".join(rules)


def write_confined(
    cmd: list[str], writable_dirs: list[str],
    config_state: ConfigStateScope | None = None,
) -> list[str]:
    """Wrap a reviewer CLI so it can write only inside ``writable_dirs``.

    With ``config_state`` it may also write its own run's folder under the
    config root's ``projects`` directory, and the global config file only when
    that scope allows it. Fails closed: without macOS sandbox-exec the reviewer
    does not run.
    """
    if sys.platform != "darwin" or not Path(_SANDBOX_EXEC).is_file():
        raise ExactSourceError("reviewer write confinement requires macOS sandbox-exec")
    return [_SANDBOX_EXEC, "-p", render_write_profile(writable_dirs, config_state), *cmd]


def discard_config_state(config_state: ConfigStateScope) -> None:
    """Remove the run's folders left under ``projects`` and the CLI temp root."""
    suffix = "-" + re.sub(r"[^A-Za-z0-9]", "-", config_state.run_name)
    for parent in (config_state.root / "projects", config_state.temp_root):
        try:
            leftovers = [
                entry for entry in parent.iterdir()
                if entry.is_dir() and not entry.is_symlink() and entry.name.endswith(suffix)
            ]
        except OSError:
            continue
        for entry in leftovers:
            shutil.rmtree(entry, ignore_errors=True)


def codex_exact_source_controls(review_dir: str, git_dir: str) -> list[str]:
    """Confine Codex shell reads to runtime files and this source store."""
    filesystem = {
        ":minimal": "read",
        str(Path(review_dir).resolve()): "read",
        str(Path(git_dir).resolve()): "read",
        # The exact-source reader and the interpreter that runs it.
        str(_READER.parent.resolve()): "read",
        str(Path(sys.prefix).resolve()): "read",
        str(Path(sys.base_prefix).resolve()): "read",
    }
    inline_table = "{" + ",".join(
        f"{json.dumps(path)}={json.dumps(access)}"
        for path, access in filesystem.items()
    ) + "}"
    return [
        "--strict-config",
        "-c", 'approval_policy="never"',
        "-c", "allow_login_shell=false",
        "-c", 'default_permissions="corsia_reviewer"',
        "-c", f"permissions.corsia_reviewer.filesystem={inline_table}",
        "-c", "permissions.corsia_reviewer.network.enabled=false",
        "-c", 'web_search="disabled"',
    ]


def _run_git(args: list[str], *, cwd: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        git_argv(*args),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _require_sha(sha: str, label: str) -> None:
    if not _SHA_RE.fullmatch(sha):
        raise ExactSourceError(f"{label} must be a 40-character lowercase SHA")


def _commit_exists(git_dir: str, sha: str, *, cwd: str) -> bool:
    return _run_git(
        [f"--git-dir={git_dir}", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=cwd,
    ).returncode == 0


def _ensure_commit(git_dir: str, sha: str, *, cwd: str) -> None:
    if _commit_exists(git_dir, sha, cwd=cwd):
        return
    fetched = _run_git(
        ["fetch", "--no-tags", "--no-write-fetch-head", "origin", sha],
        cwd=cwd,
        timeout=120,
    )
    if fetched.returncode or not _commit_exists(git_dir, sha, cwd=cwd):
        raise ExactSourceError(
            f"could not ground reviewed commit {sha[:12]}: git fetch failed"
        )


def _prepare_source(repo_dir: str, base_sha: str, head_sha: str) -> tuple[str, str]:
    _require_sha(base_sha, "base_sha")
    _require_sha(head_sha, "head_sha")
    resolved = _run_git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo_dir,
    )
    if resolved.returncode or not resolved.stdout.strip():
        raise ExactSourceError("reviewer source checkout has no readable Git object store")
    git_dir = str(Path(resolved.stdout.strip()).resolve())
    _ensure_commit(git_dir, base_sha, cwd=repo_dir)
    _ensure_commit(git_dir, head_sha, cwd=repo_dir)
    ancestry = _run_git(
        [f"--git-dir={git_dir}", "merge-base", base_sha, head_sha],
        cwd=repo_dir,
    )
    if ancestry.returncode or not ancestry.stdout.strip():
        raise ExactSourceError("exact reviewed base/head ancestry is unavailable")

    reader = reader_command(git_dir, base_sha, head_sha)
    instructions = f"""\
Read only the immutable reviewed change {base_sha}...{head_sha}, and only through
this reader (the one command you may run). Start with:
- {reader} stat
- {reader} names
- {reader} diff
Limit the diff with `{reader} diff <path> ...`, read one file at the head with
`{reader} show <path>`, and search the head tree with
`{reader} grep <fixed-string> [<pathspec> ...]` (quote the pattern).
The working directory is intentionally not a checkout. Never use bare `cat`,
`rg`, `git`, or working-tree files as evidence; they cannot represent this revision.
"""
    return instructions, git_dir


def exact_review_diff(repo_dir: str, base_sha: str, head_sha: str) -> str:
    """Return the immutable PR diff used for context and local review."""
    _, git_dir = _prepare_source(repo_dir, base_sha, head_sha)
    result = _run_git(
        [
            f"--git-dir={git_dir}", "diff", "--no-ext-diff", "--no-textconv",
            "--find-renames", f"{base_sha}...{head_sha}",
        ],
        cwd=repo_dir,
        timeout=120,
    )
    if result.returncode:
        raise ExactSourceError("could not read exact reviewed diff: git diff failed")
    return result.stdout


@contextmanager
def exact_review_source(
    repo_dir: str, base_sha: str, head_sha: str,
) -> Iterator[tuple[str, str, str]]:
    """Yield exact-read instructions, neutral cwd, and the canonical Git dir."""
    source, git_dir = _prepare_source(repo_dir, base_sha, head_sha)
    with tempfile.TemporaryDirectory(prefix="corsia-review-") as review_dir:
        yield source, review_dir, git_dir
