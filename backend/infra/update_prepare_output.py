"""Bounded, terminal-safe summaries of `corsia prepare-redeploy` output."""

from __future__ import annotations

import re


def clean_update_output(value: str | bytes | None) -> str:
    if not value:
        return ""
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return re.sub(r"\x1b\[[0-9;]*m", "", value)


def terminal_update_summary(
    stdout: str | bytes | None, stderr: str | bytes | None
) -> str:
    """Project noisy command output to one bounded terminal failure."""
    clean_stdout = clean_update_output(stdout)
    clean_stderr = clean_update_output(stderr)
    stderr_lines = [line.strip() for line in clean_stderr.splitlines() if line.strip()]
    stdout_lines = [line.strip() for line in clean_stdout.splitlines() if line.strip()]

    for line in stderr_lines:
        if re.match(r"^(?:error|fatal):", line, re.IGNORECASE):
            return line[:300]
    for line in reversed(stdout_lines):
        if line.startswith("❌") or "Redeploy refused:" in line:
            return line.removeprefix("❌").strip()[:300]
    fallback = stderr_lines[-1:] or stdout_lines[-1:]
    if fallback:
        return fallback[0][:300]
    return "the pull, dependency check, or frontend build failed"
