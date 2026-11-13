"""Run one ``gh`` command as the agents' GitHub account."""

from __future__ import annotations

import subprocess

from connections.github_agent import backend_gh_now


def run_gh(
    args: list[str], *, input_text: str | None = None, timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Run one ``gh`` command as the agents' GitHub account when there is one.

    Callers own all result and failure policy.
    """
    binary, env, _account = backend_gh_now()
    return subprocess.run(
        [binary, *args], input=input_text, capture_output=True, text=True,
        timeout=timeout, check=False, env=env,
    )
