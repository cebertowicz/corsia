"""The one door for GitHub reads: a ``gh`` runner that can only read.

CORSIA's pollers (and, next, its event scripts) read GitHub as the person, with
whatever credentials their ``gh`` holds. ``GithubReadBroker`` is a drop-in for
the ``GhRunner`` callable they already take: same arguments in, a
``CompletedProcess`` out. It refuses, by name and with a typed
``GithubReadRefused``, anything that is not a REST read of a tracked repository
(see ``github_read_request`` for the policy), runs a ``gh`` argv it rebuilt
itself, pins the host to github.com, and scrubs token-shaped text from what
comes back and from every error it raises or logs. The rate budget stays with
the caller (``github_notifications_feed.gh_get``).

``gh api`` follows redirects itself, so the broker cannot refuse a cross-host
redirect before it is followed. What keeps the token on github.com is that the
broker never accepts a URL to start from and that ``gh`` attaches its
credential only to the configured host;
``test_gh_follows_redirects_but_sends_the_token_only_to_the_configured_host``
runs the real ``gh`` against local servers to pin that.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Callable

from connections.github_read_request import REPOSITORY_ID, GithubReadRefused, parse_gh_args
from connections.github_tracked_repos import RepoScopeUnavailable, tracked_github_repositories

__all__ = ["GithubReadBroker", "GithubReadRefused", "default_broker", "scrub"]

log = logging.getLogger(__name__)

#: The host ``gh`` is pinned to, whatever ``GH_HOST`` the backend inherited.
GH_HOST = "github.com"
_REDACTED = "[redacted]"
_TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
_TOKEN_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|(?i:authorization:[ \t]*(?:(?:bearer|token|basic)[ \t]+)?)[^\s\"']+"
)
_BLANK_LINE = re.compile(r"\r?\n\r?\n")
_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
TRACKED_TTL_S = 60.0
_ID_READ_TIMEOUT_S = 30.0


def scrub(text: str) -> str:
    """``text`` without any GitHub token: ones from the environment, anything
    shaped like one, and an ``Authorization`` header's value."""
    for name in _TOKEN_ENV:
        secret = os.environ.get(name, "")
        if len(secret) >= 8:
            text = text.replace(secret, _REDACTED)
    return _TOKEN_SHAPES.sub(_REDACTED, text)


def _redact_leaves(value: Any) -> Any:
    """``value`` with ``scrub`` applied to every string in it, structure untouched."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_redact_leaves(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_leaves(item) for key, item in value.items()}
    return value


def _scrub_body(body: str) -> str:
    """A response body without tokens. JSON is decoded, its strings scrubbed and
    re-encoded: scrubbing the serialized text would eat the quotes and escapes
    around a token inside a PR body or comment and break the document."""
    try:
        return json.dumps(_redact_leaves(json.loads(body)), ensure_ascii=False)
    except ValueError:
        return scrub(body)


def _scrub_output(argv: list[str], stdout: str) -> str:
    """Scrub ``gh`` output: with ``-i`` the header block as text and the body by
    its shape; without it the whole output is the body."""
    if "-i" not in argv:
        return _scrub_body(stdout)
    boundary = _BLANK_LINE.search(stdout)
    if boundary is None:
        return scrub(stdout)
    body = stdout[boundary.end():]
    return scrub(stdout[:boundary.end()]) + (_scrub_body(body) if body.strip() else body)


def _run_gh(args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run ``gh`` against github.com only; credentials stay in ``gh``."""
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=timeout, check=False,
        env={**os.environ, "GH_HOST": GH_HOST},
    )


class _CachedTracked:
    """The tracked repositories, resolved at most once a minute (each reads git remotes)."""

    def __init__(self) -> None:
        self._at = -TRACKED_TTL_S
        self._value: frozenset[str] = frozenset()

    def __call__(self) -> frozenset[str]:
        if time.monotonic() - self._at >= TRACKED_TTL_S:
            try:
                self._value = tracked_github_repositories()
            except RepoScopeUnavailable:
                raise GithubReadRefused("repo_scope_unavailable") from None
            self._at = time.monotonic()
        return self._value


class _RepositoryIds:
    """Numeric ids of the tracked repositories, each read once from ``repos/{name}``.

    GitHub's next-page links name a repository by id (``repositories/{id}/...``).
    Such a path is allowed only for an id this resolves, so a link to any other
    repository is refused like any other out-of-scope read.
    """

    def __init__(
        self, tracked: Callable[[], frozenset[str]], inner: Callable[..., subprocess.CompletedProcess],
    ) -> None:
        self._tracked = tracked
        self._inner = inner
        self._known: dict[str, str] = {}

    def __call__(self) -> frozenset[str]:
        ids: set[str] = set()
        for name in self._tracked():
            key = name.lower()
            if key not in self._known and (found := self._read(name)) is not None:
                self._known[key] = found
            if key in self._known:
                ids.add(self._known[key])
        return frozenset(ids)

    def _read(self, name: str) -> str | None:
        if not _REPOSITORY_NAME.fullmatch(name):
            return None
        try:
            result = self._inner(
                ["api", "--method", "GET", "--jq", ".id", f"repos/{name}"], timeout=_ID_READ_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        found = str(result.stdout or "").strip()
        return found if result.returncode == 0 and REPOSITORY_ID.fullmatch(found) else None


class GithubReadBroker:
    """A ``GhRunner`` that refuses everything but a REST read of a tracked repository.

    ``inner`` runs the vetted ``gh`` argv (a fake in tests) and ``tracked``
    names the repositories in scope; both default to the real ones.
    """

    def __init__(
        self,
        inner: Callable[..., subprocess.CompletedProcess] | None = None,
        tracked: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._inner = inner or _run_gh
        self._tracked = tracked or _CachedTracked()
        self._repository_ids = _RepositoryIds(self._tracked, self._inner)

    def __call__(
        self, args: list[str], *, timeout: float = 30.0, input_text: str | None = None, **_: Any,
    ) -> subprocess.CompletedProcess:
        try:
            argv = self._vet(list(args), input_text)
        except GithubReadRefused as exc:
            log.warning("github-broker: refused (%s) %s", exc.reason, scrub(exc.detail)[:120])
            raise GithubReadRefused(exc.reason, scrub(exc.detail)[:120]) from None
        result = self._inner(argv, timeout=timeout)
        return subprocess.CompletedProcess(
            argv, result.returncode,
            _scrub_output(argv, str(result.stdout or "")), scrub(str(result.stderr or "")),
        )

    def _vet(self, args: list[str], input_text: str | None) -> list[str]:
        if input_text is not None:
            raise GithubReadRefused("input_not_allowed")
        if args == ["auth", "status"]:
            return args
        return parse_gh_args(args, self._tracked, self._repository_ids).argv()


_default: GithubReadBroker | None = None


def default_broker() -> GithubReadBroker:
    """The process-wide broker the pollers use unless a test injects a runner."""
    global _default
    if _default is None:
        _default = GithubReadBroker()
    return _default
