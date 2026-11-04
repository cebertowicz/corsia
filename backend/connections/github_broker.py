"""A read-only ``gh`` runner for CORSIA's GitHub pollers.

Requests are vetted and rebuilt and the host is pinned. Refusals, errors, argv,
stderr and the auth probe's text are scrubbed. A read's response is GitHub's
data, passed through as the webhook path receives it: GitHub never echoes the
credential. ``gh`` follows redirects; its credential stays on the configured
host, as the real-gh redirect test verifies. Each broker carries its own read
budget (``read_budget``), fixed when it is made; the caller asks and feeds it.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import TYPE_CHECKING, Callable

from connections.github_agent_home import TOKEN_NAMES
from connections.github_read_request import (
    AUTH_PROBE, GH_HOST, SINGLE_ACCOUNT_PROBE, UNSUPPORTED_FLAG, GithubReadRefused, parse_gh_args,
)
from host_access.credential_sanitizer import REDACTION_MARKER, sanitize_credential_text

if TYPE_CHECKING:
    from events.connectors.github_read_budget import ReadBudget

__all__ = ["GithubReadBroker", "GithubReadRefused", "default_broker", "person_broker"]
log = logging.getLogger(__name__)


def _scrub(text: str) -> str:
    """Remove live environment credentials and recognized credential shapes."""
    for name in TOKEN_NAMES:
        secret = os.environ.get(name, "")
        if len(secret) >= 8:
            text = text.replace(secret, REDACTION_MARKER)
    return sanitize_credential_text(text)


def _run_host_gh(args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run the vetted read as the instance's own ``gh`` login."""
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=timeout, check=False,
        env={**os.environ, "GH_HOST": GH_HOST},
    )


def _run_gh(args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run the vetted read as the agents' GitHub account when there is one, as the host's ``gh`` otherwise."""
    from connections import github_agent

    account = github_agent.view()
    if account.cause == "verifying":  # first read after a restart: wait for the answer once
        account = github_agent.verify_now()
    binary, env = github_agent.backend_gh(account)
    return subprocess.run(
        [binary, *args], capture_output=True, text=True, timeout=timeout, check=False,
        env={**(os.environ if env is None else env), "GH_HOST": GH_HOST},
    )


class GithubReadBroker:
    """Vet request shape before passing reads to ``gh``."""

    def __init__(
        self, inner: Callable[..., subprocess.CompletedProcess] | None = None,
        read_budget: ReadBudget | None = None,
    ) -> None:
        from events.connectors.github_read_budget import budget

        self._inner = inner or _run_gh
        self.read_budget = read_budget or budget

    def __call__(
        self, args: list[str], *, input_text: str | None = None, timeout: float = 30.0,
    ) -> subprocess.CompletedProcess:
        try:
            argv = self._vet(list(args), input_text)
        except GithubReadRefused as exc:
            log.warning("github-broker: refused (%s) %s", exc.reason, _scrub(exc.detail)[:120])
            raise GithubReadRefused(exc.reason, _scrub(exc.detail)[:120]) from None
        probe = tuple(argv) == AUTH_PROBE
        try:
            result = self._inner(argv, timeout=timeout)
            if probe and result.returncode != 0 and UNSUPPORTED_FLAG.search(str(result.stderr or "")):
                argv = list(SINGLE_ACCOUNT_PROBE)
                result = self._inner(argv, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise subprocess.TimeoutExpired([_scrub(part) for part in argv], exc.timeout) from None
        except OSError as exc:
            raise type(exc)(_scrub(str(exc))) from None
        stdout = str(result.stdout or "")
        return subprocess.CompletedProcess(
            [_scrub(part) for part in argv], result.returncode,
            _scrub(stdout) if probe else stdout, _scrub(str(result.stderr or "")),
        )

    def _vet(self, args: list[str], input_text: str | None) -> list[str]:
        if input_text is not None:
            raise GithubReadRefused("input_not_allowed")
        if tuple(args) == AUTH_PROBE:
            return list(args)
        return parse_gh_args(args).argv()


_default: GithubReadBroker | None = None
_person: GithubReadBroker | None = None


def default_broker() -> GithubReadBroker:
    """The process-wide broker the pollers use unless a test injects a runner."""
    global _default
    if _default is None:
        _default = GithubReadBroker()
    return _default


def person_broker() -> GithubReadBroker:
    """The broker for a read of the person's own account, such as their notifications feed."""
    global _person
    if _person is None:
        from events.connectors.github_read_budget import person_budget

        _person = GithubReadBroker(_run_host_gh, person_budget)
    return _person
