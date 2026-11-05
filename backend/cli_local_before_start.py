"""Installer-only: set up the local model before CORSIA starts.

``corsia local setup --before-start`` (hidden) runs in the installer, with no
server. It runs the server's own startup initialization and harness discovery,
decides need with the server's ``subscription_available``, then runs the same
setup flight the server runs (``_run_flight``). Each stage prints one line,
Homebrew streams its own output, and a stage silent for 10 seconds says once
that it is still working.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

EXIT_FAILED = 2
# No model source can work until the person signs in; a bare retry cannot help.
EXIT_NEEDS_PROVIDER = 3
# The install is not complete without the model, so it waits for the download.
DOWNLOAD_CEILING_SECONDS = 4 * 60 * 60
HEARTBEAT_SECONDS = 10.0
LOG_NAME = "install-local-setup.log"


class _Stages:
    """Print each stage once, indented, and remember when the last line printed."""

    def __init__(self) -> None:
        self.stage: str | None = None
        self.noted: str | None = None
        self.printed_at = time.monotonic()

    def __call__(self, stage: str) -> None:
        print(f"     {stage}", flush=True)
        self.stage = stage
        self.printed_at = time.monotonic()

    async def heartbeat(self) -> None:
        """Note a silent stage once; while Homebrew streams, its output is the progress."""
        from llm.local_setup_executable import STREAMING

        while True:
            await asyncio.sleep(1.0)
            if STREAMING.is_set():
                self.printed_at = time.monotonic()
                continue
            silent = time.monotonic() - self.printed_at >= HEARTBEAT_SECONDS
            if self.stage and self.stage != self.noted and silent:
                print(f"     Still working: {self.stage}", flush=True)
                self.noted = self.stage


def _quiet_logs() -> Path:
    """Send backend logs to a file; the terminal shows only stages."""
    from loguru import logger

    from infra.config import LOGS_DIR

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(LOGS_DIR) / LOG_NAME
    logger.remove()
    logger.add(str(path), level="INFO", diagnose=False)  # frame values can hold keys (infra.log_locals)
    logging.basicConfig(filename=str(path), level=logging.INFO, force=True)
    return path


def _installer_mode() -> None:
    """Stream Homebrew, report the download often, and wait for it to finish."""
    from llm import local_setup_download, local_setup_executable
    from llm.local_setup_results import DownloadProgress

    local_setup_executable.STREAM_COMMAND_OUTPUT = True
    DownloadProgress.STEP = 5
    DownloadProgress.SECONDS = HEARTBEAT_SECONDS
    local_setup_download.DOWNLOAD_WAIT_SECONDS = DOWNLOAD_CEILING_SECONDS
    local_setup_download.DOWNLOAD_FOLLOW_SECONDS = DOWNLOAD_CEILING_SECONDS


def _failed(why: str) -> int:
    print(f"     The local model couldn't be set up: {why}", flush=True)
    return EXIT_FAILED


def _needs_provider(*, limited: bool, forced_local: bool) -> int:
    """This Mac can't run the local model, and CORSIA never starts without a working one."""
    from api.cli_install import INSTALL_URLS

    lines = ["This Mac doesn't have enough memory to run CORSIA's local model, "
             "and CORSIA needs one working model to start."]
    if forced_local:
        lines.append("Run the install again without --local to use Claude Code or Codex.")
    else:
        lines.append("Run the install again once that limit resets, or after you sign in to the other one:"
                     if limited else "Sign in to Claude Code or Codex, then run the install again:")
        lines += [f"  {source}: run {command} and sign in. "
                  f"Not installed? curl -fsSL {INSTALL_URLS[vendor]} | bash"
                  for source, command, vendor in (("Claude Code", "claude", "claude-code"),
                                                  ("Codex", "codex", "codex"))]
    for line in lines:
        print(f"     {line}", flush=True)
    return EXIT_NEEDS_PROVIDER


async def _discover(stages: _Stages) -> None:
    """The server's startup initialization and harness discovery, without the server."""
    from loguru import logger

    from app_startup_init import initialize_startup
    from app_startup_runtime import _startup_harness_discovery

    # The first run builds the app here, exactly as the server's startup would.
    stages("Preparing CORSIA")
    await asyncio.to_thread(initialize_startup, logger)
    stages("Checking Claude Code and Codex")
    await _startup_harness_discovery()


async def _setup(force_local: bool) -> int:
    from llm import local_setup_flow
    from llm.local_setup_gates import limited_subscription_message, local_memory_ok, subscription_available

    stages = _Stages()
    heartbeat = asyncio.create_task(stages.heartbeat())
    try:
        await _discover(stages)
        subscription = None if force_local else await subscription_available()
        if subscription is not None:
            source = "Claude Code" if subscription.provider == "claude-code" else "Codex"
            account = f" as {subscription.label}" if subscription.label else ""
            print(f"     {source} is signed in{account}; no local model is needed.", flush=True)
            return 0
        notice = None if force_local else await limited_subscription_message()
        if notice:
            print(f"     {notice}", flush=True)
        if not await asyncio.to_thread(local_memory_ok):
            return _needs_provider(limited=bool(notice), forced_local=force_local)
        _installer_mode()
        result = await local_setup_flow._run_flight(on_stage=stages)
    finally:
        heartbeat.cancel()
    if result.get("downloading"):
        return _failed("the model download did not finish within 4 hours.")
    if not result.get("ok"):
        return _failed(str(result.get("message") or "setup failed."))
    print("     Local model is ready.", flush=True)
    return 0


def run_before_start(*, force_local: bool) -> int:
    """0 when a model source works; non-zero after printing what failed and why.

    ``EXIT_NEEDS_PROVIDER`` when only a sign-in can make one work, ``EXIT_FAILED`` otherwise.
    """
    log_path = _quiet_logs()
    try:
        return asyncio.run(_setup(force_local))
    except Exception as error:  # the installer must say why, never a traceback
        logging.getLogger(__name__).exception("Installer local setup failed")
        return _failed(f"{error} (details: {log_path})")
