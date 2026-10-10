"""``corsia user`` and ``corsia token`` — creating accounts and credentials.

This is the only way an account comes into existence, and that is deliberate:
the backend binds ``0.0.0.0``, so a first-run web form would hand the install
to whoever reached the port first. Running a command proves local access to
the machine.

Three rules about output, because other programs read it:

- ``token create`` prints the token and nothing else on stdout, so a shell can
  capture it directly. Everything for a human goes to stderr.
- ``--write-delegate-file`` prints no secret at all: the token goes straight to
  the credential file at mode 600, atomically, so it never reaches a terminal
  or a scrollback buffer.
- ``token list`` never prints a token value. There is nothing to print — only
  the SHA-256 is stored.

Only ``user add`` creates or migrates the schema. The other commands read a
database that must already exist, so that no lifecycle command can trigger a
migration of a production database as a side effect of minting a token.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path

_NO_DATABASE = (
    "This install has no account tables yet. Start CORSIA once, or run: "
    "corsia user add"
)


_INPUT_ENDED = "Input ended before the account was created. Run: corsia user add"


def _fail(message: str) -> int:
    """Print ``message`` to stderr and return the CLI's failure status."""
    print(message, file=sys.stderr)
    return 2


def _ensure_schema() -> None:
    """Create or migrate the schema through the startup migration runner.

    Schema only, not the rest of ``init_db``: its connector seeding can hold
    the write lock for tens of seconds in a cold process, and this command
    usually runs beside a live backend, which does that seeding at start.
    """
    from infra.database import get_db, initialize_schema

    initialize_schema(get_db())


def _resolve_user(explicit: str | None) -> str:
    """Return the account to act on: the named one, or the only one.

    Raises SystemExit with a specific message when the name is unknown, when
    no account exists, or when several do and the caller did not choose.
    """
    from infra import accounts

    if explicit:
        name = accounts.normalize_username(explicit)
        if accounts.get_user(name) is None:
            raise SystemExit(f"No account named '{name}'. Run: corsia user add {name}")
        return name
    users = accounts.list_users()
    if not users:
        raise SystemExit("No account exists yet. Run: corsia user add")
    if len(users) > 1:
        names = ", ".join(user["id"] for user in users)
        raise SystemExit(f"Several accounts exist ({names}). Pass --user <name>.")
    return users[0]["id"]


def _read_password(from_stdin: bool) -> str:
    """Read a new password from stdin, or from two matching prompts."""
    from infra.passwords import MIN_PASSWORD_LENGTH

    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    while True:
        first = getpass.getpass("Password: ")
        if len(first) < MIN_PASSWORD_LENGTH:
            print(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
                file=sys.stderr,
            )
            continue
        if first != getpass.getpass("Repeat password: "):
            print("Passwords did not match.", file=sys.stderr)
            continue
        return first


def _delegate_token_path() -> Path:
    """The credential file the agent CLIs read, resolved by their own rule."""
    from scripts.corsia_api import TOKEN_FILE

    return Path(TOKEN_FILE)


def _write_secret_file(path: Path, value: str) -> None:
    """Write ``value`` to ``path`` atomically and readable only by its owner.

    Created at mode 600 before anything is written, so the secret is never
    briefly world-readable, and replaced atomically so a reader sees either the
    old credential or the new one and never a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(value)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _prompt_username(owners: list[str]) -> str:
    """Ask for a username, offering the login name when it is a valid one."""
    from infra import accounts

    if owners:
        return input("Username: ")
    try:
        default = accounts.normalize_username(getpass.getuser())
    except (accounts.AccountError, OSError):
        return input("Username: ")
    return input(f"Username [{default}]: ").strip() or default


def _prompt_profile_name(username: str) -> str:
    """Ask what agents should call the person; with no terminal, use the Mac's name."""
    from infra.user_profile import mac_full_name

    default = mac_full_name() or username
    if not sys.stdin.isatty():
        return default
    return input(f"What should agents call you? [{default}]: ").strip() or default


def _email_choice(answer: str, found: list[str]) -> str:
    """The email an answer picks: Enter the first found, a number one of them, else what was typed."""
    answer = answer.strip()
    if not answer:
        return found[0] if found else ""
    if answer.isdigit() and 1 <= int(answer) <= len(found):
        return found[int(answer) - 1]
    if "@" in answer and len(answer.split()) == 1:
        return answer
    print(f"'{answer}' is not an email address; none saved.")
    return ""


