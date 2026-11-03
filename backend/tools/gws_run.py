"""Run the pinned gws program in the sandbox local connected apps run under.

The account's access token reaches only gws's own process, through its environment, and is
scrubbed from everything gws prints before anything else sees it.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

import certifi

from tools.gws_messages import NOT_FOUND

TIMEOUT_SECONDS = 120
CACHE_NAME = "corsia-gws"
_TOKEN_SHAPE = re.compile(r"ya29\.[A-Za-z0-9._\-]+")
TOKEN_ENV = "GOOGLE_WORKSPACE_CLI_TOKEN"

UNSAFE_NAME = re.compile(r"[^A-Za-z0-9@._+-]")


class GwsUnavailable(Exception):
    """The gws program or its sandbox can't be set up; the text is safe to show."""


class Completed(NamedTuple):
    code: int
    stdout: str
    stderr: str


def build_args(
    args: list[str], params: dict | None, body: dict | None, upload: str | None, output: str | None,
) -> list[str]:
    """The words after ``gws`` for one call; ``upload`` and ``output`` are already workspace paths."""
    built = list(args)
    for flag, value in (("--params", params), ("--json", body)):
        if value is not None:
            built += [flag, json.dumps(value)]
    for flag, value in (("--upload", upload), ("--output", output)):
        if value is not None:
            built += [flag, value]
    return built


def scrub(text: str, token: str | None) -> str:
    """``text`` with the access token, and anything shaped like one, removed."""
    if token:
        text = text.replace(token, "[token]")
    return _TOKEN_SHAPE.sub("[token]", text)


def config_dir(account_id: str) -> Path:
    """A writable folder for gws's own cache, in CORSIA's cache area and outside the workspace."""
    from tools.mcp_local_transport import mcp_cache_root

    folder = mcp_cache_root() / CACHE_NAME / "config" / UNSAFE_NAME.sub("_", account_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _launch(words: list[str], account_id: str, token: str | None, workdir: Path | None):
    """The sandboxed launch of gws for ``words``: its parameters and the profile file to delete after.

    gws only reads ``--upload`` and writes ``--output`` files inside its working directory, so that
    is the workspace.
    """
    from mcp.client.stdio import get_default_environment

    from tools.managed_runtime import ManagedRuntimeError, ensure_runtime
    from tools.mcp_config import MCPConfigError
    from tools.mcp_local_transport import safehouse_stdio_parameters

    try:
        program = str(ensure_runtime("gws"))
    except ManagedRuntimeError:
        raise GwsUnavailable(NOT_FOUND) from None
    env = {"GOOGLE_WORKSPACE_CLI_CONFIG_DIR": str(config_dir(account_id)), "SSL_CERT_FILE": certifi.where()}
    if token:
        env[TOKEN_ENV] = token
    try:
        params, profile = safehouse_stdio_parameters(
            CACHE_NAME, {"connected_app_id": CACHE_NAME}, program, words, env,
        )
    except MCPConfigError as error:
        raise GwsUnavailable(str(error)) from None
    if workdir is not None:
        params.args = [f"--workdir={workdir}" if word.startswith("--workdir=") else word for word in params.args]
    return params, profile, {**get_default_environment(), **params.env}


def _run(words: list[str], account_id: str, token: str | None, workdir: Path | None) -> Completed:
    params, profile, env = _launch(words, account_id, token, workdir)
    try:
        done = subprocess.run(
            [params.command, *params.args], env=env, cwd=workdir or params.cwd, capture_output=True,
            text=True, timeout=TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return Completed(1, "", "gws did not answer in time.")
    finally:
        profile.unlink(missing_ok=True)
    return Completed(done.returncode, scrub(done.stdout, token), scrub(done.stderr, token))


async def run_gws(words: list[str], *, account_id: str, token: str | None, workdir: Path | None = None) -> Completed:
    """Run ``gws <words>`` as one account (``token`` None: no sign-in at all) and return scrubbed output."""
    return await asyncio.to_thread(_run, words, account_id, token, workdir)
