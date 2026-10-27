"""A read-only ``gh`` runner for CORSIA's GitHub pollers.

Requests are vetted and rebuilt, the host is pinned, and output is scrubbed.
``gh`` follows redirects; its credential stays on the configured host, as the
real-gh redirect test verifies. The caller owns the shared read budget.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Any, Callable

from connections.github_read_request import GithubReadRefused, parse_gh_args

__all__ = ["GithubReadBroker", "GithubReadRefused", "default_broker", "scrub"]
log = logging.getLogger(__name__)
GH_HOST = "github.com"
_REDACTED = "[redacted]"
_TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
_TOKEN_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|(?i:authorization:[ \t]*(?:(?:bearer|token|basic)[ \t]+)?)[^\s\"']+"
)
_BLANK_LINE = re.compile(r"\r?\n\r?\n")


def scrub(text: str) -> str:
    """Remove environment secrets, token shapes and authorization values."""
    for name in _TOKEN_ENV:
        secret = os.environ.get(name, "")
        if len(secret) >= 8:
            text = text.replace(secret, _REDACTED)
    return _TOKEN_SHAPES.sub(_REDACTED, text)


def _redact_leaves(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_redact_leaves(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_leaves(item) for key, item in value.items()}
    return value


def _scrub_body(body: str) -> str:
    """Scrub decoded JSON strings so serialized escapes remain intact."""
    try:
        return json.dumps(_redact_leaves(json.loads(body)), ensure_ascii=False)
    except ValueError:
        return scrub(body)


def _scrub_output(argv: list[str], stdout: str) -> str:
    if "-i" not in argv:
        return _scrub_body(stdout)
    boundary = _BLANK_LINE.search(stdout)
    if boundary is None:
        return scrub(stdout)
    body = stdout[boundary.end():]
    return scrub(stdout[:boundary.end()]) + (_scrub_body(body) if body.strip() else body)


def _run_gh(args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=timeout, check=False,
        env={**os.environ, "GH_HOST": GH_HOST},
    )


class GithubReadBroker:
    """Vet request shape before passing reads to ``gh``."""

    def __init__(self, inner: Callable[..., subprocess.CompletedProcess] | None = None) -> None:
        self._inner = inner or _run_gh

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
        return parse_gh_args(args).argv()


_default: GithubReadBroker | None = None


def default_broker() -> GithubReadBroker:
    """The process-wide broker the pollers use unless a test injects a runner."""
    global _default
    if _default is None:
        _default = GithubReadBroker()
    return _default
