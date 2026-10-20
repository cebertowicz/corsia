"""CORSIA first-run and maintenance CLI."""

from __future__ import annotations

import argparse

import os
import sqlite3
import sys
from contextlib import closing
from unittest.mock import patch
from pathlib import Path
from uuid import uuid4

from core.workspace_scaffolding import scaffold_workspace
from infra.bind_host import (
    SETUP_BINDS,
    BindHostError,
    exposure_keys,
    recorded_setup,
    resolve_bind_host,
    write_setup,
)
from infra.platform_paths import (
    LegacyRuntimePaths,
    RuntimePaths,
    WorkspaceSelection,
    default_state_root,
    load_bootstrap,
    runtime_db_paths,
    write_bootstrap,
)

BOOTSTRAP_PATH = Path.home() / ".corsia" / "config.json"
DEFAULT_WORKSPACE = "~/corsia"
_SETUP_CHOICES = {"1": "local", "2": "server"}


def _bootstrap_path_status() -> bool | None:
    """Whether BOOTSTRAP_PATH exists: True/False, or None if a sandbox
    denies even a stat of it (pathlib's ``exists()`` only swallows
    ENOENT-class errnos, not EPERM)."""
    try:
        return BOOTSTRAP_PATH.exists()
    except PermissionError:
        return None


def _sandboxed_bootstrap_denial_error() -> RuntimeError:
    """Build the clear error for a sandbox-denied bootstrap config stat."""
    return RuntimeError(
        f"Cannot access {BOOTSTRAP_PATH}: permission denied. This is "
        f"expected inside a sandboxed agent environment where the real home "
        f"directory is hard-denied. A sandboxed session must set both "
        f"CORSIA_WORKSPACE and CORSIA_STATE_DIR directly instead of "
        f"running 'init'."
    )


def cmd_init(args: argparse.Namespace) -> int:
    """Create or safely rewrite the selected-workspace bootstrap."""
    bootstrap_status = _bootstrap_path_status()
    if bootstrap_status is None:
        print(str(_sandboxed_bootstrap_denial_error()), file=sys.stderr)
        return 2
    existing = load_bootstrap(BOOTSTRAP_PATH) if bootstrap_status else None
    if existing is not None and not args.force:
        print(f"Already configured: workspace = {existing.workspace.root}")
        print(
            f"Nothing changed. `init --force` rewrites the setup choice and workspace "
            f"name in {BOOTSTRAP_PATH}; accounts and workspace files are kept."
        )
        return 1
    if isinstance(existing, LegacyRuntimePaths):
        print(
            "init --force cannot rewrite a legacy bootstrap automatically.",
            file=sys.stderr,
        )
        return 2

    path_str = (args.path or "").strip() or _prompt_workspace_path()
    setup = args.setup or _prompt_setup(
        recorded_setup(BOOTSTRAP_PATH) if existing is not None else None
    )
    workspace = Path(path_str).expanduser().resolve()
    try:
        runtime_paths = _runtime_paths_for_workspace(
            workspace,
            workspace_name=getattr(args, "name", None),
        )
    except ValueError as error:
        print(f"Invalid workspace configuration: {error}", file=sys.stderr)
        return 2
    if existing is not None and (
        runtime_paths.workspace.root != existing.workspace.root
        or runtime_paths.state_root != existing.state_root
    ):
        print(
            "init --force cannot change workspace or state roots automatically.",
            file=sys.stderr,
        )
        return 2

    try:
        scaffold_workspace(workspace)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Workspace scaffold failed: {error}", file=sys.stderr)
        return 2
    runtime_paths.state_root.mkdir(parents=True, exist_ok=True)
    write_bootstrap(BOOTSTRAP_PATH, runtime_paths, exposure_keys(setup))

    print(f"OK — workspace configured at {workspace}")
    print(f"     setup: {_SETUP_LABELS[setup]}")
    print(f"     state stored at {runtime_paths.state_root}")
    print(f"     config written to {BOOTSTRAP_PATH}")
    return 0


