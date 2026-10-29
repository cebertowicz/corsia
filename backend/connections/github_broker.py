"""A read-only ``gh`` runner for CORSIA's GitHub pollers.

Requests are vetted and rebuilt and the host is pinned. Refusals, errors, argv,
stderr and the auth probe's text are scrubbed. A read's response is GitHub's
data, passed through as the webhook path receives it: GitHub never echoes the
credential. ``gh`` follows redirects; its credential stays on the configured
host, as the real-gh redirect test verifies. The caller owns the shared read
budget.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Callable

from connections.github_agent_home import TOKEN_NAMES
from connections.github_read_request import AUTH_PROBE, GithubReadRefused, parse_gh_args
from host_access.credential_sanitizer import REDACTION_MARKER, sanitize_credential_text

__all__ = ["GithubReadBroker", "GithubReadRefused", "default_broker"]
log = logging.getLogger(__name__)
GH_HOST = "github.com"


def _scrub(text: str) -> str:
    """Remove live environment credentials and recognized credential shapes."""
    for name in TOKEN_NAMES:
        secret = os.environ.get(name, "")
        if len(secret) >= 8:
            text = text.replace(secret, REDACTION_MARKER)
    return sanitize_credential_text(text)


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
            log.warning("github-broker: refused (%s) %s", exc.reason, _scrub(exc.detail)[:120])
            raise GithubReadRefused(exc.reason, _scrub(exc.detail)[:120]) from None
        try:
            result = self._inner(argv, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise subprocess.TimeoutExpired([_scrub(part) for part in argv], exc.timeout) from None
        except OSError as exc:
            raise type(exc)(_scrub(str(exc))) from None
        stdout = str(result.stdout or "")
        return subprocess.CompletedProcess(
            [_scrub(part) for part in argv], result.returncode,
            _scrub(stdout) if tuple(argv) == AUTH_PROBE else stdout, _scrub(str(result.stderr or "")),
        )

    def _vet(self, args: list[str], input_text: str | None) -> list[str]:
        if input_text is not None:
            raise GithubReadRefused("input_not_allowed")
        if tuple(args) == AUTH_PROBE:
            return list(args)
        return parse_gh_args(args).argv()


_default: GithubReadBroker | None = None


def default_broker() -> GithubReadBroker:
    """The process-wide broker the pollers use unless a test injects a runner."""
    global _default
    if _default is None:
        _default = GithubReadBroker()
    return _default
