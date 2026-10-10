"""Where the agents' GitHub account lives and how ``gh`` reports it: no state, no database.

The bottom layer of the agents' account. The account profile, the sign-in
controls and the account view all read these facts; nothing here imports them.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from connections.vendor_accounts import AccountError

HOME_ENV = "CORSIA_GH_AGENT_CONFIG_DIR"
# Names a host token variable could carry; each would override the account's own home.
TOKEN_NAMES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
# Names the host never supplies to an agent shell; the bot's own values are set after this.
HOST_IDENTITY_PREFIXES = ("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
HOST_IDENTITY_NAMES = frozenset({"GH_CONFIG_DIR", "GH_HOST", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"})
# Git configuration an agent shell gets while the agents' account exists, in this order.
GIT_CONFIG_SLOTS = 7
# Every name an agent shell's GitHub identity uses; Safehouse passes only listed names.
BOT_ENV_NAMES = ("GH_CONFIG_DIR", "GIT_CONFIG_COUNT",
                 *(f"GIT_CONFIG_{part}_{index}" for index in range(GIT_CONFIG_SLOTS) for part in ("KEY", "VALUE")))
# Where a locked shell's ``gh`` looks: absent and unwritable, so every ``gh`` command fails.
LOCKED_HOME = "/var/empty/corsia-gh-agent-locked"
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
DEVICE_URL = "https://github.com/login/device"
_USER_CODE = re.compile(r"one-time code: ([A-Z0-9]{4}-[A-Z0-9]{4})\b")
# A stored token line in ``hosts.yml``; its value is never read out of the match.
_TOKEN_LINE = re.compile(rb"^\s+oauth_token:\s*\S", re.MULTILINE)


def is_host_identity_name(name: str) -> bool:
    """Whether this variable carries a GitHub or Git identity only CORSIA may set for a shell."""
    return name in HOST_IDENTITY_NAMES or name.startswith(HOST_IDENTITY_PREFIXES)


def default_home() -> Path:
    """The person's own ``gh`` home: the host's ``GH_CONFIG_DIR``, else ``~/.config/gh``."""
    return Path(os.environ.get("GH_CONFIG_DIR") or "~/.config/gh").expanduser()


def configured_root() -> Path | None:
    """Where the agents' account home is created, or None when the account is off."""
    if HOME_ENV in os.environ:
        value = os.environ[HOME_ENV].strip()
        return Path(value).expanduser() if value else None
    if os.environ.get("CORSIA_TEST_MODE") == "1":
        return None
    return Path("~/.config/corsia-gh-agent").expanduser()


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def refusal(path: Path) -> str | None:
    """Why this path may not be the agents' home; None when it may."""
    from infra.config import STATE_ROOT, WORKSPACE
    from tools.host_path_policy import ordinary_host_path_denial

    resolved = path.expanduser().resolve(strict=False)
    home = Path.home().resolve(strict=False)
    default = default_home().resolve(strict=False)
    state_root = STATE_ROOT.resolve(strict=False)
    if not resolved.is_absolute() or _within(home, resolved):
        return "home_or_ancestor"
    if _within(resolved, WORKSPACE.resolve(strict=False)):
        return "inside_workspace"
    if _within(resolved, state_root) or _within(state_root, resolved):
        return "overlaps_state_root"
    if _within(resolved, default) or _within(default, resolved):
        return "overlaps_default_account"
    if ordinary_host_path_denial(resolved, for_write=True) is not None:
        return "host_path_denied"
    return None


def home_root() -> Path:
    """The validated root the agents' account home is created under."""
    root = configured_root()
    if root is None:
        raise AccountError("the agents' GitHub account is turned off")
    if refusal(root) is not None:
        raise AccountError("the agents' GitHub account home is not allowed here")
    return root


def clean_env() -> dict[str, str]:
    """The backend's environment without host tokens or identity; the caller sets the home.

    ``gh auth login --web`` would otherwise open the device page in the person's own
    browser session, where approving it signs the person's account in, not the bot's.
    """
    env = {name: value for name, value in os.environ.items()
           if name not in TOKEN_NAMES and not is_host_identity_name(name)}
    env["GH_BROWSER"] = "/usr/bin/true"
    # A login that tried to set up Git credentials would write the person's own Git config.
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def parse_status(code: int, stdout: bytes) -> tuple[bool | None, dict[str, str]]:
    """Read ``gh auth status --json hosts``: signed in only for an active, healthy github.com login.

    The report exits 0 whatever the account's health, so the entry's own state decides; a
    fatal error or an unreadable report proves nothing and is unknown, never signed out.
    The facts carry the login and where ``gh`` found its token.
    """
    if code != 0:
        return None, {}
    try:
        report = json.loads(stdout)
        hosts = report["hosts"]
    except (ValueError, KeyError, TypeError):
        return None, {}
    if hosts == {}:
        return False, {}
    try:
        entries = hosts["github.com"]
    except (KeyError, TypeError):
        return False, {}
    for entry in entries if isinstance(entries, list) else ():
        if isinstance(entry, dict) and entry.get("active") is True:
            login, source = entry.get("login"), entry.get("tokenSource")
            if entry.get("state") == "success" and isinstance(login, str) and LOGIN.match(login):
                return True, {"login": login, "token_source": source if isinstance(source, str) else ""}
            return False, {}
    return False, {}


def signin_prompt(line: str) -> dict[str, str] | None:
    """The device code a ``gh`` web sign-in prints; nothing else of its output is kept."""
    match = _USER_CODE.search(line)
    return {"user_code": match.group(1), "verification_uri": DEVICE_URL} if match else None


def stores_token(home: Path) -> bool:
    """Whether the home's ``hosts.yml`` holds a token itself rather than pointing at a keyring."""
    try:
        return _TOKEN_LINE.search((home / "hosts.yml").read_bytes()) is not None
    except OSError:
        return False