_SETUP_LABELS = {
    "local": "this computer only (listening on 127.0.0.1)",
    "server": "always-on server (listening on every interface)",
}


def _is_protected_workspace_path(path: Path) -> bool:
    """Return whether a resolved workspace path is under a macOS protected folder."""
    home = Path("~").expanduser().resolve()
    return any(
        path.is_relative_to(home / protected)
        for protected in ("Documents", "Desktop", "Downloads", "Library/Mobile Documents")
    )


def _prompt_workspace_path() -> str:
    """Explain the workspace sandbox and return a confirmed workspace path."""
    print("Where should your work live?")
    print("     Your agents work freely inside this folder; anywhere else needs your approval.")
    print("     Documents, Desktop and Downloads aren't recommended (macOS TCC permission prompts).")
    while True:
        path = input("     Folder (press Enter for ~/corsia, or type another): ").strip() or DEFAULT_WORKSPACE
        if not _is_protected_workspace_path(Path(path).expanduser().resolve()):
            return path
        if input(
            "     That folder is protected by macOS, so you'd see permission prompts. "
            "Use it anyway? [y/N] "
        ).strip() == "y":
            return path


def _prompt_setup(current: str | None) -> str:
    """Ask how CORSIA will be used; Enter keeps the current or local setup."""
    default = "2" if current == "server" else "1"
    print("     How will you use CORSIA?")
    print("       1  On this computer only            (recommended)")
    print("       2  As an always-on server I reach from other devices")
    while True:
        answer = input(f"     Choose 1 or 2 (press Enter for {default}): ").strip() or default
        if answer in _SETUP_CHOICES:
            return _SETUP_CHOICES[answer]
        print("Type 1 or 2.", file=sys.stderr)


def cmd_bind_host(args: argparse.Namespace) -> int:
    """Print the interface CORSIA listeners bind, or refuse with the fix."""
    try:
        print(resolve_bind_host(Path(args.bootstrap or BOOTSTRAP_PATH)))
    except BindHostError as error:
        print(f"CORSIA will not start: {error}", file=sys.stderr)
        return 2
    return 0


