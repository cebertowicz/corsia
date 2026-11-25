"""Log and summarize `corsia prepare-redeploy` output."""

from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from infra.log_files import nofollow_log_opener

UTC_TIMESTAMP_PATTERN = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_logged_prepare(
    script: Path, env: dict[str, str], log_path: Path,
) -> tuple[subprocess.CompletedProcess[str], OSError | None]:
    """Record the prepare interval without making logging block a successful update."""
    started = time.monotonic()
    start_line = f"{_timestamp()} Prepare started\n"
    start_written = False
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8", opener=nofollow_log_opener) as log:
            log.write(start_line)
        start_written = True
    except OSError:
        pass

    def finish(output: str, outcome: str) -> None:
        line = f"{_timestamp()} Prepare ended in {int(time.monotonic() - started)}s ({outcome})\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a" if start_written else "w", encoding="utf-8", opener=nofollow_log_opener) as log:
            if not start_written:
                log.write(start_line)
            log.write(output + line)

    try:
        result = subprocess.run(
            [str(script), "prepare-redeploy"], capture_output=True, text=True,
            env=env, timeout=600, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        try:
            finish("", "timed out" if isinstance(exc, subprocess.TimeoutExpired) else "could not start")
        except OSError:
            pass
        raise
    stdout, stderr = clean_update_output(result.stdout), clean_update_output(result.stderr)
    output = "\n".join(part.rstrip() for part in (stdout, stderr) if part.strip()) + "\n"
    try:
        finish(output, f"exit {result.returncode}")
    except OSError as exc:
        return result, exc
    return result, None


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
