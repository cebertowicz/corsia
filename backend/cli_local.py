"""Local-model CLI commands."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error

from cli_sources import READ_TIMEOUT_SECONDS, RouteMissing, _read_token, request_json
from llm.local_profiles import setup_command

SETUP_ROUTE = "/api/providers/local/setup"
SETUP_TIMEOUT_SECONDS = 60
# The server's download wait is an hour; give it two minutes more to say it is
# still downloading before this command reports the same thing itself.
FOLLOW_TIMEOUT_SECONDS = 60 * 60 + 120
FOLLOW_INTERVAL_SECONDS = 1.0


def _print_terminal_error(result: dict) -> int:
    print(result.get("message") or "Local setup failed.")
    if result.get("next_action"):
        print(result["next_action"])
    return 2


def _print_still_running(command: str) -> int:
    print("Local setup is still running.")
    print(f"Run `{command}` again to follow its progress.")
    return 1


# Exit codes beyond 0/1/2: the installer reads them to decide whether a model
# provider works before it opens the app.
EXIT_STARTED = 10
EXIT_NO_PROVIDER = 11
EXIT_STILL_DOWNLOADING = 12


def _print_still_downloading(result: dict, command: str) -> int:
    print("The download keeps going in CORSIA; the app shows its progress.")
    print(result.get("next_action") or f"Run `{command}` again to follow it.")
    return EXIT_STILL_DOWNLOADING


def _refusal(error: urllib.error.HTTPError) -> str | None:
    """The server's reason for refusing a profile (HTTP 400), else None."""
    if error.code != 400:
        return None
    try:
        detail = json.loads(error.read().decode() or "null")
    except (OSError, ValueError):
        return None
    reason = detail.get("detail") if isinstance(detail, dict) else None
    return reason if isinstance(reason, str) else None


def _consent_required(error: urllib.error.HTTPError) -> bool:
    """Whether local setup needs consent that only the app can collect."""
    if error.code != 409:
        return False
    try:
        detail = json.loads(error.read().decode() or "null")
    except (OSError, ValueError):
        return False
    payload = detail.get("detail") if isinstance(detail, dict) else None
    return isinstance(payload, dict) and payload.get("code") == "consent_required"


def _print_skipped(result: dict) -> int:
    """0 only when a signed-in subscription made setup unnecessary; else nothing works yet."""
    print(result.get("message") or "No local setup was needed.")
    return 0 if result.get("skipped") == "subscription_available" else EXIT_NO_PROVIDER


def _backend_refused(error: OSError) -> bool:
    return isinstance(getattr(error, "reason", error), ConnectionRefusedError)


def _follow_server_setup(token: str, command: str) -> int:
    """Print each stage once and settle on the server's setup flight record."""
    seen_stages: set[str] = set()
    deadline = time.monotonic() + FOLLOW_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            flight = request_json("GET", SETUP_ROUTE, token, READ_TIMEOUT_SECONDS)
        except (OSError, ValueError, RouteMissing) as error:
            return _print_terminal_error({
                "message": f"Local setup could not read the server state: {error}",
                "next_action": f"Run `{command}` again after the backend is available.",
            })
        state = flight.get("state")
        if state == "succeeded":
            result = flight.get("result") or {}
            if result.get("skipped"):
                return _print_skipped(result)
            models = result.get("models")
            if result.get("existing") and isinstance(models, int):
                noun = "model" if models == 1 else "models"
                print(f"Using your oMLX ({models} {noun}). Its settings are unchanged.")
            else:
                print("Local model is ready.")
            return 0
        if state == "downloading":
            return _print_still_downloading(flight.get("result") or {}, command)
        if state == "failed":
            return _print_terminal_error(flight.get("result") or {})
        if state == "idle":
            return _print_terminal_error({
                "message": "Local setup is no longer running; the CORSIA server may have restarted.",
                "next_action": f"Run `{command}` again.",
            })
        stage = flight.get("stage")
        if isinstance(stage, str) and stage not in seen_stages:
            print(stage)
            seen_stages.add(stage)
        time.sleep(FOLLOW_INTERVAL_SECONDS)
    return _print_still_running(command)


def cmd_setup(args: argparse.Namespace) -> int:
    if getattr(args, "before_start", False):
        from cli_local_before_start import run_before_start

        return run_before_start(force_local=not args.if_no_subscription)
    profile = getattr(args, "profile", None)
    command = setup_command(profile)
    token = _read_token()
    if not token:
        return _print_terminal_error({
            "message": "Local setup has no credential for the backend.",
            "next_action": "Run `corsia local setup` through the installed CORSIA command.",
        })
    try:
        result = request_json(
            "POST", SETUP_ROUTE, token, SETUP_TIMEOUT_SECONDS,
            {"no_subscription_only": args.if_no_subscription, "automatic": args.automatic, "profile": profile},
        )
    except urllib.error.HTTPError as error:
        if _consent_required(error):
            return _print_terminal_error({
                "message": "CORSIA needs your permission to inspect the existing oMLX.",
                "next_action": "Open the CORSIA app and choose Use it in local model setup.",
            })
        refusal = _refusal(error)
        if refusal:
            return _print_terminal_error({"message": refusal})
        return _print_terminal_error({
            "message": f"Local setup could not start on the backend: {error}",
            "next_action": f"Run `{command}` again after the backend is available.",
        })
    except (ValueError, RouteMissing) as error:
        return _print_terminal_error({
            "message": f"Local setup could not start on the backend: {error}",
            "next_action": f"Run `{command}` again after the backend is available.",
        })
    except OSError as error:
        if _backend_refused(error):
            return _print_terminal_error({
                "message": "CORSIA isn't running.",
                "next_action": f"Run `corsia start` first, then `{command}`.",
            })
        if isinstance(getattr(error, "reason", error), (TimeoutError, ConnectionResetError)):
            return _follow_server_setup(token, command)
        return _print_terminal_error({
            "message": f"Local setup could not start on the backend: {error}",
            "next_action": f"Run `{command}` again after the backend is available.",
        })
    if not result.get("ok"):
        return _print_terminal_error(result)
    if result.get("skipped"):
        return _print_skipped(result)
    if result.get("notice"):
        print(result["notice"])
    if args.start_only:
        return EXIT_STARTED
    return _follow_server_setup(token, command)


def build_parser(sub: argparse._SubParsersAction) -> None:
    local = sub.add_parser("local", help="Set up a local model")
    local_sub = local.add_subparsers(dest="local_cmd", required=True)
    setup = local_sub.add_parser("setup", help="Install and configure a local model")
    setup.add_argument(
        "--profile", metavar="ID",
        help="Local model profile to set up (GET /api/providers/local/setup recommends one); omitted, the default",
    )
    setup.add_argument(
        "--if-no-subscription", action="store_true",
        help="Skip automatic setup when Claude Code or Codex is already signed in",
    )
    setup.add_argument("--automatic", action="store_true", help=argparse.SUPPRESS)
    setup.add_argument("--start-only", action="store_true", help=argparse.SUPPRESS)
    # Installer only: set up before CORSIA starts, with no server.
    setup.add_argument("--before-start", action="store_true", help=argparse.SUPPRESS)
    setup.set_defaults(func=cmd_setup)
