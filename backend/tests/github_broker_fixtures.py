"""Shared fixtures for the GitHub read broker tests."""

from __future__ import annotations

import subprocess

import pytest

from connections.github_broker import GithubReadBroker, GithubReadRefused

REPO = "o/r"
REPO_ID = "1001"
OK = "HTTP/2.0 200 OK\r\n\r\n{}"


def secret(kind: str = "gho") -> str:
    """A token-shaped string assembled here so no literal credential sits in the source."""
    return kind + "_" + "A1b2C3d4E5" * 3


class Inner:
    """A recording stand-in for the ``gh`` process the broker runs."""

    def __init__(self, stdout: str = OK, stderr: str = "", returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.id_reads: list[str] = []
        self.result = (stdout, stderr, returncode)

    def __call__(self, args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess:
        if "--jq" in args:  # the broker's own read of a repository's numeric id
            self.id_reads.append(args[-1])
            return subprocess.CompletedProcess(args, 0, REPO_ID + "\n", "")
        self.calls.append(list(args))
        stdout, stderr, returncode = self.result
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def make_broker(inner: Inner) -> GithubReadBroker:
    return GithubReadBroker(inner, tracked=lambda: frozenset({REPO}))


def refusal(broker: GithubReadBroker, inner: Inner, args: list[str]) -> str:
    """The named reason a call is refused; the call must never reach ``gh``."""
    with pytest.raises(GithubReadRefused) as caught:
        broker(args, timeout=5.0)
    assert inner.calls == [], "a refused call must never reach gh"
    return caught.value.reason
