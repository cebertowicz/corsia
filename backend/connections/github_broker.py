"""The one door for GitHub reads: a ``gh`` runner that can only read.

CORSIA's pollers (and, next, its event scripts) read GitHub as the person, with
whatever credentials their ``gh`` holds. ``GithubReadBroker`` is a drop-in for
the ``GhRunner`` callable they already take: same arguments in, a
``CompletedProcess`` out. It refuses, by name and with a typed
``GithubReadRefused``, anything that is not a REST read of a tracked repository
(see ``github_read_request`` for the policy), runs a ``gh`` argv it rebuilt
itself, pins the host to github.com, and scrubs token-shaped text from what
comes back and from every error it raises or logs. The rate budget stays with
the caller (``github_notifications_feed.gh_get``); the broker's own repository-id
lookup reports to the same budget.

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
from events.connectors.github_read_budget import budget

__all__ = [
    "GithubReadBroker", "GithubReadRefused", "default_broker", "known_tracked_repositories", "scrub",
]

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
_STATUS = re.compile(r"^HTTP/\S+\s+(\d{3})")
TRACKED_TTL_S = 60.0
_ID_READ_TIMEOUT_S = 30.0
#: A failed id lookup is not retried for this long, so a 403 or 429 is not re-read per request.
ID_FAILURE_TTL_S = 60.0


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


_shared_tracked = _CachedTracked()


def shared_tracked_repositories() -> frozenset[str]:
    """The tracked set through the process-wide cache the default broker reads."""
    return _shared_tracked()


def known_tracked_repositories() -> frozenset[str] | None:
    """The same tracked set for a caller that is not itself a read (the PR notice,
    the quiet-watch candidates), or ``None`` while it cannot be read."""
    try:
        return shared_tracked_repositories()
    except GithubReadRefused:
        return None


def _parse_response(stdout: str) -> tuple[int | None, dict[str, str], Any]:
    """``(status, lower-cased headers, JSON body or None)`` of ``gh api -i`` output."""
    boundary = _BLANK_LINE.search(stdout)
    head, raw = (stdout[:boundary.start()], stdout[boundary.end():]) if boundary else (stdout, "")
    lines = head.replace("\r\n", "\n").split("\n")
    match = _STATUS.match(lines[0]) if lines else None
    headers = {
        name.strip().lower(): value.strip()
        for name, sep, value in (line.partition(":") for line in lines[1:]) if sep
    }
    try:
        body = json.loads(raw) if raw.strip() else None
    except ValueError:
        body = None
    return (int(match.group(1)) if match else None), headers, body


class _RepositoryIds:
    """Whether a numeric id is the id of a tracked repository.

    GitHub's next-page links name a repository by id (``repositories/{id}/...``),
    and such a path is allowed only for the id of a tracked repository. Each
    tracked repository's id is read once from ``repos/{name}``, through the
    shared read budget, and kept. A failed read is logged with its cause, kept
    for ``ID_FAILURE_TTL_S`` and raised as ``repository_id_unavailable``: failing
    to look an id up is never reported as the id being out of scope.
    """

    def __init__(
        self, tracked: Callable[[], frozenset[str]], inner: Callable[..., subprocess.CompletedProcess],
    ) -> None:
        self._tracked = tracked
        self._inner = inner
        self._known: dict[str, str] = {}
        self._failed: dict[str, tuple[float, str]] = {}

    def __call__(self, wanted: str) -> bool:
        names = {name.lower(): name for name in self._tracked()}
        if any(self._known.get(key) == wanted for key in names):
            return True
        failures: list[str] = []
        for key, name in names.items():
            if key in self._known:
                continue
            try:
                self._known[key] = self._read(name, key)
            except GithubReadRefused as exc:
                failures.append(exc.detail)
            else:
                if self._known[key] == wanted:
                    return True
        if failures:
            raise GithubReadRefused("repository_id_unavailable", "; ".join(failures)[:200])
        return False

    def _read(self, name: str, key: str) -> str:
        now = time.monotonic()
        failed = self._failed.get(key)
        if failed is not None and now - failed[0] < ID_FAILURE_TTL_S:
            raise GithubReadRefused("repository_id_unavailable", failed[1])
        try:
            found = self._fetch(name)
        except GithubReadRefused as exc:
            self._failed[key] = (now, exc.detail)
            log.warning("github-broker: repository id lookup failed (%s)", scrub(exc.detail)[:160])
            raise
        self._failed.pop(key, None)
        return found

    def _fetch(self, name: str) -> str:
        def failure(cause: str) -> GithubReadRefused:
            return GithubReadRefused("repository_id_unavailable", f"{name}: {cause}")

        if budget.blocked_until() is not None:
            raise failure("shared read budget")
        if not _REPOSITORY_NAME.fullmatch(name):
            raise failure("not an owner/name pair")
        try:
            result = self._inner(["api", "--method", "GET", "-i", f"repos/{name}"], timeout=_ID_READ_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            raise failure(type(exc).__name__) from None
        status, headers, body = _parse_response(str(result.stdout or ""))
        if status is None:
            raise failure(f"gh exit {result.returncode}: {scrub(str(result.stderr or ''))[:80]}")
        budget.record(status, headers, body)
        if not 200 <= status < 300:
            raise failure(f"http_{status}")
        found = str(body.get("id")) if isinstance(body, dict) else ""
        if not REPOSITORY_ID.fullmatch(found):
            raise failure("no numeric id in the response")
        return found


class GithubReadBroker:
    """A ``GhRunner`` that refuses everything but a REST read of a tracked repository.

    It takes the parameters ``reviewer.gh_api.run_gh`` takes and no others.
    ``inner`` runs the vetted ``gh`` argv (a fake in tests) and ``tracked``
    names the repositories in scope; both default to the real ones.
    """

    def __init__(
        self,
        inner: Callable[..., subprocess.CompletedProcess] | None = None,
        tracked: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._inner = inner or _run_gh
        self._tracked = tracked or shared_tracked_repositories
        self._repository_ids = _RepositoryIds(self._tracked, self._inner)

    def __call__(
        self, args: list[str], *, input_text: str | None = None, timeout: float = 30.0,
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
