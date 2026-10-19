#!/usr/bin/env python3
"""Safely prove setup CLI detection against an isolated temporary home.

The script intentionally writes only under its output directory. It uses the
real binary resolver with empty search roots, the production source-row shape,
the production installer route, and the vendors' own auth checks. It never
prints installer or auth output.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import os
import sys
import urllib.error
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
DEFAULT_OUTPUT = Path("/tmp/corsia-setup-proof-hjvrzO/cli-proof.json")
SOURCES = (("claude-code", "Claude Code"), ("codex", "Codex"))


def _write(path: Path, proof: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n")


def _prepare_environment(home: Path, runtime: Path) -> None:
    """Constrain every process-owned writable root to this run's temporary HOME."""
    for directory in (home, runtime / "workspace", runtime / "state", home / "tmp",
                      home / ".cache", home / ".config", home / ".local/share"):
        directory.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "CORSIA_WORKSPACE": str(runtime / "workspace"),
        "CORSIA_STATE_DIR": str(runtime / "state"),
        "CORSIA_TEST_MODE": "1",
        "CORSIA_ANALYTICS_DISABLE": "1",
        "CORSIA_ALLOW_HOME_WORKSPACE_IN_CI": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })


def _absent_rows(root: Path) -> list[dict[str, str]]:
    """Resolve both vendor binaries only within a fresh empty search root."""
    from connections import binary_from_config

    roots = (root / "empty-bin",)
    roots[0].mkdir(exist_ok=True)
    rows = []
    for provider, name in SOURCES:
        resolved = binary_from_config(provider, {}, candidate_roots=roots)
        if resolved is not None:
            raise AssertionError(f"isolated resolver found {provider}")
        rows.append({"source": provider, "name": name, "state": "not_installed"})
    return rows


def _existing_binary(provider: str) -> str | None:
    """Locate a vendor only for its own safe sign-in probe, never for install."""
    from connections import binary_from_config

    return binary_from_config(provider, {})


async def _auth_checks() -> tuple[dict[str, str], list[str]]:
    """Run each actual vendor auth check without keeping its output."""
    from connections import _codex_sign_in_state
    from connections.claude_discovery import auth_status_state

    checks: dict[str, str] = {}
    limits: list[str] = []
    for provider, _name in SOURCES:
        binary = _existing_binary(provider)
        if binary is None:
            checks[provider] = "not_proven_here: binary_not_found"
            continue
        if provider == "codex":
            checks[provider] = _codex_sign_in_state(binary)
        else:
            checks[provider] = await auth_status_state(binary)
    if checks.get("claude-code", "").startswith("not_proven_here"):
        limits.append("Claude Code auth state is not proven here because no Claude binary resolved.")
    return checks, limits


@contextmanager
def _route_uses_empty_roots(roots: tuple[Path, ...], http_status: dict[str, str]) -> Iterator[None]:
    """Make Set up call the real resolver, restricted to proof-owned roots."""
    from api import cli_install
    from connections import binary_from_config

    original_service = cli_install.service_binary
    original_login = cli_install.login_shell_binary
    original_urlopen = cli_install.urllib.request.urlopen

    def resolve(provider: str) -> str | None:
        return binary_from_config(provider, {}, candidate_roots=roots)

    def login(provider: str) -> Path | None:
        resolved = resolve(provider)
        return Path(resolved) if resolved else None

    def urlopen(request, *args, **kwargs):
        try:
            return original_urlopen(request, *args, **kwargs)
        except urllib.error.HTTPError as exc:
            url = getattr(request, "full_url", str(request))
            for vendor, install_url in cli_install.INSTALL_URLS.items():
                if install_url in url:
                    http_status[vendor] = str(exc.code)
                    break
            raise

    cli_install.service_binary = resolve
    cli_install.login_shell_binary = login
    cli_install.urllib.request.urlopen = urlopen
    try:
        yield
    finally:
        cli_install.service_binary = original_service
        cli_install.login_shell_binary = original_login
        cli_install.urllib.request.urlopen = original_urlopen


async def _install_with_route(vendor: str, roots: tuple[Path, ...]) -> dict[str, str]:
    """Use CORSIA's real Set up route and retain its public outcome only."""
    from api import cli_install
    from fastapi.responses import JSONResponse

    http_status: dict[str, str] = {}
    with _route_uses_empty_roots(roots, http_status):
        response = await cli_install.start_install(vendor, {"id": "setup-cli-proof"})
        if isinstance(response, JSONResponse):
            body = json.loads(response.body)
            outcome = {"state": "refused", "reason": str(body.get("reason", "unknown"))}
        else:
            job = cli_install._jobs[vendor]
            assert job.task is not None
            await job.task
            public = cli_install._status(vendor)
            outcome = {
                "state": str(public["state"]),
                "reason": str(public["reason"]) if public["reason"] else "",
            }
    if vendor in http_status:
        outcome["http_status"] = http_status[vendor]
    return outcome


async def _run(output: Path, *, install: bool) -> int:
    home = output.parent.resolve()
    runtime = home / "runs" / uuid4().hex
    # Backend imports require explicit roots even while HOME still names the
    # existing-install account for its read-only vendor check.
    for directory in (runtime / "workspace", runtime / "state"):
        directory.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "CORSIA_WORKSPACE": str(runtime / "workspace"),
        "CORSIA_STATE_DIR": str(runtime / "state"),
        "CORSIA_TEST_MODE": "1",
        "CORSIA_ANALYTICS_DISABLE": "1",
    })
    sys.path.insert(0, str(BACKEND))
    existing_auth, existing_limits = await _auth_checks()
    _prepare_environment(home, runtime)
    roots = (runtime / "empty-bin",)

    proof: dict[str, Any] = {
        "sources": _absent_rows(runtime),
        "existing_install_auth_checks": existing_auth,
        "temporary_home_auth_checks": {},
        "installer": {},
        "outside_write_accounting": {
            "home": str(home),
            "writes_outside_home": [],
            "status": "not_proven_here",
            "limit": "The proof confines its environment and every expected writable root to HOME; it cannot observe arbitrary OS-wide writes by vendor child processes.",
        },
        "limitations": [],
    }
    _write(output, proof)

    auth, limitations = await _auth_checks()
    proof["temporary_home_auth_checks"] = auth
    proof["limitations"] = list(dict.fromkeys([*existing_limits, *limitations]))
    if install:
        for vendor, _name in SOURCES:
            try:
                proof["installer"][vendor] = await _install_with_route(vendor, roots)
            except Exception as exc:
                proof["installer"][vendor] = {"state": "not_proven_here", "reason": type(exc).__name__}
    else:
        proof["installer"] = {provider: {"state": "not_run", "reason": "skipped_by_argument"} for provider, _ in SOURCES}
    _write(output, proof)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-installs", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(Path("/tmp").resolve()):
        raise SystemExit("output must be under /tmp")
    return asyncio.run(_run(output, install=not args.skip_installs))


if __name__ == "__main__":
    raise SystemExit(main())
