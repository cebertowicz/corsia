"""Read-only exact-revision source reader for the tool-enabled reviewer.

The Claude reviewer may run only this script (see ``claude_exact_source_controls``).
Its object store and revisions are fixed by the permission rule's literal prefix,
and every argument the model adds is placed where Git reads it as data: a
pathspec after ``--``, a ``<sha>:<path>`` revision, or the value of ``-e``.
Nothing the model supplies can become a Git option. Standard library only, so it
runs under ``python -I``.

Usage: reviewer_read.py GIT_DIR BASE_SHA HEAD_SHA MODE [ARG ...]
  stat                      diff --stat of the reviewed change
  names                     changed file names
  diff [PATH ...]           full diff, optionally limited to paths
  show PATH                 one file at the head revision
  grep PATTERN [PATH ...]   fixed-string search of the head tree
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_DIFF = ("diff", "--no-ext-diff", "--no-textconv", "--no-color")
_USAGE = "usage: reviewer_read.py GIT_DIR BASE_SHA HEAD_SHA {stat|names|diff|show|grep} [ARG ...]"


class ReadRefused(ValueError):
    """The requested read is outside the exact-revision contract."""


def _data_arg(value: str) -> str:
    if not value or value.startswith("-") or "\0" in value:
        raise ReadRefused(f"refused argument {value!r}: paths may not be empty or start with '-'")
    return value


def git_argv(git_dir: str, base: str, head: str, mode: str, args: list[str]) -> list[str]:
    """Return the fixed Git argv for one read, or raise ReadRefused."""
    for label, sha in (("base", base), ("head", head)):
        if not _SHA_RE.fullmatch(sha):
            raise ReadRefused(f"{label} must be a 40-character lowercase SHA")
    git = ["git", f"--git-dir={git_dir}", "-c", "core.fsmonitor=false"]
    revision_range = f"{base}...{head}"
    if mode in ("stat", "names"):
        if args:
            raise ReadRefused(f"{mode} takes no arguments")
        flag = "--stat" if mode == "stat" else "--name-only"
        return [*git, *_DIFF, flag, revision_range]
    if mode == "diff":
        paths = [_data_arg(path) for path in args]
        return [*git, *_DIFF, "--find-renames", revision_range, "--", *paths]
    if mode == "show":
        if len(args) != 1:
            raise ReadRefused("show takes exactly one path")
        return [*git, "show", "--no-textconv", f"{head}:{_data_arg(args[0])}"]
    if mode == "grep":
        if not args or not args[0]:
            raise ReadRefused("grep needs a pattern")
        pattern, paths = args[0], [_data_arg(path) for path in args[1:]]
        return [*git, "grep", "-n", "-F", "-e", pattern, head, "--", *paths]
    raise ReadRefused(f"unknown mode {mode!r}")


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(_USAGE, file=sys.stderr)
        return 2
    git_dir, base, head, mode, *args = argv
    try:
        command = git_argv(git_dir, base, head, mode, args)
    except ReadRefused as exc:
        print(f"reviewer_read: {exc}", file=sys.stderr)
        return 2
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }
    return subprocess.run(command, env=env, check=False).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