def _prompt_contact_email() -> str:
    """Ask which email agents may use for the person, offering the ones they are signed in with.

    Asks nothing without a terminal, and never replaces an email the profile already names.
    """
    if not sys.stdin.isatty():
        return ""
    from connections.signed_in_emails import signed_in_emails
    from infra import config
    from infra.user_profile import profile_email

    on_file = profile_email(config.WORKSPACE)
    if on_file:
        print(f"Email agents use for you: {on_file}")
        return ""
    found = signed_in_emails()
    if not found:
        return _email_choice(input("Email agents may use for you (Enter to skip): "), found)
    print("Email agents may use for you, from your Claude Code and Codex sign-ins:")
    for number, email in enumerate(found, 1):
        print(f"  {number}) {email}")
    return _email_choice(input("Pick one, type another, or press Enter for 1: "), found)


def _seed_user_profile(call_name: str, email: str = "") -> None:
    """Create the workspace profile from the answers and the Mac's timezone, if missing."""
    from infra import config
    from infra.user_profile import add_profile_email, mac_timezone, write_user_profile

    if not write_user_profile(config.WORKSPACE, call_name, mac_timezone(), email):
        add_profile_email(config.WORKSPACE, email)


def cmd_user_add(args: argparse.Namespace) -> int:
    """Create an account, offering the data-owner names already in use."""
    _ensure_schema()
    from infra import accounts

    owners = accounts.data_owners_without_accounts()
    if owners:
        print(f"Existing data belongs to: {', '.join(owners)}")
        print("Use one of these names to keep it.")
    try:
        name = accounts.normalize_username(args.name or _prompt_username(owners))
    except accounts.AccountError as error:
        return _fail(str(error))
    except EOFError:
        return _fail(_INPUT_ENDED)
    if owners and name not in owners and not accounts.has_users():
        # The first account on an install that already has data decides who
        # can reach it; any other name would strand every existing thread.
        return _fail(
            f"'{name}' owns none of the existing data. Create the first "
            f"account as one of: {', '.join(owners)}. Add others after."
        )
    # The first interactive account is who the profile describes. That also
    # fires on an upgrade with no accounts yet; an existing profile is never touched.
    seeds_profile = not accounts.has_users() and not args.password_stdin
    try:
        call_name = _prompt_profile_name(name) if seeds_profile else ""
        email = _prompt_contact_email() if seeds_profile else ""
    except EOFError:
        return _fail(_INPUT_ENDED)
    password = _read_password(args.password_stdin)
    try:
        accounts.create_user(name, password)
    except accounts.AccountError as error:
        return _fail(str(error))
    if seeds_profile:
        _seed_user_profile(call_name, email)
    print(f"Account '{name}' created.")
    return 0


def cmd_user_passwd(args: argparse.Namespace) -> int:
    """Set an account's password, ending its browser sessions."""
    from infra import accounts

    name = _resolve_user(args.name)
    password = _read_password(args.password_stdin)
    try:
        revoked = accounts.set_password(name, password)
    except accounts.AccountError as error:
        return _fail(str(error))
    print(f"Password updated for '{name}'; {revoked} session(s) revoked.")
    print("API tokens were kept. Revoke one with: corsia token revoke <label>")
    return 0


def cmd_user_list(args: argparse.Namespace) -> int:
    """List the accounts on this install."""
    from infra import accounts

    users = accounts.list_users()
    if not users:
        print("No account exists yet. Run: corsia user add")
        return 0
    for user in users:
        print(f"{user['id']}\tcreated {user['created_at']}")
    return 0


def cmd_token_create(args: argparse.Namespace) -> int:
    """Mint a non-expiring API token for agent tooling."""
    from infra import accounts

    user = _resolve_user(args.user)
    label = args.label or "api"
    token, _ = accounts.issue_token(user, kind="api", label=label)
    if args.write_delegate_file:
        path = _delegate_token_path()
        _write_secret_file(path, token)
        print(f"API token for '{user}' written to {path} (label '{label}').")
        return 0
    print(f"API token for '{user}', label '{label}' — shown once:", file=sys.stderr)
    print(token)
    return 0