def cmd_remote_address(args: argparse.Namespace) -> int:
    """Print the saved outside address while Let other devices connect is on.

    Reads the bootstrap's own database read-only, so it never creates a file or
    reads another instance's state. Prints nothing when either is missing.
    """
    try:
        bootstrap_path = Path(args.bootstrap or BOOTSTRAP_PATH)
        if args.state_root:
            state_root = Path(args.state_root)
            try:
                bootstrap = load_bootstrap(bootstrap_path)
            except OSError:
                bootstrap = None
        else:
            bootstrap = load_bootstrap(bootstrap_path)
            state_root = bootstrap.state_root
        db_path = runtime_db_paths(state_root)[0]
        with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT value FROM settings WHERE key = ?", ("devices",)).fetchone()
            # The parser is pure, but settings imports the normal runtime owner.
            # Give that import the selected bootstrap identity only while loading it;
            # this command still reads exclusively through the explicit connection.
            workspace = bootstrap.workspace.root if bootstrap else state_root.parent / ".bootstrap-read-only"
            with patch.dict(os.environ, {
                "CORSIA_WORKSPACE": str(workspace),
                "CORSIA_STATE_DIR": str(state_root),
                "CORSIA_ALLOW_HOME_WORKSPACE_IN_CI": "1",
            }):
                from infra.settings import parse_devices_setting

                enabled, address = parse_devices_setting(row["value"] if row else None)
    except (OSError, TypeError, ValueError, sqlite3.Error):
        print("Couldn't read the outside address.", file=sys.stderr)
        return 2
    if enabled and address:
        print(address)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Record the setup choice and the bind host that goes with it."""
    try:
        write_setup(BOOTSTRAP_PATH, args.setup)
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 2
    except BindHostError as error:
        print(str(error).splitlines()[0], file=sys.stderr)
        print(f"Fix {BOOTSTRAP_PATH} by hand, then run this again.", file=sys.stderr)
        return 2
    print(f"Setup: {_SETUP_LABELS[args.setup]}.")
    return 0


def _runtime_paths_for_workspace(
    workspace: Path,
    *,
    workspace_name: str | None = None,
) -> RuntimePaths:
    """Build a v2 selection, preserving an existing workspace identity."""
    root = workspace.expanduser().resolve()
    fallback_state = default_state_root()
    bootstrap_status = _bootstrap_path_status()
    if bootstrap_status is None:
        raise _sandboxed_bootstrap_denial_error()
    existing = load_bootstrap(BOOTSTRAP_PATH) if bootstrap_status else None
    workspace_id = existing.workspace.workspace_id if existing else str(uuid4())
    name = (
        workspace_name
        if workspace_name is not None
        else (existing.workspace.name if existing else root.name or "workspace")
    )
    state_root = (
        fallback_state
        if os.environ.get("CORSIA_STATE_DIR") or existing is None
        else existing.state_root
    )
    return RuntimePaths(
        workspace=WorkspaceSelection(
            workspace_id=workspace_id,
            root=root,
            name=name,
        ),
        state_root=state_root,
    )


# Commands that run before a workspace exists. The other sub-CLIs import
# infra.config at module level, which refuses until one is configured.
_BOOTSTRAP_FREE = {"init", "bind-host", "remote-address", "setup"}


def _add_bootstrap_commands(sub: argparse._SubParsersAction) -> None:
    """Register ``init``, ``bind-host``, ``remote-address`` and ``setup``."""
    init = sub.add_parser("init", help="First-run setup — configure workspace path")
    init.add_argument(
        "--workspace", "--path", dest="path", help="Workspace path (skips the prompt)"
    )
    init.add_argument("--name", help="Human-readable workspace name")
    init.add_argument(
        "--setup", choices=sorted(SETUP_BINDS), help="local or server (skips the prompt)"
    )
    init.add_argument("--force", action="store_true", help="Overwrite existing config")
    init.set_defaults(func=cmd_init)

    bind_host = sub.add_parser(
        "bind-host", help="Print the interface CORSIA listeners bind"
    )
    bind_host.add_argument("--bootstrap", help="Bootstrap file (default: ~/.corsia/config.json)")
    bind_host.set_defaults(func=cmd_bind_host)

    remote = sub.add_parser(
        "remote-address", help="Print the saved outside address while devices can connect"
    )
    remote.add_argument("--bootstrap", help="Bootstrap file (default: ~/.corsia/config.json)")
    remote.add_argument("--state-root", help="State directory to read instead of the bootstrap's (the app's own)")
    remote.set_defaults(func=cmd_remote_address)

    setup = sub.add_parser("setup", help="Choose local or server exposure")
    setup.add_argument("setup", choices=sorted(SETUP_BINDS))
    setup.set_defaults(func=cmd_setup)


def _add_runtime_commands(sub: argparse._SubParsersAction) -> None:
    """Register the commands that need a configured workspace."""
    from cli_sources import build_parser as build_sources_parser

    build_sources_parser(sub)

    from cli_accounts import build_parser as build_account_parser

    build_account_parser(sub)

    from cli_artifacts import build_parser as build_artifact_parser

    build_artifact_parser(sub)

    from cli_mcp import build_parser as build_mcp_parser

    build_mcp_parser(sub)

    from cli_local import build_parser as build_local_parser

    build_local_parser(sub)

    from cli_corsia_agent import build_parser as build_corsia_agent_parser

    build_corsia_agent_parser(sub)

    from cli_perf import build_parser as build_perf_parser

    build_perf_parser(sub)


def main(argv: list[str] | None = None) -> int:
    """Run the CORSIA first-run and maintenance command parser."""
    parser = argparse.ArgumentParser(prog="corsia-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    _add_bootstrap_commands(sub)
    words = sys.argv[1:] if argv is None else argv
    if not words or words[0] not in _BOOTSTRAP_FREE:
        _add_runtime_commands(sub)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
