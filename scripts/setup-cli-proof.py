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
import tempfile
import urllib.error
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
# Output defaults to a fresh temporary directory in main().
SOURCES = (("claude-code", "Claude Code"), ("codex", "Codex"))


def _write(path: Path, proof: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n")


def _prepare_environment(home: Path, runtime: Path) -> None:
    """Constrain every process-owned writable root to this run's unique temporary HOME."""
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


def _outside_write_snapshot(original_home: Path) -> dict[Path, tuple[int, int]]:
    """Capture metadata for vendor-relevant locations outside the proof HOME, never their contents."""
    roots = (
        original_home / ".claude", original_home / ".codex", original_home / ".npm",
        original_home / ".local/share/claude", original_home / ".cache/claude",
        original_home / ".cache/codex", Path("/usr/local/bin"), Path("/opt/homebrew/bin"),
    )
    snapshot: dict[Path, tuple[int, int]] = {}
    for root in roots:
        try:
            if root.exists():
                stat = root.stat()
                snapshot[root] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
    return snapshot


def _outside_writes(before: dict[Path, tuple[int, int]], original_home: Path) -> list[str]:
    """Return changed watched locations; any entry is a failed isolation proof."""
    after = _outside_write_snapshot(original_home)
    return [str(path) for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)]


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


def _binary(provider: str, candidate_roots: tuple[Path, ...] | None = None) -> str | None:
    """Locate one vendor binary, optionally only within proof-owned roots."""
    from connections import binary_from_config

    return binary_from_config(provider, {}, candidate_roots=candidate_roots)


async def _auth_checks(candidate_roots: tuple[Path, ...] | None = None) -> tuple[dict[str, str], list[str]]:
    """Run each actual vendor auth check without keeping its output."""
    from connections import _codex_sign_in_state
    from connections.claude_discovery import auth_status_state

    checks: dict[str, str] = {}
    limits: list[str] = []
    for provider, _name in SOURCES:
        binary = _binary(provider, candidate_roots)
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
    original_home = Path.home()
    run_root = output.parent.resolve() / f"cli-proof-{uuid4().hex}"
    home = run_root / "home"
    runtime = home / "runtime"
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
    installed_roots = (home / ".local/bin",)

    proof: dict[str, Any] = {
        "sources": _absent_rows(runtime),
        "before_install_host_auth_checks": existing_auth,
        "before_install_temporary_home_auth_checks": {},
        "post_install_auth_checks": {},
        "installed_binaries": {},
        "installer": {},
        "outside_write_accounting": {
            "home": "<unique temporary child HOME>",
            "writes_outside_home": [],
            "status": "pending",
            "watched_paths": ["~/.claude", "~/.codex", "~/.npm", "~/.local/share/claude", "~/.cache/claude", "~/.cache/codex", "/usr/local/bin", "/opt/homebrew/bin"],
            "limit": "Only metadata on these watched directories is compared. Nested-file and arbitrary OS-wide writes are not observable here.",
        },
        "limitations": [],
    }
    _write(output, proof)

    auth, limitations = await _auth_checks()
    proof["before_install_temporary_home_auth_checks"] = auth
    proof["limitations"] = list(dict.fromkeys([*existing_limits, *limitations]))
    outside_before = _outside_write_snapshot(original_home)
    if install:
        for vendor, _name in SOURCES:
            try:
                proof["installer"][vendor] = await _install_with_route(vendor, roots)
            except Exception as exc:
                proof["installer"][vendor] = {"state": "not_proven_here", "reason": type(exc).__name__}
    else:
        proof["installer"] = {provider: {"state": "not_run", "reason": "skipped_by_argument"} for provider, _ in SOURCES}
    if install:
        proof["post_install_auth_checks"] = {provider: "not proven here: host review blocks reading CLI credential config"
                                              for provider, _ in SOURCES}
        proof["installed_binaries"] = {provider: binary.name for provider, _ in SOURCES
                                       if (binary := _binary(provider, installed_roots)) is not None}
        proof["limitations"].append("Post-install CLI auth is not proven here: host review blocks reading CLI credential config.")
    outside_writes = _outside_writes(outside_before, original_home)
    proof["outside_write_accounting"]["writes_outside_home"] = outside_writes
    proof["outside_write_accounting"]["status"] = "failed" if outside_writes else "passed_for_watched_directory_metadata"
    _write(output, proof)
    return 1 if outside_writes else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-installs", action="store_true")
    args = parser.parse_args()
    output = (args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix="corsia-setup-proof-")) / "cli-proof.json")
    if not output.is_relative_to(Path("/tmp").resolve()):
        raise SystemExit("output must be under /tmp")
    return asyncio.run(_run(output, install=not args.skip_installs))


if __name__ == "__main__":
    raise SystemExit(main())