def cmd_token_service(args: argparse.Namespace) -> int:
    """Mint the lifecycle shell's short-lived service token, stdout only."""
    from infra import accounts

    users = accounts.list_users()
    if not users:
        raise SystemExit("No account exists yet. Run: corsia user add")
    token, _ = accounts.issue_token(users[0]["id"], kind="service", label="lifecycle")
    print(token)
    return 0


def cmd_token_list(args: argparse.Namespace) -> int:
    """List token labels, kinds and ages — never a token value."""
    from infra import accounts

    user = args.user and _resolve_user(args.user)
    tokens = accounts.list_tokens(user)
    if not tokens:
        print("No tokens.")
        return 0
    for token in tokens:
        label = token["label"] or "-"
        print(
            f"{token['user_id']}\t{token['kind']}\t{label}"
            f"\tcreated {token['created_at']}"
        )
    return 0


def cmd_token_revoke(args: argparse.Namespace) -> int:
    """Delete every token of one account carrying a label."""
    from infra import accounts

    user = _resolve_user(args.user)
    revoked = accounts.revoke_label(user, args.label)
    if not revoked:
        return _fail(f"No token labelled '{args.label}' for '{user}'.")
    print(f"Revoked {revoked} token(s) labelled '{args.label}' for '{user}'.")
    return 0


def _run(handler, args: argparse.Namespace) -> int:
    """Run one account command, reporting a database that has no tables yet."""
    try:
        return handler(args)
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return _fail(_NO_DATABASE)
        raise


def _dispatch(handler):
    """Bind one command handler through the missing-table reporter."""
    return lambda args: _run(handler, args)


def _add_user_commands(sub: argparse._SubParsersAction) -> None:
    """Register ``corsia user add|passwd|list``."""
    user = sub.add_parser("user", help="Manage accounts on this install")
    commands = user.add_subparsers(dest="user_cmd", required=True)

    add = commands.add_parser("add", help="Create an account")
    add.add_argument("name", nargs="?", help="Username: 1-32 of a-z, 0-9, - or _")
    add.add_argument(
        "--password-stdin", action="store_true", help="Read the password from stdin"
    )
    add.set_defaults(func=_dispatch(cmd_user_add))

    passwd = commands.add_parser("passwd", help="Change a password")
    passwd.add_argument("name", help="Account to update")
    passwd.add_argument(
        "--password-stdin", action="store_true", help="Read the password from stdin"
    )
    passwd.set_defaults(func=_dispatch(cmd_user_passwd))

    listing = commands.add_parser("list", help="List accounts")
    listing.set_defaults(func=_dispatch(cmd_user_list))


def _add_token_commands(sub: argparse._SubParsersAction) -> None:
    """Register ``corsia token create|list|revoke|service``."""
    token = sub.add_parser("token", help="Manage API tokens")
    commands = token.add_subparsers(dest="token_cmd", required=True)

    create = commands.add_parser("create", help="Mint an API token (shown once)")
    create.add_argument("--user", help="Account (default: the only one)")
    create.add_argument("--label", help="Label to identify the token later")
    create.add_argument(
        "--write-delegate-file",
        action="store_true",
        help="Write it to the agent credential file instead of printing it",
    )
    create.set_defaults(func=_dispatch(cmd_token_create))

    listing = commands.add_parser("list", help="List tokens, never their values")
    listing.add_argument("--user", help="Account (default: every account)")
    listing.set_defaults(func=_dispatch(cmd_token_list))

    revoke = commands.add_parser("revoke", help="Revoke tokens by label")
    revoke.add_argument("label", help="Label to revoke")
    revoke.add_argument("--user", help="Account (default: the only one)")
    revoke.set_defaults(func=_dispatch(cmd_token_revoke))

    service = commands.add_parser(
        "service", help="Mint the lifecycle shell's 1-hour service token"
    )
    service.set_defaults(func=_dispatch(cmd_token_service))


def build_parser(sub: argparse._SubParsersAction) -> None:
    """Register the account and token commands on the CORSIA CLI parser."""
    _add_user_commands(sub)
    _add_token_commands(sub)
